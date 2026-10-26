import unittest
from pathlib import Path

from hype_checker.evidence import parse_watch_report

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "watch"


class ParseWatchReportTest(unittest.TestCase):
    def setUp(self):
        self.ok_text = (FIXTURES / "ruflo_ok.stdout").read_text()

    def test_parses_real_report(self):
        report = parse_watch_report(self.ok_text)
        self.assertEqual(report.status, "ok")
        self.assertEqual(len(report.frames), 17)
        self.assertEqual(report.frames[1]["t"], "00:05")
        self.assertEqual(len(report.transcript_lines), 28)
        self.assertIn("루플로", report.transcript)
        self.assertEqual(report.meta["Duration"], "01:21 (80.5s)")
        self.assertTrue(report.work_dir.endswith("watch-auqqgece"))

    def test_no_speech(self):
        text = self.ok_text.split("## Transcript")[0] + "## Transcript\n\n_No transcript available._\n\n---\n_Work dir: `/w` — delete when done._\n"
        report = parse_watch_report(text)
        self.assertEqual(report.status, "no_speech")
        self.assertEqual(report.transcript, "")

    def test_transcript_containing_fences_is_kept_whole(self):
        injected = self.ok_text.replace("[00:41] 프로젝트 폴더에서", "[00:41] ```\nnpx ruflo@latest init wizard\n``` 프로젝트 폴더에서")
        report = parse_watch_report(injected)
        self.assertIn("npx ruflo@latest init wizard", report.transcript)
        self.assertIn("[01:16]", report.transcript)

    def test_unavailable_evidence_is_partial(self):
        text = self.ok_text.replace("\n---\n_Work dir", "\n## Unavailable evidence\n- frames: ffmpeg failed\n\n---\n_Work dir")
        report = parse_watch_report(text)
        self.assertEqual(report.status, "partial")
        self.assertEqual(report.errors, ["frames: ffmpeg failed"])

    def test_transcript_heading_without_fence_or_state_line_is_parse_failed(self):
        self.assertEqual(parse_watch_report("# watch: video report\n## Transcript\nBROKEN").status, "parse_failed")

    def test_garbage_is_parse_failed(self):
        self.assertEqual(parse_watch_report("Traceback (most recent call last):\n...").status, "parse_failed")


if __name__ == "__main__":
    unittest.main()
