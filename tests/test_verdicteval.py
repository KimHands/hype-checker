import unittest
from unittest import mock

from hype_checker import verdicteval
from hype_checker.llm import ClaudeUnavailable
from hype_checker.verdicteval import PASS, passes, run_verdict_eval, score_verdicts


def gold_case(case_id, grades, kind="general"):
    tools = [{"name": f"{case_id}-t{i}", "identifier": f"o/{case_id}{i}", "grade": g, "why": "x", "b": None}
             for i, g in enumerate(grades)]
    return {"id": case_id, "kind": kind, "split": "dev", "tools": tools}


def result_for(case, predicted, cost=0.01):
    tools = [{"name": t["name"], "identifier": t["identifier"], "grade": g}
             for t, g in zip(case["tools"], predicted) if g is not None]
    return {"id": case["id"], "verdict": {"tools": tools, "repro": {"cost_usd": cost}}}


class ScoreVerdictsTest(unittest.TestCase):
    def test_fatal_error_counted(self):
        case = gold_case("a", ["👎"])
        summary = score_verdicts([case], [result_for(case, ["👍"])])
        self.assertEqual(summary["fatal_errors"], 1)
        self.assertEqual(summary["confirmed_precision"], 0.0)
        self.assertFalse(passes(summary))

    def test_hold_rate_counts_only_tools_that_should_be_judged(self):
        """A correct ❔ (gold ❔) is not an unwarranted hold (design-v3 §7, amended 2026-11-01)."""
        case = gold_case("a", ["👍", "👍", "🤔", "👎", "❔", "❔"])
        summary = score_verdicts([case], [result_for(case, ["❔", "👍", "🤔", "👎", "❔", "❔"])])
        self.assertEqual(summary["hold_rate"], 0.25)

    def test_confirmed_precision_counts_caution_as_ok(self):
        case = gold_case("a", ["🤔", "👍"])
        summary = score_verdicts([case], [result_for(case, ["👍", "👍"])])
        self.assertEqual(summary["confirmed_precision"], 1.0)
        self.assertEqual(summary["fatal_errors"], 0)

    def test_missed_tool_counts_as_hold(self):
        case = gold_case("a", ["👍", "👍"])
        summary = score_verdicts([case], [result_for(case, ["👍", None])])
        self.assertEqual(summary["hold_rate"], 0.5)

    def test_missing_result_holds_everything(self):
        case = gold_case("a", ["👍"])
        summary = score_verdicts([case], [])
        self.assertEqual(summary["hold_rate"], 1.0)

    def test_match_by_name_when_identifier_missing(self):
        case = gold_case("a", ["👎"])
        case["tools"][0]["identifier"] = None
        pred = {"id": "a", "verdict": {"tools": [{"name": "A-T0", "identifier": None, "grade": "👍"}],
                                       "repro": {"cost_usd": 0.0}}}
        self.assertEqual(score_verdicts([case], [pred])["fatal_errors"], 1)

    def test_against_false_positive(self):
        case = gold_case("a", ["👍", "🤔", "👎"])
        summary = score_verdicts([case], [result_for(case, ["👎", "👎", "👎"])])
        self.assertEqual(summary["against_false_positive"], 2)

    def test_adversarial_cap(self):
        bad = gold_case("x", ["👎"], "adversarial")
        ok = gold_case("y", ["👎"], "adversarial")
        summary = score_verdicts([bad, ok], [result_for(bad, ["👍"]), result_for(ok, ["🤔"])])
        self.assertEqual(summary["adversarial_within_cap"], 0.5)
        flipped = gold_case("z", ["👍"], "adversarial")
        self.assertEqual(score_verdicts([flipped], [result_for(flipped, ["👎"])])["adversarial_within_cap"], 0.0)

    def test_cost_sum_and_none(self):
        a, b = gold_case("a", ["👍"]), gold_case("b", ["👍"])
        self.assertAlmostEqual(score_verdicts([a, b], [result_for(a, ["👍"], 0.01), result_for(b, ["👍"], 0.02)])["cost_usd"], 0.03)
        self.assertIsNone(score_verdicts([a, b], [result_for(a, ["👍"], None), result_for(b, ["👍"], 0.02)])["cost_usd"])


