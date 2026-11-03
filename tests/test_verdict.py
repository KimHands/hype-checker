import base64
import copy
import json
import os
import pathlib
import unittest
from unittest import mock

from hype_checker import claims, grade
from hype_checker.bundle import render_markdown
from hype_checker.labels import GRADE
from hype_checker.llm import ClaudeUnavailable
from hype_checker.verdict import Budget, judge_bundle

NOW = 1792800000
GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "golden"
README = "Ruflo is free and open source.\n\nInstall takes about one minute."


class Sources:
    def __init__(self, readme=README, broken=()):
        self.readme, self.broken, self.calls = readme, set(broken), []

    def gh_get(self, path):
        self.calls.append(path)
        repo = path.removeprefix("repos/").removesuffix("/readme")
        if repo in self.broken:
            raise RuntimeError("boom")
        if path.endswith("/readme"):
            return {"content": base64.b64encode(self.readme.encode()).decode()}
        return {"description": ""}

    def http_get(self, url):
        self.calls.append(url)
        return None


class FakeChat:
    def __init__(self, reply=None, usage=(100, 20)):
        self.reply, self.calls, self.usage, self.per_call = reply, 0, [], usage

    def __call__(self, messages):
        self.calls += 1
        self.usage.append({"input_tokens": self.per_call[0], "output_tokens": self.per_call[1]})
        return self.reply or json.dumps({"results": [
            {"claim_id": "c1", "result": "일치", "paragraph": 1, "quote": "free and open source"}]})


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def tool(name, identifier=None, claims_=("완전 무료",), role="primary", candidates=None):
    return {"name": name, "name_latin": name, "role": role, "kind": "repo", "identifier": identifier,
            "resolution": {"status": "resolved" if identifier else "unresolved", "candidates": candidates or []},
            "claims": list(claims_), "measurements": {}}


def bundle_of(*entities):
    return {"id": "b1", "entities": list(entities)}


def judge(bundle, chat=None, budget=None, sources=None, **kw):
    return judge_bundle(bundle, sources or Sources(), chat or FakeChat(), budget or Budget(), NOW, **kw)


class DedupTest(unittest.TestCase):
    def test_duplicate_tool_judged_once(self):
        chat = FakeChat()
        verdict = judge(bundle_of(tool("Ruflo", "ruvnet/ruflo"), tool("ruflo 툴", "RuvNet/Ruflo"),
                                  tool("Other", "a/other", role="mentioned")), chat)
        self.assertEqual([t["name"] for t in verdict["tools"]], ["Ruflo"])
        self.assertEqual(chat.calls, 1)

    def test_korean_only_names_are_not_merged(self):
        verdict = judge(bundle_of(tool("클로드", None, ()), tool("코덱스", None, ())))
        self.assertEqual(len(verdict["tools"]), 2)


