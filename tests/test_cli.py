import json
import tempfile
import unittest
from pathlib import Path

from hype_checker.cli import clean, resume
from hype_checker.pipeline import check
from tests.test_pipeline import RUFLO_GH, RUFLO_HTTP, make_context


class ResumeTest(unittest.TestCase):
    def test_adds_entity_seen_on_screen_and_measures_it(self):
        ctx = make_context(RUFLO_GH, RUFLO_HTTP, chat=lambda m: json.dumps({"entities": []}))
        case_dir = check("영상 캡션: 오늘의 도구는 화면 참고", ctx)
        resume(case_dir, ctx, name="Ruflo", kind="repo", evidence="frame_0003 t=00:10 화면 제목 Ruflo")
        bundle = json.loads((case_dir / "bundle.json").read_text())
        added = bundle["entities"][-1]
        self.assertEqual((added["origin"], added["identifier"]), ("human", "ruvnet/ruflo"))
        self.assertIn("Ruflo", (case_dir / "bundle.md").read_text())
        log = json.loads((case_dir / "bundle.json").read_text())["interventions"]
        self.assertEqual((log[0]["action"], log[0]["evidence"]), ("resume", "frame_0003 t=00:10 화면 제목 Ruflo"))

    def test_resume_requires_evidence(self):
        ctx = make_context(RUFLO_GH, RUFLO_HTTP, chat=lambda m: json.dumps({"entities": []}))
        with self.assertRaises(ValueError):
            resume(check("캡션", ctx), ctx, name="Ruflo", kind="repo", evidence=" ")

    def test_explicit_identifier_skips_resolution(self):
        ctx = make_context(RUFLO_GH, RUFLO_HTTP, chat=lambda m: json.dumps({"entities": []}))
        case_dir = check("캡션", ctx)
        resume(case_dir, ctx, name="Ruflo", kind="repo", identifier="ruvnet/ruflo", evidence="frame_0004 github.com/ruvnet/ruflo")
        added = json.loads((case_dir / "bundle.json").read_text())["entities"][-1]
        self.assertEqual(added["resolution"]["status"], "given")
        self.assertFalse(any("search/" in argv[-1] for argv in ctx.runner.log))

    def test_resume_replaces_live_candidate_with_same_name(self):
        ctx = make_context(RUFLO_GH, RUFLO_HTTP)
        case_dir = check("클로드 코드에 루플로", ctx)
        resume(case_dir, ctx, name="ruflo", kind="repo", evidence="frame_0003 화면 제목 Ruflo")
        bundle = json.loads((case_dir / "bundle.json").read_text())
        live = [e for e in bundle["entities"] if e["role"] != "dropped" and e["name_latin"] != "Claude Code"]
        self.assertEqual([e["origin"] for e in live], ["human"])
        self.assertTrue(bundle["entities"][0]["dropped_reason"].startswith("superseded by resume"))
        self.assertEqual(bundle["interventions"][-1]["superseded"], 1)


class DropTest(unittest.TestCase):
    def test_drop_marks_entity_without_deleting_its_record(self):
        from hype_checker.cli import drop
        ctx = make_context(RUFLO_GH, RUFLO_HTTP)
        case_dir = check("클로드 코드에 루플로", ctx)
        drop(case_dir, "Ruflo", reason="화면 확인 결과 다른 도구")
        entity = json.loads((case_dir / "bundle.json").read_text())["entities"][0]
        self.assertEqual((entity["role"], entity["dropped_reason"]), ("dropped", "화면 확인 결과 다른 도구"))
        self.assertEqual(entity["identifier"], "ruvnet/ruflo")

    def test_drop_unknown_name_fails(self):
        from hype_checker.cli import drop
        ctx = make_context(RUFLO_GH, RUFLO_HTTP)
        with self.assertRaises(KeyError):
            drop(check("클로드 코드에 루플로", ctx), "Nope", reason="x")


class CleanTest(unittest.TestCase):
    def test_removes_frames_only(self):
        case_dir = Path(tempfile.mkdtemp()) / "out" / "c1"
        (case_dir / "frames").mkdir(parents=True)
        (case_dir / "frames" / "f.jpg").write_bytes(b"x")
        (case_dir / "bundle.json").write_text("{}")
        clean(case_dir, case_dir.parent)
        self.assertFalse((case_dir / "frames").exists())
        self.assertTrue((case_dir / "bundle.json").exists())

    def test_refuses_dirs_outside_out(self):
        from hype_checker.evidence import UnsafeDelete
        with self.assertRaises(UnsafeDelete):
            clean(Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "out")

    def test_refuses_symlink(self):
        from hype_checker.evidence import UnsafeDelete
        target = Path(tempfile.mkdtemp())
        (target / "frames").mkdir()
        out_root = Path(tempfile.mkdtemp()) / "out"
        out_root.mkdir()
        (out_root / "c").symlink_to(target)
        with self.assertRaises(UnsafeDelete):
            clean(out_root / "c", out_root)
        self.assertTrue((target / "frames").exists())


if __name__ == "__main__":
    unittest.main()
