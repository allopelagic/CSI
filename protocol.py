"""Version 1 wire format, strict JSON and shared message schemas (stdlib only)."""
from __future__ import annotations

import json
import math
import re
import struct
from collections import deque

VERSION = 1
MAX_FRAME = 65536
MAX_RX = 128 * 1024
MAX_TX = 256 * 1024
MAX_SEQUENCE = 2147483647
MAX_CONNECTIONS = 64
MAX_ROOMS = 32
ROOM_CAPACITY = 16
IO_BUDGET = 32 * 1024
FRAME_BUDGET = 8
ACCEPT_BUDGET = 16
LOGIN_TIMEOUT = 10.0
FRAME_TIMEOUT = 10.0
SEND_TIMEOUT = 10.0
FLUSH_TIMEOUT = 0.5
SELECT_INTERVAL = 0.1
CONNECT_TIMEOUT = 5.0
PENDING_TIMEOUT = 5.0
MAX_PENDING = 64
MAX_INPUT = 128
MAX_LINE = 512

COMMON = {"version", "type", "request_id", "event_id", "session_id", "payload"}
OPERATIONS = ("LOGIN", "LIST_QUIZZES", "LIST_ROOMS", "CREATE_ROOM", "JOIN_ROOM",
              "PLAYER_LIST", "READY", "START_QUIZ", "ANSWER", "LEADERBOARD",
              "LEAVE_ROOM", "LOGOUT")
ERRORS = {"INVALID_MESSAGE", "INVALID_SESSION", "INVALID_STATE", "DUPLICATE_USER",
          "DUPLICATE_NAME", "QUIZ_NOT_FOUND", "ROOM_NOT_FOUND", "ROOM_FULL",
          "ALREADY_IN_ROOM", "NOT_ROOM_HOST", "PLAYERS_NOT_READY", "QUESTION_CLOSED",
          "DUPLICATE_ANSWER", "INVALID_ANSWER"}


class ProtocolError(ValueError):
    """Malformed wire data or message schema."""


def require(condition, message="Invalid message structure."):
    if not condition:
        raise ProtocolError(message)


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def string(value, low, high, *, printable=False, nonblank=False):
    return (isinstance(value, str) and low <= len(value) <= high
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)
            and (not printable or all(c.isprintable() for c in value))
            and (not nonblank or bool(value.strip())))


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value) is not None


def name(value):
    return string(value, 1, 40, printable=True, nonblank=True)


def room_code(value):
    return (isinstance(value, str) and re.fullmatch(r"R[0-9]{6}", value) is not None
            and value != "R000000")


def session_id(value):
    return isinstance(value, str) and re.fullmatch(r"s-[0-9a-f]{32}", value) is not None


def sequence(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + r"-[1-9][0-9]{0,9}", value):
        return None
    number = int(value[len(prefix) + 1:])
    return number if number <= MAX_SEQUENCE else None


def safe_request_id(message):
    value = message.get("request_id") if isinstance(message, dict) else None
    return value if sequence(value, "req") is not None else None


