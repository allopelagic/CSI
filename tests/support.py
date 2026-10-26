from __future__ import annotations

import json
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import protocol as p

ROOT = Path(__file__).resolve().parents[1]


def bank(counts=(5, 6), limit=5):
    """Synthetic replacement bank: unequal counts, IDs, points, order and Unicode."""
    return {"schema_version": 1, "future": {}, "quizzes": [
        {"quiz_id": f"quiz-{i}", "title": f"Quiz {i} 网络", "description": "",
         "questions": [{"question_id": f"Q{i}_{j}", "text": f"Question {j}?",
                        "options": {"A": "alpha", "B": "beta", "C": "gamma", "D": "delta"},
                        "answer": "ABCD"[j % 4], "time_limit": limit, "points": 7 + j * 13}
                       for j in range(count)]} for i, count in enumerate(counts)]}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Process:
    def __init__(self, args, cwd=ROOT):
        self.process = subprocess.Popen([sys.executable, "-u", *map(str, args)], cwd=cwd,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self.lines = queue.Queue()
        self.stderr = []
        self.history = []
        self.deferred = []
        self.threads = [threading.Thread(target=self._read, args=(self.process.stdout,), daemon=True),
                        threading.Thread(target=self._errors, daemon=True)]
        for thread in self.threads:
            thread.start()

    def _read(self, stream):
        for line in stream:
            text = line.rstrip("\n")
            self.history.append(text)
            self.lines.put(text)
        self.lines.put(None)

    def _errors(self):
        for line in self.process.stderr:
            self.stderr.append(line.rstrip("\n"))

    def send(self, line):
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def wait_line(self, prefix, timeout=4):
        for index, line in enumerate(self.deferred):
            if line.startswith(prefix):
                return self.deferred.pop(index)
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                line = self.lines.get(timeout=max(0.01, until - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                raise AssertionError(f"Process exited waiting for {prefix}: {self.history[-5:]} {self.stderr}")
            if line.startswith(prefix):
                return line
            self.deferred.append(line)
        raise AssertionError(f"Timed out waiting for {prefix}: {self.history[-5:]} {self.stderr}")

    def event(self, kind, timeout=4):
        line = self.wait_line(kind + " ", timeout)
        return json.loads(line[len(kind) + 1:])

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for thread in self.threads:
            thread.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


class RunningServer(Process):
    def __init__(self, path, port=None):
        self.port = free_port() if port is None else port
        super().__init__(["server.py", "--host", "127.0.0.1", "--port", self.port, "--questions", path])
        try:
            self.wait_line("SERVER_READY")
        except BaseException:
            self.close()
            raise

    def stop(self):
        self.process.send_signal(signal.SIGINT)
        self.wait_line("SERVER_STOPPED")
        self.process.wait(timeout=2)
        if self.process.returncode:
            raise AssertionError(self.stderr)


class CLI(Process):
    def __init__(self, server, uid, name=None):
        super().__init__(["client.py", "--server", "127.0.0.1", "--port", server.port,
                          "--id", uid, "--name", uid if name is None else name])
        self.uid = uid

    def login(self):
        self.event("LOGIN_RESPONSE")
        return self

    def command(self, text, response):
        self.send(text)
        return self.event(response)


class WirePeer:
    """Adverse wire tests only; assessed end-to-end tests use CLI above."""
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.sock.settimeout(3)
        self.rx = p.Framer()
        self.sequence = 0
        self.session = None

    def send(self, op, payload, **overrides):
        self.sequence += 1
        message = p.envelope(op + "_REQUEST", payload, request_id=f"req-{self.sequence}", session=self.session)
        message.update(overrides)
        self.sock.sendall(p.encode(message))
        return message

    def receive(self):
        while not self.rx.complete():
            data = self.sock.recv(65536)
            if not data:
                raise EOFError
            self.rx.feed(data, time.monotonic())
        message = self.rx.pop()
        p.validate_server_message(message)
        if message["type"] == "LOGIN_RESPONSE":
            self.session = message["session_id"]
        return message

    def close(self):
        self.sock.close()
