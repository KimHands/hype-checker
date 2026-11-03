import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import socket
from http.server import ThreadingHTTPServer
from pathlib import Path

from hype_checker.v2.inbox import Inbox
from hype_checker.v2.reply import format_reply, format_verdict_replies, send_reply
from hype_checker.v2.server import make_handler
from hype_checker.v2.webhook import Config, parse_shares, unknown_senders, valid_signature, verify_subscription
from hype_checker.v2.worker import Runtime, process_pending
from hype_checker.v2.limits import Limits
from tests.test_pipeline import RUFLO_GH, RUFLO_HTTP, make_context

FIXTURES = Path(__file__).parent / "fixtures" / "v2"
ME = "900000000000001"
ACCOUNT = "17841400000000001"
CONFIG = Config(verify_token="tok", app_secrets=("old-secret", "app-secret"), allowed_senders=frozenset({ME}),
                account_id=ACCOUNT)
NOW_S = 1761440000 + 60   # one minute after the fixture messages


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def sign(body, secret="app-secret"):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class SubscriptionTest(unittest.TestCase):
    def test_echoes_challenge_only_for_matching_token(self):
        ok = {"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "1158201444"}
        self.assertEqual(verify_subscription(ok, CONFIG), "1158201444")
        self.assertIsNone(verify_subscription({**ok, "hub.verify_token": "nope"}, CONFIG))
        self.assertIsNone(verify_subscription({**ok, "hub.mode": "unsubscribe"}, CONFIG))


class SignatureTest(unittest.TestCase):
    def test_accepts_any_configured_secret(self):
        body = b'{"object":"instagram"}'
        self.assertTrue(valid_signature(body, sign(body), CONFIG))
        self.assertTrue(valid_signature(body, sign(body, "old-secret"), CONFIG))

    def test_rejects_wrong_missing_or_unprefixed(self):
        body = b'{"object":"instagram"}'
        self.assertFalse(valid_signature(body, sign(body, "other"), CONFIG))
        self.assertFalse(valid_signature(body, None, CONFIG))
        self.assertFalse(valid_signature(body, sign(body)[len("sha256="):], CONFIG))


class ParseTest(unittest.TestCase):
    def test_ig_post_gives_caption_and_media_id(self):
        [share] = parse_shares(fixture("ig_post.json"), CONFIG, NOW_S)
        self.assertEqual((share.kind, share.media_id, share.sender), ("ig_post", "18139494541428835", ME))
        self.assertIn("Ruflo", share.caption)

    def test_ig_post_id_key_variant(self):
        payload = fixture("ig_post.json")
        attachment = payload["entry"][0]["messaging"][0]["message"]["attachments"][0]["payload"]
        attachment["id"] = attachment.pop("ig_post_media_id")
        self.assertEqual(parse_shares(payload, CONFIG, NOW_S)[0].media_id, "18139494541428835")

    def test_ig_reel(self):
        [share] = parse_shares(fixture("ig_reel.json"), CONFIG, NOW_S)
        self.assertEqual((share.kind, share.media_id), ("ig_reel", "18000000000000002"))

    def test_plain_text_message(self):
        payload = fixture("ig_post.json")
        payload["entry"][0]["messaging"][0]["message"] = {"mid": "t1", "text": "https://github.com/ruvnet/ruflo"}
        [share] = parse_shares(payload, CONFIG, NOW_S)
        self.assertEqual((share.kind, share.caption), ("text", "https://github.com/ruvnet/ruflo"))

    def test_drops_strangers_echoes_unsupported_and_other_objects(self):
        stranger = fixture("ig_post.json")
        stranger["entry"][0]["messaging"][0]["sender"]["id"] = "123"
        echo = fixture("ig_post.json")
        echo["entry"][0]["messaging"][0]["message"]["is_echo"] = True
        unsupported = fixture("ig_post.json")
        unsupported["entry"][0]["messaging"][0]["message"] = {"mid": "u1", "is_unsupported": True}
        page = {**fixture("ig_post.json"), "object": "page"}
        other_account = fixture("ig_post.json")
        other_account["entry"][0]["messaging"][0]["recipient"]["id"] = "999"
        for payload in (stranger, echo, unsupported, page, other_account, {}):
            self.assertEqual(parse_shares(payload, CONFIG, NOW_S), [])

    def test_logs_first_dm_sender_only_while_pending(self):
        pending = Config(verify_token="tok", app_secrets=("app-secret",), allowed_senders=frozenset({"pending"}),
                         account_id=ACCOUNT)
        echo = fixture("ig_post.json")
        echo["entry"][0]["messaging"][0]["message"]["is_echo"] = True
        other_account = fixture("ig_post.json")
        other_account["entry"][0]["messaging"][0]["recipient"]["id"] = "999"
        self.assertEqual(unknown_senders(fixture("ig_post.json"), pending), [ME])
        self.assertEqual(unknown_senders(echo, pending), [])
        self.assertEqual(unknown_senders(other_account, pending), [])
        read_receipt = fixture("ig_post.json")
        del read_receipt["entry"][0]["messaging"][0]["message"]
        self.assertEqual(unknown_senders(read_receipt, pending), [])
        self.assertEqual(parse_shares(fixture("ig_post.json"), pending, NOW_S), [])
        stranger = fixture("ig_post.json")
        stranger["entry"][0]["messaging"][0]["sender"]["id"] = "123"
        self.assertEqual(unknown_senders(stranger, CONFIG), [])   # ADR 0011: strangers dropped unlogged

    def test_drops_events_outside_the_reply_window(self):
        self.assertEqual(parse_shares(fixture("ig_post.json"), CONFIG, NOW_S + 25 * 3600), [])


class InboxTest(unittest.TestCase):
    def test_deduplicates_by_message_id_even_after_done(self):
        inbox = Inbox(Path(tempfile.mkdtemp()))
        [share] = parse_shares(fixture("ig_post.json"), CONFIG, NOW_S)
        self.assertTrue(inbox.enqueue(share))
        self.assertFalse(inbox.enqueue(share))
        item = inbox.pending()[0]
        inbox.finish(item, inbox.read(item), {"case_dir": "x"})
        self.assertEqual(inbox.pending(), [])
        self.assertFalse(inbox.enqueue(share))


class ConcurrencyTest(unittest.TestCase):
    def test_same_message_arriving_twice_at_once_is_queued_once(self):
        inbox = Inbox(Path(tempfile.mkdtemp()))
        [share] = parse_shares(fixture("ig_post.json"), CONFIG, NOW_S)
        results = []
        threads = [threading.Thread(target=lambda: results.append(inbox.enqueue(share))) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((results.count(True), len(inbox.pending())), (1, 1))


class SlowClientTest(unittest.TestCase):
    def test_dripping_body_hits_the_whole_request_deadline(self):
        from hype_checker.v2.server import BodyTooSlow, read_body
        left, right = socket.socketpair()
        stop = threading.Event()

        def drip():
            while not stop.is_set():
                left.sendall(b"x")
                stop.wait(0.05)
        threading.Thread(target=drip, daemon=True).start()
        try:
            with self.assertRaises(BodyTooSlow):
                read_body(right.makefile("rb"), right, 10_000, deadline_s=0.3)
        finally:
            stop.set()
            left.close()
            right.close()

    def test_slow_headers_are_cut_at_the_connection_deadline(self):
        from hype_checker.v2.server import BoundedServer
        server = BoundedServer(("127.0.0.1", 0), make_handler(CONFIG, Inbox(Path(tempfile.mkdtemp())), clock=lambda: NOW_S),
                               max_connections=1, deadline_s=0.5)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        held = socket.create_connection(("127.0.0.1", server.server_port))
        held.sendall(b"POST /webhook HTTP/1.1\r\nX-Slow: ")
        try:
            held.settimeout(3)
            self.assertEqual(held.recv(1), b"")   # server hung up although headers never finished
            time.sleep(0.2)   # slot is released just after the hang-up
            url = f"http://127.0.0.1:{server.server_port}/webhook?hub.mode=subscribe&hub.verify_token=tok&hub.challenge=5"
            self.assertEqual(urllib.request.urlopen(url, timeout=3).read(), b"5")
        finally:
            held.close()
            server.shutdown()
            server.server_close()

    def test_connection_cap_refuses_extra_clients(self):
        from hype_checker.v2.server import BoundedServer
        server = BoundedServer(("127.0.0.1", 0), make_handler(CONFIG, Inbox(Path(tempfile.mkdtemp())), clock=lambda: NOW_S),
                               max_connections=1)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        held = socket.create_connection(("127.0.0.1", server.server_port))
        held.sendall(b"POST /webhook HTTP/1.1\r\nContent-Length: 50\r\nX-Hub-Signature-256: sha256=" + b"0" * 64 + b"\r\n\r\n{")
        try:
            url = f"http://127.0.0.1:{server.server_port}/webhook?hub.mode=subscribe&hub.verify_token=tok&hub.challenge=1"
            with self.assertRaises((urllib.error.URLError, ConnectionError)):
                urllib.request.urlopen(url, timeout=2)
        finally:
            held.close()
            server.shutdown()
            server.server_close()


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.inbox = Inbox(Path(tempfile.mkdtemp()))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(CONFIG, self.inbox, clock=lambda: NOW_S))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/webhook"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, body, signature):
        request = urllib.request.Request(self.base, data=body, method="POST",
                                         headers={"X-Hub-Signature-256": signature} if signature else {})
        try:
            return urllib.request.urlopen(request).status
        except urllib.error.HTTPError as error:
            return error.code

    def test_truncated_body_does_not_block_other_requests(self):
        stalled = socket.create_connection(("127.0.0.1", self.server.server_port))
        stalled.sendall(b"POST /webhook HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n\r\n{")
        try:
            url = self.base + "?hub.mode=subscribe&hub.verify_token=tok&hub.challenge=7"
            self.assertEqual(urllib.request.urlopen(url, timeout=3).read(), b"7")
        finally:
            stalled.close()

    def test_unsigned_post_is_refused_before_reading_the_body(self):
        unsigned = socket.create_connection(("127.0.0.1", self.server.server_port))
        unsigned.sendall(b"POST /webhook HTTP/1.1\r\nHost: x\r\nContent-Length: 900000\r\n\r\n{")
        try:
            unsigned.settimeout(3)
            self.assertIn(b" 403 ", unsigned.recv(64))
        finally:
            unsigned.close()

    def test_rejects_absurd_content_length_and_hides_python_version(self):
        status, server_header = self._raw_post_status(b"9" * 5000)
        self.assertEqual(status, 400)
        self.assertNotIn("Python", server_header)

    def _raw_post_status(self, length):
        request = urllib.request.Request(self.base, data=b"{}", method="POST",
                                         headers={"Content-Length": length.decode()})
        try:
            response = urllib.request.urlopen(request)
            return response.status, response.headers.get("Server", "")
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Server", "")

    def test_pending_logs_each_sender_once_even_on_retries(self):
        import contextlib, io
        pending = Config(verify_token="tok", app_secrets=("app-secret",), allowed_senders=frozenset({"pending"}),
                         account_id=ACCOUNT)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pending, self.inbox, clock=lambda: NOW_S))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        body = (FIXTURES / "ig_post.json").read_bytes()
        request = lambda: urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/webhook", data=body, method="POST",
            headers={"X-Hub-Signature-256": sign(body)}))
        log = io.StringIO()
        try:
            with contextlib.redirect_stderr(log):
                request(), request(), request()
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(log.getvalue().count(ME), 1)
        self.assertEqual(self.inbox.pending(), [])

    def test_verification_get(self):
        url = self.base + "?hub.mode=subscribe&hub.verify_token=tok&hub.challenge=42"
        self.assertEqual(urllib.request.urlopen(url).read(), b"42")

    def test_signed_post_is_queued_and_unsigned_is_refused(self):
        body = (FIXTURES / "ig_post.json").read_bytes()
        self.assertEqual(self.post(body, sign(body, "other")), 403)
        self.assertEqual(self.inbox.pending(), [])
        self.assertEqual(self.post(body, sign(body)), 200)
        self.assertEqual(len(self.inbox.pending()), 1)


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context(RUFLO_GH, RUFLO_HTTP)
        self.inbox = Inbox(Path(tempfile.mkdtemp()))
        self.inbox.enqueue(parse_shares(fixture("ig_post.json"), CONFIG, NOW_S)[0])
        self.sent = []
        self.slept = []
        self.runtime = Runtime(limits=Limits(self.inbox.root / "limits"), sleep=self.slept.append)

    def run_worker(self, send=None, now_s=NOW_S):
        return process_pending(self.inbox, self.ctx, CONFIG, send=send, now_s=now_s, runtime=self.runtime)

    def test_builds_bundle_from_caption_and_drafts_reply_without_sending(self):
        [result] = self.run_worker()
        bundle = json.loads((Path(result["case_dir"]) / "bundle.json").read_text())
        self.assertEqual(bundle["dm"]["kind"], "ig_post")
        self.assertNotIn("url", bundle["dm"])
        [message] = result["messages"]
        self.assertIn("ruvnet/ruflo", message["text"])
        self.assertFalse(message["sent"])
        self.assertFalse(any("npx" in " ".join(argv) for argv in self.ctx.runner.log))
        self.assertEqual(self.inbox.pending(), [])

    def test_send_is_called_only_when_given(self):
        self.run_worker(send=lambda recipient, text: self.sent.append((recipient, text)))
        self.assertEqual(self.sent[0][0], ME)

    def test_draft_made_earlier_is_sent_once_later(self):
        self.run_worker()
        record = lambda: self.run_worker(send=lambda r, t: self.sent.append(r))[0]
        self.assertTrue(record()["messages"][0]["sent"])
        record()
        self.assertEqual(len(self.sent), 1)

    def test_rate_limit_gives_up_for_good_and_old_drafts_are_not_sent(self):
        from hype_checker.v2.reply import SendRejected

        def fail(recipient, text):
            self.sent.append(text)
            raise SendRejected("HTTP 429")
        message = self.run_worker(send=fail)[0]["messages"][0]
        self.assertEqual((message["error"], message["attempted_at"], message["rejections"], message["gave_up"]),
                         ("HTTP 429", None, 3, True))
        self.assertEqual((len(self.sent), self.slept), (3, [30, 60]))
        self.run_worker(send=fail)
        self.assertEqual(len(self.sent), 3)
        late = WorkerTest()
        late.setUp()
        late.run_worker()
        [record] = late.run_worker(send=lambda r, t: late.sent.append(r), now_s=NOW_S + 25 * 3600)
        self.assertEqual((late.sent, record["messages"][0]["sent"]), ([], False))

    def test_unknown_outcome_is_never_resent(self):
        def lost(recipient, text):
            self.sent.append(recipient)
            raise TimeoutError()
        self.assertTrue(self.run_worker(send=lost)[0]["messages"][0]["error"].startswith("unknown outcome"))
        self.run_worker(send=lost)
        self.assertEqual(self.sent, [ME])

    def test_draft_is_not_sent_once_sender_leaves_the_allowlist(self):
        from dataclasses import replace
        self.run_worker()
        records = process_pending(self.inbox, self.ctx, replace(CONFIG, allowed_senders=frozenset()),
                                  send=lambda r, t: self.sent.append(r), now_s=NOW_S, runtime=self.runtime)
        self.assertEqual((self.sent, records[0]["messages"][0]["error"]), ([], "sender not allowed now"))

    def test_link_or_path_in_a_dm_is_never_downloaded_or_opened(self):
        for i, text in enumerate(["https://www.instagram.com/reel/abc/", "https://youtu.be/x", "/etc/hosts.mp4"]):
            payload = fixture("ig_post.json")
            payload["entry"][0]["messaging"][0]["message"] = {"mid": f"m{i}", "text": text}
            self.inbox.enqueue(parse_shares(payload, CONFIG, NOW_S)[0])
        records = self.run_worker()
        self.assertFalse(any(r.get("error") for r in records))
        self.assertFalse(any("watch.py" in " ".join(argv) for argv in self.ctx.runner.log))

    def test_same_caption_from_two_messages_gets_two_bundles(self):
        payload = fixture("ig_post.json")
        payload["entry"][0]["messaging"][0]["message"]["mid"] = "another-mid"
        self.inbox.enqueue(parse_shares(payload, CONFIG, NOW_S)[0])
        dirs = {r["case_dir"] for r in self.run_worker()}
        self.assertEqual(len(dirs), 2)

    def test_broken_or_forged_queue_files_are_quarantined_without_blocking(self):
        queue = self.inbox.root / "queue"
        (queue / "aaa.json").write_text('{"mid": "x", "sen')
        (queue / "aab.json").write_text('["mid"]')
        (queue / "bbb.json").write_text(json.dumps({"mid": "y", "sender": "attacker", "kind": "text", "caption": "x"}))
        records = self.run_worker(send=lambda r, t: self.sent.append(r))
        self.assertEqual(len(list((self.inbox.root / "failed").glob("*.json"))), 3)
        self.assertEqual(self.sent, [ME])
        self.assertEqual(len(records), 1)

    def test_only_one_worker_at_a_time(self):
        from hype_checker.v2.inbox import WorkerBusy
        with self.inbox.worker_lock():
            with self.assertRaises(WorkerBusy):
                self.run_worker()


