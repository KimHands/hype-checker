import datetime
import tempfile
import unittest
from pathlib import Path

from hype_checker.verdictset import VerdictsetError, load_verdictset, validate

VERDICTSET = Path(__file__).resolve().parent.parent / "eval" / "verdict-goldset.yaml"


def tool(**overrides):
    base = {"name": "T", "identifier": None, "grade": "🤔", "why": "reason", "b": None}
    base.update(overrides)
    return base


def case(case_id="c1", **overrides):
    base = {
        "id": case_id, "split": "dev", "kind": "general",
        "input": {"type": "text", "value": "caption"},
        "as_of": "2026-11-01", "tools": [tool()], "source_card": "card",
    }
    base.update(overrides)
    return base


def adversarial(count):
    return [case(f"a{i}", kind="adversarial", source_card=None) for i in range(count)]


class LoadVerdictsetTest(unittest.TestCase):
    def test_loads_repo_file(self):
        cases = load_verdictset(VERDICTSET)
        adversarial_cases = [c for c in cases if c["kind"] == "adversarial"]
        self.assertGreaterEqual(len(adversarial_cases), 15)
        self.assertTrue({c["split"] for c in cases} == {"dev", "holdout"})


class ValidateTest(unittest.TestCase):
    def test_accepts_minimal_valid_set(self):
        validate(adversarial(15) + [case()])

    def test_rejects_unknown_grade(self):
        bad = case(tools=[tool(grade="✅")])
        with self.assertRaises(VerdictsetError):
            validate(adversarial(15) + [bad])

    def test_rejects_unknown_b(self):
        bad = case(tools=[tool(b="보류")])
        with self.assertRaises(VerdictsetError):
            validate(adversarial(15) + [bad])

    def test_accepts_null_b(self):
        validate(adversarial(15) + [case(tools=[tool(b=None)])])

    def test_requires_adversarial_quota(self):
        with self.assertRaises(VerdictsetError):
            validate(adversarial(14) + [case()])

    def test_ids_unique(self):
        with self.assertRaises(VerdictsetError):
            validate(adversarial(15) + [case("dup"), case("dup")])

    def test_holdout_ids_unique(self):
        dupes = [case("h", split="holdout"), case("h", split="holdout")]
        with self.assertRaises(VerdictsetError):
            validate(adversarial(15) + dupes)

    def test_rejects_bad_split_and_kind(self):
        for field, value in (("split", "test"), ("kind", "other")):
            with self.assertRaises(VerdictsetError):
                validate(adversarial(15) + [case(**{field: value})])

    def test_general_case_needs_source_card(self):
        with self.assertRaises(VerdictsetError):
            validate(adversarial(15) + [case(source_card=None)])

    def test_rejects_missing_why(self):
        with self.assertRaises(VerdictsetError):
            validate(adversarial(15) + [case(tools=[tool(why="")])])

    def test_rejects_bad_input_type(self):
        with self.assertRaises(VerdictsetError):
            validate(adversarial(15) + [case(input={"type": "mp4", "value": "x"})])

    def test_rejects_non_dict_input(self):
        with self.assertRaises(VerdictsetError):
            validate(adversarial(15) + [case(input="caption")])

    def test_rejects_non_dict_tool(self):
        with self.assertRaises(VerdictsetError):
            validate(adversarial(15) + [case(tools=["T"])])

    def test_rejects_missing_or_bad_as_of(self):
        missing = case()
        del missing["as_of"]
        for bad in (missing, case(as_of="11/01/2026"), case(as_of=None)):
            with self.assertRaises(VerdictsetError):
                validate(adversarial(15) + [bad])

    def test_accepts_yaml_date_and_iso_string_as_of(self):
        dated = case(as_of=datetime.date(2026, 11, 1))
        validate(adversarial(15) + [dated, case("c2", as_of="2026-11-01")])

    def test_unhashable_values_raise_verdictset_error(self):
        bad_cases = {
            "grade": case(tools=[tool(grade=["👍"])]),
            "b": case(tools=[tool(b={"x": 1})]),
            "split": case(split=["dev"]),
            "kind": case(kind={"a": 1}),
            "input.type": case(input={"type": ["text"], "value": "x"}),
            "id": case(case_id=["c1"]),
        }
        for field, bad in bad_cases.items():
            with self.subTest(field=field):
                with self.assertRaises(VerdictsetError):
                    validate(adversarial(15) + [bad])

    def test_rejects_impossible_calendar_dates(self):
        for bad_date in ("2026-13-45", "2026-02-30", "20261101"):
            with self.subTest(as_of=bad_date):
                with self.assertRaises(VerdictsetError):
                    validate(adversarial(15) + [case(as_of=bad_date)])


class LoadErrorsTest(unittest.TestCase):
    def load_text(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v.yaml"
            path.write_text(text, encoding="utf-8")
            return load_verdictset(path)

    def test_rejects_missing_cases_key(self):
        with self.assertRaises(VerdictsetError):
            self.load_text("other: []\n")

    def test_rejects_non_mapping_top_level(self):
        for text in ("- a\n- b\n", "just a string\n", ""):
            with self.assertRaises(VerdictsetError):
                self.load_text(text)

    def test_rejects_non_list_cases(self):
        with self.assertRaises(VerdictsetError):
            self.load_text("cases: 3\n")


if __name__ == "__main__":
    unittest.main()
