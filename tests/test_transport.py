import socket
import unittest
from collections import deque

import protocol as p
from game import Delivery
from server_network import Connection, Server
from tests.support import bank
from tests.test_game import Clock


class PartialSender:
    def __init__(self, chunk=3):
        self.chunk = chunk
        self.sent = bytearray()

    def send(self, data):
        count = min(self.chunk, len(data))
        self.sent.extend(data[:count])
        return count


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.server = Server("127.0.0.1", 0, bank()["quizzes"], self.clock)
        self.addCleanup(self.server.close)
        self.peer, sock = socket.socketpair()
        self.addCleanup(self.peer.close)
        sock.setblocking(False)
        self.c = Connection(1, sock, self.clock())
        self.server.connections[1] = self.c
        self.server.game.connect(1)
        self.server.next_peer = 2

    def test_partial_send_preserves_fifo_bytes_and_write_budget(self):
        sender = PartialSender(3)
        wire = p.encode({"text": "网络" * 10000})
        c = Connection(200, sender, self.clock(), tx=deque([memoryview(wire)]), tx_bytes=len(wire))
        self.server._write(c)
        self.assertEqual(len(sender.sent), p.IO_BUDGET)
        self.assertEqual(c.tx_bytes, len(wire) - p.IO_BUDGET)
        self.server._write(c)
        self.assertEqual(bytes(sender.sent), wire)
        self.assertEqual(c.tx_bytes, 0)
        self.assertFalse(c.tx)

    def test_receive_budget_and_absolute_partial_timeout(self):
        wire = p.encode({"text": "x" * 40000})
        self.peer.sendall(wire)
        self.server._read(self.c)
        self.assertEqual(len(self.c.rx), p.IO_BUDGET)
        self.assertEqual(self.c.rx.partial_since(), self.clock.now)
        self.server.game.sessions[1].token = "s-" + "a" * 32
        self.clock.now += 9.9
        self.server._timeouts()
        self.assertFalse(self.c.closing)
        self.clock.now += 0.1
        self.server._timeouts()
        self.assertTrue(self.c.closing)

    def test_login_timeout_is_absolute_and_cleanup_idempotent(self):
        self.clock.now += p.LOGIN_TIMEOUT
        self.server._timeouts()
        self.server._mark_close(self.c)
        self.assertEqual(len(self.server.cleanup), 1)
        self.server._finish_closes()
        self.assertNotIn(1, self.server.connections)
        self.assertNotIn(1, self.server.game.sessions)
        self.server.game.disconnect(1)

    def test_send_stall_only_counts_while_output_queued(self):
        self.server.game.sessions[1].token = "s-" + "a" * 32
        self.clock.now += 20
        self.server._timeouts()
        self.assertFalse(self.c.closing)
        self.server.game.send(1, "LIST_ROOMS_RESPONSE", {"rooms": []}, "req-1")
        self.server._deliver()
        self.clock.now += 9
        self.server._write(self.c)
        self.server._timeouts()
        self.assertFalse(self.c.closing)
        self.server.game.send(1, "LIST_ROOMS_RESPONSE", {"rooms": []}, "req-2")
        self.server._deliver()
        self.clock.now += 10
        self.server._timeouts()
        self.assertTrue(self.c.closing)

    def test_output_bound_and_event_number_only_after_successful_enqueue(self):
        self.c.tx_bytes = p.MAX_TX
        self.server.game.outputs.append(Delivery(1, "ROOM_CLOSED_EVENT", {
            "room_code": "R000001", "reason": "SERVER_SHUTDOWN"}, None, None))
        self.server._deliver()
        self.assertTrue(self.c.closing)
        self.assertEqual(self.c.last_event, 0)
        self.server._drain_cleanup()
        self.assertNotIn(1, self.server.game.sessions)

    def test_no_zero_selector_mask_and_rx_restoration(self):
        wire = p.encode({"x": "y"})
        # 128 KiB full with complete frames; no read interest until consumed.
        self.c.rx.buffer = bytearray(b'x' * p.MAX_RX)
        self.server._interest(self.c)
        self.assertFalse(self.c.registered)
        self.c.rx.buffer.clear()
        self.c.rx.feed(wire, self.clock())
        self.server._interest(self.c)
        self.assertTrue(self.c.registered)

    def test_event_exhaustion_closes_without_wrap(self):
        self.c.last_event = p.MAX_SEQUENCE
        self.server.game.send(1, "ROOM_CLOSED_EVENT", {"room_code": "R000001", "reason": "SERVER_SHUTDOWN"})
        self.server._deliver()
        self.assertTrue(self.c.closing)
        self.assertFalse(self.c.tx)

    def test_last_event_is_queued_then_gracefully_closes(self):
        self.c.last_event = p.MAX_SEQUENCE - 1
        self.server.game.send(1, "ROOM_CLOSED_EVENT", {"room_code": "R000001", "reason": "SERVER_SHUTDOWN"})
        self.server._deliver()
        self.assertEqual(self.c.last_event, p.MAX_SEQUENCE)
        self.assertTrue(self.c.closing)
        self.assertEqual(self.c.close_at, self.clock.now + p.FLUSH_TIMEOUT)
        self.assertTrue(self.c.tx)

    def test_accept_fairness_and_64_connection_limit(self):
        clients = []
        self.server._finish_closes()
        try:
            for index in range(65):
                sock = socket.create_connection(self.server.address, timeout=1)
                clients.append(sock)
                if index % 16 == 15:
                    before = len(self.server.connections)
                    self.server._accept()
                    self.assertLessEqual(len(self.server.connections) - before, p.ACCEPT_BUDGET)
            self.server._accept()
            self.assertEqual(len(self.server.connections), p.MAX_CONNECTIONS)
            clients[-1].settimeout(1)
            self.assertEqual(clients[-1].recv(1), b'')
        finally:
            for sock in clients:
                sock.close()