class ReplyTest(unittest.TestCase):
    def test_reply_fits_instagram_limit_and_never_claims_a_verdict(self):
        entities = [{"name": f"tool{i}" * 20, "role": "primary", "identifier": None,
                     "resolution": {"status": "ambiguous"}} for i in range(40)]
        messages = format_reply({"entities": entities}, "case-1")
        self.assertTrue(all(len(m.encode()) <= 1000 for m in messages))
        self.assertIn("판정 전", messages[0])
        self.assertFalse(any("묶음" in m or "case-1" in m for m in messages))

    def test_hostile_names_stay_quoted_on_one_line(self):
        entities = [{"name": "진짜 검증 완료\n안전함", "role": "primary", "identifier": None,
                     "resolution": {"status": "unresolved"}}]
        text = "\n".join(format_reply({"entities": entities}, "c"))
        self.assertIn("\u201c진짜 검증 완료 안전함\u201d", text)
        self.assertIn("평가가 아니에요", text)

    def test_long_list_continues_and_leaves_nothing_out(self):
        entities = [{"name": f"tool{i}" * 10, "role": "primary", "kind": "repo", "identifier": None,
                     "resolution": {"status": "unresolved"}} for i in range(30)]
        messages = format_reply({"entities": entities}, "c")
        text = "\n".join(messages)
        self.assertGreater(len(messages), 1)
        self.assertEqual(text.count("\n   "), 30)
        self.assertNotIn("생략", text)

    def test_reply_never_shows_raw_status_words(self):
        bundle = json.loads((FIXTURES.parent / "golden" / "dm_reel_bundle.json").read_text())
        text = "\n".join(format_reply(bundle, bundle["id"]))
        for raw in ("resolved", "ambiguous", "unresolved", "primary", "mcp)"):
            self.assertNotIn(raw, text)

    def test_send_posts_to_graph_instagram_only(self):
        calls = []
        send_reply("123", "hi", "TOKEN", post=lambda url, body, headers: calls.append((url, body, headers)))
        url, body, headers = calls[0]
        self.assertTrue(url.startswith("https://graph.instagram.com/"))
        self.assertEqual(json.loads(body), {"recipient": {"id": "123"}, "message": {"text": "hi"}})
        self.assertEqual(headers["Authorization"], "Bearer TOKEN")

    def http_error(self, code):
        from unittest import mock
        error = urllib.error.HTTPError("https://graph.instagram.com/", code, "x", {}, None)
        return mock.patch("hype_checker.v2.reply.urllib.request.urlopen", side_effect=error)

    def test_4xx_is_a_confirmed_rejection(self):
        from hype_checker.v2.reply import SendRejected
        for code in (400, 403, 429):
            with self.http_error(code), self.assertRaises(SendRejected) as caught:
                send_reply("123", "hi", "TOKEN")
            self.assertEqual(str(caught.exception), f"HTTP {code}")

    def test_5xx_is_an_unknown_outcome_not_a_rejection(self):
        from hype_checker.v2.reply import SendRejected, SendUnknown
        with self.http_error(502), self.assertRaises(SendUnknown) as caught:
            send_reply("123", "hi", "TOKEN")
        self.assertNotIsInstance(caught.exception, SendRejected)


