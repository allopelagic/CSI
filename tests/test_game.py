import copy
import unittest

import protocol as p
from game import Close, Delivery, Game
from tests.support import bank


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class GameTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.game = Game(bank()["quizzes"], self.clock)
        self.counters = {}
        for peer in range(1, 5):
            self.game.connect(peer)
            self.request(peer, "LOGIN", {"user_id": f"u{peer}", "name": f"Name {peer}"})

    def request(self, peer, op, payload=None, **changes):
        self.counters[peer] = self.counters.get(peer, 0) + 1
        session = self.game.sessions[peer]
        message = p.envelope(op + "_REQUEST", {} if payload is None else payload,
                             request_id=f"req-{self.counters[peer]}", session=session.token)
        if op == "LOGIN":
            message["session_id"] = None
        message.update(changes)
        self.game.handle(peer, message)
        outputs = self.game.take_outputs()
        for item in outputs:
            if isinstance(item, Delivery):
                wire = p.envelope(item.kind, item.payload, request_id=item.request_id,
                                  event_id="evt-1" if item.kind.endswith("_EVENT") else None, session=item.session)
                p.validate_server_message(wire)
        return outputs

    def reply(self, outputs, peer=1):
        return next(x for x in outputs if isinstance(x, Delivery) and x.peer == peer and x.request_id)

    def error(self, outputs, code, peer=1):
        reply = self.reply(outputs, peer)
        self.assertEqual(reply.kind, "ERROR_RESPONSE")
        self.assertEqual(reply.payload["code"], code)
        return reply

    def lobby(self, members=(1, 2), quiz="quiz-0"):
        out = self.request(members[0], "CREATE_ROOM", {"quiz_id": quiz})
        code = self.reply(out, members[0]).payload["room"]["room_code"]
        for peer in members[1:]:
            self.request(peer, "JOIN_ROOM", {"room_code": code})
        return self.game.rooms[code]

    def start(self, members=(1, 2), quiz="quiz-0"):
        room = self.lobby(members, quiz)
        for peer in members:
            self.request(peer, "READY", {"ready": True})
        self.request(members[0], "START_QUIZ")
        return room

    def answer(self, room, peer, answer=None, qid=None):
        q = room.quiz["questions"][room.index]
        return self.request(peer, "ANSWER", {"room_code": room.code,
                            "question_id": q["question_id"] if qid is None else qid,
                            "answer": q["answer"] if answer is None else answer})

    def test_catalogue_has_no_question_information(self):
        out = self.reply(self.request(1, "LIST_QUIZZES")).payload["quizzes"]
        self.assertEqual([q["question_count"] for q in out], [5, 6])
        self.assertEqual(set(out[0]), {"quiz_id", "title", "description", "question_count"})

    def test_identity_uniqueness_and_connection_binding(self):
        self.game.connect(5)
        self.error(self.request(5, "LOGIN", {"user_id": "u1", "name": "New"}), "DUPLICATE_USER", 5)
        self.error(self.request(5, "LOGIN", {"user_id": "new", "name": "Name 1"}), "DUPLICATE_NAME", 5)
        forged = self.game.sessions[1].token
        self.error(self.request(5, "LIST_QUIZZES", session_id=forged), "INVALID_SESSION", 5)
        self.error(self.request(1, "LOGIN", {"user_id": "new", "name": "New"}), "INVALID_STATE")
        self.request(1, "LOGOUT")
        self.request(5, "LOGIN", {"user_id": "u1", "name": "Name 1"})
        self.assertNotEqual(self.game.sessions[5].token, forged)

    def test_sequence_consumption_order(self):
        before = self.game.sessions[1].last_request
        self.error(self.request(1, "LIST_QUIZZES", version=True, request_id="req-100"), "INVALID_MESSAGE")
        self.assertEqual(self.game.sessions[1].last_request, before)
        self.error(self.request(1, "UNKNOWN", request_id="req-100"), "INVALID_MESSAGE")
        self.assertEqual(self.game.sessions[1].last_request, 100)
        self.error(self.request(1, "LIST_QUIZZES", request_id="req-100"), "INVALID_MESSAGE")
        self.error(self.request(1, "READY", {"ready": 1}, request_id="req-101"), "INVALID_MESSAGE")
        self.assertEqual(self.game.sessions[1].last_request, 101)
        out = self.request(1, "LIST_QUIZZES", request_id="req-102")
        self.assertEqual(self.reply(out).kind, "LIST_QUIZZES_RESPONSE")

    def test_invalid_session_error_does_not_echo_forged_token(self):
        other = self.game.sessions[2].token
        out = self.request(1, "LIST_QUIZZES", session_id=other)
        reply = self.error(out, "INVALID_SESSION")
        self.assertEqual(reply.session, self.game.sessions[1].token)
        self.assertNotIn(other, str(reply.payload))

    def test_room_business_rules_and_same_ready_is_silent(self):
        self.error(self.request(1, "CREATE_ROOM", {"quiz_id": "missing"}), "QUIZ_NOT_FOUND")
        self.error(self.request(1, "JOIN_ROOM", {"room_code": "R999999"}), "ROOM_NOT_FOUND")
        room = self.lobby()
        self.error(self.request(1, "CREATE_ROOM", {"quiz_id": "quiz-0"}), "ALREADY_IN_ROOM")
        self.error(self.request(2, "START_QUIZ"), "NOT_ROOM_HOST", 2)
        self.error(self.request(1, "START_QUIZ"), "PLAYERS_NOT_READY")
        self.request(1, "READY", {"ready": True})
        out = self.request(1, "READY", {"ready": True})
        self.assertEqual(len(out), 1)
        self.request(3, "JOIN_ROOM", {"room_code": room.code})
        self.assertTrue(self.game.sessions[1].ready)
        self.assertFalse(self.game.sessions[3].ready)
        self.request(1, "LEAVE_ROOM")
        self.assertEqual(room.host, 2)
        self.request(2, "LEAVE_ROOM")
        self.request(3, "LEAVE_ROOM")
        self.assertNotIn(room.code, self.game.rooms)
        newer = self.lobby((1,))
        self.assertNotEqual(newer.code, room.code)
        self.error(self.request(1, "START_QUIZ"), "PLAYERS_NOT_READY")

    def test_start_order_and_no_open_answer_leak(self):
        room = self.lobby()
        for peer in (1, 2):
            self.request(peer, "READY", {"ready": True})
        out = self.request(1, "START_QUIZ")
        self.assertEqual([x.kind for x in out if x.peer == 1],
                         ["START_QUIZ_RESPONSE", "QUIZ_STARTED_EVENT", "QUESTION_EVENT"])
        question = next(x.payload for x in out if x.kind == "QUESTION_EVENT")
        self.assertNotIn("answer", question)
        self.assertNotIn("correct_answer", question)
        ack = self.reply(self.answer(room, 1))
        self.assertEqual(set(ack.payload), {"room_code", "question_id", "accepted"})
        self.assertTrue(all(x["score"] == 0 for x in self.game.board(room)))
        self.assertEqual(self.reply(self.request(1, "LEADERBOARD")).payload["leaderboard"], self.game.board(room))

    def test_invalid_duplicate_and_last_ack_before_result(self):
        room = self.start()
        self.error(self.answer(room, 1, "Z"), "INVALID_ANSWER")
        self.assertEqual(room.answers, {})
        self.answer(room, 1)
        self.error(self.answer(room, 1), "DUPLICATE_ANSWER")
        out = self.answer(room, 2, "D")
        self.assertEqual([x.kind for x in out if x.peer == 2],
                         ["ANSWER_RESPONSE", "QUESTION_RESULT_EVENT", "QUESTION_EVENT"])
        self.assertEqual(self.game.board(room)[0]["score"], 7)
        self.error(self.answer(room, 1, qid="Q0_0"), "QUESTION_CLOSED")
        self.error(self.answer(room, 1, qid="future"), "INVALID_ANSWER")
        self.assertEqual(room.index, 1)

    def test_deadline_equal_rejected_and_late_tick_has_full_next_time(self):
        room = self.start()
        deadline = room.deadline
        self.clock.now = deadline
        out = self.answer(room, 1, qid="Q0_0")
        self.error(out, "QUESTION_CLOSED")
        self.assertEqual(room.deadline, deadline + 5)
        result = next(x.payload for x in out if x.kind == "QUESTION_RESULT_EVENT")
        self.assertIsNone(result["your_answer"])
        self.assertEqual(result["points_awarded"], 0)
        self.clock.now += 200
        self.game.tick()
        self.assertEqual(room.index, 2)
        self.assertEqual(room.deadline, self.clock.now + 5)
        self.game.take_outputs()
        self.game.tick()
        self.assertEqual(self.game.take_outputs(), [])

    def test_accepted_before_deadline_and_scored_only_once(self):
        room = self.start()
        self.clock.now = room.deadline - 0.000001
        self.answer(room, 1)
        self.clock.now = room.deadline
        self.game.tick()
        self.game.take_outputs()
        self.game.tick()
        self.assertEqual(self.game.board(room)[0]["score"], 7)
        self.assertEqual(room.index, 1)

    def test_departure_rechecks_active_answers_after_status(self):
        room = self.start((1, 2, 3))
        self.answer(room, 2)
        self.answer(room, 1)
        out = self.request(3, "LEAVE_ROOM")
        kinds = [x.kind for x in out if x.peer == 1]
        self.assertEqual(kinds, ["PLAYER_STATUS_EVENT", "QUESTION_RESULT_EVENT", "QUESTION_EVENT"])
        self.assertEqual([e["score"] for e in self.game.board(room)], [7, 7, 0])
        self.assertFalse(next(e for e in self.game.board(room) if e["user_id"] == "u3")["active"])

    def test_accepted_answer_survives_disconnect_and_reconnect_does_not_replace(self):
        room = self.start((1, 2, 3))
        old_token = self.game.sessions[1].token
        self.answer(room, 1)
        self.game.disconnect(1)
        self.game.take_outputs()
        self.game.connect(1)
        self.request(1, "LOGIN", {"user_id": "u1", "name": "Name 1"})
        self.assertNotEqual(self.game.sessions[1].token, old_token)
        self.error(self.request(1, "JOIN_ROOM", {"room_code": room.code}), "INVALID_STATE")
        self.answer(room, 2)
        self.answer(room, 3)
        entry = next(x for x in self.game.board(room) if x["user_id"] == "u1")
        self.assertEqual(entry["score"], 7)
        self.assertFalse(entry["active"])
        self.assertEqual(room.host, 2)

    def test_single_player_continues_empty_room_discards(self):
        room = self.start()
        self.request(1, "LEAVE_ROOM")
        self.assertEqual(room.host, 2)
        self.answer(room, 2)
        self.assertEqual(room.index, 1)
        self.game.disconnect(2)
        outputs = self.game.take_outputs()
        self.assertNotIn(room.code, self.game.rooms)
        self.clock.now += 10000
        self.game.tick()
        self.assertEqual(self.game.take_outputs(), [])
        self.assertFalse(any(x.kind == "QUIZ_FINISHED_EVENT" for x in outputs))

    def test_finished_winners_rank_query_and_no_rematch(self):
        room = self.start((1, 2, 3))
        for _ in range(5):
            self.answer(room, 1)
            self.answer(room, 2)
            out = self.answer(room, 3, "D")
        final = next(x.payload for x in out if x.kind == "QUIZ_FINISHED_EVENT")
        self.assertEqual(final["winner"], ["u1", "u2"])
        self.assertEqual([e["rank"] for e in final["final_leaderboard"]], [1, 1, 3])
        self.assertEqual(room.state, "FINISHED")
        self.assertIsNone(room.deadline)
        self.assertEqual(self.reply(self.request(1, "LEADERBOARD")).payload["leaderboard"], final["final_leaderboard"])
        self.error(self.request(1, "START_QUIZ"), "INVALID_STATE")
        self.error(self.request(1, "READY", {"ready": True}), "INVALID_STATE")
        self.error(self.answer(room, 1, qid="Q0_0"), "QUESTION_CLOSED")

    def test_all_zero_players_are_joint_winners(self):
        room = self.start()
        for _ in range(5):
            self.clock.now = room.deadline
            self.game.tick()
        outputs = self.game.take_outputs()
        final = next(x.payload for x in outputs if x.kind == "QUIZ_FINISHED_EVENT")
        self.assertEqual(final["winner"], ["u1", "u2"])
        self.assertEqual([e["score"] for e in final["final_leaderboard"]], [0, 0])

    def test_output_snapshots_not_mutable_references(self):
        room = self.lobby()
        self.request(1, "READY", {"ready": True})
        output = self.request(2, "PLAYER_LIST")
        snapshot = copy.deepcopy(output)
        self.request(1, "LEAVE_ROOM")
        room.quiz["title"] = "changed"
        self.assertEqual(output, snapshot)

    def test_capacity_room_number_and_room_limit(self):
        room = self.lobby()
        for peer in range(5, 20):
            self.game.connect(peer)
            self.request(peer, "LOGIN", {"user_id": f"u{peer}", "name": f"Name {peer}"})
        for peer in range(5, 19):
            self.request(peer, "JOIN_ROOM", {"room_code": room.code})
        self.error(self.request(19, "JOIN_ROOM", {"room_code": room.code}), "ROOM_FULL", 19)
        self.assertEqual(self.reply(self.request(3, "LIST_ROOMS"), 3).payload, {"rooms": []})
        self.game.next_room = 1000000
        self.error(self.request(3, "CREATE_ROOM", {"quiz_id": "quiz-0"}), "INVALID_STATE", 3)
        self.game.next_room = 2
        old = p.MAX_ROOMS
        try:
            p.MAX_ROOMS = 1
            self.error(self.request(3, "CREATE_ROOM", {"quiz_id": "quiz-0"}), "INVALID_STATE", 3)
        finally:
            p.MAX_ROOMS = old

    def test_logout_token_snapshot_and_shutdown_no_advancement(self):
        room = self.start()
        old_token = self.game.sessions[1].token
        out = self.request(1, "LOGOUT")
        self.assertEqual(self.reply(out).session, old_token)
        self.assertTrue(any(isinstance(x, Close) for x in out))
        self.assertIsNone(self.game.sessions[1].token)
        self.game.shutdown()
        self.clock.now += 1000
        self.game.tick()
        out = self.game.take_outputs()
        self.assertEqual([x.kind for x in out], ["ROOM_CLOSED_EVENT"])
        self.assertEqual(room.index, 0)

    def test_two_rooms_timers_and_answers_are_isolated(self):
        self.game.by_quiz["quiz-0"]["questions"][1]["time_limit"] = 10
        left = self.start((1, 2))
        self.clock.now += 2
        right = self.start((3, 4), "quiz-1")
        self.answer(left, 1)
        self.answer(left, 2)
        self.assertEqual(right.index, 0)
        self.assertEqual(right.answers, {})
        self.assertTrue(all(x["score"] == 0 for x in self.game.board(right)))
        self.clock.now = right.deadline
        self.game.tick()
        self.assertEqual(right.index, 1)
        self.assertEqual(left.index, 1)
