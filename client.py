"""CLI client: the main thread owns sockets/state/output; stdin only feeds a queue."""
from __future__ import annotations

import argparse
import json
import queue
import selectors
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

import protocol as p

HELP = """/quizzes                 list quiz catalogue
/rooms                   list joinable lobbies
/create quiz_id          create and join a room
/join R000001            join a lobby
/players                 show room members and host
/ready | /unready        set lobby readiness
/start                   host starts when everyone is ready
/answer question_id A    submit A/B/C/D (duplicates/late answers reach server)
/score                   query settled scores
/leave                   leave room, keep login
/quit                    log out and close
/help                    show this help"""


def safe(text):
    return "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in str(text))


class Input:
    def __init__(self, stream):
        self.stream = stream
        self.lines = queue.Queue(maxsize=p.MAX_INPUT)
        self.eof = threading.Event()
        self.overflow = threading.Event()
        self.force_quit = threading.Event()

    def read(self):
        try:
            while True:
                line = self.stream.readline(p.MAX_LINE + 2)
                if not line:
                    break
                text = line.removesuffix("\n").removesuffix("\r")
                if len(text) > p.MAX_LINE:
                    while line and not line.endswith("\n"):
                        line = self.stream.readline(p.MAX_LINE + 2)
                    text = None  # Discard the entire overlong line, never execute its tail.
                try:
                    self.lines.put_nowait(text)
                except queue.Full:
                    self.overflow.set()
                    if text == "/quit":
                        self.force_quit.set()
        except (OSError, UnicodeError):
            self.overflow.set()
        finally:
            self.eof.set()

    def take_quit(self):
        # An explicit exit must remain accessible even behind a blocked command.
        with self.lines.mutex:
            try:
                self.lines.queue.remove("/quit")
            except ValueError:
                return False
            self.lines.not_full.notify()
            return True


@dataclass
class Pending:
    op: str
    created: float
    warned: bool = False