class PassesTest(unittest.TestCase):
    base = {"scored_tools": 10, "gold_confirmed": 3, "predicted_confirmed": 3, "confirmed_precision": 0.95, "fatal_errors": 0, "against_false_positive": 1,
            "hold_rate": 0.4, "adversarial_within_cap": 1.0}

    def test_pass_constants(self):
        self.assertEqual(PASS, {"confirmed_precision": 0.90, "fatal_errors": 0,
                                "against_false_positive": 1, "hold_rate": 0.40})

    def test_boundaries(self):
        self.assertTrue(passes(self.base))
        for key, value in [("confirmed_precision", 0.89), ("fatal_errors", 1), ("against_false_positive", 2),
                           ("hold_rate", 0.41), ("adversarial_within_cap", 0.9)]:
            self.assertFalse(passes({**self.base, key: value}), key)

    def test_empty_split_does_not_pass(self):
        summary = score_verdicts([], [])
        self.assertEqual(summary["scored_tools"], 0)
        self.assertFalse(passes(summary))

    def test_all_caution_with_gold_confirmed_does_not_pass(self):
        case = gold_case("a", ["👍", "👍", "🤔"])
        summary = score_verdicts([case], [result_for(case, ["🤔", "🤔", "🤔"])])
        self.assertEqual((summary["gold_confirmed"], summary["predicted_confirmed"]), (2, 0))
        self.assertFalse(passes(summary))

    def test_no_confirmed_is_pass_only_without_fatal(self):
        quiet = {**self.base, "confirmed_precision": None, "gold_confirmed": 0, "predicted_confirmed": 0}
        self.assertTrue(passes(quiet))
        self.assertFalse(passes({**quiet, "fatal_errors": 1}))


class RunnerTest(unittest.TestCase):
    def test_runs_split_with_fakes(self):
        case = gold_case("a", ["👍"])
        other = {**gold_case("b", ["👍"]), "split": "holdout"}
        seen = []

        def fake_check(raw, ctx, source=None, tag=""):
            seen.append(raw)
            return "/nonexistent"

        def fake_judge(bundle, sources, chat, budget, now, model=None):
            return {"tools": [{"name": "a-t0", "identifier": "o/a0", "grade": "👍"}], "repro": {"cost_usd": 0.01}}

        case["input"] = {"type": "text", "value": "hello"}
        ctx = mock.Mock(sources="S", now=1.0)
        with mock.patch.object(verdicteval, "load_verdictset", return_value=[case, other]), \
             mock.patch.object(verdicteval, "_load_bundle", return_value={}):
            results, summary = run_verdict_eval("p", ctx, "chat", "dev", "m", check_fn=fake_check, judge_fn=fake_judge)
        self.assertEqual(seen, ["hello"])
        self.assertEqual([r["id"] for r in results], ["a"])
        self.assertEqual(summary["confirmed_precision"], 1.0)

    def test_claude_unavailable_propagates(self):
        case = {**gold_case("a", ["👍"]), "input": {"type": "text", "value": "hi"}}

        def down(*args, **kwargs):
            raise ClaudeUnavailable("outage")

        with mock.patch.object(verdicteval, "load_verdictset", return_value=[case]), \
             mock.patch.object(verdicteval, "_load_bundle", return_value={}):
            with self.assertRaises(ClaudeUnavailable):
                run_verdict_eval("p", mock.Mock(now=1.0), "chat", "dev", "m",
                                 check_fn=lambda *a, **k: "d", judge_fn=down)


if __name__ == "__main__":
    unittest.main()