def _verdict_bundle(tools, skipped=0, extraction="ok"):
    return {"id": "c1", "extraction": {"status": extraction},
            "entities": [{"name": t["name"], "role": "primary", "kind": "mcp"} for t in tools],
            "verdict": {"tools": tools, "skipped_tools": skipped, "status": "ok"}}


def _tool(name, grade="🤔", reasons=("공식 위치 하나를 찾았어요",)):
    return {"name": name, "identifier": None, "grade": grade, "reasons": list(reasons), "claims": []}


class VerdictReplyTest(unittest.TestCase):
    def test_six_long_tools_all_shown_across_detail_messages(self):
        tools = [_tool(f"도구{i}" * 8, reasons=["사유 " * 20] * 3) for i in range(6)]
        messages = format_verdict_replies(_verdict_bundle(tools), "c1")
        self.assertLessEqual(len(messages), 6)
        for message in messages:
            self.assertLessEqual(len(message.encode()), 1000)
        self.assertIn("근거는 다음 메시지로 보낼게요.", messages[0])
        self.assertTrue(messages[-1].endswith("정정'이라고 답장해 주세요."))
        details = "\n".join(messages[1:])
        self.assertTrue(all(f"{n}. 도구{n - 1}" in details for n in range(1, 7)))
        self.assertNotIn("생략", details)

    def test_detail_block_is_friendly_and_links_the_repo(self):
        tool = {**_tool("agent-desktop"), "identifier": "vercel-labs/agent-desktop", "score": 4, "stars": 4312,
                "pushed_days": 3, "good": ["공식 위치 하나를 찾았어요"], "watch": ["스타가 며칠 사이에 몰렸어요"],
                "score_parts": [(1, "activity"), (-1, "warnings"), (-1, "stars")],
                "alternatives": [{"identifier": "big/desktop-agent", "stars": 28000}]}
        detail = format_verdict_replies(_verdict_bundle([tool]), "c1")[1]
        self.assertIn("1. agent-desktop — 추천 4/10 · 🤔\n🔗 github.com/vercel-labs/agent-desktop\n"
                      "⭐ 4,312 · 최근 30일 안에 업데이트", detail)
        self.assertIn("👍 좋은 점\n · 공식 위치 하나를 찾았어요", detail)
        self.assertIn("⚠️ 살펴볼 점\n · 스타가 며칠 사이에 몰렸어요", detail)
        self.assertIn("💡 같은 일을 하는 더 유명한 도구\n · github.com/big/desktop-agent ⭐ 28,000", detail)
        self.assertIn("📊 기본 5점 · 업데이트 +1 · 주의 신호 -1 · 스타 규모 -1 → 4점", detail)

    def test_extraction_failure_differs_from_no_tools(self):
        failed = format_verdict_replies(_verdict_bundle([], extraction="failed"), "c1")
        skipped = format_verdict_replies(_verdict_bundle([], extraction="skipped"), "c1")
        empty = format_verdict_replies(_verdict_bundle([]), "c1")
        self.assertEqual(failed, ["지금은 처리하지 못했어요. 잠시 뒤 다시 보내 주세요."])
        self.assertEqual(skipped, failed)
        self.assertEqual(empty, ["게시물에서 소개하는 도구 이름을 찾지 못했어요."])

    def test_reasons_come_only_from_fact_texts(self):
        tool = _tool("Refero", reasons=["캡션의 '유료 플랜' → 일치"])
        tool["claims"] = [{"claim": "캡션 문장 전체가 여기 그대로 들어가면 안 돼요 정말로", "result": "일치", "quote": "q"}]
        messages = format_verdict_replies(_verdict_bundle([tool]), "c1")
        self.assertIn(" · 캡션의 '유료 플랜' → 일치", messages[1])
        self.assertNotIn("캡션 문장 전체", "\n".join(messages))

    def test_every_owner_line_is_sent_and_the_summary_continues(self):
        tools = [_tool(f"도구{i}" * 8, reasons=["사유"]) for i in range(6)]
        owner = [f"내 워크플로우: {'긴이름' * 30}{n} → 후보" for n in range(6)] + ["카드 초안: out/c1/card.md"]
        messages = format_verdict_replies(_verdict_bundle(tools), "c1", owner_lines=owner)
        self.assertTrue(all(len(m.encode()) <= 1000 for m in messages))
        self.assertIn("카드 초안: out/c1/card.md", "\n".join(messages))
        self.assertEqual("\n".join(messages).count("내 워크플로우:"), 6)

    def test_footer_notices_exact(self):
        messages = format_verdict_replies(_verdict_bundle([_tool("A", "👍")]), "c1")
        self.assertTrue(messages[0].startswith("🔎 Hyper Checker 자동 확인 (게시물 기준)\n"))
        self.assertTrue(messages[1].endswith(
            "\n\n자동 확인 결과예요. 기능이 좋은지는 직접 써 봐야 알아요.\n설치 전 공식 페이지를 꼭 확인하세요."
            "\n틀렸다면 '정정'이라고 답장해 주세요."))




    def test_single_oversized_line_is_byte_truncated_safely(self):
        tools = [_tool("T0", reasons=["가" * 400])]
        text = format_verdict_replies(_verdict_bundle(tools), "c1")[1]
        self.assertLessEqual(len(text.encode()), 1000)
        self.assertIn(" · 가", text)
        self.assertTrue(text.endswith("정정'이라고 답장해 주세요."))

    def test_summary_continues_instead_of_collapsing_tools(self):
        tools = [_tool("가" * 60) for _ in range(6)]
        messages = format_verdict_replies(_verdict_bundle(tools), "c1")
        self.assertTrue(all(len(m.encode()) <= 1000 for m in messages))
        text = "\n".join(messages)
        for number in range(1, 7):
            self.assertIn(f"{number}. \u201c{'가' * 60}\u201d", text)
        self.assertNotIn("생략", text)
        self.assertNotIn("판정하지 않았어요", text)

    def test_tools_are_kept_when_only_the_footer_does_not_fit(self):
        """Real reel 2026-11-02: five tools fit one message, the footer did not, and two tools were dropped."""
        tools = [{**_tool(f"도구{n}"), "identifier": f"https://mcp{n}.example.com/mcp",
                  "watch": ["원격 MCP 주소라 코드는 측정하지 않았어요"], "good": ["공식 위치 하나를 찾았어요"]} for n in range(5)]
        details = format_verdict_replies(_verdict_bundle(tools), "c1")[1:]
        text = "\n\n".join(details)
        for number in range(5):
            self.assertIn(f"{number + 1}. 도구{number} ", text)
        self.assertNotIn("생략", text)
        self.assertEqual(text.count("틀렸다면 '정정'"), 1)
        self.assertTrue(details[-1].endswith("정정'이라고 답장해 주세요."))

    def test_skipped_line_follows_every_tool(self):
        tools = [_tool("가" * 60) for _ in range(6)]
        text = "\n".join(format_verdict_replies(_verdict_bundle(tools, skipped=2), "c1"))
        self.assertLess(text.index("6. \u201c"), text.index("…외 2개는 판정하지 않았어요."))

    def test_owner_lines_absent_for_guest_and_present_for_owner(self):
        bundle = _verdict_bundle([_tool("A")])
        guest = format_verdict_replies(bundle, "c1")
        owner = format_verdict_replies(bundle, "c1", owner_lines=["내 워크플로우: 후보"])
        self.assertNotIn("내 워크플로우", "\n".join(guest))
        self.assertLess(owner[0].index("내 워크플로우"), owner[0].index("근거는 다음"))

    def test_long_owner_lines_fit_each_message(self):
        messages = format_verdict_replies(_verdict_bundle([_tool("A")]), "c1", owner_lines=["가" * 500] * 10)
        self.assertTrue(all(len(m.encode()) <= 1000 for m in messages))
        self.assertIn("근거는 다음 메시지로 보낼게요.", "\n".join(messages))

    def test_skipped_tools_and_no_check_marks(self):
        messages = format_verdict_replies(_verdict_bundle([_tool("A", "👎")], skipped=3), "c1")
        self.assertIn("…외 3개는 판정하지 않았어요.", messages[0])
        for message in messages:
            self.assertNotIn("✅", message)
            self.assertNotIn("❌", message)

    def test_summary_line_format(self):
        messages = format_verdict_replies(_verdict_bundle([_tool("A\nB", "❔")]), "c1")
        self.assertIn("도구 1개를 확인했어요.", messages[0])
        self.assertIn("1. \u201cA B\u201d · MCP 서버\n   추천 판단 보류 · ❔ 판정 보류", messages[0])

    def test_fallback_reply_says_measurement_only(self):
        [text] = format_reply({"entities": []}, "c1", fallback=True)
        self.assertIn("이번엔 판정 없이 측정만 보내요", text)


