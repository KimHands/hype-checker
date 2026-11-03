import io
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from hype_checker.pipeline import check
from hype_checker.v2.inbox import Inbox
from hype_checker.v2.limits import Limits
from hype_checker.v2.media import MediaRefused, _MetaOnlyRedirects, fetch_reel, is_media_url
from hype_checker.v2.webhook import parse_shares
from hype_checker.v2.worker import Runtime, process_pending
from tests.test_pipeline import RUFLO_GH, RUFLO_HTTP, CannedRunner, make_context
from tests.test_v2 import CONFIG, NOW_S, fixture

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16
VIDEO_CONFIG = replace(CONFIG, video="on")
PAGE_URL = "https://www.instagram.com/reel/DQabc123/"
REEL_URL = "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=18000000000000002&signature=SIG"
SCREEN_TEXT = "github.com/ruvnet/ruflo"


def watch_report(frames, transcript="[00:00] 오늘 소개할 도구는 이거예요"):
    lines = "\n".join(f"- `{path}` (t=00:0{i}, reason=keyframe)" for i, path in enumerate(frames))
    return (f"# watch: video report\n\n- **Title:** reel.mp4\n\n## Frames\n\n{lines}\n\n## Transcript\n\n"
            f"```\n{transcript}\n```\n\n---\n_Work dir: `/x`_\n")


class WatchRunner(CannedRunner):
    """Fakes watch: writes two frames into --out-dir and reports them."""

    def run(self, argv, timeout_s=None):
        if argv[:2] != ["python3", self.watch_script]:
            return super().run(argv, timeout_s)
        self.log.append(list(argv))
        out = Path(argv[argv.index("--out-dir") + 1])
        frames = []
        for i in range(2):
            frame = out / f"frame_000{i}.jpg"
            frame.write_bytes(b"\xff\xd8jpeg")
            frames.append(str(frame))
        return subprocess.CompletedProcess(argv, 0, stdout=watch_report(frames), stderr="")


def video_context(extraction_seen):
    ctx = make_context(RUFLO_GH, RUFLO_HTTP)
    ctx.runner = WatchRunner(RUFLO_GH)
    ctx.sources.runner = ctx.runner

    def extraction(messages):
        extraction_seen.append(messages[-1]["content"])
        return json.dumps({"entities": [{"name": "ruflo", "name_latin": "Ruflo", "kind": "repo", "role": "primary",
                                         "evidence_quote": SCREEN_TEXT}]})
    ctx.chat = extraction
    ctx.vision = lambda messages: SCREEN_TEXT
    return ctx


class FakeResponse(io.BytesIO):
    def __init__(self, body, url=REEL_URL, content_type="video/mp4"):
        super().__init__(body)
        self.url, self.headers = url, {"Content-Type": content_type}

    def geturl(self):
        return self.url


class MediaUrlTest(unittest.TestCase):
    def test_only_https_meta_media_hosts(self):
        self.assertTrue(is_media_url(REEL_URL))
        self.assertTrue(is_media_url("https://scontent-icn2-1.cdninstagram.com/v/t66/x.mp4"))
        for url in ("http://lookaside.fbsbx.com/x", "https://fbsbx.com.evil.example/x", "https://evil.example/x",
                    "https://evilfbcdn.net/x", None, ""):
            self.assertFalse(is_media_url(url), url)

    def test_redirect_off_meta_is_refused(self):
        with self.assertRaises(MediaRefused):
            _MetaOnlyRedirects().redirect_request(None, None, 302, "", {}, "https://evil.example/x.mp4")


class FetchReelTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_writes_video(self):
        path = fetch_reel(REEL_URL, self.dir, open_url=lambda url: FakeResponse(MP4))
        self.assertEqual(path.read_bytes(), MP4)

    def test_refuses_video_type_that_is_not_mp4(self):
        with self.assertRaises(MediaRefused):
            fetch_reel(REEL_URL, self.dir, open_url=lambda url: FakeResponse(b"#EXTM3U\nhttp://10.0.0.1/x"))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_refuses_slow_download(self):
        with self.assertRaises(MediaRefused):
            fetch_reel(REEL_URL, self.dir, open_url=lambda url: FakeResponse(MP4), timeout_s=-1)

    def test_refuses_non_video_and_leaves_nothing(self):
        with self.assertRaises(MediaRefused):
            fetch_reel(REEL_URL, self.dir, open_url=lambda url: FakeResponse(b"<html>", content_type="text/html"))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_refuses_oversized_and_leaves_nothing(self):
        with self.assertRaises(MediaRefused):
            fetch_reel(REEL_URL, self.dir, open_url=lambda url: FakeResponse(MP4 * 10000), max_bytes=100000)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_refuses_final_url_off_meta(self):
        with self.assertRaises(MediaRefused):
            fetch_reel(REEL_URL, self.dir, open_url=lambda url: FakeResponse(b"x", url="https://evil.example/x"))

    def test_refuses_non_meta_url_without_opening(self):
        with self.assertRaises(MediaRefused):
            fetch_reel("https://evil.example/x.mp4", self.dir, open_url=lambda url: self.fail("opened"))


