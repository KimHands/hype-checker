import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from hype_checker.alternatives import find_alternatives
from hype_checker.score import score_tool
from hype_checker.v2.inbox import Inbox
from hype_checker.v2.limits import Limits
from hype_checker.v2.store import Store
from hype_checker.v2.webhook import parse_shares
from hype_checker.v2.worker import Runtime, process_pending
from tests.test_pipeline import RUFLO_GH, RUFLO_HTTP, make_context
from tests.test_v2 import CONFIG, NOW_S, fixture

NOW = 1792800000
RECENT = "2026-10-20T00:00:00Z"


class ScoreTest(unittest.TestCase):

    def test_unverified_or_unmeasured_tool_has_no_score(self):
        self.assertEqual(score_tool("❔", [], stars=5000), (None, []))
        self.assertEqual(score_tool("🤔", []), (None, []))

    def test_popular_active_tool_scores_high_whatever_the_post_claimed(self):
        self.assertEqual(score_tool("👍", [], stars=60000, pushed_days=5)[0], 9)
        self.assertEqual(score_tool("🤔", [], stars=60000, pushed_days=5)[0], 9)
        self.assertEqual(score_tool("👎", ["claim.c1"], stars=60000, pushed_days=5)[0], 9)

    def test_same_warning_counted_once_and_claims_do_not_move_the_score(self):
        plain = score_tool("🤔", ["warning.install_scripts", "warning.install_scripts"], stars=4000, pushed_days=100)[0]
        contradicted = score_tool("👎", ["warning.install_scripts", "claim.c1"], stars=4000, pushed_days=100)[0]
        self.assertEqual(plain, contradicted)
        self.assertEqual(plain, 5)

    def test_tool_red_flag_caps_at_three(self):
        score, parts = score_tool("👎", ["warning.security_holder"], stars=60000, pushed_days=5)
        self.assertEqual(score, 3)
        self.assertIn((0, "red_flag"), parts)


    def test_warnings_cap_at_two_points(self):
        ids = ["warning.repo_mismatch", "warning.install_scripts", "warning.advisories"]
        self.assertEqual(score_tool("🤔", ids, stars=4000, pushed_days=100)[0], 4)

    def test_stays_within_zero_to_ten(self):
        self.assertEqual(score_tool("🤔", ["warning.archived", "warning.fork"], stars=10, pushed_days=900)[0], 1)
        self.assertEqual(score_tool("👍", [], stars=90000, pushed_days=1)[0], 9)


def _entity(stars=4000):
    meta = {"stargazers_count": stars, "description": "Let AI agents click desktop apps via accessibility",
            "topics": ["computer-use", "agents"], "pushed_at": RECENT}
    return {"identifier": "vercel-labs/agent-desktop",
            "measurements": {"github": {"meta": {"status": "ok", "value": meta}}}}


def _item(name, stars, pushed=RECENT, topics=("computer-use", "agents")):
    return {"full_name": name, "stargazers_count": stars, "pushed_at": pushed, "description": "d", "topics": list(topics)}


class FakeSearch:
    def __init__(self, items):
        self.items, self.paths = items, []

    def gh_get(self, path, timeout_s=None):
        import base64
        self.paths.append(path)
        if path.endswith("/readme"):
            return {"content": base64.b64encode(b"Agent Desktop lets agents click apps.").decode()}
        if path.startswith("repos/"):
            return {"description": "desktop agent"}
        return {"items": self.items}


