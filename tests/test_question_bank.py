import copy
import json
import tempfile
import unittest
from pathlib import Path

from protocol import ProtocolError
from question_bank import MAX_FILE, load_bank, validate_bank
from tests.support import ROOT, bank


class QuestionBankTests(unittest.TestCase):
    def test_submitted_bank_and_single_quiz_hidden_bank(self):
        self.assertGreaterEqual(len(load_bank(ROOT / "questions.json")), 2)
        self.assertEqual(len(validate_bank(bank((5,)))), 1)

    def test_order_and_extra_members_are_preserved(self):
        value = bank((7, 5))
        value["quizzes"][0]["extra"] = {"valid": True}
        value["quizzes"][0]["questions"][0]["extra"] = []
        quizzes = validate_bank(value)
        self.assertEqual([len(q["questions"]) for q in quizzes], [7, 5])
        self.assertEqual(quizzes[0]["questions"][0]["question_id"], "Q0_0")

    def test_every_required_field_is_required(self):
        base = bank()
        paths = [(), ("quizzes", 0), ("quizzes", 0, "questions", 0)]
        for path in paths:
            obj = base
            for key in path:
                obj = obj[key]
            for field in obj:
                if field == "future":
                    continue
                value = copy.deepcopy(base)
                target = value
                for key in path:
                    target = target[key]
                del target[field]
                with self.subTest(path=path, field=field), self.assertRaises(ProtocolError):
                    validate_bank(value)

    def test_boundary_rejections(self):
        cases = {"quiz_id": ["", "x" * 33, "中文", None, True],
                 "title": ["", " " * 3, "x" * 101, "x\n", 1],
                 "description": ["x" * 301, "\x1b", None]}
        for key, values in cases.items():
            for bad in values:
                value = bank()
                value["quizzes"][0][key] = bad
                with self.subTest(key=key, bad=bad), self.assertRaises(ProtocolError):
                    validate_bank(value)
        cases = {"question_id": ["", "q" * 33, "a b", False],
                 "text": ["", "\t", "x" * 501, None],
                 "answer": ["a", "AB", 1, []],
                 "time_limit": [4, 61, True, 5.0, "5"],
                 "points": [0, 1001, True, 1.0, None],
                 "options": [{"A": "a"}, {k: "" for k in "ABCD"},
                             {k: "x" for k in "ABCDE"}, {k: True for k in "ABCD"}]}
        for key, values in cases.items():
            for bad in values:
                value = bank()
                value["quizzes"][0]["questions"][0][key] = bad
                with self.subTest(key=key, bad=bad), self.assertRaises(ProtocolError):
                    validate_bank(value)

    def test_count_version_and_duplicate_ids(self):
        for version in (True, 1.0, 0, "1", None):
            with self.assertRaises(ProtocolError):
                validate_bank(dict(bank(), schema_version=version))
        for counts in ((), (4,), (51,), (5,) * 21):
            with self.assertRaises(ProtocolError):
                validate_bank(bank(counts))
        value = bank()
        value["quizzes"][1]["quiz_id"] = value["quizzes"][0]["quiz_id"]
        with self.assertRaises(ProtocolError):
            validate_bank(value)
        value = bank()
        value["quizzes"][1]["questions"][0]["question_id"] = "Q0_0"
        with self.assertRaises(ProtocolError):
            validate_bank(value)

    def test_boundary_values_and_control_options(self):
        value = bank((50,) * 20, limit=60)
        q = value["quizzes"][0]
        q.update(title="T" * 100, description=" " * 300)
        q["questions"][0].update(text="x" * 500, points=1000,
                                 options={key: "\x1b\n" * 100 for key in "ABCD"})
        self.assertEqual(len(validate_bank(value)), 20)

    def test_file_errors_and_byte_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bank.json"
            for raw in (b'', b'[]', b'{}', b'{', b'\xff', b' ' * (MAX_FILE + 1)):
                path.write_bytes(raw)
                with self.subTest(size=len(raw)), self.assertRaises(ProtocolError):
                    load_bank(path)
            raw = json.dumps(bank((5,))).encode()
            path.write_bytes(raw + b' ' * (MAX_FILE - len(raw)))
            self.assertEqual(len(load_bank(path)), 1)
            with self.assertRaises(ProtocolError):
                load_bank(Path(directory) / "missing")
