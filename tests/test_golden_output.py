"""Pins the reader-facing formats to one real DM case so wording cannot drift between outputs."""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from hype_checker.bundle import render_markdown
from hype_checker.verdict import Budget, judge_bundle
from hype_checker.v2.reply import format_reply, format_verdict_replies

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


NOW = 1792800000
# Scripted claim-check replies for the verdict golden (no network): claim text -> result, paragraph, quote.
CLAIM_REPLIES = {
    "출시된 앱 화면 62만 장 넘게": ("일치", 1, "search over 620,000 screens"),
    "유료 플랜": ("일치", 1, "Requires a paid subscription"),
    "무료 월 15회": ("불일치", 1, "Free plan includes 10 searches per month"),
}


def load_bundle(name="dm_reel_bundle.json"):
    return json.loads((GOLDEN / name).read_text())


class NoSources:
    def gh_get(self, path):
        raise AssertionError(f"golden must not reach GitHub: {path}")

    def http_get(self, url):
        raise AssertionError(f"golden must not reach the network: {url}")


def golden_chat(messages):
    claim, (result, paragraph, quote) = next((c, r) for c, r in CLAIM_REPLIES.items() if c in messages[-1]["content"])
    golden_chat.usage.append({"input_tokens": 900, "output_tokens": 60})
    return json.dumps({"results": [{"claim_id": "c1", "result": result, "paragraph": paragraph, "quote": quote}]})


def build_verdict(bundle):
    """Regenerate: bundle = load_bundle("dm_reel_verdict_bundle.json"); bundle["verdict"] = build_verdict(bundle)."""
    golden_chat.usage = []
    env = {k: v for k, v in os.environ.items() if not k.startswith("HC_PRICE")}
    with mock.patch.dict(os.environ, env, clear=True):
        return judge_bundle(bundle, NoSources(), golden_chat, Budget(clock=lambda: 0.0), NOW, model="claude-golden-fake")


class GoldenOutputTest(unittest.TestCase):
    """If a format change is intended, regenerate the .txt/.md files and review the diff by eye."""

    def test_dm_reply_matches_golden(self):
        bundle = load_bundle()
        self.assertEqual("\n-----\n".join(format_reply(bundle, bundle["id"])), (GOLDEN / "dm_reel_reply.txt").read_text())

    def test_verdict_reply_matches_golden(self):
        bundle = load_bundle("dm_reel_verdict_bundle.json")
        joined = "\n-----\n".join(format_verdict_replies(bundle, bundle["id"]))
        self.assertEqual(joined, (GOLDEN / "dm_reel_verdict_reply.txt").read_text())

    def test_bundle_markdown_matches_golden(self):
        self.assertEqual(render_markdown(load_bundle()), (GOLDEN / "dm_reel_bundle.md").read_text())

    def test_verdict_matches_golden(self):
        stored = load_bundle("dm_reel_verdict_bundle.json")
        expected = stored.pop("verdict")
        self.assertEqual(build_verdict(stored), expected)

    def test_verdict_markdown_matches_golden(self):
        bundle = load_bundle("dm_reel_verdict_bundle.json")
        self.assertEqual(render_markdown(bundle), (GOLDEN / "dm_reel_verdict_bundle.md").read_text())

    def test_remote_mcp_is_not_reported_as_unidentified(self):
        markdown = render_markdown(load_bundle())
        self.assertEqual(markdown.count("원격 MCP 주소라 코드 측정 대상이 아님"), 4)
        self.assertEqual(markdown.count("❓ 측정 없음"), 1)   # only Lazyweb, which has no single location


if __name__ == "__main__":
    unittest.main()