if __name__ == "__main__":
    unittest.main()


class ConfigTest(unittest.TestCase):
    def write(self, text, mode=0o600):
        path = Path(tempfile.mkdtemp()) / "v2.env"
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_loads_lists_and_optional_token(self):
        from hype_checker.v2.config import load_config
        config, token = load_config(self.write("IG_VERIFY_TOKEN=t\nIG_APP_SECRETS=a, b\nIG_ALLOWED_SENDERS=1,2\n"))
        self.assertEqual((config.app_secrets, config.allowed_senders, token), (("a", "b"), frozenset({"1", "2"}), None))

    def test_refuses_readable_file_and_missing_keys(self):
        from hype_checker.v2.config import ConfigError, load_config
        with self.assertRaises(ConfigError):
            load_config(self.write("IG_VERIFY_TOKEN=t\nIG_APP_SECRETS=a\nIG_ALLOWED_SENDERS=1\n", mode=0o644))
        with self.assertRaises(ConfigError):
            load_config(self.write("IG_VERIFY_TOKEN=t\n"))


OWNER = "900000000000009"
LIMIT_TEXT = "오늘 확인 한도에 도달했어요. 한국 시간 오전 9시 이후에 다시 보내 주세요."
WINDOW_TEXT = "24시간 창이 지나 보낼 수 없다"