class AlternativesTest(unittest.TestCase):
    def test_keeps_only_same_purpose_ids_from_the_list(self):
        sources = FakeSearch([_item("big/desktop-agent", 28000), _item("other/awesome-list", 90000)])
        chat = lambda messages: json.dumps({"same": ["big/desktop-agent", "evil/injected"]})
        self.assertEqual(find_alternatives(_entity(), sources, chat, NOW),
                         [{"identifier": "big/desktop-agent", "stars": 28000}])

    def test_excludes_itself_less_popular_and_stale_repos(self):
        sources = FakeSearch([_item("vercel-labs/agent-desktop", 9000), _item("small/x", 5000),
                              _item("old/y", 50000, pushed="2020-01-01T00:00:00Z")])
        chat = lambda messages: self.fail("no candidate should reach Claude")
        self.assertEqual(find_alternatives(_entity(), sources, chat, NOW), [])

    def test_search_uses_topics_and_star_floor(self):
        sources = FakeSearch([])
        find_alternatives(_entity(stars=4000), sources, lambda m: "{}", NOW)
        self.assertIn("topic%3Acomputer-use", sources.paths[0])
        self.assertFalse(any("topic%3Aagents" in path for path in sources.paths))
        self.assertIn("stars%3A%3E8000", sources.paths[0])

    def test_unmeasured_repo_is_unknown_and_remote_mcp_has_none(self):
        from hype_checker.alternatives import SearchIncomplete
        with self.assertRaises(SearchIncomplete):
            find_alternatives({"identifier": "a/b"}, FakeSearch([]), lambda m: "{}", NOW)
        self.assertEqual(find_alternatives({"identifier": "https://mcp.x/mcp"}, FakeSearch([]), lambda m: "{}", NOW), [])


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "hc.db")

    def test_private_file(self):
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_reel_lasts_seven_days(self):
        self.store.reel_put("k", ["a", "b"], "case", NOW)
        self.assertEqual(self.store.reel_get("k", NOW + 6 * 86400), (["a", "b"], NOW))
        self.assertIsNone(self.store.reel_get("k", NOW + 8 * 86400))

    def test_tool_lasts_three_days_case_insensitive(self):
        self.store.tool_put("Vercel-Labs/Agent-Desktop", {"official": 1}, NOW)
        self.assertEqual(self.store.tool_get("vercel-labs/agent-desktop", NOW + 2 * 86400), {"official": 1})
        self.assertIsNone(self.store.tool_get("vercel-labs/agent-desktop", NOW + 4 * 86400))

    def test_mcp_url_case_is_kept_github_case_is_folded(self):
        self.store.tool_put("https://mcp.example.com/TenantA", {"official": "A"}, NOW)
        self.assertIsNone(self.store.tool_get("https://mcp.example.com/tenanta", NOW))
        self.assertEqual(self.store.tool_get("https://MCP.example.com/TenantA", NOW), {"official": "A"})
        self.store.tool_put("Owner/Repo", {"official": "R"}, NOW)
        self.assertEqual(self.store.tool_get("owner/repo", NOW), {"official": "R"})

    def test_ipv6_host_case_folds_and_port_stays(self):
        from hype_checker.v2.store import tool_key
        self.assertEqual(tool_key("https://[2001:DB8::A]:8443/mcp"), "https://[2001:db8::a]:8443/mcp")
        self.assertEqual(tool_key("https://[2001:DB8::A]/mcp"), tool_key("https://[2001:db8::a]/mcp"))

    def test_url_without_path_keeps_query_case(self):
        from hype_checker.v2.store import tool_key
        self.assertNotEqual(tool_key("https://mcp.example.com?Tenant=A"), tool_key("https://mcp.example.com?Tenant=a"))
        self.assertEqual(tool_key("HTTPS://MCP.Example.com?Tenant=A"), "https://mcp.example.com?Tenant=A")
        self.assertEqual(tool_key("https://User@Host.com:8443/P"), "https://User@host.com:8443/P")

    def test_underscore_in_a_name_never_matches_another_repo(self):
        self.store.tool_put("owner/toolXname", {"official": "OTHER REPO README"}, NOW)
        self.assertIsNone(self.store.tool_get("owner/tool_name", NOW))
        self.store.tool_put("owner/a%b", {"official": "x"}, NOW)
        self.assertIsNone(self.store.tool_get("owner/a", NOW))

    def test_each_part_keeps_its_own_age(self):
        self.store.tool_put("a/b", {"official": {"status": "ok"}}, NOW)
        self.store.tool_put("a/b", {"alternatives": []}, NOW + 2 * 86400)
        self.assertEqual(self.store.tool_get("a/b", NOW + 4 * 86400), {"alternatives": []})


class WorkerSafetyTest(unittest.TestCase):
    def setUp(self):
        self.inbox = Inbox(Path(tempfile.mkdtemp()))
        self.store = Store(self.inbox.root / "hc.db")
        self.runtime = Runtime(limits=Limits(self.inbox.root / "limits"), sleep=lambda s: None, store=self.store)
        self.inbox.enqueue(parse_shares(fixture("ig_reel.json"), CONFIG, NOW_S)[0])

    def test_crash_still_tells_the_sender(self):
        from unittest import mock
        with mock.patch("hype_checker.v2.worker.check", side_effect=RuntimeError("boom")):
            [record] = process_pending(self.inbox, make_context(RUFLO_GH, RUFLO_HTTP), CONFIG, now_s=NOW_S,
                                       runtime=self.runtime)
        self.assertEqual([m["text"] for m in record["messages"]], ["지금은 처리하지 못했어요. 잠시 뒤 다시 보내 주세요."])

    def test_lookup_failure_during_the_item_is_not_cached(self):
        from unittest import mock
        ctx = make_context(RUFLO_GH, RUFLO_HTTP)
        original = ctx.sources.gh_get

        def flaky(path, timeout_s=None):
            ctx.sources.errors.append(f"{path}: failed")
            return original(path)
        ctx.sources.gh_get = flaky
        with mock.patch("hype_checker.v2.worker._measured_fully", return_value=True):
            process_pending(self.inbox, ctx, CONFIG, now_s=NOW_S, runtime=self.runtime)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM reels").fetchone()[0], 0)


class MeasuredFullyTest(unittest.TestCase):
    def test_partly_failed_measurement_is_not_cached(self):
        from hype_checker.v2.worker import _measured_fully
        self.assertFalse(_measured_fully({"github": {"meta": {"status": "ok"}, "star_history": {"status": "failed"}}}))
        self.assertTrue(_measured_fully({"github": {"meta": {"status": "ok"}}, "npm": {"status": "ok"}}))