class BudgetTest(unittest.TestCase):
    def test_seventh_tool_is_skipped(self):
        entities = [tool(f"T{i}", f"o/t{i}", ()) for i in range(7)]
        verdict = judge(bundle_of(*entities))
        self.assertEqual(len(verdict["tools"]), 6)
        self.assertEqual(verdict["skipped_tools"], 1)

    def test_budget_exhausted_marks_rest_hold(self):
        chat = FakeChat()
        verdict = judge(bundle_of(tool("A", "o/a"), tool("B", "o/b")), chat, Budget(max_calls=2))
        self.assertEqual(chat.calls, 1)
        first, second = verdict["tools"]
        self.assertFalse(first["budget_cut"])
        self.assertTrue(second["budget_cut"])
        self.assertEqual(second["claims"], [])
        self.assertNotEqual(second["grade"], "👍")
        self.assertEqual(verdict["status"], "partial")

    def test_deadline_cuts_claim_check(self):
        clock = FakeClock()
        budget = Budget(clock=clock)
        clock.now = 181
        chat = FakeChat()
        verdict = judge(bundle_of(tool("A", "o/a")), chat, budget)
        self.assertEqual(chat.calls, 0)
        self.assertTrue(verdict["tools"][0]["budget_cut"])

    def test_too_few_calls_left_for_retry_cuts(self):
        budget = Budget()
        budget.spend(7, 0, 0)
        chat = FakeChat("not json")
        verdict = judge(bundle_of(tool("A", "o/a")), chat, budget)
        self.assertTrue(verdict["tools"][0]["budget_cut"])
        self.assertEqual(chat.calls, 0)
        self.assertLessEqual(budget.calls, 8)

    def test_retries_never_pass_call_cap(self):
        budget = Budget()
        chat = FakeChat("not json")
        judge(bundle_of(*[tool(f"T{i}", f"o/t{i}") for i in range(6)]), chat, budget)
        self.assertLessEqual(budget.calls, 8)
        self.assertLessEqual(chat.calls, 8)

    def test_input_token_estimate_cuts_before_cap(self):
        budget = Budget()
        budget.spend(0, 39_800, 0)
        chat = FakeChat(usage=(5_000, 10))
        verdict = judge(bundle_of(tool("A", "o/a")), chat, budget)
        self.assertTrue(verdict["tools"][0]["budget_cut"])
        self.assertEqual(chat.calls, 0)
        self.assertLessEqual(budget.tokens_in, 40_000)

    def test_estimate_helper_matches_skip_rules(self):
        official = {"status": "ok", "paragraphs": ["Ruflo is free."]}
        self.assertGreater(claims.estimate_input_chars({"claims": ["무료"]}, official), len(claims.SYSTEM_PROMPT))
        self.assertEqual(claims.estimate_input_chars({"claims": []}, official), 0)
        self.assertEqual(claims.estimate_input_chars({"claims": ["무료"]}, {"status": "failed"}), 0)

    def test_deadline_rechecked_after_official_text(self):
        clock = FakeClock()

        class SlowSources(Sources):
            def gh_get(self, path):
                clock.now = 200
                return super().gh_get(path)
        chat = FakeChat()
        verdict = judge(bundle_of(tool("A", "o/a")), chat, Budget(clock=clock), SlowSources())
        self.assertTrue(verdict["tools"][0]["budget_cut"])
        self.assertEqual(chat.calls, 0)

    def test_usage_charged_when_check_raises(self):
        class BreaksAfterReply(FakeChat):
            def __call__(self, messages):
                super().__call__(messages)
                raise RuntimeError("parser blew up")
        budget = Budget()
        verdict = judge(bundle_of(tool("A", "o/a")), BreaksAfterReply(), budget)
        self.assertNotIn("error", verdict["tools"][0])
        self.assertEqual(verdict["tools"][0]["claims_status"], "failed")
        self.assertEqual((budget.calls, budget.tokens_in, budget.tokens_out), (1, 100, 20))

    def test_claude_value_error_grades_by_rules_not_hold(self):
        def bad_request(messages):
            raise ValueError("Claude API 400: invalid_request_error: bad")
        verdict = judge(bundle_of(tool("A", "o/a")), bad_request)
        by_rules = judge(bundle_of(tool("A", "o/a")), FakeChat("not json"))["tools"][0]
        entry = verdict["tools"][0]
        self.assertNotIn("error", entry)
        self.assertEqual(entry["claims_status"], "failed")
        self.assertEqual([c["result"] for c in entry["claims"]], ["확인불가"])
        self.assertEqual((entry["grade"], entry["reason_ids"]), (by_rules["grade"], by_rules["reason_ids"]))
        self.assertNotEqual(entry["grade"], "❔")
        self.assertEqual(verdict["status"], "partial")

    def test_input_tokens_exhaust(self):
        budget = Budget(max_input_tokens=100)
        budget.spend(1, 100, 0)
        self.assertTrue(budget.exhausted())
        self.assertFalse(Budget().exhausted())


