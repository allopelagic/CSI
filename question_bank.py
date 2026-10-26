"""Read and validate the entire server-only input before opening a listener."""
from pathlib import Path

from protocol import ProtocolError, identifier, integer, loads, options, require, string

MAX_FILE = 1_000_000


def validate_bank(root):
    require(isinstance(root, dict), "Question bank must be an object.")
    require(type(root.get("schema_version")) is int and root["schema_version"] == 1,
            "schema_version must be integer 1.")
    quizzes = root.get("quizzes")
    require(isinstance(quizzes, list) and 1 <= len(quizzes) <= 20, "quizzes must contain 1-20 items.")
    quiz_ids, question_ids = set(), set()
    for qi, q in enumerate(quizzes):
        where = f"quizzes[{qi}]"
        require(isinstance(q, dict), f"{where} must be an object.")
        require(identifier(q.get("quiz_id")), f"{where}.quiz_id is invalid.")
        require(q["quiz_id"] not in quiz_ids, f"{where}.quiz_id is duplicated.")
        quiz_ids.add(q["quiz_id"])
        for key, low, high, nonblank in (("title", 1, 100, True), ("description", 0, 300, False)):
            require(string(q.get(key), low, high, printable=True, nonblank=nonblank), f"{where}.{key} is invalid.")
        questions = q.get("questions")
        require(isinstance(questions, list) and 5 <= len(questions) <= 50, f"{where}.questions must contain 5-50 items.")
        for ni, item in enumerate(questions):
            here = f"{where}.questions[{ni}]"
            require(isinstance(item, dict), f"{here} must be an object.")
            require(identifier(item.get("question_id")), f"{here}.question_id is invalid.")
            require(item["question_id"] not in question_ids, f"{here}.question_id is duplicated.")
            question_ids.add(item["question_id"])
            require(string(item.get("text"), 1, 500, printable=True, nonblank=True), f"{here}.text is invalid.")
            try:
                options(item.get("options"))
            except ProtocolError as exc:
                raise ProtocolError(f"{here}.options must contain exactly A/B/C/D with 1-200 character values.") from exc
            require(item.get("answer") in ("A", "B", "C", "D"), f"{here}.answer is invalid.")
            require(integer(item.get("time_limit"), 5, 60), f"{here}.time_limit must be integer 5-60.")
            require(integer(item.get("points"), 1, 1000), f"{here}.points must be integer 1-1000.")
    return quizzes


def load_bank(path):
    try:
        with Path(path).open("rb") as source:
            raw = source.read(MAX_FILE + 1)
        require(1 <= len(raw) <= MAX_FILE, "Question bank must contain 1-1000000 bytes.")
        return validate_bank(loads(raw))
    except OSError as exc:
        raise ProtocolError(f"Cannot read question bank ({exc.__class__.__name__}).") from exc