def text_share(mid, text, sender=ME, config=CONFIG):
    payload = fixture("ig_post.json")
    payload["entry"][0]["messaging"][0]["sender"]["id"] = sender
    payload["entry"][0]["messaging"][0]["message"] = {"mid": mid, "text": text}
    return parse_shares(payload, config, NOW_S)


def fake_verdict(cost_usd):
    repro = {"model": "m", "rules_version": "r", "claims_prompt_sha": "s", "tokens_in": 0, "tokens_out": 0,
             "cost_usd": cost_usd, "calls": 0}
    return {"tools": [], "skipped_tools": 0, "status": "ok", "repro": repro}


class ModeAndDisputeParseTest(unittest.TestCase):
    def test_exact_correction_word_is_a_dispute(self):
        [share] = text_share("d1", " 정정\n")
        self.assertEqual(share.kind, "dispute")
        [other] = text_share("d2", "정정해 주세요")
        self.assertEqual(other.kind, "text")

    def test_owner_is_implicitly_allowed(self):
        from dataclasses import replace
        config = replace(CONFIG, owner_sender=OWNER)
        self.assertEqual(text_share("o1", "hi", sender=OWNER, config=config)[0].sender, OWNER)
        self.assertEqual(text_share("o2", "hi", sender=OWNER), [])