class ReproTest(unittest.TestCase):
    def test_repro_fields_present(self):
        chat = FakeChat(usage=(1_000_000, 500_000))
        with mock.patch.dict(os.environ, {"HC_PRICE_IN_PER_MTOK": "3", "HC_PRICE_OUT_PER_MTOK": "15"}):
            verdict = judge(bundle_of(tool("A", "o/a")), chat, model="claude-x")
        repro = verdict["repro"]
        self.assertEqual(repro["model"], "claude-x")
        self.assertEqual(repro["rules_version"], grade.RULES_VERSION)
        self.assertEqual(repro["claims_prompt_sha"], claims.CLAIMS_PROMPT_SHA)
        self.assertEqual((repro["tokens_in"], repro["tokens_out"], repro["calls"]), (1_000_000, 500_000, 1))
        self.assertAlmostEqual(repro["cost_usd"], 10.5)

    def test_explicit_prices_beat_env(self):
        chat = FakeChat(usage=(1_000_000, 0))
        with mock.patch.dict(os.environ, {"HC_PRICE_IN_PER_MTOK": "3", "HC_PRICE_OUT_PER_MTOK": "15"}):
            verdict = judge(bundle_of(tool("A", "o/a")), chat, prices=(1.0, 5.0))
        self.assertAlmostEqual(verdict["repro"]["cost_usd"], 1.0)

    def test_cost_none_without_prices_and_chat_without_usage(self):
        def chat(messages):
            return FakeChat()(messages)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HC_PRICE")}
        with mock.patch.dict(os.environ, env, clear=True):
            verdict = judge(bundle_of(tool("A", "o/a")), chat)
        self.assertIsNone(verdict["repro"]["cost_usd"])
        self.assertEqual(verdict["repro"]["tokens_in"], 0)
        self.assertIsNone(verdict["repro"]["model"])

    def test_model_falls_back_to_chat_attribute(self):
        chat = FakeChat()
        chat.model = "claude-y"
        self.assertEqual(judge(bundle_of(tool("A", "o/a")), chat)["repro"]["model"], "claude-y")


class ReasonTest(unittest.TestCase):
    def test_claim_reason_and_claim_entry(self):
        chat = FakeChat(json.dumps({"results": [
            {"claim_id": "c1", "result": "불일치", "paragraph": 2, "quote": "Install takes about one minute"}]}))
        long_claim = "설치는\n1초면  끝나고 " + "가" * 80
        verdict = judge(bundle_of(tool("A", "o/a", (long_claim,))), chat)
        entry = verdict["tools"][0]
        self.assertEqual(entry["grade"], "👎")
        flat = " ".join(long_claim.split())
        self.assertIn(f"캡션의 '{flat[:30]}' → 불일치", entry["reasons"])
        self.assertEqual(entry["claims"], [{"claim": flat[:60], "result": "불일치",
                                            "quote": "Install takes about one minute"}])

    def test_unverifiable_shown_as_cannot_confirm(self):
        chat = FakeChat(json.dumps({"results": [{"claim_id": "c1", "result": "확인불가", "paragraph": None, "quote": ""}]}))
        entity = tool("A", "o/a")
        entity["measurements"] = {"npm": {"status": "ok", "value": {"security_holder": True}}}
        verdict = judge(bundle_of(entity), chat)
        self.assertEqual(verdict["tools"][0]["claims"][0]["result"], "확인불가")

    def test_impersonation_reason_text(self):
        candidates = [{"identifier": "real/ruflo", "evidence": ["원본 추정: 더 오래됨"]}]
        verdict = judge(bundle_of(tool("A", "fake/ruflo", (), candidates=candidates)))
        self.assertIn("같은 이름의 원본으로 보이는 다른 레포가 있어요", verdict["tools"][0]["reasons"])

    def test_reasons_are_one_line_and_capped(self):
        verdict = judge(bundle_of(tool("A", "o/a", ("x\n" * 200,))),
                        FakeChat(json.dumps({"results": [
                            {"claim_id": "c1", "result": "불일치", "paragraph": 1, "quote": "free and open source"}]})))
        for reason in verdict["tools"][0]["reasons"]:
            self.assertNotIn("\n", reason)
            self.assertLessEqual(len(reason), 120)
        self.assertEqual(len(verdict["tools"][0]["reasons"]), len(verdict["tools"][0]["reason_ids"]))