def exact(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON member.")
        result[key] = value
    return result


def _unicode(value):
    if isinstance(value, str):
        require(not any(0xD800 <= ord(c) <= 0xDFFF for c in value), "Invalid Unicode.")
    elif isinstance(value, float):
        require(math.isfinite(value), "Non-finite JSON number.")
    elif isinstance(value, dict):
        for k, v in value.items():
            _unicode(k)
            _unicode(v)
    elif isinstance(value, list):
        for v in value:
            _unicode(v)


def loads(data):
    def bad_constant(_):
        raise ProtocolError("Non-finite JSON number.")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=bad_constant)
        require(isinstance(value, dict), "JSON root must be an object.")
        _unicode(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid UTF-8 JSON object.") from exc


def encode(message):
    try:
        require(isinstance(message, dict))
        _unicode(message)
        body = json.dumps(message, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Cannot encode message.") from exc
    require(1 <= len(body) <= MAX_FRAME, "Frame length out of range.")
    return struct.pack("!I", len(body)) + body


class Framer:
    """Bounded wire buffer. Segment timestamps retain the age of a trailing half-frame."""
    def __init__(self):
        self.buffer = bytearray()
        self.segments = deque()

    def __len__(self):
        return len(self.buffer)

    def feed(self, data, now):
        require(len(self.buffer) + len(data) <= MAX_RX, "Receive buffer limit.")
        if data:
            self.buffer.extend(data)
            self.segments.append([len(data), now])
        self.partial_since()  # Validate every available length header immediately.

    def _time_at(self, offset):
        for length, timestamp in self.segments:
            if offset < length:
                return timestamp
            offset -= length
        return None

    def partial_since(self):
        offset = 0
        while offset < len(self.buffer):
            remaining = len(self.buffer) - offset
            if remaining < 4:
                return self._time_at(offset)
            length = struct.unpack_from("!I", self.buffer, offset)[0]
            require(1 <= length <= MAX_FRAME, "Frame length out of range.")
            if remaining < 4 + length:
                return self._time_at(offset)
            offset += 4 + length
        return None

    def complete(self):
        return len(self.buffer) >= 4 and len(self.buffer) >= 4 + struct.unpack_from("!I", self.buffer)[0]

    def pop(self):
        require(self.complete(), "Incomplete frame.")
        length = struct.unpack_from("!I", self.buffer)[0]
        body = bytes(self.buffer[4:4 + length])
        consumed = 4 + length
        del self.buffer[:consumed]
        while consumed:
            size, timestamp = self.segments.popleft()
            if size > consumed:
                self.segments.appendleft([size - consumed, timestamp])
                break
            consumed -= size
        return loads(body)


def envelope(kind, payload, *, request_id=None, event_id=None, session=None):
    return {"version": VERSION, "type": kind, "request_id": request_id,
            "event_id": event_id, "session_id": session, "payload": payload}


def validate_request_common(message):
    exact(message, COMMON)
    require(type(message["version"]) is int and message["version"] == VERSION)
    require(isinstance(message["type"], str)
            and re.fullmatch(r"[A-Z_]{1,64}", message["type"]) is not None)
    require(sequence(message["request_id"], "req") is not None)
    require(message["event_id"] is None)
    require(message["session_id"] is None or session_id(message["session_id"]))
    require(isinstance(message["payload"], dict))
    return sequence(message["request_id"], "req")


def validate_request_payload(message):
    kind, p = message["type"], message["payload"]
    require(kind in {x + "_REQUEST" for x in OPERATIONS})
    op = kind.removesuffix("_REQUEST")
    fields = {"LOGIN": {"user_id", "name"}, "CREATE_ROOM": {"quiz_id"},
              "JOIN_ROOM": {"room_code"}, "READY": {"ready"},
              "ANSWER": {"room_code", "question_id", "answer"}}.get(op, set())
    exact(p, fields)
    if op == "LOGIN":
        require(identifier(p["user_id"]) and name(p["name"]))
    elif op == "CREATE_ROOM":
        require(identifier(p["quiz_id"]))
    elif op == "JOIN_ROOM":
        require(room_code(p["room_code"]))
    elif op == "READY":
        require(type(p["ready"]) is bool)
    elif op == "ANSWER":
        require(room_code(p["room_code"]) and identifier(p["question_id"]))
        require(isinstance(p["answer"], str))  # Bad strings are business INVALID_ANSWER.
    return op


def quiz(value):
    exact(value, {"quiz_id", "title", "description", "question_count"})
    require(identifier(value["quiz_id"]))
    require(string(value["title"], 1, 100, printable=True, nonblank=True))
    require(string(value["description"], 0, 300, printable=True))
    require(integer(value["question_count"], 5, 50))


def player(value):
    exact(value, {"user_id", "name", "ready"})
    require(identifier(value["user_id"]) and name(value["name"]) and type(value["ready"]) is bool)


def array(value, low, high, validator):
    require(isinstance(value, list) and low <= len(value) <= high)
    for item in value:
        validator(item)


def unique(values):
    require(len(values) == len(set(values)))


def room(value):
    exact(value, {"room_code", "quiz_id", "quiz_title", "state", "host_user_id",
                  "players", "current_question_id"})
    require(room_code(value["room_code"]) and identifier(value["quiz_id"]))
    require(string(value["quiz_title"], 1, 100, printable=True, nonblank=True))
    require(value["state"] in ("LOBBY", "QUESTION_OPEN", "FINISHED"))
    array(value["players"], 1, 16, player)
    ids = [p["user_id"] for p in value["players"]]
    unique(ids)
    unique([p["name"] for p in value["players"]])
    require(value["host_user_id"] in ids)
    require(identifier(value["current_question_id"]) if value["state"] == "QUESTION_OPEN"
            else value["current_question_id"] is None)


def summary(value):
    exact(value, {"room_code", "quiz_id", "quiz_title", "host_user_id", "player_count"})
    require(room_code(value["room_code"]) and identifier(value["quiz_id"]))
    require(string(value["quiz_title"], 1, 100, printable=True, nonblank=True))
    require(identifier(value["host_user_id"]) and integer(value["player_count"], 1, 15))


def leaderboard(value):
    require(isinstance(value, list) and 1 <= len(value) <= 16)
    for e in value:
        exact(e, {"rank", "user_id", "name", "score", "active"})
        require(integer(e["rank"], 1, 16) and identifier(e["user_id"]) and name(e["name"]))
        require(integer(e["score"], 0, 50000) and type(e["active"]) is bool)
    unique([e["user_id"] for e in value])
    require(value == sorted(value, key=lambda e: (-e["score"], e["user_id"])))
    for e in value:
        require(e["rank"] == 1 + sum(x["score"] > e["score"] for x in value))


def options(value):
    exact(value, {"A", "B", "C", "D"})
    require(all(string(v, 1, 200) for v in value.values()))


def validate_server_message(message):
    """Validate full server schema; connection correlation is the client's responsibility."""
    exact(message, COMMON)
    require(type(message["version"]) is int and message["version"] == VERSION)
    kind, p = message["type"], message["payload"]
    require(isinstance(kind, str) and isinstance(p, dict))
    require(message["session_id"] is None or session_id(message["session_id"]))
    if kind == "ERROR_RESPONSE":
        require(message["event_id"] is None)
        require(message["request_id"] is None or sequence(message["request_id"], "req") is not None)
        exact(p, {"code", "message"})
        require(isinstance(p["code"], str) and p["code"] in ERRORS and string(p["message"], 1, 200, printable=True))
        return
    events = {"PLAYER_STATUS_EVENT", "ROOM_CLOSED_EVENT", "QUIZ_STARTED_EVENT",
              "QUESTION_EVENT", "QUESTION_RESULT_EVENT", "QUIZ_FINISHED_EVENT"}
    if kind in events:
        require(message["request_id"] is None and sequence(message["event_id"], "evt") is not None)
    else:
        require(kind in {x + "_RESPONSE" for x in OPERATIONS})
        require(message["event_id"] is None and sequence(message["request_id"], "req") is not None)
    require(session_id(message["session_id"]) or kind == "LOGOUT_RESPONSE")
    if kind in ("LOGIN_RESPONSE", "LOGOUT_RESPONSE"):
        exact(p, set())
    elif kind == "LIST_QUIZZES_RESPONSE":
        exact(p, {"quizzes"})
        array(p["quizzes"], 1, 20, quiz)
        unique([q["quiz_id"] for q in p["quizzes"]])
    elif kind == "LIST_ROOMS_RESPONSE":
        exact(p, {"rooms"})
        array(p["rooms"], 0, 32, summary)
        codes = [r["room_code"] for r in p["rooms"]]
        unique(codes)
        require(codes == sorted(codes))
    elif kind in {x + "_RESPONSE" for x in ("CREATE_ROOM", "JOIN_ROOM", "PLAYER_LIST", "READY", "START_QUIZ")} or kind == "PLAYER_STATUS_EVENT":
        exact(p, {"room"})
        room(p["room"])
    elif kind == "ANSWER_RESPONSE":
        exact(p, {"room_code", "question_id", "accepted"})
        require(room_code(p["room_code"]) and identifier(p["question_id"]) and p["accepted"] is True)
    elif kind in ("LEAVE_ROOM_RESPONSE", "ROOM_CLOSED_EVENT"):
        exact(p, {"room_code"} if kind == "LEAVE_ROOM_RESPONSE" else {"room_code", "reason"})
        require(room_code(p["room_code"]))
        if kind == "ROOM_CLOSED_EVENT":
            require(p["reason"] == "SERVER_SHUTDOWN")
    elif kind == "LEADERBOARD_RESPONSE":
        exact(p, {"room_code", "leaderboard"})
        require(room_code(p["room_code"]))
        leaderboard(p["leaderboard"])
    elif kind == "QUIZ_STARTED_EVENT":
        exact(p, {"room_code", "quiz_id", "title", "total_questions"})
        require(room_code(p["room_code"]) and identifier(p["quiz_id"]))
        require(string(p["title"], 1, 100, printable=True, nonblank=True))
        require(integer(p["total_questions"], 5, 50))
    elif kind == "QUESTION_EVENT":
        exact(p, {"room_code", "question_id", "number", "total_questions", "text", "options", "time_limit", "points"})
        require(room_code(p["room_code"]) and identifier(p["question_id"]))
        require(integer(p["total_questions"], 5, 50) and integer(p["number"], 1, p["total_questions"]))
        require(string(p["text"], 1, 500, printable=True, nonblank=True))
        options(p["options"])
        require(integer(p["time_limit"], 5, 60) and integer(p["points"], 1, 1000))
    elif kind == "QUESTION_RESULT_EVENT":
        exact(p, {"room_code", "question_id", "correct_answer", "your_answer", "points_awarded", "leaderboard"})
        require(room_code(p["room_code"]) and identifier(p["question_id"]))
        require(p["correct_answer"] in ("A", "B", "C", "D"))
        require(p["your_answer"] in (None, "A", "B", "C", "D"))
        require(integer(p["points_awarded"], 0, 1000))
        require(p["points_awarded"] == 0 or p["your_answer"] == p["correct_answer"])
        leaderboard(p["leaderboard"])
    elif kind == "QUIZ_FINISHED_EVENT":
        exact(p, {"room_code", "quiz_id", "final_leaderboard", "winner"})
        require(room_code(p["room_code"]) and identifier(p["quiz_id"]))
        leaderboard(p["final_leaderboard"])
        require(p["winner"] == [e["user_id"] for e in p["final_leaderboard"] if e["rank"] == 1])
