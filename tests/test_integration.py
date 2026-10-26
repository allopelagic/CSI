"""Actual CLI subprocesses talking to the actual server over loopback TCP."""
import json
import signal
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path

import protocol as p
from tests.support import CLI, ROOT, Process, RunningServer, WirePeer, bank


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "replacement.json"
        self.data = bank()
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        self.server = RunningServer(self.path)
        self.addCleanup(self.server.close)

    def client(self, uid):
        client = CLI(self.server, uid)
        self.addCleanup(client.close)
        return client.login()

    def lobby(self, clients, quiz="quiz-0"):
        room = clients[0].command("/create " + quiz, "CREATE_ROOM_RESPONSE")["room"]
        for client in clients[1:]:
            client.command("/join " + room["room_code"], "JOIN_ROOM_RESPONSE")
        for client in clients:
            client.command("/ready", "READY_RESPONSE")
        return room

    def start(self, clients, quiz="quiz-0"):
        room = self.lobby(clients, quiz)
        reply = clients[0].command("/start", "START_QUIZ_RESPONSE")
        self.assertEqual(reply["room"]["state"], "QUESTION_OPEN")
        for client in clients:
            event = client.event("QUIZ_STARTED_EVENT")
            self.assertEqual(event["room_code"], room["room_code"])
        return room

    def test_full_cli_workflow_wrong_duplicate_late_and_results(self):
        clients = [self.client("alice"), self.client("bob")]
        catalogue = clients[0].command("/quizzes", "LIST_QUIZZES_RESPONSE")["quizzes"]
        self.assertEqual([q["question_count"] for q in catalogue], [5, 6])
        self.assertNotIn("answer", str(catalogue))
        room = self.start(clients)
        all_questions = self.data["quizzes"][0]["questions"]
        for index, question in enumerate(all_questions):
            for client in clients:
                event = client.event("QUESTION_EVENT")
                self.assertEqual(event["question_id"], question["question_id"])
                self.assertEqual(event["number"], index + 1)
                self.assertNotIn("answer", event)
            qid, correct = question["question_id"], question["answer"]
            clients[0].command(f"/answer {qid} {correct.lower()}", "ANSWER_RESPONSE")
            if index == 0:
                error = clients[0].command(f"/answer {qid} {correct}", "ERROR_RESPONSE")
                self.assertEqual(error["code"], "DUPLICATE_ANSWER")
                error = clients[1].command(f"/answer {qid} invalid", "ERROR_RESPONSE")
                self.assertEqual(error["code"], "INVALID_ANSWER")
                board = clients[0].command("/score", "LEADERBOARD_RESPONSE")["leaderboard"]
                self.assertEqual([e["score"] for e in board], [0, 0])
            wrong = "B" if correct == "A" else "A"
            clients[1].command(f"/answer {qid} {wrong}", "ANSWER_RESPONSE")
            for client in clients:
                result = client.event("QUESTION_RESULT_EVENT")
                self.assertEqual(result["correct_answer"], correct)
                self.assertEqual(result["points_awarded"], question["points"] if client is clients[0] else 0)
            error = clients[0].command(f"/answer {qid} {correct}", "ERROR_RESPONSE")
            self.assertEqual(error["code"], "QUESTION_CLOSED")
        expected_score = sum(q["points"] for q in all_questions)
        for client in clients:
            final = client.event("QUIZ_FINISHED_EVENT")
            self.assertEqual(final["winner"], ["alice"])
            self.assertEqual([e["score"] for e in final["final_leaderboard"]], [expected_score, 0])
        clients[0].command("/players", "PLAYER_LIST_RESPONSE")
        error = clients[0].command("/start", "ERROR_RESPONSE")
        self.assertEqual(error["code"], "INVALID_STATE")
        clients[0].command("/leave", "LEAVE_ROOM_RESPONSE")
        clients[1].command("/leave", "LEAVE_ROOM_RESPONSE")
        newer = clients[0].command("/create quiz-1", "CREATE_ROOM_RESPONSE")["room"]
        self.assertNotEqual(newer["room_code"], room["room_code"])
        clients[0].command("/quit", "LOGOUT_RESPONSE")
        clients[0].process.wait(timeout=2)
        self.assertEqual(clients[0].process.returncode, 0)

    def test_ten_real_clients_two_independent_rooms_different_and_same_quiz(self):
        clients = [self.client(f"player{i:02}") for i in range(10)]
        for quiz_ids in (("quiz-0", "quiz-1"), ("quiz-1", "quiz-1")):
            with self.subTest(quizzes=quiz_ids):
                groups = [clients[:5], clients[5:]]
                rooms = [self.start(group, quiz_id) for group, quiz_id in zip(groups, quiz_ids)]
                self.assertNotEqual(rooms[0]["room_code"], rooms[1]["room_code"])
                quizzes = [next(q for q in self.data["quizzes"] if q["quiz_id"] == qid) for qid in quiz_ids]
                for index in range(max(len(q["questions"]) for q in quizzes)):
                    for group, quiz, room in zip(groups, quizzes, rooms):
                        if index >= len(quiz["questions"]):
                            continue
                        question = quiz["questions"][index]
                        for client in group:
                            event = client.event("QUESTION_EVENT")
                            self.assertEqual(event["room_code"], room["room_code"])
                            self.assertEqual(event["question_id"], question["question_id"])
                            client.command(f"/answer {question['question_id']} {question['answer']}", "ANSWER_RESPONSE")
                        for client in group:
                            result = client.event("QUESTION_RESULT_EVENT")
                            self.assertEqual({e["user_id"] for e in result["leaderboard"]}, {c.uid for c in group})
                            self.assertEqual(result["room_code"], room["room_code"])
                for group, quiz in zip(groups, quizzes):
                    expected = sum(q["points"] for q in quiz["questions"])
                    for client in group:
                        final = client.event("QUIZ_FINISHED_EVENT")
                        self.assertEqual(final["winner"], sorted(c.uid for c in group))
                        self.assertEqual([e["score"] for e in final["final_leaderboard"]], [expected] * 5)
                        client.command("/leave", "LEAVE_ROOM_RESPONSE")

    def test_real_timeout_missing_answer_and_no_deadline_extension(self):
        clients = [self.client("early"), self.client("missing")]
        self.start(clients)
        question = clients[0].event("QUESTION_EVENT")
        clients[1].event("QUESTION_EVENT")
        started = time.monotonic()
        clients[0].command(f"/answer {question['question_id']} A", "ANSWER_RESPONSE")
        result = clients[1].event("QUESTION_RESULT_EVENT", timeout=7)
        elapsed = time.monotonic() - started
        self.assertGreater(elapsed, 4)
        self.assertLess(elapsed, 6.5)
        self.assertIsNone(result["your_answer"])
        self.assertEqual(result["points_awarded"], 0)
        error = clients[1].command(f"/answer {question['question_id']} A", "ERROR_RESPONSE")
        self.assertEqual(error["code"], "QUESTION_CLOSED")
        next_question = clients[1].event("QUESTION_EVENT")
        self.assertEqual(next_question["number"], 2)

    def test_duplicate_identity_and_reuse_after_logout(self):
        first = self.client("same")
        duplicate = CLI(self.server, "same", "different")
        self.addCleanup(duplicate.close)
        self.assertEqual(duplicate.event("ERROR_RESPONSE")["code"], "DUPLICATE_USER")
        duplicate.process.wait(timeout=2)
        self.assertNotEqual(duplicate.process.returncode, 0)
        duplicate_name = CLI(self.server, "different", "same")
        self.addCleanup(duplicate_name.close)
        self.assertEqual(duplicate_name.event("ERROR_RESPONSE")["code"], "DUPLICATE_NAME")
        first.command("/quit", "LOGOUT_RESPONSE")
        self.client("same")

    def test_disconnect_keeps_answer_and_transfers_host(self):
        clients = [self.client(uid) for uid in ("host", "second", "third")]
        self.start(clients)
        for client in clients:
            client.event("QUESTION_EVENT")
        clients[0].command("/answer Q0_0 A", "ANSWER_RESPONSE")
        clients[0].process.kill()
        clients[0].process.wait(timeout=2)
        while True:
            status = clients[1].event("PLAYER_STATUS_EVENT")
            if len(status["room"]["players"]) == 2 and status["room"]["state"] == "QUESTION_OPEN":
                break
        self.assertEqual(status["room"]["host_user_id"], "second")
        for client in clients[1:]:
            client.command("/answer Q0_0 A", "ANSWER_RESPONSE")
        result = clients[1].event("QUESTION_RESULT_EVENT")
        host = next(e for e in result["leaderboard"] if e["user_id"] == "host")
        self.assertFalse(host["active"])
        self.assertEqual(host["score"], 7)
        self.client("host")

    def test_ctrl_c_notifies_clients_and_same_port_restarts(self):
        clients = [self.client("a"), self.client("b")]
        self.start(clients)
        self.server.stop()
        for client in clients:
            self.assertEqual(client.event("ROOM_CLOSED_EVENT")["reason"], "SERVER_SHUTDOWN")
            client.process.wait(timeout=2)
            self.assertEqual(client.process.returncode, 0)
        restarted = RunningServer(self.path, self.server.port)
        self.addCleanup(restarted.close)
        restarted.stop()

    def test_client_ctrl_c_and_stdin_eof_release_identity(self):
        client = self.client("interrupt")
        client.process.send_signal(signal.SIGINT)
        client.process.wait(timeout=2)
        self.assertEqual(client.process.returncode, 0)
        replacement = self.client("interrupt")
        replacement.process.stdin.close()
        replacement.process.wait(timeout=2)
        self.assertEqual(replacement.process.returncode, 0)
        self.client("interrupt")

    def test_invalid_commands_and_overlong_line_remain_usable(self):
        client = self.client("commands")
        for command in ("/unknown", "/join", "/ready extra", "/answer", "/create bad/id", "x" * 600):
            client.send(command)
            client.wait_line("LOCAL_ERROR")
        quizzes = client.command("/quizzes", "LIST_QUIZZES_RESPONSE")
        self.assertEqual(len(quizzes["quizzes"]), 2)

    def test_client_runs_with_no_question_bank_in_its_directory(self):
        for filename in ("client.py", "protocol.py"):
            (Path(self.directory.name) / filename).write_bytes((ROOT / filename).read_bytes())
        client = Process(["client.py", "--server", "127.0.0.1", "--port", self.server.port,
                          "--id", "isolated", "--name", "Isolated"], cwd=self.directory.name)
        self.addCleanup(client.close)
        client.event("LOGIN_RESPONSE")
        client.send("/quizzes")
        self.assertEqual(len(client.event("LIST_QUIZZES_RESPONSE")["quizzes"]), 2)

    def test_fragmented_coalesced_requests_and_bad_peer_isolation(self):
        peer = WirePeer(self.server.port)
        self.addCleanup(peer.close)
        login = p.encode(p.envelope("LOGIN_REQUEST", {"user_id": "wire", "name": "Wire"}, request_id="req-1"))
        for byte in login:
            peer.sock.sendall(bytes([byte]))
        peer.receive()
        peer.sequence = 1
        wires = [p.encode(p.envelope("LIST_QUIZZES_REQUEST", {}, request_id=f"req-{i}", session=peer.session))
                 for i in range(2, 22)]
        peer.sock.sendall(b"".join(wires))
        for i in range(2, 22):
            self.assertEqual(peer.receive()["request_id"], f"req-{i}")
        healthy = self.client("healthy")
        for raw in (struct.pack("!I", 0), struct.pack("!I", 65537),
                    struct.pack("!I", 2) + b'[]', struct.pack("!I", 13) + b'{"x":1,"x":2}'):
            with socket.create_connection(("127.0.0.1", self.server.port), timeout=2) as bad:
                bad.sendall(raw)
                self.assertEqual(bad.recv(1), b'')
            self.assertEqual(len(healthy.command("/quizzes", "LIST_QUIZZES_RESPONSE")["quizzes"]), 2)

    def test_session_spoof_replay_and_bad_payload_preserve_connection(self):
        peers = [WirePeer(self.server.port) for _ in range(2)]
        for i, peer in enumerate(peers):
            self.addCleanup(peer.close)
            peer.send("LOGIN", {"user_id": f"wire{i}", "name": f"Wire{i}"})
            peer.receive()
        peer, other = peers
        peer.send("LIST_QUIZZES", {}, session_id=other.session)
        error = peer.receive()
        self.assertEqual(error["payload"]["code"], "INVALID_SESSION")
        self.assertEqual(error["session_id"], peer.session)
        replay = peer.send("LIST_QUIZZES", {})
        peer.receive()
        peer.sock.sendall(p.encode(replay))
        self.assertEqual(peer.receive()["payload"]["code"], "INVALID_MESSAGE")
        peer.send("READY", {"ready": 1})
        self.assertEqual(peer.receive()["payload"]["code"], "INVALID_MESSAGE")
        peer.send("LIST_QUIZZES", {})
        self.assertEqual(peer.receive()["type"], "LIST_QUIZZES_RESPONSE")

    def test_last_request_response_flushes_before_eof(self):
        peer = WirePeer(self.server.port)
        self.addCleanup(peer.close)
        peer.send("LOGIN", {"user_id": "last", "name": "Last"})
        peer.receive()
        peer.send("LIST_ROOMS", {}, request_id="req-2147483647")
        reply = peer.receive()
        self.assertEqual(reply["request_id"], "req-2147483647")
        self.assertEqual(reply["type"], "LIST_ROOMS_RESPONSE")
        with self.assertRaises(EOFError):
            peer.receive()


class StartupTests(unittest.TestCase):
    def test_invalid_banks_fail_without_ready_or_listening(self):
        from tests.support import free_port
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for raw in (None, b'', b'{}', b'{', b'[]', b'{"schema_version":true,"quizzes":[]}'):
                with self.subTest(raw=raw):
                    if raw is not None:
                        path.write_bytes(raw)
                    port = free_port()
                    proc = Process(["server.py", "--host", "127.0.0.1", "--port", port, "--questions", path])
                    try:
                        proc.process.wait(timeout=3)
                        for thread in proc.threads:
                            thread.join(timeout=1)
                        self.assertNotEqual(proc.process.returncode, 0)
                        self.assertFalse(any("SERVER_READY" in line for line in proc.history))
                        self.assertTrue(proc.stderr)
                        with socket.socket() as probe:
                            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)
                    finally:
                        proc.close()
