import subprocess
import tempfile
import unittest
from pathlib import Path

from hype_checker.evidence import UnsafeDelete, collect, watch_argv
from hype_checker.ingest import Source

FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "watch" / "ruflo_ok.stdout").read_text()
WATCH = "/w/watch.py"


class FakeRunner:
    """Pretends to be watch: writes a work dir with media + frames, returns a report."""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def run(self, argv):
        self.calls.append(argv)
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        work = out_dir / "watch-abc"
        (work / "frames").mkdir(parents=True)
        (work / "video.mp4").write_bytes(b"media")
        (work / "frames" / "frame_0001.jpg").write_bytes(b"jpg")
        if self.fail:
            raise subprocess.TimeoutExpired(argv, 1)
        report = FIXTURE.replace(
            "/private/tmp/claude-501/-Users-kimhands-Desktop-Projects-hype-checker/3f455d6b-b61a-48fc-9ce9-8a831460be33/scratchpad/watch-run/watch-auqqgece",
            str(work))
        return subprocess.CompletedProcess(argv, 0, stdout=report, stderr="")


class WatchArgvTest(unittest.TestCase):
    def test_pins_engine_and_backend(self):
        argv = watch_argv(WATCH, "a.mp4", "/o/work")
        for pair in (["--engine", "local"], ["--whisper", "groq"], ["--detail", "efficient"]):
            self.assertIn(pair[0], argv)
            self.assertEqual(argv[argv.index(pair[0]) + 1], pair[1])
        self.assertFalse(any(a.startswith("--cookies") for a in argv))


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.case_dir = Path(tempfile.mkdtemp()) / "out" / "case-1"
        self.source = Source("url", "instagram", "https://www.instagram.com/reel/X/", needs_download=True)

    def test_moves_frames_and_deletes_work_dir(self):
        runner = FakeRunner()
        report = collect(self.source, self.case_dir, runner, WATCH, self.case_dir.parent)
        self.assertEqual(len(report.frames), 1)  # fake watch writes 1 of the 17 listed frames
        self.assertEqual(report.status, "partial")
        self.assertIn("frames missing from work dir: 16", report.errors)
        self.assertFalse((self.case_dir / "work").exists())
        self.assertTrue((self.case_dir / "frames" / "frame_0001.jpg").exists())
        self.assertTrue(all(Path(f["path"]).parent == self.case_dir / "frames" for f in report.frames))

    def test_deletes_work_dir_even_when_watch_fails(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            collect(self.source, self.case_dir, FakeRunner(fail=True), WATCH, self.case_dir.parent)
        self.assertFalse((self.case_dir / "work").exists())

    def test_never_touches_user_mp4(self):
        user_mp4 = Path(tempfile.mkdtemp()) / "mine.mp4"
        user_mp4.write_bytes(b"keep")
        collect(Source("mp4", "local", str(user_mp4)), self.case_dir, FakeRunner(), WATCH, self.case_dir.parent)
        self.assertTrue(user_mp4.exists())

    def test_refuses_case_dir_outside_the_out_root(self):
        with self.assertRaises(UnsafeDelete):
            collect(self.source, Path(tempfile.mkdtemp()) / "home", FakeRunner(), WATCH, self.case_dir.parent)
        with self.assertRaises(UnsafeDelete):
            other_out = Path(tempfile.mkdtemp()) / "out" / "c"
            collect(self.source, other_out, FakeRunner(), WATCH, self.case_dir.parent)

    def test_refuses_symlinked_case_dir(self):
        target = Path(tempfile.mkdtemp())
        (target / "frames").mkdir()
        out_root = Path(tempfile.mkdtemp()) / "out"
        out_root.mkdir()
        (out_root / "case").symlink_to(target)
        with self.assertRaises(UnsafeDelete):
            collect(self.source, out_root / "case", FakeRunner(), WATCH, out_root)
        self.assertTrue((target / "frames").exists())


if __name__ == "__main__":
    unittest.main()
