"""Owner data (workflow.yaml) must never reach a guest: replies, bundle, inbox record or logs (owner-spec 3-6, Codex #5)."""

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from hype_checker.owner import make_owner_lines_for
from hype_checker.v2.inbox import Inbox
from hype_checker.v2.limits import Limits
from hype_checker.v2.webhook import parse_shares
from hype_checker.v2.worker import Runtime, process_one
from tests.test_pipeline import RUFLO_GH, RUFLO_HTTP, make_context
from tests.test_v2 import CONFIG, ME, NOW_S, OWNER, fixture, text_share

CANARY = "CANARY-7f3a"
WORKFLOW = f"""\
items:
  - id: {CANARY}/other-tool
    aliases: [{CANARY}-alias]
    capabilities: {CANARY} 다중 에이전트 오케스트레이션
    lane: agents
    status: active
    note: {CANARY}-note
"""


def judged(bundle, *args, **kwargs):
    entry = {"name": "Ruflo", "identifier": "ruvnet/ruflo", "grade": "🤔", "reason_ids": ["fact.x"],
             "reasons": ["최근 1년 안 활동 있음"], "claims": [], "claims_status": "ok", "budget_cut": False}
    repro = {"model": "m", "rules_version": "r", "claims_prompt_sha": "s" * 12, "tokens_in": 0, "tokens_out": 0,
             "cost_usd": 0.0, "calls": 0}
    return {"tools": [entry], "skipped_tools": 0, "status": "ok", "repro": repro}


class IsolationCanaryTest(unittest.TestCase):
    def setUp(self):
        workflow = Path(tempfile.mkdtemp()) / "workflow.yaml"
        workflow.write_text(WORKFLOW, encoding="utf-8")
        workflow.chmod(0o600)
        self.merge_prompts = []

        def chat(messages):
            self.merge_prompts.append(messages)
            return '{"b": "이미보유", "match": "%s/other-tool"}' % CANARY
        chat.usage = []
        self.config = replace(CONFIG, verdict="on", owner_sender=OWNER)
        self.ctx = make_context(RUFLO_GH, RUFLO_HTTP)
        self.inbox = Inbox(Path(tempfile.mkdtemp()))
        self.runtime = Runtime(limits=Limits(self.inbox.root / "limits"), chat=chat, model="m", prices=(3.0, 15.0),
                               owner_lines_for=make_owner_lines_for(chat, workflow_path=workflow, model="m"),
                               sleep=lambda s: None)
        self.sent = []

    def run_one(self, share):
        self.inbox.enqueue(share)
        [item] = self.inbox.pending()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("hype_checker.v2.worker.judge_bundle", side_effect=judged), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            record = process_one(self.inbox, item, self.ctx, self.config,
                                 lambda recipient, text: self.sent.append(text), NOW_S, self.runtime)
        return record, out.getvalue() + err.getvalue()

    def test_isolation_canary(self):
        guest_share = parse_shares(fixture("ig_post.json"), self.config, NOW_S)[0]
        record, logs = self.run_one(guest_share)
        self.assertEqual(record["mode"], "guest")
        case_dir = Path(record["case_dir"])
        exposed = {
            "sent": "\n".join(self.sent),
            "bundle.json": (case_dir / "bundle.json").read_text(),
            "bundle.md": (case_dir / "bundle.md").read_text(),
            "logs": logs,
            "inbox": "\n".join(p.read_text() for p in self.inbox.root.rglob("*.json")),
        }
        self.assertTrue(self.sent)
        for where, text in exposed.items():
            with self.subTest(where):
                self.assertNotIn(CANARY, text)
        self.assertFalse((case_dir / "owner.json").exists())
        self.assertFalse((case_dir / "card.md").exists())
        self.assertEqual(self.merge_prompts, [])

        owner_record, _ = self.run_one(text_share("owner-mid", "Ruflo 써봐", sender=OWNER, config=self.config)[0])
        owner_dir = Path(owner_record["case_dir"])
        self.assertEqual(owner_record["mode"], "owner")
        self.assertTrue((owner_dir / "owner.json").exists())
        self.assertTrue((owner_dir / "card.md").exists())
        self.assertNotIn(CANARY, (owner_dir / "bundle.json").read_text())
        self.assertIn(f"내 워크플로우: Ruflo → 이미보유({CANARY}/other-tool)", owner_record["messages"][0]["text"])
        self.assertFalse((case_dir / "owner.json").exists())


if __name__ == "__main__":
    unittest.main()
