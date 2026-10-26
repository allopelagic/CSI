import io
import socket
import unittest

import protocol as p
from client import Client, Input, Pending, safe
from tests.test_game import Clock


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.sock, self.other = socket.socketpair()
        self.addCleanup(self.other.close)
        self.addCleanup(self.sock.close)
        self.clock = Clock()
        self.output = io.StringIO()
        self.client = Client(self.sock, "u", "User", stdin=io.StringIO(), stdout=self.output, clock=self.clock)
        self.addCleanup(self.client.selector.close)

    def login(self):
        token = "s-" + "a" * 32
        self.client.handle(p.envelope("LOGIN_RESPONSE", {}, request_id="req-1", session=token))
        return token

    def test_response_correlation_and_replayed_response_rejected(self):
        token = self.login()
        with self.assertRaises(p.ProtocolError):
            self.client.handle(p.envelope("LOGIN_RESPONSE", {}, request_id="req-1", session=token))
        self.client.send_request("LIST_QUIZZES", {})
        with self.assertRaises(p.ProtocolError):
            self.client.handle(p.envelope("LOGOUT_RESPONSE", {}, request_id="req-2", session=token))

    def test_event_sequence_and_wrong_connection_token_rejected(self):
        token = self.login()
        event = p.envelope("QUIZ_STARTED_EVENT", {"room_code": "R000001", "quiz_id": "q", "title": "Quiz", "total_questions": 5},
                           event_id="evt-2", session=token)
        with self.assertRaises(p.ProtocolError):
            self.client.handle(event)
        event["event_id"] = "evt-1"
        event["session_id"] = "s-" + "b" * 32
        with self.assertRaises(p.ProtocolError):
            self.client.handle(event)

    def test_null_error_displayed_without_consuming_pending(self):
        self.client.handle(p.envelope("ERROR_RESPONSE", {"code": "INVALID_MESSAGE", "message": "Bad message."}))
        self.assertIn("req-1", self.client.pending)
        self.assertIn("ERROR_RESPONSE", self.output.getvalue())

    def test_pending_full_still_has_bounded_exit(self):
        self.login()
        for i in range(p.MAX_PENDING):
            self.client.pending[f"req-{100 + i}"] = Pending("LEADERBOARD", self.clock())
        self.client.input.lines.put("/quit")
        self.client._inputs()
        self.assertEqual(self.client.quitting_at, self.clock.now + p.FLUSH_TIMEOUT)
        self.assertEqual(len(self.client.pending), p.MAX_PENDING)

    def test_last_request_sequence_causes_bounded_close(self):
        self.login()
        self.client.next_request = p.MAX_SEQUENCE
        self.client.send_request("LIST_ROOMS", {})
        self.assertEqual(self.client.quitting_at, self.clock.now + p.FLUSH_TIMEOUT)
        self.assertIn("req-2147483647", self.client.pending)

    def test_stdin_eof_can_exit_stalled_login(self):
        self.client.input.eof.set()
        self.clock.now += p.PENDING_TIMEOUT
        self.client._inputs()
        self.assertTrue(self.client.done)

    def test_scripted_commands_wait_for_login_before_quit(self):
        self.client.input.lines.put("/quizzes")
        self.client.input.lines.put("/quit")
        self.client._inputs()
        self.assertFalse(self.client.done)
        self.assertIsNone(self.client.quitting_at)
        self.login()
        self.client._inputs()
        self.assertTrue(any(item.op == "LIST_QUIZZES" for item in self.client.pending.values()))

    def test_input_bounds_and_long_line_wholly_discarded(self):
        reader = Input(io.StringIO("x" * 2000 + "/quit\n/help\n"))
        reader.read()
        self.assertIsNone(reader.lines.get_nowait())
        self.assertEqual(reader.lines.get_nowait(), "/help")
        reader = Input(io.StringIO("/help\n" * 200 + "/quit\n"))
        reader.read()
        self.assertEqual(reader.lines.qsize(), p.MAX_INPUT)
        self.assertTrue(reader.overflow.is_set())
        self.assertTrue(reader.force_quit.is_set())

    def test_display_escapes_terminal_controls(self):
        text = safe("hello\x1b[2J\n\r\t\u202e网络")
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\n", text)
        self.assertIn("\\u001b", text)
        self.assertIn("网络", text)
        self.client.display("TEST", {"option": "\x1b[31m\x7f\u202e"})
        self.assertEqual(self.output.getvalue().count("\n"), 1)
        self.assertNotIn("\x1b", self.output.getvalue())
