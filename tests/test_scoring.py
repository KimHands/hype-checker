import unittest

from hype_checker.scoring import names_match, score_case, summarize


def gold_entity(name, role="primary", identifier=None, status="unannotated", aliases=()):
    return {"name": name, "aliases": list(aliases), "role": role, "kind": "repo",
            "identifier": identifier, "identifier_status": status}


def case(group, entities, input_type="mp4"):
    return {"id": "c", "group": group, "input": {"type": input_type},
            "expected": {"entities": entities, "verdict": {"a": "버림", "b": "불필요"}}}


def predicted(name, latin=None, role="primary", identifier=None, status="resolved", top=None, origin="transcript"):
    candidates = [{"identifier": top or identifier}] if (top or identifier) else []
    return {"name": name, "name_latin": latin or name, "role": role, "origin": origin, "identifier": identifier, "kind": "repo",
            "resolution": {"status": status, "candidates": candidates}}


class NamesMatchTest(unittest.TestCase):
    def test_matches(self):
        self.assertTrue(names_match(gold_entity("Ruflo", aliases=["claude-flow"]), predicted("루플로", "Ruflo")))
        self.assertTrue(names_match(gold_entity("Liner Search MCP"), predicted("Liner")))
        self.assertTrue(names_match(gold_entity("Claude Figma 커넥터"), predicted("Figma 커넥터", "Figma")))
        self.assertTrue(names_match(gold_entity("Liner Search MCP"), predicted("Liner Web Search MCP")))
        self.assertTrue(names_match(gold_entity("InspoAI"), predicted("Inspo AI MCP")))
        self.assertFalse(names_match(gold_entity("REA"), predicted("Ruflo")))
        self.assertFalse(names_match(gold_entity("Mobbin"), predicted("Moblin MCP")))
        self.assertFalse(names_match(gold_entity("Buzz"), predicted("Bu")))


class ScoreCaseTest(unittest.TestCase):
    def test_extraction_and_resolution(self):
        gold = case("A-reel", [gold_entity("Ruflo", identifier="ruvnet/ruflo", status="annotated")])
        bundle = {"entities": [predicted("루플로", "Ruflo", identifier="ruvnet/ruflo"),
                               predicted("Claude Code", role="mentioned", status="skipped_mentioned")]}
        score = score_case(gold, bundle)
        self.assertEqual(score["extraction"], {"gold_primary": 1, "found": 1, "found_strict": 1, "predicted_primary": 1,
                                               "predicted_primary_matched": 1, "role_correct": 1})
        self.assertEqual(score["identification"], {"annotated": 1, "correct": 1})
        self.assertEqual(score["resolution"], {"annotated": 1, "top1_correct": 1, "resolved": 1, "resolved_correct": 1,
                                               "resolved_unmatched": 0})

    def test_counts_resolved_claims_outside_gold(self):
        gold = case("A-reel", [gold_entity("Mobbin", identifier="https://api.mobbin.com/mcp", status="annotated")])
        bundle = {"entities": [predicted("Moblin MCP", identifier="eerimoq/moblin")]}
        self.assertEqual(score_case(gold, bundle)["resolution"]["resolved_unmatched"], 1)

    def test_mcp_identifiers_compare_normalized(self):
        gold = case("A-reel", [gold_entity("Mobbin", identifier="https://api.mobbin.com/mcp", status="annotated")])
        bundle = {"entities": [predicted("Mobbin", identifier="https://api.mobbin.com/mcp/")]}
        self.assertEqual(score_case(gold, bundle)["resolution"]["top1_correct"], 1)

    def test_unannotated_is_excluded_from_resolution_but_queued(self):
        gold = case("A-reel", [gold_entity("graphify")])
        bundle = {"entities": [predicted("graphify", status="ambiguous", top="Graphify-Labs/graphify")]}
        score = score_case(gold, bundle)
        self.assertEqual(score["resolution"]["annotated"], 0)
        self.assertEqual(score["review_queue"], [{"gold": "graphify", "top": "Graphify-Labs/graphify", "status": "ambiguous"}])

    def test_control_with_invented_primary_hurts_precision(self):
        gold = case("D-control", [])
        bundle = {"entities": [predicted("TemuSeller")]}
        score = score_case(gold, bundle)
        self.assertEqual((score["extraction"]["predicted_primary"], score["extraction"]["predicted_primary_matched"]), (1, 0))

    def test_url_group_scores_normalization(self):
        gold = case("B-github", [gold_entity("kordoc", identifier="chrisryugj/kordoc", status="annotated")], input_type="url")
        bundle = {"entities": [predicted("kordoc", identifier="chrisryugj/kordoc", status="from_url", origin="url")]}
        score = score_case(gold, bundle)
        self.assertEqual(score["url_normalization"], {"cases": 1, "correct": 1})
        self.assertNotIn("extraction", score)


