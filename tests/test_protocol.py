import copy
import struct
import unittest

import protocol as p


class FramingTests(unittest.TestCase):
    def test_every_fragment_boundary_and_coalesced_tail(self):
        message = p.envelope("LOGIN_REQUEST", {"user_id": "u", "name": "网络"}, request_id="req-1")
        wire = p.encode(message)
        for split in range(len(wire) + 1):
            with self.subTest(split=split):
                rx = p.Framer()
                rx.feed(wire[:split], 1)
                rx.feed(wire[split:] + wire + wire[:7], 2)
                self.assertEqual(rx.pop(), message)
                self.assertEqual(rx.pop(), message)
                self.assertFalse(rx.complete())
                self.assertEqual(rx.partial_since(), 2)
                rx.feed(wire[7:], 9)
                self.assertEqual(rx.pop(), message)
                self.assertIsNone(rx.partial_since())

    def test_partial_age_does_not_extend_with_drips(self):
        wire = p.encode({"x": "y"})
        rx = p.Framer()
        rx.feed(wire + wire[:1], 1)
        rx.feed(wire[1:5], 8)
        self.assertEqual(rx.partial_since(), 1)
        rx.pop()
        self.assertEqual(rx.partial_since(), 1)
        rx.feed(wire[5:], 10)
        self.assertIsNone(rx.partial_since())

    def test_complete_queued_frames_are_not_partial(self):
        rx = p.Framer()
        rx.feed(p.encode({"x": 1}) * 100, 1)
        self.assertIsNone(rx.partial_since())

    def test_bad_lengths_close_without_waiting_for_body(self):
        for length in (0, 65537, 2**32 - 1):
            with self.subTest(length=length), self.assertRaises(p.ProtocolError):
                p.Framer().feed(struct.pack("!I", length), 0)

    def test_receive_buffer_bound(self):
        rx = p.Framer()
        wire = p.encode({"x": "y"})
        with self.assertRaises(p.ProtocolError):
            rx.feed(wire * (p.MAX_RX // len(wire) + 1), 0)

    def test_strict_json(self):
        bad = [b'', b'[]', b'null', b'1', b'{"x":1,"x":2}', b'{"x":NaN}',
               b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e999}',
               b'{"x":"\\ud800"}', b'{"\\udfff":0}', b'\xef\xbb\xbf{}',
               b'{"x":"\xff"}', b'{} trailing', b'{' * 1500]
        for raw in bad:
            with self.subTest(raw=raw[:50]), self.assertRaises(p.ProtocolError):
                p.loads(raw)

    def test_length_is_utf8_bytes(self):
        wire = p.encode({"网络": "题目"})
        self.assertEqual(struct.unpack("!I", wire[:4])[0], len(wire) - 4)
        self.assertIn("网络".encode(), wire)
        for bad in ({"x": float("nan")}, {"x": float("inf")}, {"x": "\ud800"}):
            with self.assertRaises(p.ProtocolError):
                p.encode(bad)


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.message = p.envelope("LOGIN_REQUEST", {"user_id": "u", "name": "User"}, request_id="req-1")

    def test_request_sequences(self):
        valid = ("req-1", "req-20", "req-2147483647")
        invalid = (None, True, 1, "req-0", "req-01", "req--1", "req-2147483648", "req-1.0")
        for v in valid:
            self.assertIsNotNone(p.sequence(v, "req"))
        for v in invalid:
            self.assertIsNone(p.sequence(v, "req"))

    def test_public_format(self):
        mutations = [("version", True), ("version", 1.0), ("version", 2),
                     ("type", "login_REQUEST"), ("type", []), ("payload", []),
                     ("request_id", "req-01"), ("event_id", "evt-1"),
                     ("session_id", "s-" + "A" * 32)]
        for key, value in mutations:
            with self.subTest(key=key, value=value), self.assertRaises(p.ProtocolError):
                p.validate_request_common(dict(self.message, **{key: value}))
        for altered in (dict(self.message, extra=1), {k: v for k, v in self.message.items() if k != "payload"}):
            with self.assertRaises(p.ProtocolError):
                p.validate_request_common(altered)

    def test_payload_exact_types_and_business_answer(self):
        for payload in ({"user_id": "u", "name": " "}, {"user_id": "中", "name": "x"},
                        {"user_id": "u", "name": "x\x1b"}, {"user_id": "u", "name": "x", "x": 1}):
            with self.assertRaises(p.ProtocolError):
                p.validate_request_payload(dict(self.message, payload=payload))
        for bad in (1, "true", None, []):
            with self.assertRaises(p.ProtocolError):
                p.validate_request_payload(p.envelope("READY_REQUEST", {"ready": bad}))
        message = p.envelope("ANSWER_REQUEST", {"room_code": "R000001", "question_id": "q", "answer": "wrong"})
        self.assertEqual(p.validate_request_payload(message), "ANSWER")

    def test_all_request_schemas(self):
        payloads = {"LOGIN": {"user_id": "u", "name": "用户"}, "CREATE_ROOM": {"quiz_id": "q"},
                    "JOIN_ROOM": {"room_code": "R999999"}, "READY": {"ready": False},
                    "ANSWER": {"room_code": "R000001", "question_id": "q", "answer": "A"}}
        for op in p.OPERATIONS:
            message = p.envelope(op + "_REQUEST", payloads.get(op, {}), request_id="req-1")
            self.assertEqual(p.validate_request_payload(message), op)
            with self.assertRaises(p.ProtocolError):
                p.validate_request_payload(dict(message, payload=dict(message["payload"], extra=0)))

    def test_rank_and_room_relational_invariants(self):
        board = [{"rank": 1, "user_id": "a", "name": "A", "score": 2, "active": True},
                 {"rank": 1, "user_id": "b", "name": "B", "score": 2, "active": False},
                 {"rank": 3, "user_id": "c", "name": "C", "score": 0, "active": True}]
        p.leaderboard(board)
        broken = copy.deepcopy(board)
        broken[-1]["rank"] = 2
        with self.assertRaises(p.ProtocolError):
            p.leaderboard(broken)
        with self.assertRaises(p.ProtocolError):
            p.leaderboard(board[::-1])

    def test_malformed_server_messages_raise_protocol_error(self):
        for code in ([], {}, True, None, "OTHER"):
            with self.assertRaises(p.ProtocolError):
                p.validate_server_message(p.envelope("ERROR_RESPONSE", {"code": code, "message": "Bad"}))