class ShareMediaUrlTest(unittest.TestCase):
    def test_reel_keeps_media_url_post_does_not(self):
        [reel] = parse_shares(fixture("ig_reel.json"), VIDEO_CONFIG, NOW_S)
        self.assertEqual(reel.media_url, REEL_URL)
        [post] = parse_shares(fixture("ig_post.json"), VIDEO_CONFIG, NOW_S)
        self.assertIsNone(post.media_url)

    def test_video_off_never_keeps_the_link(self):
        [reel] = parse_shares(fixture("ig_reel.json"), CONFIG, NOW_S)
        self.assertIsNone(reel.media_url)
        self.assertTrue(reel.has_media_url)

    def test_unparseable_queue_item_loses_link_in_failed_record(self):
        inbox = Inbox(Path(tempfile.mkdtemp()))
        item = inbox.root / "queue" / "x.json"
        for cut in ('{"mid": "m", "media_url": "' + REEL_URL + '", broken', '{"media_url":"' + REEL_URL):
            item.write_text(cut)
            inbox.quarantine(item, "unreadable")
            self.assertNotIn("signature", (inbox.root / "failed" / "x.json").read_text())


class PipelineVideoTest(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.ctx = video_context(self.seen)
        video = Path(tempfile.mkdtemp()) / "reel.mp4"
        video.write_bytes(b"mp4")
        self.case_dir = check("캡션 글자", self.ctx, tag="m1", video=video)
        self.bundle = json.loads((self.case_dir / "bundle.json").read_text())

    def test_reads_caption_transcript_and_screen(self):
        text = self.bundle["text"]
        self.assertEqual((text["caption"], text["screen"]), ("캡션 글자", SCREEN_TEXT))
        self.assertIn("오늘 소개할 도구", text["transcript"])
        self.assertIn('<untrusted source="screen">', self.seen[0])

    def test_frames_are_deleted(self):
        self.assertFalse((self.case_dir / "frames").exists())
        self.assertEqual(self.bundle["evidence"]["frames"], [])

    def test_identifies_tool_named_on_screen(self):
        [entity] = self.bundle["entities"]
        self.assertEqual(entity["identifier"], "ruvnet/ruflo")

    def test_watch_failure_falls_back_to_caption(self):
        ctx = video_context([])
        ctx.runner.run = lambda argv, timeout_s=None: subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
        bundle = json.loads((check("캡션만", ctx, tag="m2", video=Path("/nope.mp4")) / "bundle.json").read_text())
        self.assertEqual(bundle["text"]["caption"], "캡션만")


class WorkerVideoTest(unittest.TestCase):
    def setUp(self):
        self.ctx = video_context([])
        self.inbox = Inbox(Path(tempfile.mkdtemp()))
        self.inbox.enqueue(parse_shares(fixture("ig_reel.json"), VIDEO_CONFIG, NOW_S)[0])
        self.fetched = []

    def fetch(self, url, dest):
        self.fetched.append(url)
        dest.mkdir(parents=True)
        (dest / "reel.mp4").write_bytes(b"mp4")
        return dest / "reel.mp4"

    def run_worker(self, video="on", fetch=None):
        runtime = Runtime(limits=Limits(self.inbox.root / "limits"), sleep=lambda s: None, fetch=fetch or self.fetch)
        return process_pending(self.inbox, self.ctx, replace(CONFIG, video=video), now_s=NOW_S, runtime=runtime)

    def test_reads_video_then_deletes_it_and_the_link(self):
        [record] = self.run_worker()
        self.assertEqual(self.fetched, [REEL_URL])
        self.assertEqual(list((self.inbox.root / "media").rglob("*")), [])
        self.assertNotIn("media_url", record["share"])
        self.assertNotIn(REEL_URL, json.dumps(record))
        bundle = json.loads((Path(record["case_dir"]) / "bundle.json").read_text())
        self.assertTrue(bundle["dm"]["video"].startswith("read, then deleted"))
        self.assertNotIn(REEL_URL, json.dumps(bundle))

    def test_video_off_reads_caption_only(self):
        [record] = self.run_worker(video="off")
        self.assertEqual(self.fetched, [])
        bundle = json.loads((Path(record["case_dir"]) / "bundle.json").read_text())
        self.assertEqual(bundle["dm"]["video"], "caption only")

    def test_leftovers_of_a_killed_worker_are_swept(self):
        stale = self.inbox.root / "media" / "old" / "reel.mp4"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(MP4)
        case = self.ctx.out_root / "20260101-000000-pasted-dead"
        (case / "frames").mkdir(parents=True)
        (case / ".video-in-progress").touch()
        research = self.ctx.out_root / "20260101-000000-local-keep"
        (research / "frames").mkdir(parents=True)
        self.run_worker()
        self.assertFalse(stale.exists())
        self.assertFalse((case / "frames").exists())
        self.assertTrue((research / "frames").exists())

    def test_fetch_failure_falls_back_to_caption(self):
        def refuse(url, dest):
            raise MediaRefused("expired")
        [record] = self.run_worker(fetch=refuse)
        bundle = json.loads((Path(record["case_dir"]) / "bundle.json").read_text())
        self.assertEqual(bundle["dm"]["video"], "not fetched (MediaRefused)")
        self.assertTrue(record["messages"])


if __name__ == "__main__":
    unittest.main()


class RunnerTimeoutTest(unittest.TestCase):
    def test_timeout_kills_watch_children(self):
        import os
        import sys
        from hype_checker.runner import Runner
        work = Path(tempfile.mkdtemp())
        script, pid_file = work / "watch.py", work / "child.pid"
        script.write_text("import subprocess, sys, time\n"
                          f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                          f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                          "time.sleep(30)\n")
        runner = Runner(watch_script=str(script), timeout_s=1)
        with self.assertRaises(subprocess.TimeoutExpired):
            runner.run(["python3", str(script)])
        child = int(pid_file.read_text())
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            import time
            time.sleep(0.05)
        self.fail("ffmpeg-like child survived the watch timeout")


class AcknowledgeTest(unittest.TestCase):
    def setUp(self):
        self.inbox = Inbox(Path(tempfile.mkdtemp()))
        self.inbox.enqueue(parse_shares(fixture("ig_reel.json"), CONFIG, NOW_S)[0])
        self.log = []

    def send(self, recipient, text):
        self.log.append(text)

    def run_worker(self, acknowledge=True, limits=None):
        runtime = Runtime(limits=limits or Limits(self.inbox.root / "limits"), sleep=lambda s: None,
                          acknowledge=acknowledge)
        return process_pending(self.inbox, make_context(RUFLO_GH, RUFLO_HTTP), CONFIG, send=self.send,
                               now_s=NOW_S, runtime=runtime)

    def test_ack_goes_first_then_the_result(self):
        [record] = self.run_worker()
        self.assertTrue(self.log[0].startswith("릴스 잘 받았어요"))
        self.assertGreater(len(self.log), 1)
        self.assertTrue(record["acknowledged"])

    def test_no_ack_when_switched_off(self):
        self.run_worker(acknowledge=False)
        self.assertFalse(any(t.startswith("릴스 잘 받았어요") for t in self.log))

    def test_no_ack_when_over_the_daily_limit(self):
        class Full(Limits):
            def allow(self, sender, today):
                return False
        self.run_worker(limits=Full(self.inbox.root / "limits"))
        self.assertFalse(any(t.startswith("릴스 잘 받았어요") for t in self.log))

    def test_failed_ack_does_not_block_the_result(self):
        calls = []

        def flaky(recipient, text):
            calls.append(text)
            if len(calls) == 1:
                raise OSError("down")
        self.send = flaky
        [record] = self.run_worker()
        self.assertFalse(record["acknowledged"])
        self.assertGreater(len(calls), 1)


class ReelPageTest(unittest.TestCase):
    """Real ig_reel events carry the public reel page, not a CDN file (seen 2026-11-02)."""

    def test_page_link_is_accepted_only_for_reel_paths(self):
        from hype_checker.v2.media import is_reel_page
        self.assertTrue(is_reel_page(PAGE_URL))
        self.assertTrue(is_reel_page("https://instagram.com/p/X/"))
        for url in ("http://www.instagram.com/reel/X/", "https://www.instagram.com/accounts/login/",
                    "https://www.instagram.com.evil.example/reel/X/", "https://evil.example/reel/X/"):
            self.assertFalse(is_reel_page(url), url)

    def test_worker_hands_page_link_to_watch_and_keeps_no_trace(self):
        payload = fixture("ig_reel.json")
        payload["entry"][0]["messaging"][0]["message"]["attachments"][0]["payload"]["url"] = PAGE_URL
        inbox = Inbox(Path(tempfile.mkdtemp()))
        inbox.enqueue(parse_shares(payload, VIDEO_CONFIG, NOW_S)[0])
        ctx = video_context([])
        runtime = Runtime(limits=Limits(inbox.root / "limits"), sleep=lambda s: None,
                          fetch=lambda url, dest: self.fail("page links are not fetched by media.py"))
        [record] = process_pending(inbox, ctx, VIDEO_CONFIG, now_s=NOW_S, runtime=runtime)
        watch = [argv for argv in ctx.runner.log if argv[:2] == ["python3", ctx.watch_script]]
        self.assertEqual(watch[0][2], PAGE_URL)
        bundle_text = (Path(record["case_dir"]) / "bundle.json").read_text()
        self.assertNotIn(PAGE_URL, bundle_text)
        self.assertNotIn(PAGE_URL, json.dumps(record))
        self.assertIn("read, then deleted", json.loads(bundle_text)["dm"]["video"])
