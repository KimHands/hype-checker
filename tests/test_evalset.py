import unittest
from pathlib import Path

from hype_checker.evalset import GoldsetError, load_goldset, validate

GOLDSET = Path(__file__).resolve().parent.parent / "eval" / "goldset.yaml"


def minimal_case(**overrides):
    case = {
        "id": "x", "group": "A-reel", "as_of": "2026-10-01",
        "input": {"type": "text"},
        "expected": {
            "entities": [{"name": "T", "kind": "repo", "role": "primary",
                          "identifier": None, "identifier_status": "unannotated"}],
            "verdict": {"a": "버림", "b": "불필요"},
        },
    }
    case.update(overrides)
    return case


class LoadGoldsetTest(unittest.TestCase):
    def test_real_goldset_is_valid(self):
        cases = load_goldset(GOLDSET)
        self.assertEqual(len(cases), 18)
        self.assertEqual({c["group"] for c in cases}, {"A-reel", "B-github", "C-youtube", "D-control"})


class ValidateTest(unittest.TestCase):
    def test_accepts_minimal_case(self):
        validate([minimal_case()])

    def test_rejects_duplicate_ids(self):
        with self.assertRaises(GoldsetError):
            validate([minimal_case(), minimal_case()])

    def test_rejects_unknown_verdict(self):
        case = minimal_case()
        case["expected"]["verdict"]["b"] = "보류"
        with self.assertRaisesRegex(GoldsetError, "verdict"):
            validate([case])

    def test_annotated_requires_identifier(self):
        case = minimal_case()
        case["expected"]["entities"][0]["identifier_status"] = "annotated"
        with self.assertRaisesRegex(GoldsetError, "identifier"):
            validate([case])

    def test_rejects_unknown_identifier_status(self):
        case = minimal_case()
        case["expected"]["entities"][0]["identifier_status"] = "maybe"
        with self.assertRaises(GoldsetError):
            validate([case])


if __name__ == "__main__":
    unittest.main()