class ReelCacheTest(unittest.TestCase):
    """The Ruflo fixture lacks star history/contributors/releases; these tests treat it as fully measured."""

    def setUp(self):
        from unittest import mock
        patcher = mock.patch("hype_checker.v2.worker._measured_fully", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.inbox = Inbox(Path(tempfile.mkdtemp()))
        self.store = Store(self.inbox.root / "hc.db")
        self.runtime = Runtime(limits=Limits(self.inbox.root / "limits"), sleep=lambda s: None, store=self.store)

    def share(self, mid, caption=None, media_id=True):
        payload = fixture("ig_reel.json")
        message = payload["entry"][0]["messaging"][0]["message"]
        message["mid"] = mid
        if caption is not None:
            message["attachments"][0]["payload"]["title"] = caption
        if not media_id:
            message["attachments"][0]["payload"].pop("reel_video_id")
        self.inbox.enqueue(parse_shares(payload, CONFIG, NOW_S)[0])

    def run_worker(self, ctx=None, now_s=NOW_S):
        return process_pending(self.inbox, ctx or make_context(RUFLO_GH, RUFLO_HTTP), CONFIG, now_s=now_s,
                               runtime=self.runtime)

    def test_same_reel_gets_the_saved_reply_with_a_note_without_measuring(self):
        self.share("m1")
        [first] = self.run_worker()
        self.share("m2")
        ctx = make_context({}, {})
        second = next(r for r in self.run_worker(ctx, NOW_S + 2 * 86400) if r.get("cached"))
        texts = [m["text"] for m in second["messages"]]
        self.assertEqual(texts[0], "같은 게시물을 2일 전에 확인한 결과를 다시 보내 드려요. 스타 수 등은 그때 기준이에요.")
        self.assertEqual(texts[1:], [m["text"] for m in first["messages"]])
        self.assertEqual(ctx.runner.log, [])

    def test_edited_caption_is_measured_again(self):
        self.share("m1")
        self.run_worker()
        self.share("m2", caption="완전히 다른 캡션: Liner Search MCP 유료 플랜")
        self.assertFalse(any(r.get("cached") for r in self.run_worker()))

    def test_share_without_media_id_is_never_cached(self):
        self.share("m1", media_id=False)
        self.run_worker()
        self.share("m2", media_id=False)
        self.assertFalse(any(r.get("cached") for r in self.run_worker()))

    def test_dispute_on_a_cached_reply_forgets_it_too(self):
        from tests.test_v2 import text_share
        self.share("m1")
        self.run_worker()
        self.share("m2")
        self.assertTrue(any(r.get("cached") for r in self.run_worker()))
        self.inbox.enqueue(text_share("d1", "정정")[0])
        self.run_worker()
        self.share("m3")
        records = self.run_worker()
        self.assertFalse(any(r.get("cached") and r["share"]["mid"] == "m3" for r in records))

    def test_dispute_forgets_the_saved_reply(self):
        from tests.test_v2 import text_share
        self.share("m1")
        self.run_worker()
        self.inbox.enqueue(text_share("d1", "정정")[0])
        self.run_worker()
        self.share("m2")
        self.assertFalse(any(r.get("cached") for r in self.run_worker()))

    def test_failed_download_or_screen_read_is_not_cached(self):
        from hype_checker.v2.worker import _complete
        good = {"extraction": {"status": "ok"}, "evidence": {"status": "not_run"}, "dm": {"video": "caption only"}}
        self.assertTrue(_complete(good))
        self.assertFalse(_complete({**good, "dm": {"video": "not fetched (OSError)"}}))
        self.assertFalse(_complete({**good, "evidence": {"status": "ok", "screen_error": "ClaudeUnavailable"}}))

    def test_only_complete_finished_replies_are_cached(self):
        from hype_checker.v2.worker import _reusable
        case = Path(tempfile.mkdtemp())
        good = {"verdict": {"status": "ok", "tools": [{"alternatives_checked": True}]},
                "extraction": {"status": "ok"}, "evidence": {"status": "ok"}}
        on = replace(CONFIG, verdict="on")
        for broken in ({"verdict": {"status": "partial", "tools": []}}, {"extraction": {"status": "failed"}},
                       {"evidence": {"status": "watch_failed"}},
                       {"verdict": {"status": "ok", "tools": [{"alternatives_checked": False}]}}):
            (case / "bundle.json").write_text(json.dumps({**good, **broken}))
            self.assertFalse(_reusable(case, on), broken)
        (case / "bundle.json").write_text(json.dumps(good))
        self.assertTrue(_reusable(case, on))


class FooterTest(unittest.TestCase):
    def test_footer_survives_one_huge_tool(self):
        from hype_checker.v2.reply import format_verdict_replies
        from tests.test_v2 import _tool, _verdict_bundle
        tool = {**_tool("가" * 60), "good": ["나" * 200] * 2, "watch": ["다" * 200] * 2}
        messages = format_verdict_replies(_verdict_bundle([tool]), "c1")
        self.assertTrue(messages[-1].endswith("정정'이라고 답장해 주세요."))
        self.assertTrue(all(len(m.encode()) <= 1000 for m in messages))


class ToolCacheFailureTest(unittest.TestCase):
    def test_failed_search_is_not_cached_and_not_scored(self):
        from hype_checker.verdict import Budget, judge_bundle
        store = Store(Path(tempfile.mkdtemp()) / "hc.db")
        entity = {**_entity(), "name": "agent-desktop", "role": "primary", "claims": []}
        sources = FakeSearch([])
        sources.gh_get = lambda path, timeout_s=None: None
        sources.http_get = lambda url: None
        tool = judge_bundle({"entities": [entity]}, sources, lambda m: "{}", Budget(clock=lambda: 0.0), NOW,
                            cache=store)["tools"][0]
        self.assertFalse(tool["alternatives_checked"])
        self.assertNotIn("alternative", [key for _, key in tool["score_parts"]])
        self.assertIsNone((store.tool_get("vercel-labs/agent-desktop", NOW) or {}).get("alternatives"))

    def test_broken_cache_does_not_break_the_tool(self):
        from hype_checker.verdict import Budget, judge_bundle

        class Broken:
            def tool_get(self, *a):
                raise OSError("disk")
            tool_put = tool_get
        entity = {**_entity(), "name": "agent-desktop", "role": "primary", "claims": []}
        sources = FakeSearch([])
        sources.http_get = lambda url: None
        tool = judge_bundle({"entities": [entity]}, sources, lambda m: "{}", Budget(clock=lambda: 0.0), NOW,
                            cache=Broken())["tools"][0]
        self.assertNotIn("error", tool)
        self.assertTrue(tool["alternatives_checked"])

    def test_out_of_time_stops_between_searches(self):
        from hype_checker.alternatives import SearchIncomplete
        sources = FakeSearch([_item("big/x", 90000)])
        with self.assertRaises(SearchIncomplete):
            find_alternatives(_entity(), sources, lambda m: self.fail("no Claude"), NOW, time_left=lambda: 0)
        self.assertEqual(sources.paths, [])


class UncheckedClaimsTest(unittest.TestCase):
    def test_reply_says_when_claims_were_not_checked(self):
        from hype_checker.v2.reply import format_verdict_replies
        from tests.test_v2 import _tool, _verdict_bundle
        placeholders = [{"claim": "x", "result": "확인불가", "quote": ""}]
        tool = {**_tool("T"), "budget_cut": True, "claims_total": 4, "claims": placeholders, "claims_status": "not_run"}
        detail = format_verdict_replies(_verdict_bundle([tool]), "c1")[1]
        self.assertIn("게시물 주장 4개는 시간·예산이 모자라 공식 문서와 대조하지 못했어요", detail)

    def test_impersonation_warning_beats_praise_for_space(self):
        from hype_checker.v2.reply import format_verdict_replies
        from tests.test_v2 import _tool, _verdict_bundle
        tool = {**_tool("도" * 10, "👎"), "identifier": "owner/" + "r" * 65, "score": 3, "stars": 123456,
                "pushed_days": 3, "score_parts": [(0, "red_flag")],
                "good": ["게시물의 '" + "가" * 30 + "' 설명이 공식 문서와 맞아요"] * 2,
                "watch": ["같은 이름의 원본으로 보이는 다른 레포가 있어요", "npm이 보안상 내리고 자리만 남긴 이름이에요"]}
        detail = format_verdict_replies(_verdict_bundle([tool]), "c1")[1]
        self.assertIn("같은 이름의 원본으로 보이는 다른 레포가 있어요", detail)
        self.assertLess(detail.index("⚠️ 살펴볼 점"), detail.index("👍 좋은 점") if "👍 좋은 점" in detail else 10**6)

    def test_notice_survives_a_long_korean_url(self):
        from hype_checker.v2.reply import format_verdict_replies
        from tests.test_v2 import _tool, _verdict_bundle
        tool = {**_tool("가" * 60), "identifier": "https://mcp.example.com/" + "가" * 70, "stars": 123456,
                "pushed_days": 3, "claims_total": 9, "claims": [], "claims_status": "not_run"}
        detail = format_verdict_replies(_verdict_bundle([tool]), "c1")[1]
        self.assertIn("공식 문서와 대조하지 못했어요", detail)

    def test_notice_survives_a_crowded_block(self):
        from hype_checker.v2.reply import format_verdict_replies
        from tests.test_v2 import _tool, _verdict_bundle
        tool = {**_tool("가" * 60), "identifier": "very-long-owner-name/" + "r" * 60, "stars": 123456, "pushed_days": 3,
                "claims_total": 9, "claims": [], "claims_status": "not_run",
                "good": ["나" * 90] * 2, "watch": ["다" * 90] * 2, "alternatives": [{"identifier": "a/" + "b" * 60, "stars": 999999}]}
        details = format_verdict_replies(_verdict_bundle([tool]), "c1")[1:]
        text = "\n".join(details)
        self.assertTrue(all(len(m.encode()) <= 1000 for m in details))
        self.assertIn("게시물 주장 9개는 시간·예산이 모자라 공식 문서와 대조하지 못했어요", text)
        self.assertIn("👍 좋은 점", text)
        self.assertIn("💡 같은 일을 하는 더 유명한 도구", text)
        self.assertTrue(details[-1].endswith("정정'이라고 답장해 주세요."))

    def test_partial_readme_is_still_used_for_the_claim_check(self):
        from hype_checker.claims import estimate_input_chars
        official = {"status": "partial", "paragraphs": ["Agent Desktop clicks apps."]}
        self.assertGreater(estimate_input_chars({"claims": ["앱을 클릭"]}, official), 0)


class DedupeTest(unittest.TestCase):
    def test_spellings_of_one_repo_pool_their_claims(self):
        from hype_checker.verdict import _unique_primaries
        entities = [{"role": "primary", "identifier": "acme/widget", "name": "Widget", "claims": ["로컬 실행"]},
                    {"role": "primary", "identifier": "acme/widget", "name": "acme/widget", "claims": ["모든 작업이 무료", "로컬 실행"]}]
        [tool] = _unique_primaries(entities)
        self.assertEqual(tool["claims"], ["로컬 실행", "모든 작업이 무료"])
        self.assertEqual(entities[0]["claims"], ["로컬 실행"])
        three = [{"role": "primary", "identifier": "a/b", "name": n, "claims": [f"{n}{i}" for i in range(3)]}
                 for n in ("x", "y", "z")]
        self.assertEqual(len(_unique_primaries(three)[0]["claims"]), 9)

    def test_similar_identifiers_stay_separate_tools(self):
        from hype_checker.verdict import _unique_primaries
        entities = [{"role": "primary", "identifier": "a/b-c", "name": "b-c"},
                    {"role": "primary", "identifier": "a/bc", "name": "bc"},
                    {"role": "primary", "identifier": "A/BC", "name": "BC"}]
        self.assertEqual([e["identifier"] for e in _unique_primaries(entities)], ["a/b-c", "a/bc"])


class AlternativeGuardTest(unittest.TestCase):
    def test_real_tools_are_not_mistaken_for_lists(self):
        from hype_checker.alternatives import _is_collection
        for name in ("facebook/react", "x/playlist-sync", "y/event-listener", "z/bookmarks-cli"):
            self.assertFalse(_is_collection({"full_name": name, "description": "", "topics": []}), name)
        for item in ({"full_name": "a/awesome-mcp", "description": ""},
                     {"full_name": "a/x", "description": "A curated list of agents"},
                     {"full_name": "a/x", "description": "", "topics": ["tutorial"]}):
            self.assertTrue(_is_collection(item), item)

    def test_lists_and_tutorials_are_never_candidates(self):
        sources = FakeSearch([_item("someone/awesome-computer-use", 90000), _item("x/computer-use-course", 90000)])
        self.assertEqual(find_alternatives(_entity(), sources, lambda m: self.fail("asked"), NOW), [])

    def test_empty_search_reply_is_unknown_not_none_found(self):
        from hype_checker.alternatives import SearchIncomplete
        sources = FakeSearch([])
        sources.gh_get = lambda path, timeout_s=None: {}
        with self.assertRaises(SearchIncomplete):
            find_alternatives(_entity(), sources, lambda m: "{}", NOW)

    def test_claude_down_during_alternatives_keeps_the_verdict(self):
        from hype_checker.llm import ClaudeUnavailable
        from hype_checker.verdict import Budget, judge_bundle
        entity = {**_entity(), "name": "agent-desktop", "role": "primary", "claims": []}
        sources = FakeSearch([_item("big/desktop-agent", 28000)])
        sources.http_get = lambda url: None

        def down(messages):
            raise ClaudeUnavailable("529")
        verdict = judge_bundle({"entities": [entity]}, sources, down, Budget(clock=lambda: 0.0), NOW)
        self.assertFalse(verdict["tools"][0]["alternatives_checked"])
        self.assertNotIn("error", verdict["tools"][0])

    def test_impersonation_caps_score_whatever_the_claims_say(self):
        from hype_checker import grade
        from unittest import mock
        from hype_checker.verdict import Budget, judge_bundle
        meta = {"stargazers_count": 60000, "pushed_at": RECENT, "topics": []}
        entity = {"identifier": "copy/tool", "name": "tool", "role": "primary", "claims": ["a", "b", "c"],
                  "measurements": {"github": {"meta": {"status": "ok", "value": meta}}}}
        sources = FakeSearch([])
        sources.http_get = lambda url: None
        with mock.patch.object(grade, "_is_impersonation", return_value=True):
            tool = judge_bundle({"entities": [entity]}, sources, lambda m: "{}", Budget(clock=lambda: 0.0), NOW)["tools"][0]
        self.assertLessEqual(tool["score"] if tool["score"] is not None else 0, 3)

    def test_failed_readme_is_not_cached_and_marks_verdict_partial(self):
        from hype_checker.verdict import Budget, judge_bundle
        store = Store(Path(tempfile.mkdtemp()) / "hc.db")
        entity = {**_entity(), "name": "agent-desktop", "role": "primary", "claims": []}
        sources = FakeSearch([])
        sources.errors = []
        sources.gh_get = lambda path, timeout_s=None: (sources.errors.append("readme: failed") if path.endswith("/readme")
                                                       else {"items": []})
        sources.http_get = lambda url: None
        verdict = judge_bundle({"entities": [entity]}, sources, lambda m: "{}", Budget(clock=lambda: 0.0), NOW,
                               cache=store)
        self.assertIsNone((store.tool_get("vercel-labs/agent-desktop", NOW) or {}).get("official"))
        self.assertEqual(verdict["status"], "partial")


class TimeLimitTest(unittest.TestCase):
    def test_slow_call_is_cut_at_its_total_limit(self):
        import time as _time
        from hype_checker.http import TookTooLong, call_within
        started = _time.monotonic()
        with self.assertRaises(TookTooLong):
            call_within(lambda: _time.sleep(2), 0.2)
        self.assertLess(_time.monotonic() - started, 1)
        self.assertEqual(call_within(lambda: 7, 1), 7)

    def test_claude_post_is_bounded_including_error_bodies(self):
        from unittest import mock
        import time as _time
        from hype_checker import llm
        with mock.patch.object(llm, "_send", side_effect=lambda request, timeout_s: _time.sleep(2)):
            with self.assertRaises(OSError):
                llm._post("https://api.anthropic.com/v1/messages", b"{}", {}, timeout_s=0.2)

    def test_abandoned_read_stops_at_its_own_deadline(self):
        import io, time as _time
        from hype_checker.http import TookTooLong, read_capped

        class Trickle(io.RawIOBase):
            def read1(self, n=-1):
                _time.sleep(0.02)
                return b"x"
        with self.assertRaises(TookTooLong):
            read_capped(Trickle(), deadline=_time.monotonic() + 0.1)

    def test_broken_registry_json_is_a_failed_lookup(self):
        from unittest import mock
        from hype_checker.resolve import LiveSources
        sources = LiveSources(runner=None)
        with mock.patch("hype_checker.http.get_json", side_effect=ValueError("Expecting value")):
            self.assertIsNone(sources.http_get("https://registry.npmjs.org/x/latest"))
        self.assertTrue(sources.errors)

    def test_extraction_keeps_similar_repo_spellings_apart(self):
        from hype_checker.extract import extract_entities
        reply = json.dumps({"entities": [
            {"name": "a/b-c", "name_latin": "a/b-c", "kind": "repo", "role": "primary", "evidence_quote": "a/b-c", "claims": []},
            {"name": "a/bc", "name_latin": "a/bc", "kind": "repo", "role": "primary", "evidence_quote": "a/bc", "claims": []}]})
        result = extract_entities("", "a/b-c a/bc", "", chat=lambda m: reply)
        self.assertEqual(sorted(e["name"] for e in result.entities), ["a/b-c", "a/bc"])

    def test_null_success_is_a_failed_lookup(self):
        import subprocess
        from hype_checker.resolve import LiveSources

        class Null:
            def run(self, argv, timeout_s=None):
                return subprocess.CompletedProcess(argv, 0, stdout="null", stderr="")
        sources = LiveSources(Null())
        self.assertIsNone(sources.gh_get("repos/o/r/readme"))
        self.assertTrue(sources.errors)

    def test_alias_votes_count_but_spellings_stay_separate_until_identified(self):
        from hype_checker.extract import _merge
        found = [{"name": "acme/widget", "name_latin": "Widget", "role": "primary", "claims": ["a"]},
                 {"name": "Widget", "name_latin": "Widget", "role": "primary", "claims": ["b"]}]
        tools = {t["name"]: t for t in _merge(list(enumerate(found)), 2, "")}
        self.assertEqual({n: t["role"] for n, t in tools.items()}, {"acme/widget": "primary", "Widget": "primary"})
        self.assertEqual((tools["acme/widget"]["claims"], tools["Widget"]["claims"]), (["a"], ["b"]))


    def test_alias_first_still_keeps_the_written_repo(self):
        from hype_checker.extract import _merge
        found = [{"name": "Widget", "name_latin": "Widget", "role": "primary", "claims": []},
                 {"name": "acme/widget", "name_latin": "Widget", "role": "primary", "claims": []}]
        tools = {t["name"]: t["role"] for t in _merge(list(enumerate(found)), 2, "")}
        self.assertEqual(tools["acme/widget"], "primary")


    def test_alias_given_in_a_later_chunk_still_links_votes(self):
        from hype_checker.extract import _merge
        found = [(0, {"name": "acme/widget", "name_latin": "acme/widget", "role": "mentioned", "claims": []}),
                 (1, {"name": "acme/widget", "name_latin": "Widget", "role": "primary", "claims": []}),
                 (2, {"name": "Widget", "name_latin": "Widget", "role": "primary", "claims": []})]
        roles = {t["name"]: t["role"] for t in _merge(found, 3, "")}
        self.assertEqual(roles, {"acme/widget": "primary", "Widget": "primary"})

    def test_last_path_part_alone_lends_no_votes(self):
        from hype_checker.extract import _merge
        found = [(0, {"name": "uuid", "name_latin": "uuid", "kind": "npm", "role": "primary", "claims": []}),
                 (1, {"name": "uuid", "name_latin": "uuid", "kind": "npm", "role": "primary", "claims": []}),
                 (2, {"name": "demo/uuid", "name_latin": "demo/uuid", "kind": "repo", "role": "primary", "claims": []})]
        roles = {t["name"]: t["role"] for t in _merge(found, 3, "")}
        self.assertEqual(roles, {"uuid": "primary", "demo/uuid": "mentioned"})



    def test_headline_alias_stays_primary_and_mention_stays_mention(self):
        from hype_checker.extract import _merge
        found = [{"name": "Widget", "name_latin": "Widget", "role": "primary", "claims": []},
                 {"name": "위젯", "name_latin": "acme/widget", "role": "mentioned", "claims": []}]
        roles = {t["name_latin"]: t["role"] for t in _merge(list(enumerate(found)), 2, "Widget 추천")}
        self.assertEqual(roles, {"Widget": "primary", "acme/widget": "mentioned"})


    def test_same_repo_across_chunks_keeps_every_claim(self):
        from hype_checker.extract import _merge
        found = [{"name": "acme/widget", "name_latin": "Widget", "role": "primary", "claims": ["로컬 실행", "파일 편집", "앱 클릭"]},
                 {"name": "acme/widget", "name_latin": "acme/widget", "role": "primary", "claims": ["모든 기능 영구 무료"]}]
        [tool] = _merge(list(enumerate(found)), 2, "")
        self.assertEqual(len(tool["claims"]), 4)

    def test_package_is_not_replaced_by_a_same_named_repo(self):
        from hype_checker.extract import _merge
        found = [(0, {"name": "uuid", "name_latin": "uuid", "kind": "npm", "role": "primary", "claims": ["a"]}),
                 (0, {"name": "demo/uuid", "name_latin": "demo/uuid", "kind": "repo", "role": "mentioned", "claims": ["b"]})]
        tools = {t["name"]: (t["kind"], t["role"]) for t in _merge(found, 1, "uuid 패키지 추천")}
        self.assertEqual(tools, {"uuid": ("npm", "primary"), "demo/uuid": ("repo", "mentioned")})


    def test_product_is_not_merged_into_a_same_named_repo(self):
        from hype_checker.extract import _merge
        found = [(0, {"name": "Cursor", "name_latin": "Cursor", "kind": "product", "role": "primary", "claims": ["a"]}),
                 (0, {"name": "acme/cursor", "name_latin": "acme/cursor", "kind": "repo", "role": "mentioned", "claims": ["b"]})]
        tools = _merge(found, 1, "Cursor 추천")
        self.assertEqual(sorted((t["name"], t["kind"]) for t in tools), [("Cursor", "product"), ("acme/cursor", "repo")])

    def test_two_spellings_in_one_chunk_are_one_vote(self):
        from hype_checker.extract import _merge
        found = [(0, {"name": "acme/widget", "name_latin": "acme/widget", "kind": "repo", "role": "primary", "claims": []}),
                 (0, {"name": "Widget", "name_latin": "Widget", "kind": "repo", "role": "primary", "claims": []})]
        self.assertEqual({t["role"] for t in _merge(found, 2, "일반적인 개발 이야기")}, {"mentioned"})


    def test_ambiguous_alias_joins_no_repo(self):
        from hype_checker.extract import _merge
        found = [{"name": "a/widget", "name_latin": "Widget", "role": "primary", "claims": []},
                 {"name": "b/widget", "name_latin": "Widget", "role": "primary", "claims": []},
                 {"name": "Widget", "name_latin": "Widget", "role": "primary", "claims": []}]
        self.assertEqual(len(_merge(list(enumerate(found)), 1, "")), 3)

    def test_post_spelling_wins_over_alias_when_merging(self):
        from hype_checker.extract import extract_entities
        reply = json.dumps({"entities": [
            {"name": "a/b-c", "name_latin": "tool", "kind": "repo", "role": "primary", "evidence_quote": "a/b-c", "claims": []},
            {"name": "a/bc", "name_latin": "tool", "kind": "repo", "role": "primary", "evidence_quote": "a/bc", "claims": []}]})
        result = extract_entities("", "a/b-c a/bc", "", chat=lambda m: reply)
        self.assertEqual(sorted(e["name"] for e in result.entities), ["a/b-c", "a/bc"])

    def test_empty_success_is_a_failed_lookup(self):
        import subprocess
        from hype_checker.resolve import LiveSources

        class Empty:
            def run(self, argv, timeout_s=None):
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        sources = LiveSources(Empty())
        self.assertIsNone(sources.gh_get("repos/o/r/readme"))
        self.assertTrue(sources.errors)

    def test_no_search_wait_past_the_deadline(self):
        from hype_checker.resolve import LiveSources
        waited, now = [], [179.5]
        sources = LiveSources(runner=None, sleep=waited.append, clock=lambda: now[0])
        sources.deadline = 180
        sources._last_search = 179.0
        self.assertIsNone(sources.gh_get("search/repositories?q=x"))
        self.assertEqual(waited, [])

    def test_empty_reply_during_extraction_is_a_failure_not_a_crash(self):
        from hype_checker.extract import extract_entities

        def empty(messages):
            raise ValueError("Claude API: 빈 응답")
        self.assertEqual(extract_entities("", "캡션", "", chat=empty).status, "failed")

    def test_claude_outage_during_extraction_is_a_skip_not_a_crash(self):
        from hype_checker.extract import extract_entities
        from hype_checker.llm import ClaudeUnavailable

        def down(messages):
            raise ClaudeUnavailable("529")
        self.assertEqual(extract_entities("", "캡션", "", chat=down).status, "skipped")

    def test_extraction_and_screen_reading_wait_for_a_whole_call(self):
        from hype_checker.extract import extract_entities
        from hype_checker.onscreen import read_screen
        frame = Path(tempfile.mkdtemp()) / "f.jpg"
        frame.write_bytes(b"jpg")
        self.assertEqual(read_screen([{"path": str(frame)}], lambda m: self.fail("asked"), lambda: 30), "")
        result = extract_entities("", "캡션", "", chat=lambda m: self.fail("asked"), time_left=lambda: 30)
        self.assertNotEqual(result.status, "ok")

    def test_cached_alternative_unknown_without_todays_stars(self):
        from hype_checker.verdict import Budget, judge_bundle
        store = Store(Path(tempfile.mkdtemp()) / "hc.db")
        store.tool_put("o/r", {"alternatives": [{"identifier": "big/x", "stars": 500}]}, NOW)
        entity = {"identifier": "o/r", "name": "r", "role": "primary", "claims": [],
                  "measurements": {"github": {"meta": {"status": "failed"}}}}
        sources = FakeSearch([])
        sources.http_get = lambda url: None
        tool = judge_bundle({"entities": [entity]}, sources, lambda m: "{}", Budget(clock=lambda: 0.0), NOW,
                            cache=store)["tools"][0]
        self.assertEqual(tool["alternatives"], [])
        self.assertFalse(tool["alternatives_checked"])

    def test_cached_alternative_dropped_when_the_tool_caught_up(self):
        from hype_checker.verdict import Budget, judge_bundle
        store = Store(Path(tempfile.mkdtemp()) / "hc.db")
        store.tool_put("vercel-labs/agent-desktop", {"alternatives": [{"identifier": "big/x", "stars": 9000}]}, NOW)
        entity = {**_entity(stars=60000), "name": "agent-desktop", "role": "primary", "claims": []}
        sources = FakeSearch([])
        sources.http_get = lambda url: None
        tool = judge_bundle({"entities": [entity]}, sources, lambda m: "{}", Budget(clock=lambda: 0.0), NOW,
                            cache=store)["tools"][0]
        self.assertEqual(tool["alternatives"], [])

    def test_remaining_time_bounds_each_search(self):
        seen = []
        sources = FakeSearch([])
        sources.gh_get = lambda path, timeout_s=None: seen.append(timeout_s) or {"items": []}
        find_alternatives(_entity(), sources, lambda m: "{}", NOW, time_left=lambda: 42)
        self.assertTrue(seen and all(t == 42 for t in seen))

    def test_claude_is_not_asked_without_a_full_call_left(self):
        from hype_checker.alternatives import SearchIncomplete
        sources = FakeSearch([_item("big/x", 90000)])
        with self.assertRaises(SearchIncomplete):
            find_alternatives(_entity(), sources, lambda m: self.fail("asked"), NOW, time_left=lambda: 30)

    def test_runner_timeout_only_shortens(self):
        import sys
        from hype_checker.runner import Runner, TimeoutExpired
        script = Path(tempfile.mkdtemp()) / "watch.py"
        script.write_text("import time; time.sleep(5)\n")
        runner = Runner(watch_script=str(script), timeout_s=600)
        with self.assertRaises(TimeoutExpired):
            runner.run(["python3", str(script)], timeout_s=0.5)


class ToolCacheTest(unittest.TestCase):
    def test_second_judgement_reuses_official_text_and_alternatives(self):
        from hype_checker.verdict import Budget, judge_bundle
        store = Store(Path(tempfile.mkdtemp()) / "hc.db")
        entity = {**_entity(), "name": "agent-desktop", "role": "primary", "claims": []}
        bundle = {"entities": [entity]}
        sources = FakeSearch([_item("big/desktop-agent", 28000)])
        sources.http_get = lambda url: None
        chat = lambda messages: json.dumps({"same": ["big/desktop-agent"]})
        first = judge_bundle(bundle, sources, chat, Budget(clock=lambda: 0.0), NOW, cache=store)["tools"][0]
        calls = len(sources.paths)
        second = judge_bundle(bundle, sources, lambda m: self.fail("cached"), Budget(clock=lambda: 0.0), NOW,
                              cache=store)["tools"][0]
        self.assertEqual(len(sources.paths), calls)
        self.assertTrue(second["cached"])
        self.assertEqual(second["alternatives"], first["alternatives"])
        self.assertEqual(first["alternatives"], [{"identifier": "big/desktop-agent", "stars": 28000}])


if __name__ == "__main__":
    unittest.main()
