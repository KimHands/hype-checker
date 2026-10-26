"""Parses the watch 0.3.2 Markdown report (design-v1 §4-2).

The transcript is untrusted text and may itself contain fences or headings,
so the block is cut at the last fence before the trailer, never the first.
"""

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

REPORT_HEADER = "# watch: video report"
TRANSCRIPT_HEADING = "\n## Transcript\n"
UNAVAILABLE_HEADING = "## Unavailable evidence"
TRAILER = "\n---\n_Work dir: `"
META_LINE = re.compile(r"^- \*\*(.+?):\*\* (.*)$")
FRAME_LINE = re.compile(r"^- `(.+?)` \(t=([\d:]+), reason=([^)]*)\)$")
SEGMENT_LINE = re.compile(r"^\[\d{2}:\d{2}(?::\d{2})?\] ")
STATE_LINE = re.compile(r"^_.+_$", re.MULTILINE)  # e.g. "_No transcript available._"


@dataclass
class WatchReport:
    status: str                      # ok | no_speech | partial | parse_failed
    meta: dict = field(default_factory=dict)
    frames: list = field(default_factory=list)
    transcript: str = ""
    errors: list = field(default_factory=list)
    work_dir: str | None = None

    @property
    def transcript_lines(self):
        return [line for line in self.transcript.splitlines() if SEGMENT_LINE.match(line)]


def _parse_head(head):
    meta, frames = {}, []
    for line in head.splitlines():
        if match := META_LINE.match(line):
            meta[match.group(1)] = match.group(2)
        elif match := FRAME_LINE.match(line):
            frames.append({"path": match.group(1), "t": match.group(2), "reason": match.group(3)})
    return meta, frames


def _split_transcript(section):
    opening = section.find("```\n")
    closing = section.rfind("\n```")
    if opening == -1 or closing <= opening:
        return "", section
    return section[opening + 4:closing], section[closing + 4:]


def _parse_errors(tail):
    if UNAVAILABLE_HEADING not in tail:
        return []
    block = tail.split(UNAVAILABLE_HEADING, 1)[1]
    return [line[2:].strip() for line in block.splitlines() if line.startswith("- ")]


def parse_watch_report(text):
    if REPORT_HEADER not in text or TRANSCRIPT_HEADING not in text:
        return WatchReport(status="parse_failed")
    head, rest = text.split(TRANSCRIPT_HEADING, 1)
    trailer_at = rest.rfind(TRAILER)
    section = rest if trailer_at == -1 else rest[:trailer_at]
    work_dir = None if trailer_at == -1 else rest[trailer_at + len(TRAILER):].split("`", 1)[0]
    meta, frames = _parse_head(head)
    if "```" not in section and not STATE_LINE.search(section):
        return WatchReport(status="parse_failed", meta=meta, frames=frames, work_dir=work_dir)
    transcript, tail = _split_transcript(section)
    errors = _parse_errors(tail)
    status = "partial" if errors else ("ok" if transcript.strip() else "no_speech")
    return WatchReport(status, meta, frames, transcript, errors, work_dir)


# ── collection: run watch, keep frames, always delete the work dir ──

WATCH_FIXED_ARGS = ["--engine", "local", "--whisper", "groq", "--detail", "efficient",
                    "--max-frames", "20", "--resolution", "768"]


class UnsafeDelete(Exception):
    pass


def watch_argv(watch_script, source_value, work_dir):
    return ["python3", watch_script, source_value, *WATCH_FIXED_ARGS, "--out-dir", str(work_dir)]


def guard_case_dir(case_dir, out_root):
    """Deletion only ever happens inside <out_root>/<case>, never through a symlink."""
    case_dir, out_root = Path(case_dir), Path(out_root)
    if case_dir.is_symlink() or out_root.is_symlink():
        raise UnsafeDelete(f"symlinked case dir refused: {case_dir}")
    if case_dir.resolve().parent != out_root.resolve() or out_root.name != "out":
        raise UnsafeDelete(f"case dir must live directly under {out_root}: {case_dir}")


def _remove_work(work, case_dir):
    if work.exists():
        if work.resolve().parent != case_dir.resolve():
            raise UnsafeDelete(f"refusing to delete outside case dir: {work}")
        shutil.rmtree(work)


def _keep_frames(report, work, frames_dir):
    frames_dir.mkdir(parents=True, exist_ok=True)
    kept = []
    for frame in report.frames:
        path = Path(frame["path"])
        if path.is_file() and work.resolve() in path.resolve().parents:
            target = frames_dir / path.name
            shutil.move(str(path), target)
            kept.append({**frame, "path": str(target)})
    missing = len(report.frames) - len(kept)
    report.frames = kept
    if missing:
        report.errors.append(f"frames missing from work dir: {missing}")
        report.status = "partial"


def collect(source, case_dir, runner, watch_script, out_root):
    case_dir = Path(case_dir)
    guard_case_dir(case_dir, out_root)
    work = case_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    try:
        result = runner.run(watch_argv(watch_script, source.value, work))
        report = parse_watch_report(result.stdout)
        if report.status == "parse_failed" and result.returncode != 0:
            report.status = "watch_failed"
            report.errors = [result.stderr.strip()[-500:]]
        _keep_frames(report, work, case_dir / "frames")
        return report
    finally:
        _remove_work(work, case_dir)