class Client:
    BARRIERS = {"LOGIN", "CREATE_ROOM", "JOIN_ROOM", "READY", "START_QUIZ", "LEAVE_ROOM", "LOGOUT"}

    def __init__(self, sock, user_id, name, stdin=None, stdout=None, clock=time.monotonic):
        self.sock = sock
        self.sock.setblocking(False)
        self.clock = clock
        self.stdout = sys.stdout if stdout is None else stdout
        self.input = Input(sys.stdin if stdin is None else stdin)
        self.selector = selectors.DefaultSelector()
        self.selector.register(sock, selectors.EVENT_READ)
        self.rx = p.Framer()
        self.tx = deque()
        self.tx_bytes = 0
        self.last_send = clock()
        self.pending: dict[str, Pending] = {}
        self.next_request = 1
        self.last_event = 0
        self.session = None
        self._user_id = user_id
        self.room = None
        self.barrier = None
        self.quitting_at = None
        self.done = False
        self.status = 0
        self.send_request("LOGIN", {"user_id": user_id, "name": name})

    def display(self, kind, payload=None):
        text = kind if payload is None else kind + " " + json.dumps(payload, ensure_ascii=False, allow_nan=False)
        print(safe(text), file=self.stdout, flush=True)

    def send_request(self, op, payload):
        if len(self.pending) >= p.MAX_PENDING or self.next_request > p.MAX_SEQUENCE:
            self.display("LOCAL_ERROR request capacity reached")
            return False
        rid = f"req-{self.next_request}"
        message = p.envelope(op + "_REQUEST", payload, request_id=rid, session=self.session)
        p.validate_request_common(message)
        p.validate_request_payload(message)
        data = p.encode(message)
        if self.tx_bytes + len(data) > p.MAX_TX:
            self.display("LOCAL_ERROR output buffer full")
            return False
        self.next_request += 1
        self.pending[rid] = Pending(op, self.clock())
        if op in self.BARRIERS:
            self.barrier = rid
        if not self.tx:
            self.last_send = self.clock()
        self.tx.append(memoryview(data))
        self.tx_bytes += len(data)
        return True

    def _quit(self):
        if self.quitting_at is not None:
            return
        self.quitting_at = self.clock() + p.FLUSH_TIMEOUT
        if self.session is None:
            self.done = True
        elif len(self.pending) < p.MAX_PENDING:
            self.send_request("LOGOUT", {})

    def command(self, line):
        if line is None:
            self.display("LOCAL_ERROR line exceeds 512 characters")
            return
        args = line.split()
        if not args:
            return
        cmd = args[0]
        if cmd == "/help" and len(args) == 1:
            for entry in HELP.splitlines():
                self.display(entry)
            return
        if cmd == "/quit" and len(args) == 1:
            self._quit()
            return
        simple = {"/quizzes": "LIST_QUIZZES", "/rooms": "LIST_ROOMS", "/players": "PLAYER_LIST",
                  "/start": "START_QUIZ", "/score": "LEADERBOARD", "/leave": "LEAVE_ROOM"}
        try:
            if cmd in simple and len(args) == 1:
                self.send_request(simple[cmd], {})
            elif cmd in ("/ready", "/unready") and len(args) == 1:
                self.send_request("READY", {"ready": cmd == "/ready"})
            elif cmd == "/create" and len(args) == 2:
                self.send_request("CREATE_ROOM", {"quiz_id": args[1]})
            elif cmd == "/join" and len(args) == 2:
                self.send_request("JOIN_ROOM", {"room_code": args[1]})
            elif cmd == "/answer" and len(args) == 3:
                if self.room is None:
                    self.display("LOCAL_ERROR join a room before answering")
                else:
                    self.send_request("ANSWER", {"room_code": self.room["room_code"],
                                      "question_id": args[1], "answer": args[2].upper()})
            else:
                self.display("LOCAL_ERROR unknown command or wrong arguments; use /help")
        except p.ProtocolError:
            self.display("LOCAL_ERROR invalid command argument")

    def _inputs(self):
        if self.quitting_at is not None:
            return
        if self.input.overflow.is_set():
            self.input.overflow.clear()
            self.display("LOCAL_ERROR input queue full or input decoding failed; line discarded")
        if self.input.force_quit.is_set():
            self._quit()
            return
        for _ in range(p.FRAME_BUDGET):
            if self.barrier is not None or len(self.pending) >= p.MAX_PENDING:
                if self.input.take_quit():
                    self._quit()
                break
            try:
                line = self.input.lines.get_nowait()
            except queue.Empty:
                if self.input.eof.is_set() and not self.pending:
                    self._quit()
                break
            self.command(line)
            if self.quitting_at is not None:
                break

    def handle(self, message):
        p.validate_server_message(message)
        kind, rid, payload = message["type"], message["request_id"], message["payload"]
        if kind.endswith("_EVENT"):
            p.require(message["session_id"] == self.session and self.session is not None)
            event_number = p.sequence(message["event_id"], "evt")
            p.require(event_number == self.last_event + 1, "Event number did not increase by one.")
            self.last_event = event_number
            if kind == "PLAYER_STATUS_EVENT":
                p.require(any(x["user_id"] == self.user_id for x in payload["room"]["players"]))
                self.room = payload["room"]
            elif kind == "ROOM_CLOSED_EVENT":
                p.require(self.room is not None and payload["room_code"] == self.room["room_code"])
                self.room = None
            elif kind in ("QUESTION_EVENT", "QUESTION_RESULT_EVENT", "QUIZ_STARTED_EVENT", "QUIZ_FINISHED_EVENT"):
                p.require(self.room is not None and payload["room_code"] == self.room["room_code"])
                if kind == "QUESTION_EVENT":
                    self.room = dict(self.room, state="QUESTION_OPEN", current_question_id=payload["question_id"])
                elif kind == "QUIZ_FINISHED_EVENT":
                    self.room = dict(self.room, state="FINISHED", current_question_id=None)
        else:
            if kind == "ERROR_RESPONSE" and rid is None:
                p.require(message["session_id"] == self.session)
                self.display(kind, payload)
                return
            p.require(rid in self.pending, "Unknown or repeated response ID.")
            pending = self.pending[rid]
            p.require(kind == "ERROR_RESPONSE" or kind == pending.op + "_RESPONSE", "Unexpected response type.")
            if kind == "LOGIN_RESPONSE":
                p.require(self.session is None)
                self.session = message["session_id"]
            else:
                p.require(message["session_id"] == self.session, "Unexpected server session.")
            del self.pending[rid]
            if self.barrier == rid:
                self.barrier = None
            if "room" in payload:
                p.require(any(x["user_id"] == self.user_id for x in payload["room"]["players"]))
                self.room = payload["room"]
            if kind == "LEAVE_ROOM_RESPONSE":
                self.room = None
            elif kind == "LOGOUT_RESPONSE":
                self.done = True
            elif kind == "ERROR_RESPONSE" and pending.op == "LOGIN":
                self.done, self.status = True, 1
        self.display(kind, payload)

    @property
    def user_id(self):
        return self._user_id

    def _read(self):
        remaining = p.IO_BUDGET
        while remaining and len(self.rx) < p.MAX_RX:
            try:
                data = self.sock.recv(min(remaining, p.MAX_RX - len(self.rx)))
            except BlockingIOError:
                break
            if not data:
                # Decode all already-received frames before interpreting EOF.
                self.remote_eof = True
                break
            remaining -= len(data)
            self.rx.feed(data, self.clock())

    def _write(self):
        remaining = p.IO_BUDGET
        while remaining and self.tx:
            try:
                count = self.sock.send(self.tx[0][:remaining])
            except BlockingIOError:
                break
            if not count:
                raise OSError("Connection closed while sending")
            remaining -= count
            self.tx_bytes -= count
            self.last_send = self.clock()
            if count == len(self.tx[0]):
                self.tx.popleft()
            else:
                self.tx[0] = self.tx[0][count:]

    def run(self):
        self.remote_eof = False
        threading.Thread(target=self.input.read, daemon=True, name="stdin-reader").start()
        try:
            while not self.done:
                self._inputs()
                now = self.clock()
                if self.quitting_at is not None and now >= self.quitting_at:
                    break
                for rid, pending in self.pending.items():
                    if not pending.warned and now - pending.created >= p.PENDING_TIMEOUT:
                        pending.warned = True
                        self.display("WARNING response pending " + rid + "; not retried")
                partial = self.rx.partial_since()
                if partial is not None and now - partial >= p.FRAME_TIMEOUT:
                    raise p.ProtocolError("Incomplete server frame timed out.")
                if self.tx and now - self.last_send >= p.SEND_TIMEOUT:
                    raise OSError("Server is not reading output")
                for _ in range(p.FRAME_BUDGET):
                    if self.done or not self.rx.complete():
                        break
                    self.handle(self.rx.pop())
                if self.done:
                    break
                if self.remote_eof and not self.rx.complete():
                    p.require(not len(self.rx), "Server closed during a frame.")
                    self.display("SERVER_DISCONNECTED")
                    break
                events = (selectors.EVENT_READ if len(self.rx) < p.MAX_RX and not self.remote_eof else 0)
                if self.tx:
                    events |= selectors.EVENT_WRITE
                # Full RX contains complete frames and is drained on the next iteration.
                if not events:
                    continue
                self.selector.modify(self.sock, events)
                timeout = 0.0 if self.rx.complete() else p.SELECT_INTERVAL
                if self.quitting_at is not None:
                    timeout = min(timeout, max(0, self.quitting_at - self.clock()))
                for _, mask in self.selector.select(timeout):
                    if mask & selectors.EVENT_READ:
                        self._read()
                    if mask & selectors.EVENT_WRITE:
                        self._write()
        except KeyboardInterrupt:
            self._quit()
            until = self.clock() + p.FLUSH_TIMEOUT
            while not self.done and self.clock() < until:
                try:
                    self.selector.modify(self.sock, selectors.EVENT_READ | (selectors.EVENT_WRITE if self.tx else 0))
                    for _, mask in self.selector.select(min(p.SELECT_INTERVAL, max(0, until - self.clock()))):
                        if mask & selectors.EVENT_READ:
                            self._read()
                        if mask & selectors.EVENT_WRITE:
                            self._write()
                    for _ in range(p.FRAME_BUDGET):
                        if not self.rx.complete():
                            break
                        self.handle(self.rx.pop())
                    if self.remote_eof:
                        break
                except (OSError, p.ProtocolError):
                    break
        except (OSError, p.ProtocolError) as exc:
            self.display("CONNECTION_ERROR " + safe(str(exc)))
            self.status = 1
        finally:
            self.selector.close()
            self.sock.close()
        return self.status


def port_number(text):
    try:
        port = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be 1-65535")
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description="Networked Quiz Tournament CLI")
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", required=True, type=port_number)
    parser.add_argument("--id", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    if not p.identifier(args.id) or not p.name(args.name):
        parser.error("id must be 1-32 ASCII letters/digits/_/-; name 1-40 printable, nonblank characters")
    try:
        sock = socket.create_connection((args.server, args.port), timeout=p.CONNECT_TIMEOUT)
    except OSError as exc:
        print("Connection failed: " + safe(str(exc)), file=sys.stderr, flush=True)
        return 1
    return Client(sock, args.id, args.name).run()


if __name__ == "__main__":
    sys.exit(main())
