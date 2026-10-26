"""Bounded nonblocking raw TCP transport. This thread owns all mutable state."""
from __future__ import annotations

import selectors
import socket
import time
from collections import deque
from dataclasses import dataclass, field

import protocol as p
from game import Close, Game


@dataclass
class Connection:
    peer: int
    sock: socket.socket
    accepted: float
    rx: p.Framer = field(default_factory=p.Framer)
    tx: deque = field(default_factory=deque)
    tx_bytes: int = 0
    last_send: float = 0.0
    last_event: int = 0
    closing: bool = False
    close_at: float = 0.0
    registered: bool = False


class Server:
    def __init__(self, host, port, quizzes, clock=time.monotonic):
        self.clock = clock
        self.selector = selectors.DefaultSelector()
        self.game = Game(quizzes, clock)
        self.connections: dict[int, Connection] = {}
        self.cleanup = deque()
        self.cleanup_set = set()
        self.next_peer = 1
        self.stopping = False
        self.listener = None
        try:
            last_error = None
            for family, socktype, proto, _, address in socket.getaddrinfo(
                    host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE):
                sock = socket.socket(family, socktype, proto)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(address)
                    sock.listen(p.MAX_CONNECTIONS)
                    sock.setblocking(False)
                    self.listener = sock
                    break
                except OSError as exc:
                    last_error = exc
                    sock.close()
            if self.listener is None:
                raise last_error or OSError("No bind address available")
            self.address = self.listener.getsockname()
            self.selector.register(self.listener, selectors.EVENT_READ, None)
        except BaseException:
            if self.listener is not None:
                self.listener.close()
            self.selector.close()
            raise

    def _interest(self, c):
        events = 0
        if not c.closing and not self.stopping and len(c.rx) < p.MAX_RX:
            events |= selectors.EVENT_READ
        if c.tx:
            events |= selectors.EVENT_WRITE
        if c.registered and not events:
            self.selector.unregister(c.sock)
            c.registered = False
        elif c.registered:
            self.selector.modify(c.sock, events, c)
        elif events:
            self.selector.register(c.sock, events, c)
            c.registered = True

    def _mark_close(self, c, drain=False):
        if not c.closing:
            c.closing = True
            c.close_at = self.clock() + (p.FLUSH_TIMEOUT if drain else 0.0)
            if c.peer not in self.cleanup_set:
                self.cleanup_set.add(c.peer)
                self.cleanup.append(c.peer)
        elif not drain:
            c.close_at = self.clock()

    def _deliver(self):
        # Finish the current transition's immutable outputs before disconnect cleanup.
        for item in self.game.take_outputs():
            c = self.connections.get(item.peer)
            if c is None or c.closing:
                continue
            if isinstance(item, Close):
                self._mark_close(c, drain=True)
                continue
            event = item.kind.endswith("_EVENT")
            if event and c.last_event >= p.MAX_SEQUENCE:
                self._mark_close(c)
                continue
            message = p.envelope(item.kind, item.payload, request_id=item.request_id,
                                 event_id=f"evt-{c.last_event + 1}" if event else None,
                                 session=item.session)
            try:
                data = p.encode(message)
            except p.ProtocolError:
                self._mark_close(c)
                continue
            if c.tx_bytes + len(data) > p.MAX_TX:
                self._mark_close(c)
                continue
            if not c.tx:
                c.last_send = self.clock()
            c.tx.append(memoryview(data))
            c.tx_bytes += len(data)
            if event:
                c.last_event += 1

    def _drain_cleanup(self):
        while self.cleanup:
            peer = self.cleanup.popleft()
            self.cleanup_set.discard(peer)
            self.game.disconnect(peer)
            self._deliver()  # Further slow peers enqueue cleanup, never recurse.

    def _finish_closes(self):
        self._drain_cleanup()
        now = self.clock()
        for c in tuple(self.connections.values()):
            if c.closing and (not c.tx or now >= c.close_at):
                if c.registered:
                    self.selector.unregister(c.sock)
                c.sock.close()
                del self.connections[c.peer]
            else:
                self._interest(c)

    def _accept(self):
        for _ in range(p.ACCEPT_BUDGET):
            try:
                sock, _ = self.listener.accept()
            except BlockingIOError:
                break
            sock.setblocking(False)
            if len(self.connections) >= p.MAX_CONNECTIONS:
                sock.close()
                continue
            peer = self.next_peer
            self.next_peer += 1
            c = Connection(peer, sock, self.clock())
            self.connections[peer] = c
            self.game.connect(peer)
            self._interest(c)

    def _read(self, c):
        remaining = p.IO_BUDGET
        while remaining and not c.closing and len(c.rx) < p.MAX_RX:
            try:
                data = c.sock.recv(min(remaining, p.MAX_RX - len(c.rx)))
                if not data:
                    self._mark_close(c)
                    break
                remaining -= len(data)
                c.rx.feed(data, self.clock())
            except BlockingIOError:
                break
            except (OSError, p.ProtocolError):
                self._mark_close(c)
                break

    def _write(self, c):
        remaining = p.IO_BUDGET
        while remaining and c.tx:
            try:
                count = c.sock.send(c.tx[0][:remaining])
            except BlockingIOError:
                break
            except OSError:
                self._mark_close(c)
                break
            if not count:
                self._mark_close(c)
                break
            c.last_send = self.clock()
            remaining -= count
            c.tx_bytes -= count
            if count == len(c.tx[0]):
                c.tx.popleft()
            else:
                c.tx[0] = c.tx[0][count:]

    def _timeouts(self):
        now = self.clock()
        for c in tuple(self.connections.values()):
            if c.closing:
                continue
            session = self.game.sessions[c.peer]
            partial = c.rx.partial_since()
            if ((session.token is None and now - c.accepted >= p.LOGIN_TIMEOUT)
                    or (partial is not None and now - partial >= p.FRAME_TIMEOUT)
                    or (c.tx and now - c.last_send >= p.SEND_TIMEOUT)):
                self._mark_close(c)

    def _wait_time(self):
        now = self.clock()
        deadlines = [now + p.SELECT_INTERVAL]
        for room in self.game.rooms.values():
            if room.deadline is not None:
                deadlines.append(room.deadline)
        for c in self.connections.values():
            if c.closing:
                deadlines.append(c.close_at)
                continue
            if c.rx.complete():
                return 0.0
            if self.game.sessions[c.peer].token is None:
                deadlines.append(c.accepted + p.LOGIN_TIMEOUT)
            partial = c.rx.partial_since()
            if partial is not None:
                deadlines.append(partial + p.FRAME_TIMEOUT)
            if c.tx:
                deadlines.append(c.last_send + p.SEND_TIMEOUT)
        return max(0.0, min(deadlines) - now)

    def step(self):
        self.game.tick()
        self._deliver()
        self._timeouts()
        self._finish_closes()
        for key, mask in self.selector.select(self._wait_time()):
            c = key.data
            if c is None:
                self._accept()
                continue
            if c.peer not in self.connections:
                continue
            if mask & selectors.EVENT_READ and not c.closing:
                self._read(c)
            if mask & selectors.EVENT_WRITE:
                self._write(c)
        self._timeouts()
        for c in tuple(self.connections.values()):
            for _ in range(p.FRAME_BUDGET):
                if c.closing or not c.rx.complete():
                    break
                try:
                    message = c.rx.pop()
                except p.ProtocolError:
                    self._mark_close(c)
                    break
                self.game.handle(c.peer, message)
                self._deliver()
                if self.game.sessions.get(c.peer) and self.game.sessions[c.peer].last_request >= p.MAX_SEQUENCE:
                    self._mark_close(c, drain=True)
            self._drain_cleanup()
        self._finish_closes()

    def run(self):
        try:
            while True:
                self.step()
        except KeyboardInterrupt:
            self.shutdown()
        finally:
            self.close()

    def shutdown(self):
        self.stopping = True
        if self.listener is not None:
            self.selector.unregister(self.listener)
            self.listener.close()
            self.listener = None
        self.game.shutdown()
        self._deliver()
        for c in self.connections.values():
            self._mark_close(c, drain=True)
        until = self.clock() + p.FLUSH_TIMEOUT
        while self.connections and self.clock() < until:
            self._finish_closes()
            for key, mask in self.selector.select(max(0, min(p.SELECT_INTERVAL, until - self.clock()))):
                if mask & selectors.EVENT_WRITE:
                    self._write(key.data)
        self._finish_closes()

    def close(self):
        self.game.stopping = True
        for c in tuple(self.connections.values()):
            self.game.disconnect(c.peer)
            c.sock.close()
        self.connections.clear()
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        self.selector.close()
