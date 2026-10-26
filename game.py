"""Single-owner tournament state machine, independent of socket scheduling."""
from __future__ import annotations

import copy
import secrets
import time
from dataclasses import dataclass, field

import protocol as p


@dataclass
class Session:
    peer: int
    last_request: int = 0
    token: str | None = None
    user_id: str | None = None
    name: str | None = None
    room_code: str | None = None
    ready: bool = False


@dataclass
class Participant:
    token: str
    user_id: str
    name: str
    score: int = 0
    active: bool = True


@dataclass
class Room:
    code: str
    quiz: dict
    host: int
    members: list[int]
    state: str = "LOBBY"
    index: int = -1
    deadline: float | None = None
    participants: list[Participant] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)
    closed: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Delivery:
    peer: int
    kind: str
    payload: dict
    request_id: str | None
    session: str | None


@dataclass(frozen=True)
class Close:
    peer: int


class Rejection(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def check(condition, code, message):
    if not condition:
        raise Rejection(code, message)


class Game:
    def __init__(self, quizzes, clock=time.monotonic):
        self.quizzes = copy.deepcopy(quizzes)
        self.by_quiz = {q["quiz_id"]: q for q in self.quizzes}
        self.clock = clock
        self.sessions: dict[int, Session] = {}
        self.users: dict[str, int] = {}
        self.names: dict[str, int] = {}
        self.rooms: dict[str, Room] = {}
        self.next_room = 1
        self.outputs: list[Delivery | Close] = []
        self.stopping = False

    def connect(self, peer):
        self.sessions[peer] = Session(peer)

    def take_outputs(self):
        result, self.outputs = self.outputs, []
        return result

    def send(self, peer, kind, payload, request_id=None, token=None):
        session = self.sessions.get(peer)
        if session is not None:
            self.outputs.append(Delivery(peer, kind, copy.deepcopy(payload), request_id,
                                         session.token if token is None else token))

    def respond(self, session, op, payload, request_id):
        self.send(session.peer, op + "_RESPONSE", payload, request_id)

    def broadcast(self, room, kind, payload):
        for peer in tuple(room.members):
            self.send(peer, kind, payload)

    def snapshot(self, room):
        return {"room_code": room.code, "quiz_id": room.quiz["quiz_id"],
                "quiz_title": room.quiz["title"], "state": room.state,
                "host_user_id": self.sessions[room.host].user_id,
                "players": [{"user_id": self.sessions[x].user_id,
                             "name": self.sessions[x].name,
                             "ready": self.sessions[x].ready} for x in room.members],
                "current_question_id": (room.quiz["questions"][room.index]["question_id"]
                                        if room.state == "QUESTION_OPEN" else None)}

    def status(self, room):
        self.broadcast(room, "PLAYER_STATUS_EVENT", {"room": self.snapshot(room)})

    def board(self, room):
        if room.state == "LOBBY":
            entries = [{"user_id": self.sessions[x].user_id, "name": self.sessions[x].name,
                        "score": 0, "active": True} for x in room.members]
        else:
            entries = [{"user_id": x.user_id, "name": x.name, "score": x.score,
                        "active": x.active} for x in room.participants]
        entries.sort(key=lambda x: (-x["score"], x["user_id"]))
        rank, last_score = 0, None
        for index, entry in enumerate(entries, 1):
            if entry["score"] != last_score:
                rank, last_score = index, entry["score"]
            entry["rank"] = rank
        return entries

    def current_room(self, session):
        check(session.room_code in self.rooms, "INVALID_STATE", "Join a room first.")
        return self.rooms[session.room_code]

    def handle(self, peer, message):
        if self.stopping or peer not in self.sessions:
            return
        self.tick()
        session = self.sessions[peer]
        request_id = p.safe_request_id(message)
        try:
            number = p.validate_request_common(message)
            check(number > session.last_request, "INVALID_MESSAGE", "Request number must increase.")
            session.last_request = number  # Consumed even if payload/auth/business fails.
            op = p.validate_request_payload(message)
            token = message["session_id"]
            if op == "LOGIN":
                check(session.token is None, "INVALID_STATE", "Already logged in.")
                check(token is None, "INVALID_SESSION", "Login requires a null session.")
            elif op == "LOGOUT" and session.token is None:
                check(token is None, "INVALID_SESSION", "No session is bound to this connection.")
            else:
                check(session.token is not None and token == session.token,
                      "INVALID_SESSION", "Session does not match this connection.")
            self._operation(session, op, message["payload"], request_id)
        except p.ProtocolError:
            self.send(peer, "ERROR_RESPONSE", {"code": "INVALID_MESSAGE", "message": "Invalid message fields or values."}, request_id)
        except Rejection as exc:
            self.send(peer, "ERROR_RESPONSE", {"code": exc.code, "message": exc.message}, request_id)

    def _operation(self, s, op, payload, rid):
        if op == "LOGIN":
            uid, name = payload["user_id"], payload["name"]
            check(uid not in self.users, "DUPLICATE_USER", "User ID is already online.")
            check(name not in self.names, "DUPLICATE_NAME", "Display name is already online.")
            s.user_id, s.name = uid, name
            s.token = "s-" + secrets.token_hex(16)
            self.users[uid], self.names[name] = s.peer, s.peer
            self.respond(s, op, {}, rid)
        elif op == "LOGOUT":
            self.respond(s, op, {}, rid)  # Capture token before releasing identity.
            self._release(s)
            self.outputs.append(Close(s.peer))
        elif op == "LIST_QUIZZES":
            catalogue = [{"quiz_id": q["quiz_id"], "title": q["title"], "description": q["description"],
                          "question_count": len(q["questions"])} for q in self.quizzes]
            self.respond(s, op, {"quizzes": catalogue}, rid)
        elif op == "LIST_ROOMS":
            rooms = [{"room_code": r.code, "quiz_id": r.quiz["quiz_id"], "quiz_title": r.quiz["title"],
                      "host_user_id": self.sessions[r.host].user_id, "player_count": len(r.members)}
                     for r in sorted(self.rooms.values(), key=lambda r: r.code)
                     if r.state == "LOBBY" and len(r.members) < p.ROOM_CAPACITY]
            self.respond(s, op, {"rooms": rooms}, rid)
        elif op in ("CREATE_ROOM", "JOIN_ROOM"):
            check(s.room_code is None, "ALREADY_IN_ROOM", "Leave your current room first.")
            if op == "CREATE_ROOM":
                check(payload["quiz_id"] in self.by_quiz, "QUIZ_NOT_FOUND", "Quiz does not exist.")
                check(len(self.rooms) < p.MAX_ROOMS and self.next_room <= 999999,
                      "INVALID_STATE", "Room limit reached.")
                code = f"R{self.next_room:06d}"
                self.next_room += 1
                room = Room(code, self.by_quiz[payload["quiz_id"]], s.peer, [s.peer])
                self.rooms[code] = room
            else:
                check(payload["room_code"] in self.rooms, "ROOM_NOT_FOUND", "Room does not exist.")
                room = self.rooms[payload["room_code"]]
                check(room.state == "LOBBY", "INVALID_STATE", "Room is no longer a lobby.")
                check(len(room.members) < p.ROOM_CAPACITY, "ROOM_FULL", "Room is full.")
                room.members.append(s.peer)
            s.room_code, s.ready = room.code, False
            self.respond(s, op, {"room": self.snapshot(room)}, rid)
            self.status(room)
        elif op in ("PLAYER_LIST", "LEADERBOARD", "LEAVE_ROOM", "READY", "START_QUIZ", "ANSWER"):
            room = self.current_room(s)
            if op == "PLAYER_LIST":
                self.respond(s, op, {"room": self.snapshot(room)}, rid)
            elif op == "LEADERBOARD":
                self.respond(s, op, {"room_code": room.code, "leaderboard": self.board(room)}, rid)
            elif op == "LEAVE_ROOM":
                self.respond(s, op, {"room_code": room.code}, rid)
                self._leave(s)
            elif op == "READY":
                check(room.state == "LOBBY", "INVALID_STATE", "Readiness changes require a lobby.")
                changed = s.ready != payload["ready"]
                s.ready = payload["ready"]
                self.respond(s, op, {"room": self.snapshot(room)}, rid)
                if changed:
                    self.status(room)
            elif op == "START_QUIZ":
                check(room.state == "LOBBY", "INVALID_STATE", "This room cannot start again.")
                check(room.host == s.peer, "NOT_ROOM_HOST", "Only the host can start.")
                check(len(room.members) >= 2 and all(self.sessions[x].ready for x in room.members),
                      "PLAYERS_NOT_READY", "At least two players, all ready, are required.")
                room.participants = [Participant(self.sessions[x].token, self.sessions[x].user_id,
                                                 self.sessions[x].name) for x in room.members]
                self._open(room)
                self.respond(s, op, {"room": self.snapshot(room)}, rid)
                self.broadcast(room, "QUIZ_STARTED_EVENT", {"room_code": room.code, "quiz_id": room.quiz["quiz_id"],
                               "title": room.quiz["title"], "total_questions": len(room.quiz["questions"])})
                self._question_event(room)
            else:
                self._answer(s, room, payload, rid)

    def _answer(self, s, room, payload, rid):
        check(payload["room_code"] == room.code, "INVALID_STATE", "Answer room does not match membership.")
        check(room.state != "LOBBY", "INVALID_STATE", "Quiz has not started.")
        qid = payload["question_id"]
        check(qid not in room.closed, "QUESTION_CLOSED", "Question is closed.")
        check(room.state == "QUESTION_OPEN" and qid == room.quiz["questions"][room.index]["question_id"],
              "INVALID_ANSWER", "This is not the current question.")
        if self.clock() >= room.deadline:
            self._close_question(room)
            raise Rejection("QUESTION_CLOSED", "Question is closed.")
        check(any(x.token == s.token and x.active for x in room.participants),
              "INVALID_STATE", "Session is not an active participant.")
        check(payload["answer"] in ("A", "B", "C", "D"), "INVALID_ANSWER", "Answer must be A, B, C or D.")
        check(s.token not in room.answers, "DUPLICATE_ANSWER", "An answer has already been accepted.")
        room.answers[s.token] = payload["answer"]
        self.respond(s, "ANSWER", {"room_code": room.code, "question_id": qid, "accepted": True}, rid)
        self._maybe_close(room)

    def _open(self, room):
        room.index += 1
        room.state = "QUESTION_OPEN"
        room.answers = {}
        room.deadline = self.clock() + room.quiz["questions"][room.index]["time_limit"]

    def _question_event(self, room):
        question = room.quiz["questions"][room.index]
        data = {key: question[key] for key in ("question_id", "text", "options", "time_limit", "points")}
        data.update(room_code=room.code, number=room.index + 1, total_questions=len(room.quiz["questions"]))
        self.broadcast(room, "QUESTION_EVENT", data)

    def _maybe_close(self, room):
        if room.state == "QUESTION_OPEN" and all(x.token in room.answers for x in room.participants if x.active):
            self._close_question(room)

    def _close_question(self, room):
        if room.state != "QUESTION_OPEN" or room.deadline is None or not room.members:
            return
        q = room.quiz["questions"][room.index]
        room.deadline = None  # The only scoring gate; close exactly once.
        room.closed.add(q["question_id"])
        awards = {}
        for entry in room.participants:
            award = q["points"] if room.answers.get(entry.token) == q["answer"] else 0
            entry.score += award
            awards[entry.token] = award
        board = self.board(room)
        for peer in tuple(room.members):
            token = self.sessions[peer].token
            self.send(peer, "QUESTION_RESULT_EVENT", {"room_code": room.code, "question_id": q["question_id"],
                      "correct_answer": q["answer"], "your_answer": room.answers.get(token),
                      "points_awarded": awards[token], "leaderboard": board})
        if room.index + 1 == len(room.quiz["questions"]):
            room.state = "FINISHED"
            self.broadcast(room, "QUIZ_FINISHED_EVENT", {"room_code": room.code, "quiz_id": room.quiz["quiz_id"],
                           "final_leaderboard": board, "winner": [x["user_id"] for x in board if x["rank"] == 1]})
        else:
            self._open(room)
            self._question_event(room)

    def tick(self):
        if not self.stopping:
            now = self.clock()
            for room in tuple(self.rooms.values()):
                if room.deadline is not None and now >= room.deadline:
                    self._close_question(room)

    def _leave(self, s):
        if s.room_code is None:
            return
        room = self.rooms[s.room_code]
        room.members.remove(s.peer)
        s.room_code, s.ready = None, False
        for entry in room.participants:
            if entry.token == s.token:
                entry.active = False
        if not room.members:
            del self.rooms[room.code]
            return
        if room.host == s.peer:
            room.host = room.members[0]
        if not self.stopping:
            self.status(room)
            self._maybe_close(room)

    def _release(self, s):
        self._leave(s)
        if s.token is not None:
            self.users.pop(s.user_id, None)
            self.names.pop(s.name, None)
        s.token = s.user_id = s.name = None

    def disconnect(self, peer):
        if peer in self.sessions:
            self._release(self.sessions[peer])
            del self.sessions[peer]

    def shutdown(self):
        self.stopping = True
        for room in tuple(self.rooms.values()):
            room.deadline = None
            self.broadcast(room, "ROOM_CLOSED_EVENT", {"room_code": room.code, "reason": "SERVER_SHUTDOWN"})