class WorkerV3Test(unittest.TestCase):
    """Task 12: per-message send state, process-then-send loop, limits, disputes, HC_VERDICT switch."""

    def setUp(self):
        from unittest import mock
        self.mock = mock
        self.ctx = make_context(RUFLO_GH, RUFLO_HTTP)
        self.inbox = Inbox(Path(tempfile.mkdtemp()))
        self.inbox.enqueue(parse_shares(fixture("ig_post.json"), CONFIG, NOW_S)[0])
        self.sent, self.slept, self.log = [], [], []
        self.runtime = Runtime(limits=Limits(self.inbox.root / "limits"), sleep=self.slept.append)

    def send(self, recipient, text):
        self.log.append(("send", text))
        self.sent.append(text)

    def run_worker(self, send=None, now_s=NOW_S, config=CONFIG):
        return process_pending(self.inbox, self.ctx, config, send=send, now_s=now_s, runtime=self.runtime)

    def verdict_on(self, **runtime):
        from dataclasses import replace
        self.runtime = replace(self.runtime, chat=lambda messages: "", model="m", prices=(3.0, 15.0), **runtime)
        return replace(CONFIG, verdict="on")

    def patch_two_messages(self):
        judge = self.mock.patch("hype_checker.v2.worker.judge_bundle", return_value=fake_verdict(0.01))
        replies = self.mock.patch("hype_checker.v2.worker.format_verdict_replies", return_value=["one", "two"])
        judge.start(), replies.start()
        self.addCleanup(judge.stop)
        self.addCleanup(replies.stop)

    def test_second_message_retried_alone(self):
        from hype_checker.v2.reply import SendRejected
        config = self.verdict_on()
        self.patch_two_messages()

        def second_busy_once(recipient, text):
            if text == "two" and not self.slept:
                raise SendRejected("HTTP 429")
            self.sent.append(text)
        [record] = self.run_worker(send=second_busy_once, config=config)
        self.assertEqual((self.sent, self.slept), (["one", "two"], [30]))
        self.assertEqual([m["sent"] for m in record["messages"]], [True, True])
        bundle = json.loads((Path(record["case_dir"]) / "bundle.json").read_text())
        self.assertIn("verdict", bundle)

    def test_message_two_waits_for_message_one(self):
        from hype_checker.v2.reply import SendRejected
        config = self.verdict_on()
        self.patch_two_messages()

        def first_fails(recipient, text):
            raise SendRejected("HTTP 400")
        [record] = self.run_worker(send=first_fails, config=config)
        self.assertEqual(record["messages"][1], {"text": "two", "sent": False, "attempted_at": None, "error": None,
                                                 "rejections": 0, "gave_up": False})
        self.assertTrue(record["messages"][0]["gave_up"])
        self.run_worker(send=self.send, config=config)
        self.assertEqual(self.sent, [])

    def test_unsent_second_message_is_retried_alone_on_a_later_pass(self):
        config = self.verdict_on()
        self.patch_two_messages()
        self.run_worker(config=config)
        [(path, record)] = self.inbox.done()
        record["messages"][0]["sent"] = True
        self.inbox.update(path, messages=record["messages"])
        [record] = self.run_worker(send=self.send, config=config)
        self.assertEqual(self.sent, ["two"])
        self.assertEqual([m["sent"] for m in record["messages"]], [True, True])

    def test_403_gives_up_without_retry(self):
        from hype_checker.v2.reply import SendRejected
        calls = []

        def forbidden(recipient, text):
            calls.append(text)
            raise SendRejected("HTTP 403")
        [record] = self.run_worker(send=forbidden)
        self.run_worker(send=forbidden)
        self.assertEqual((len(calls), self.slept, record["messages"][0]["gave_up"]), (1, [], True))

    def test_window_rechecked_before_each_backoff_retry(self):
        from hype_checker.v2.reply import SendRejected
        from hype_checker.v2.sending import send_messages
        self.run_worker()
        [(path, record)] = self.inbox.done()
        now = [NOW_S]
        calls = []

        def busy(recipient, text):
            calls.append(text)
            raise SendRejected("HTTP 429")

        def sleep(seconds):
            now[0] += 25 * 3600
        record = send_messages(self.inbox, path, record, busy, CONFIG, lambda: now[0], sleep)
        self.assertEqual(len(calls), 1)
        self.assertEqual((record["messages"][0]["sent"], record["messages"][0]["error"]), (False, WINDOW_TEXT))
        self.assertEqual(self.inbox.read(path)["messages"][0]["error"], WINDOW_TEXT)

    def test_server_error_is_unknown_outcome_never_resent(self):
        from hype_checker.v2.reply import SendUnknown
        calls = []

        def broken(recipient, text):
            calls.append(text)
            raise SendUnknown("HTTP 502")
        [record] = self.run_worker(send=broken)
        self.run_worker(send=broken)
        self.assertEqual(len(calls), 1)
        self.assertTrue(record["messages"][0]["attempted_at"])

    def test_window_expired_not_sent(self):
        self.run_worker()
        late = NOW_S + 25 * 3600
        [record] = self.run_worker(send=self.send, now_s=late)
        [record] = self.run_worker(send=self.send, now_s=late)
        self.assertEqual(self.sent, [])
        self.assertEqual((record["messages"][0]["sent"], record["messages"][0]["error"]), (False, WINDOW_TEXT))

    def test_429_retries_then_succeeds_without_resending_earlier(self):
        from hype_checker.v2.reply import SendRejected
        calls = []

        def busy_once(recipient, text):
            calls.append(text)
            if len(calls) == 1:
                raise SendRejected("HTTP 429")
        [record] = self.run_worker(send=busy_once)
        self.assertEqual((len(calls), self.slept, record["messages"][0]["sent"]), (2, [30], True))

    def test_backlog_does_not_delay_first_send(self):
        from hype_checker.v2 import worker
        payload = fixture("ig_post.json")
        payload["entry"][0]["messaging"][0]["message"]["mid"] = "second-mid"
        self.inbox.enqueue(parse_shares(payload, CONFIG, NOW_S)[0])
        real_check = worker.check

        def logged_check(*args, **kwargs):
            self.log.append(("check", kwargs.get("tag")))
            return real_check(*args, **kwargs)
        passes = []
        with self.mock.patch("hype_checker.v2.worker.check", side_effect=logged_check):
            worker.run_loop(self.inbox, self.ctx, CONFIG, self.send, stop=lambda: passes.append(1) or len(passes) > 1,
                            runtime=self.runtime, clock=lambda: NOW_S)
        self.assertEqual([kind for kind, _ in self.log], ["check", "send", "check", "send"])
        self.assertEqual(self.inbox.pending(), [])

    def test_loop_sleeps_between_passes_and_retries_unsent(self):
        from hype_checker.v2 import worker
        self.run_worker()
        passes = []
        worker.run_loop(self.inbox, self.ctx, CONFIG, self.send, stop=lambda: passes.append(1) or len(passes) > 2,
                        runtime=self.runtime, clock=lambda: NOW_S)
        self.assertEqual((len(self.sent), self.slept), (1, [worker.POLL_S, worker.POLL_S]))

    def test_limit_reached_skips_measurement(self):
        from dataclasses import replace
        self.runtime = replace(self.runtime, limits=Limits(self.inbox.root / "limits", per_sender=0))
        with self.mock.patch("hype_checker.v2.worker.check") as check:
            [record] = self.run_worker(send=self.send)
        self.assertEqual(check.call_count, 0)
        self.assertEqual(self.sent, [LIMIT_TEXT])
        self.assertNotIn("case_dir", record)

    def test_limit_notice_sent_once_then_silent(self):
        from dataclasses import replace
        self.runtime = replace(self.runtime, limits=Limits(self.inbox.root / "limits", per_sender=0))
        self.inbox.enqueue(text_share("second", "Ruflo")[0])
        with self.mock.patch("hype_checker.v2.worker.check") as check:
            records = self.run_worker(send=self.send)
            self.inbox.enqueue(text_share("third", "Ruflo")[0])
            records = self.run_worker(send=self.send)
        self.assertEqual(check.call_count, 0)
        self.assertEqual(self.sent, [LIMIT_TEXT])
        self.assertEqual(len(records), 3)
        self.assertTrue(all(r["limited"] for r in records))
        self.assertEqual(self.inbox.pending(), [])

    def test_malformed_day_file_does_not_crash_the_loop(self):
        from hype_checker.v2 import worker
        day = time.strftime("%Y-%m-%d", time.gmtime(NOW_S))
        (self.inbox.root / "limits").mkdir(exist_ok=True)
        (self.inbox.root / "limits" / f"{day}.json").write_text(json.dumps({"senders": {}}))
        with self.mock.patch("hype_checker.v2.worker.check") as check:
            worker.run_loop(self.inbox, self.ctx, CONFIG, self.send, stop=lambda: bool(self.slept),
                            runtime=self.runtime, clock=lambda: NOW_S)
        self.assertEqual(check.call_count, 0)
        self.assertEqual(self.inbox.pending(), [])

    def test_each_item_counts_against_the_limit_even_on_failure(self):
        with self.mock.patch("hype_checker.v2.worker.check", side_effect=RuntimeError("boom")):
            [record] = self.run_worker()
        self.assertEqual(record["error"], "RuntimeError")
        day = time.strftime("%Y-%m-%d", time.gmtime(NOW_S))
        counts = json.loads((self.inbox.root / "limits" / f"{day}.json").read_text())
        self.assertEqual((counts["total"], counts["senders"]), (1, {ME: 1}))

    def test_dispute_saved_without_reply(self):
        import stat
        self.run_worker(send=self.send)
        first_id = self.inbox.done()[0][0].stem
        [share] = text_share("dispute-mid", "정정")
        self.inbox.enqueue(share)
        records = self.run_worker(send=self.send)
        self.assertEqual(len(self.sent), 1)
        path = self.inbox.root / "disputes" / "dispute-mid.json"
        saved = json.loads(path.read_text())
        self.assertEqual((saved["mid"], saved["sender"], saved["previous"]), ("dispute-mid", ME, first_id))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.inbox.pending(), [])
        self.assertTrue(all(not r.get("messages") for r in records if r["share"]["kind"] == "dispute"))

    def test_dispute_with_nothing_before_has_no_previous(self):
        self.inbox = Inbox(Path(tempfile.mkdtemp()))
        self.inbox.enqueue(text_share("lonely", "정정")[0])
        self.run_worker(send=self.send)
        saved = json.loads((self.inbox.root / "disputes" / "lonely.json").read_text())
        self.assertIsNone(saved["previous"])

    def test_verdict_off_uses_v2_reply(self):
        with self.mock.patch("hype_checker.v2.worker.judge_bundle", side_effect=AssertionError("judged")):
            [record] = self.run_worker()
        bundle = json.loads((Path(record["case_dir"]) / "bundle.json").read_text())
        self.assertEqual([m["text"] for m in record["messages"]], format_reply(bundle, Path(record["case_dir"]).name))

    def test_claude_unavailable_falls_back(self):
        from hype_checker.llm import ClaudeUnavailable
        config = self.verdict_on()
        with self.mock.patch("hype_checker.v2.worker.judge_bundle", side_effect=ClaudeUnavailable("down")):
            [record] = self.run_worker(config=config)
        bundle = json.loads((Path(record["case_dir"]) / "bundle.json").read_text())
        expected = format_reply(bundle, Path(record["case_dir"]).name, fallback=True)[0]
        self.assertEqual([m["text"] for m in record["messages"]], [expected])

    def test_missing_price_falls_back_without_calling_claude(self):
        from dataclasses import replace
        config = self.verdict_on()
        self.runtime = replace(self.runtime, prices=None)
        with self.mock.patch("hype_checker.v2.worker.judge_bundle", side_effect=AssertionError("judged")):
            [record] = self.run_worker(config=config)
        self.assertIn("이번엔 판정 없이 측정만 보내요.", record["messages"][0]["text"])

    def spending_judge(self, then=None):
        def judge(bundle, sources, chat, budget, now, model=None, prices=None, cache=None):
            self.assertEqual(prices, (3.0, 15.0))
            budget.spend(1, 100_000, 0)
            if then:
                raise then
            return fake_verdict(0.3)
        return judge

    def day_counts(self):
        day = time.strftime("%Y-%m-%d", time.gmtime(NOW_S))
        return json.loads((self.inbox.root / "limits" / f"{day}.json").read_text())

    def test_prices_present_records_cost_in_limits(self):
        config = self.verdict_on()
        with self.mock.patch("hype_checker.v2.worker.judge_bundle", side_effect=self.spending_judge()):
            self.run_worker(config=config)
        self.assertAlmostEqual(self.day_counts()["usd"], 0.3)

    def test_cost_counted_when_claude_fails_or_later_step_breaks(self):
        from hype_checker.llm import ClaudeUnavailable
        config = self.verdict_on()
        with self.mock.patch("hype_checker.v2.worker.judge_bundle",
                             side_effect=self.spending_judge(ClaudeUnavailable("down"))):
            self.run_worker(config=config)
        self.inbox.enqueue(text_share("second", "Ruflo")[0])
        with self.mock.patch("hype_checker.v2.worker.judge_bundle", side_effect=self.spending_judge()), \
                self.mock.patch("hype_checker.v2.worker.format_verdict_replies", side_effect=RuntimeError("x")):
            [_, broken] = sorted(self.run_worker(config=config), key=lambda r: "error" in r)
        self.assertEqual(broken["error"], "RuntimeError")
        self.assertAlmostEqual(self.day_counts()["usd"], 0.6)

    def test_failed_verdict_falls_back_to_measurement_only(self):
        config = self.verdict_on()
        errored = {"name": "Ruflo", "identifier": None, "grade": "❔", "reason_ids": ["error"],
                   "reasons": ["확인 중 오류가 났어요"], "claims": [], "claims_status": "not_run",
                   "budget_cut": False, "error": "RuntimeError"}
        failed = dict(fake_verdict(0.0), status="failed", tools=[errored])
        with self.mock.patch("hype_checker.v2.worker.judge_bundle", return_value=failed):
            [record] = self.run_worker(config=config)
        bundle = json.loads((Path(record["case_dir"]) / "bundle.json").read_text())
        expected = format_reply(bundle, Path(record["case_dir"]).name, fallback=True)[0]
        self.assertEqual([m["text"] for m in record["messages"]], [expected])

    def broken_extraction(self):
        from hype_checker.v2 import worker
        original = worker._attach_dm

        def attach(case_dir, share, fetch_error=None):
            bundle = original(case_dir, share, fetch_error)
            bundle["extraction"] = {"status": "failed"}
            return bundle
        return self.mock.patch("hype_checker.v2.worker._attach_dm", side_effect=attach)

    def test_fallback_keeps_extraction_failure_message(self):
        from dataclasses import replace
        from hype_checker.llm import ClaudeUnavailable
        with self.broken_extraction(), \
                self.mock.patch("hype_checker.v2.worker.judge_bundle", side_effect=AssertionError("judged")):
            [record] = self.run_worker(config=replace(CONFIG, verdict="on"))
        self.assertEqual([m["text"] for m in record["messages"]], ["지금은 처리하지 못했어요. 잠시 뒤 다시 보내 주세요."])
        self.inbox.enqueue(text_share("second", "Ruflo")[0])
        config = self.verdict_on()
        with self.broken_extraction(), \
                self.mock.patch("hype_checker.v2.worker.judge_bundle", side_effect=ClaudeUnavailable("down")):
            records = self.run_worker(config=config)
        second = next(r for r in records if r["share"]["mid"] == "second")
        self.assertEqual([m["text"] for m in second["messages"]], ["지금은 처리하지 못했어요. 잠시 뒤 다시 보내 주세요."])

    def test_verdict_on_without_chat_falls_back(self):
        from dataclasses import replace
        with self.mock.patch("hype_checker.v2.worker.judge_bundle", side_effect=AssertionError("judged")):
            [record] = self.run_worker(config=replace(CONFIG, verdict="on"))
        self.assertIn("이번엔 판정 없이 측정만 보내요.", record["messages"][0]["text"])

    def test_owner_lines_only_for_owner(self):
        from dataclasses import replace
        asked = []
        config = replace(self.verdict_on(owner_lines_for=lambda case_dir, budget: asked.append(budget) or ["owner"]),
                         owner_sender=OWNER)
        with self.mock.patch("hype_checker.v2.worker.judge_bundle", return_value=fake_verdict(None)), \
                self.mock.patch("hype_checker.v2.worker.format_verdict_replies", return_value=["x"]) as replies:
            [guest] = self.run_worker(config=config)
            self.assertEqual((asked, replies.call_args.kwargs.get("owner_lines")), ([], None))
            self.inbox.enqueue(text_share("owner-mid", "Ruflo 써봐", sender=OWNER, config=config)[0])
            self.run_worker(config=config)
        self.assertEqual(len(asked), 1)
        self.assertEqual(replies.call_args.kwargs.get("owner_lines"), ["owner"])
        self.assertEqual(guest["mode"], "guest")
        from hype_checker.verdict import Budget
        self.assertIsInstance(asked[0], Budget)

    def owner_config(self, hook):
        from dataclasses import replace
        config = replace(self.verdict_on(owner_lines_for=hook), owner_sender=OWNER)
        self.inbox = Inbox(Path(tempfile.mkdtemp()))
        self.inbox.enqueue(text_share("owner-mid", "Ruflo 써봐", sender=OWNER, config=config)[0])
        self.runtime = replace(self.runtime, limits=Limits(self.inbox.root / "limits"))
        return config

    def test_owner_hook_shares_the_item_budget_and_daily_cost(self):
        def hook(case_dir, budget):
            budget.spend(2, 100000, 0)
            return ["카드 초안: x"]
        config = self.owner_config(hook)
        with self.mock.patch("hype_checker.v2.worker.judge_bundle", return_value=fake_verdict(0.0)):
            self.run_worker(config=config)
        self.assertAlmostEqual(self.day_counts()["usd"], 0.3)

    def test_owner_hook_failure_still_sends_verdict_reply(self):
        import contextlib
        import io

        def hook(case_dir, budget):
            raise RuntimeError("CANARY-7f3a in a traceback")
        config = self.owner_config(hook)
        err = io.StringIO()
        with self.mock.patch("hype_checker.v2.worker.judge_bundle", return_value=fake_verdict(0.0)), \
                self.mock.patch("hype_checker.v2.worker.format_verdict_replies", return_value=["v"]) as replies, \
                contextlib.redirect_stderr(err):
            [record] = self.run_worker(config=config)
        self.assertEqual([m["text"] for m in record["messages"]], ["v"])
        self.assertIsNone(replies.call_args.kwargs.get("owner_lines"))
        self.assertIn("RuntimeError", err.getvalue())
        self.assertNotIn("CANARY", err.getvalue())

    def test_measurement_only_fallback_skips_owner_mode(self):
        from hype_checker.llm import ClaudeUnavailable
        asked = []
        config = self.owner_config(lambda case_dir, budget: asked.append(case_dir) or ["x"])
        with self.mock.patch("hype_checker.v2.worker.judge_bundle", side_effect=ClaudeUnavailable("down")):
            [record] = self.run_worker(config=config)
        self.assertEqual(asked, [])
        self.assertIn("이번엔 판정 없이 측정만 보내요.", record["messages"][0]["text"])

    def test_legacy_record_with_reply_is_sent_as_one_message(self):
        self.run_worker()
        [(path, record)] = self.inbox.done()
        legacy = {k: v for k, v in record.items() if k != "messages"}
        legacy.update(reply="legacy text", sent=False)
        path.write_text(json.dumps(legacy, ensure_ascii=False))
        [record] = self.run_worker(send=self.send)
        self.assertEqual(self.sent, ["legacy text"])
        self.assertTrue(record["messages"][0]["sent"])

    def test_legacy_attempted_record_is_never_resent(self):
        self.run_worker()
        [(path, record)] = self.inbox.done()
        legacy = {k: v for k, v in record.items() if k != "messages"}
        legacy.update(reply="legacy text", sent=False, send_attempted_at=NOW_S)
        path.write_text(json.dumps(legacy, ensure_ascii=False))
        self.run_worker(send=self.send)
        self.assertEqual(self.sent, [])


