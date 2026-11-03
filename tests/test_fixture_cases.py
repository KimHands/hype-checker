"""Synthetic adversarial cases (live: false) checked with simulated registry data instead of live lookups."""

import unittest
from pathlib import Path

from hype_checker.grade import grade_tool
from hype_checker.verdictset import load_verdictset
from tests.test_facts import build, kordoc_entity, npm_value

GOLDSET = Path(__file__).resolve().parent.parent / "eval" / "verdict-goldset.yaml"
DAY = 86400


def gold(case_id):
    case = next(c for c in load_verdictset(GOLDSET) if c["id"] == case_id)
    assert case.get("live") is False, case_id
    return case["tools"][0]["grade"]


def security_holder():
    entity = kordoc_entity()
    entity["measurements"]["npm"]["value"] = {**npm_value(), "security_holder": True}
    return entity


def impersonating_copy():
    entity = kordoc_entity()
    entity["identifier"] = "copycat/kordoc"
    entity["resolution"] = {"status": "resolved", "candidates": [
        {"identifier": "copycat/kordoc", "score": 3, "evidence": ["github name match 3"]},
        {"identifier": "chrisryugj/kordoc", "score": 3, "evidence": ["원본 추정: 같은 이름 중 가장 오래됐고 스타가 10배 이상"]}]}
    return entity


def archived_and_stale():
    entity = kordoc_entity()
    meta = entity["measurements"]["github"]["meta"]["value"]
    meta.update(archived=True, pushed_at="2024-01-01T00:00:00Z")
    return entity


SIMULATED = {
    "a01-npm-security-holder-a": security_holder,
    "a02-npm-security-holder-b": security_holder,
    "a03-impersonation-a": impersonating_copy,
    "a04-impersonation-b": impersonating_copy,
    "a05-archived-latest-a": archived_and_stale,
    "a06-archived-latest-b": archived_and_stale,
}


class FixtureCasesTest(unittest.TestCase):
    def test_every_non_live_case_has_a_simulation(self):
        non_live = {c["id"] for c in load_verdictset(GOLDSET) if c.get("live") is False}
        self.assertEqual(non_live, set(SIMULATED))

    def test_simulated_grade_matches_gold(self):
        for case_id, make in SIMULATED.items():
            with self.subTest(case=case_id):
                entity = make()
                self.assertEqual(grade_tool(entity, build(entity), [])["grade"], gold(case_id))


if __name__ == "__main__":
    unittest.main()
