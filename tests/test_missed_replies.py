"""DMs that got no reply or an empty one in the first public week (2026-11-02 ~ 11-09)."""

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from hype_checker.resolve import promote_listed_repos
from hype_checker.v2.inbox import Inbox
from hype_checker.v2.limits import Limits
from hype_checker.v2.reply import NOTHING, format_verdict_replies, video_problem
from hype_checker.v2.webhook import not_queued_shape, parse_shares
from hype_checker.v2.worker import UNSUPPORTED_GUIDE, Runtime, _video_note, process_pending
from tests.test_pipeline import RUFLO_GH, RUFLO_HTTP, make_context
from tests.test_public import PUBLIC, STRANGER
from tests.test_v2 import NOW_S, _tool, _verdict_bundle, fixture

TOP_10 = "GitHub 주간 TOP 10\n1. pingdotgg/t3code — ★ +17,100\n2. mattpocock/skills — ★ +9,664\n"
AUDIENCE_LIMIT = ("Media unavailable: yt-dlp failed (exit 1): ERROR: [Instagram] X: "
                  "This content isn't available to everyone: It can't be seen by certain audiences.")
NO_ACCESS = "Media unavailable: yt-dlp failed (exit 1): WARNING: [Instagram] X: Instagram API is not granting access"


def repo(name, role="mentioned", kind="repo"):
    return {"name": name, "kind": kind, "role": role}


def event_with(message):
    payload = fixture("ig_post.json")
    payload["entry"][0]["messaging"][0]["message"] = {"mid": "m1", **message}
    return payload


def no_tools(kind="ig_reel", errors=None, status="partial"):
    bundle = _verdict_bundle([])
    bundle["dm"] = {"kind": kind}
    bundle["evidence"] = {"status": status, "errors": errors or []}
    return bundle


class ListedRepoTest(unittest.TestCase):
    def test_written_repos_become_primary_when_nothing_else_is(self):
        promoted = promote_listed_repos([repo("pingdotgg/t3code"), repo("mattpocock/skills")], TOP_10)
        self.assertEqual([e["role"] for e in promoted], ["primary", "primary"])

    def test_a_post_with_its_own_tool_keeps_listed_repos_mentioned(self):
        entities = [repo("Ruflo", "primary", "product"), repo("pingdotgg/t3code")]
        self.assertEqual([e["role"] for e in promote_listed_repos(entities, TOP_10)], ["primary", "mentioned"])

    def test_plain_product_names_stay_mentioned(self):
        promoted = promote_listed_repos([repo("클로드", kind="product"), repo("cursor", kind="product")], "클로드 cursor")
        self.assertEqual([e["role"] for e in promoted], ["mentioned", "mentioned"])

    def test_a_repo_the_post_never_wrote_is_not_promoted(self):
        self.assertEqual(promote_listed_repos([repo("made/up")], TOP_10)[0]["role"], "mentioned")


class HonestEmptyReplyTest(unittest.TestCase):
    def reply(self, bundle):
        [message] = format_verdict_replies(bundle, "c1")
        return message

    def test_restricted_reel_says_the_video_could_not_be_read(self):
        message = self.reply(no_tools(errors=[AUDIENCE_LIMIT]))
        self.assertIn("공개 범위", message)
        self.assertNotEqual(message, NOTHING)

    def test_blocked_download_asks_to_send_it_again(self):
        self.assertIn("다시 보내", self.reply(no_tools(errors=[NO_ACCESS])))

    def test_image_post_says_images_are_not_read(self):
        self.assertIn("이미지", self.reply(no_tools("ig_post", status="not_run")))

    def test_read_reel_with_no_tool_keeps_the_plain_notice(self):
        self.assertEqual(self.reply(no_tools(status="ok")), NOTHING)

    def test_tools_found_from_the_caption_alone_are_labelled(self):
        bundle = _verdict_bundle([_tool("Ruflo")])
        bundle["evidence"] = {"status": "partial", "errors": [NO_ACCESS]}
        self.assertIn("영상을 읽지 못해", format_verdict_replies(bundle, "c1")[0])

    def test_failed_transcription_is_not_an_unread_video(self):
        evidence = {"evidence": {"status": "partial", "errors": ["Transcription failed: malformed segments"]}}
        self.assertIsNone(video_problem(evidence))

    def test_record_does_not_claim_an_unread_video_was_read(self):
        self.assertTrue(_video_note(no_tools(errors=[NO_ACCESS]), None).startswith("not read"))


class UnsupportedAttachmentTest(unittest.TestCase):
    def setUp(self):
        self.inbox = Inbox(Path(tempfile.mkdtemp()))
        self.sent = []
        self.limits = Limits(self.inbox.root / "limits")
        self.runtime = Runtime(limits=self.limits, sleep=lambda _: None)

    def send(self, recipient, text):
        self.sent.append((recipient, text))

    def run_events(self, *messages):
        for mid, message in messages:
            payload = event_with({**message, "mid": mid})
            payload["entry"][0]["messaging"][0]["sender"]["id"] = STRANGER
            for share in parse_shares(payload, PUBLIC, NOW_S):
                self.inbox.enqueue(share)
        return process_pending(self.inbox, make_context(RUFLO_GH, RUFLO_HTTP), PUBLIC, send=self.send, now_s=NOW_S,
                               runtime=self.runtime)

    def test_other_attachment_and_unsupported_message_are_queued(self):
        for message in ({"attachments": [{"type": "image", "payload": {"url": "https://x"}}]}, {"is_unsupported": True}):
            [share] = parse_shares(event_with(message), replace(PUBLIC, unlimited_senders=frozenset()), NOW_S)
            self.assertEqual((share.kind, share.caption), ("unsupported", ""))

    def test_guide_goes_out_once_a_day_and_spends_nothing(self):
        attachment = {"attachments": [{"type": "story_mention", "payload": {}}]}
        self.run_events(("a", attachment), ("b", attachment))
        self.assertEqual(self.sent, [(STRANGER, UNSUPPORTED_GUIDE)])
        self.assertTrue(self.limits.allow(STRANGER, time.strftime("%Y-%m-%d", time.gmtime(NOW_S))))

    def test_deleted_message_gets_no_reply(self):
        self.assertEqual(parse_shares(event_with({"is_deleted": True}), PUBLIC, NOW_S), [])

    def test_not_queued_shape_has_field_names_and_types_only(self):
        payload = event_with({"is_deleted": True, "attachments": [{"type": "x", "payload": {"url": "https://secret"}}]})
        shape = not_queued_shape(payload)
        self.assertIn("is_deleted", shape)
        self.assertIn("x", shape)
        self.assertNotIn("secret", json.dumps(shape))


if __name__ == "__main__":
    unittest.main()