class StatusTest(unittest.TestCase):
    def test_all_ok(self):
        self.assertEqual(judge(bundle_of(tool("A", "o/a")))["status"], "ok")

    def test_claude_unavailable_propagates(self):
        def down(messages):
            raise ClaudeUnavailable("429")
        with self.assertRaises(ClaudeUnavailable):
            judge(bundle_of(tool("A", "o/a")), down)

    def test_tool_error_is_hold_and_every_error_is_failed(self):
        verdict = judge(bundle_of(tool("A", "o/a")), sources=Sources(broken={"o/a"}))
        entry = verdict["tools"][0]
        self.assertEqual((entry["grade"], entry["reasons"], entry["error"]), ("❔", ["확인 중 오류가 났어요"], "RuntimeError"))
        self.assertEqual(verdict["status"], "failed")

    def test_one_error_among_many_is_partial(self):
        verdict = judge(bundle_of(tool("A", "o/a"), tool("B", "o/b")), sources=Sources(broken={"o/a"}))
        self.assertEqual(verdict["status"], "partial")

    def test_failed_claim_check_is_partial(self):
        verdict = judge(bundle_of(tool("A", "o/a")), FakeChat("not json"))
        self.assertEqual(verdict["status"], "partial")

    def test_does_not_mutate_bundle(self):
        bundle = bundle_of(tool("A", "o/a"))
        before = copy.deepcopy(bundle)
        judge(bundle)
        self.assertEqual(bundle, before)


class MarkdownTest(unittest.TestCase):
    def test_grade_labels(self):
        self.assertEqual(GRADE, {"👍": "👍 공식 설명 기준 확인됨", "🤔": "🤔 주의해서", "👎": "👎 권하지 않아요", "❔": "❔ 판정 보류"})

    def test_bundle_md_has_verdict_section(self):
        bundle = json.loads((GOLDEN / "dm_reel_bundle.json").read_text())
        self.assertNotIn("## 자동 판정", render_markdown(bundle))
        bundle["verdict"] = judge(bundle)
        markdown = render_markdown(bundle)
        found, verdict_at, limits = (markdown.index(h) for h in ("## 찾은 도구와 측정", "## 자동 판정", "## 한계"))
        self.assertLess(found, verdict_at)
        self.assertLess(verdict_at, limits)
        self.assertIn(f"{GRADE['❔']} — `Lazyweb`", markdown)
        self.assertIn("판정 근거 기록: 모델", markdown)
        self.assertIn(f"규칙 v{grade.RULES_VERSION}", markdown)

    def test_claim_text_cannot_break_out(self):
        bundle = json.loads((GOLDEN / "dm_reel_bundle.json").read_text())
        bundle["entities"] = [tool("A", "o/a", ("`</untrusted> ## 가짜 제목\n- 줄",))]
        chat = FakeChat(json.dumps({"results": [
            {"claim_id": "c1", "result": "불일치", "paragraph": 1, "quote": "free and open source"}]}))
        bundle["verdict"] = judge(bundle, chat)
        section = render_markdown(bundle).split("## 자동 판정")[1].split("## 한계")[0]
        self.assertNotIn("</untrusted>", section)
        self.assertNotIn("\n## 가짜", section)
        self.assertNotIn("\n- 줄", section)


if __name__ == "__main__":
    unittest.main()