class SummarizeTest(unittest.TestCase):
    def test_per_group_rates_and_skips(self):
        results = [
            {"group": "A-reel", "status": "scored", "score": {"extraction": {"gold_primary": 2, "found": 1, "predicted_primary": 2,
             "predicted_primary_matched": 1, "role_correct": 1}, "resolution": {"annotated": 1, "top1_correct": 1, "resolved": 1, "resolved_correct": 1, "resolved_unmatched": 0}}},
            {"group": "C-youtube", "status": "skipped", "reason": "download not allowed"},
        ]
        summary = summarize(results)
        self.assertEqual(summary["A-reel"]["extraction_recall"], 0.5)
        self.assertEqual(summary["A-reel"]["extraction_precision"], 0.5)
        self.assertEqual(summary["C-youtube"]["skipped"], 1)


if __name__ == "__main__":
    unittest.main()


class Round2ReviewTest(unittest.TestCase):
    """Cases from the Codex implementation review (2026-10-24)."""

    def test_dropped_predictions_never_score(self):
        gold = case("A-reel", [gold_entity("Ruflo", identifier="ruvnet/ruflo", status="annotated")])
        dropped = {**predicted("Ruflo", identifier="ruvnet/ruflo"), "role": "dropped"}
        score = score_case(gold, {"entities": [dropped]})
        self.assertEqual(score["extraction"]["found"], 0)
        self.assertEqual(score["identification"]["correct"], 0)

    def test_one_prediction_matches_at_most_one_gold(self):
        gold = case("A-reel", [gold_entity(n) for n in ("Mobbin", "Nicelydone", "Refero")])
        score = score_case(gold, {"entities": [predicted("Mobbin Nicelydone Refero")]})
        self.assertEqual(score["extraction"]["found"], 1)

    def test_strict_extraction_requires_kind_and_role(self):
        gold = case("A-reel", [{**gold_entity("graphify"), "kind": "repo"}])
        score = score_case(gold, {"entities": [{**predicted("Graphify"), "kind": "plugin"}]})
        self.assertEqual((score["extraction"]["found"], score["extraction"]["found_strict"]), (1, 0))

    def test_identification_denominator_is_fixed(self):
        gold = case("A-reel", [gold_entity("Ruflo", identifier="ruvnet/ruflo", status="annotated"),
                               gold_entity("Liner", identifier="https://platform.liner.com/api/v1/mcp", status="annotated")])
        score = score_case(gold, {"entities": [predicted("Ruflo", identifier="ruvnet/ruflo")]})
        self.assertEqual(score["identification"], {"annotated": 2, "correct": 1})

    def test_unverifiable_counts_like_none_exists(self):
        gold = case("A-reel", [{**gold_entity("Nicelydone", status="unverifiable"), "kind": "mcp"}])
        score = score_case(gold, {"entities": [predicted("Nicelydone", identifier="FilipMichalek/NicelyDone")]})
        self.assertEqual(score["none_exists"], {"matched": 1, "wrongly_resolved": 1})
        self.assertEqual(score["identification"], {"annotated": 0, "correct": 0})

    def test_product_wrongly_resolved_to_a_repo_counts(self):
        gold = case("C-youtube", [{**gold_entity("Aside", status="none_exists"), "kind": "product"}])
        score = score_case(gold, {"entities": [predicted("Aside", identifier="someone/aside")]})
        self.assertEqual(score["none_exists"], {"matched": 1, "wrongly_resolved": 1})