class ConfigV3Test(unittest.TestCase):
    def test_owner_and_verdict_switch(self):
        from hype_checker.v2.config import load_config
        path = Path(tempfile.mkdtemp()) / "v2.env"
        path.write_text("IG_VERIFY_TOKEN=t\nIG_APP_SECRETS=a\nIG_ALLOWED_SENDERS=1\nIG_OWNER_SENDER=9\nHC_VERDICT=on\n")
        path.chmod(0o600)
        config, _ = load_config(path)
        self.assertEqual((config.owner_sender, config.verdict), ("9", "on"))
        path.write_text("IG_VERIFY_TOKEN=t\nIG_APP_SECRETS=a\nIG_ALLOWED_SENDERS=1\n")
        config, _ = load_config(path)
        self.assertEqual((config.owner_sender, config.verdict), (None, "off"))

    def test_verdict_chat_needs_switch_model_and_key(self):
        from dataclasses import replace
        from unittest import mock
        from hype_checker import cli
        self.assertIsNone(cli._verdict_chat(CONFIG))
        on = replace(CONFIG, verdict="on")
        import contextlib, io
        warning = io.StringIO()
        with mock.patch("hype_checker.v2.config.load_claude_settings", return_value=None), \
                contextlib.redirect_stderr(warning):
            self.assertIsNone(cli._verdict_chat(on))
        self.assertIn("측정만 보낸다", warning.getvalue())
        settings = {"model": "m", "prices": (3.0, 15.0), "api_key": "k"}
        with mock.patch("hype_checker.v2.config.load_claude_settings", return_value=settings):
            self.assertEqual((cli._verdict_chat(on)["model"], cli._verdict_chat(on)["prices"]), ("m", (3.0, 15.0)))

    def test_claude_settings_fail_closed_without_any_price(self):
        import os
        from unittest import mock
        from hype_checker.v2.config import load_claude_settings
        path = Path(tempfile.mkdtemp()) / "v2.env"
        base = "ANTHROPIC_API_KEY=k\nHC_CLAUDE_MODEL=m\nHC_PRICE_IN_PER_MTOK=3\n"
        path.write_text(base)
        path.chmod(0o600)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HC_")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(load_claude_settings(path))
            path.write_text(base + "HC_PRICE_OUT_PER_MTOK=15\n")
            self.assertEqual(load_claude_settings(path), {"model": "m", "prices": (3.0, 15.0), "api_key": "k"})
