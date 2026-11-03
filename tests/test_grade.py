import unittest

from hype_checker.grade import RULES_VERSION, grade_tool

OK_CHECKS = {"located": True, "measured": True, "active_1y": True, "no_warning": True}
OK_FACTS = [
    {"id": "identity.located", "kind": "identity", "text": "a"},
    {"id": "activity.pushed", "kind": "activity", "text": "b"},
    {"id": "trust.stars", "kind": "trust", "text": "c"},
    {"id": "trust.license", "kind": "trust", "text": "d"},
]
MATCH = [{"claim_id": "c1", "result": "일치", "paragraph": 1, "quote": "q"}]


def entity(identifier="a/b", status="resolved", candidates=None):
    return {"identifier": identifier,
            "resolution": {"status": status, "candidates": candidates or []}}


def built(checks=None, facts=None):
    return {"facts": facts if facts is not None else OK_FACTS, "checks": {**OK_CHECKS, **(checks or {})}}


class GradeTest(unittest.TestCase):
    def test_rules_version(self):
        self.assertEqual(RULES_VERSION, "4")

    def test_not_located_is_hold(self):
        facts = built({"located": False}, [{"id": "identity.not_located", "kind": "missing", "text": "x"}])
        result = grade_tool(entity(None, "ambiguous"), facts, MATCH)
        self.assertEqual(result["grade"], "❔")
        self.assertIn("located", result["blocked_by"])

    def test_security_holder_is_against(self):
        facts = built({"no_warning": False, "measured": True},
                      OK_FACTS + [{"id": "warning.security_holder", "kind": "warning", "text": "w"}])
        result = grade_tool(entity(), facts, MATCH)
        self.assertEqual(result["grade"], "👎")
        self.assertIn("warning.security_holder", result["reason_ids"])

    def test_contradicted_claim_is_against(self):
        claims = MATCH + [{"claim_id": "c2", "result": "불일치", "paragraph": 2, "quote": "q"}]
        result = grade_tool(entity(), built(), claims)
        self.assertEqual(result["grade"], "👎")
        self.assertIn("claim.c2", result["reason_ids"])

    def test_impersonating_copy_is_against(self):
        candidates = [{"identifier": "real/tool", "evidence": ["원본 추정: 같은 이름 중 가장 오래됨"]},
                      {"identifier": "fake/tool", "evidence": []}]
        result = grade_tool(entity("fake/tool", candidates=candidates), built(), MATCH)
        self.assertEqual(result["grade"], "👎")

    def test_original_itself_is_not_impersonation(self):
        candidates = [{"identifier": "Real/Tool", "evidence": ["원본 추정: x"]}]
        result = grade_tool(entity("real/tool", candidates=candidates), built(), MATCH)
        self.assertEqual(result["grade"], "👍")

    def test_unmeasured_caps_at_caution(self):
        facts = built({"measured": False, "active_1y": False},
                      [{"id": "identity.located", "kind": "identity", "text": "a"},
                       {"id": "measure.remote", "kind": "missing", "text": "m"}])
        claims = MATCH + [{"claim_id": "c2", "result": "일치", "paragraph": 2, "quote": "q"}]
        result = grade_tool(entity("https://x.example/mcp"), facts, claims)
        self.assertEqual(result["grade"], "🤔")
        self.assertIn("measured", result["blocked_by"])
        self.assertIn("measure.remote", result["reason_ids"])

    def test_full_evidence_is_confirmed(self):
        result = grade_tool(entity(), built(), MATCH)
        self.assertEqual(result["grade"], "👍")
        self.assertEqual(result["blocked_by"], [])

    def test_warning_is_caution_not_against(self):
        facts = built({"no_warning": False}, OK_FACTS + [{"id": "warning.advisories", "kind": "warning", "text": "w"}])
        result = grade_tool(entity(), facts, MATCH)
        self.assertEqual(result["grade"], "🤔")
        self.assertEqual(result["blocked_by"], ["no_warning"])
        self.assertEqual(result["reason_ids"][0], "warning.advisories")

    def test_no_matching_claim_blocks_confirmed(self):
        """The post claimed something (entity has claims) but nothing was confirmed: unchecked or 확인불가 → 🤔."""
        claimed = {**entity(), "claims": ["무료 월 15회"]}
        for claims in ([{"claim_id": "c1", "result": "확인불가", "paragraph": None, "quote": ""}], []):
            result = grade_tool(claimed, built(), claims)
            self.assertEqual(result["grade"], "🤔")
            self.assertEqual(result["blocked_by"], ["claim_match"])

    def test_bare_link_without_claims_can_be_confirmed(self):
        """A shared link has no caption to check (design-v3 §1 ⑤, v3 2026-11-01)."""
        result = grade_tool({**entity(), "origin": "url", "claims": []}, built(), [])
        self.assertEqual(result["grade"], "👍")
        self.assertEqual(result["blocked_by"], [])

    def test_caption_without_extracted_claims_is_not_confirmed(self):
        """Extraction can miss claims a caption makes (holdout Ruflo): no confirmed claim → no 👍."""
        result = grade_tool({**entity(), "origin": "caption", "claims": []}, built(), [])
        self.assertEqual(result["grade"], "🤔")
        self.assertEqual(result["blocked_by"], ["claim_match"])

    def test_reason_ids_two_to_three(self):
        many = OK_FACTS + [{"id": f"trust.x{i}", "kind": "trust", "text": "t"} for i in range(5)]
        cases = [grade_tool(entity(), built(), MATCH),
                 grade_tool(entity(), built(facts=many), MATCH),
                 grade_tool(entity(), built(facts=OK_FACTS[:1]), []),
                 grade_tool(entity(None, "unresolved"), built({"located": False}, OK_FACTS[:1]), [])]
        for result in cases:
            self.assertTrue(1 <= len(result["reason_ids"]) <= 3, result)
        self.assertEqual(len(cases[1]["reason_ids"]), 3)
        self.assertEqual(len(cases[0]["reason_ids"]), 3)


if __name__ == "__main__":
    unittest.main()
