"""check: input → evidence → entities → identifiers → measurements → bundle (design-v1 §2)."""

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from hype_checker import preflight
from hype_checker.bundle import write_bundle
from hype_checker.evidence import collect
from hype_checker.extract import LLM_MODEL, SYSTEM_PROMPT, extract_entities
from hype_checker.ingest import Source, classify
from hype_checker.measure.depsdev import measure_depsdev
from hype_checker.measure.github import measure_repo
from hype_checker.measure.npm import measure_npm
from hype_checker.measure.pypi import measure_pypi
from hype_checker.onscreen import read_screen
from hype_checker import samename
from hype_checker.resolve import attach_written_repos, resolve

WATCH_VERSION = "0.3.2"
NPM_EVIDENCE = re.compile(r"^npm:(\S+) → ")
LIMITATIONS = [
    "watch → yt-dlp 자식 프로세스는 실행 전 설정 점검(preflight)만 했다. 프로세스 트리 샌드박스는 없다",
    "GitHub stargazer 목록은 2026-06-30부터 비공개 — 스타 계정 신호는 측정하지 않았다(일별 개수만)",
    "원격 MCP 엔드포인트·상용 제품은 공식성을 코드가 판정하지 않는다. 1차 출처 확인은 판정 세션 몫",
]
WEB_NOTE = "일반 웹 URL은 v1 범위 밖이다. 본문이나 캡션을 텍스트로 붙여 넣어 달라"


@dataclass
class Context:
    runner: object
    sources: object
    chat: object
    out_root: Path
    now: float
    watch_script: str
    home: Path
    env: dict
    vision: object = None   # Claude chat that reads frames (server); None → frames are left for a person (Mac)


def _case_id(source, now, tag=""):
    digest = hashlib.sha1((source.value + tag).encode()).hexdigest()[:8]
    return f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))}-{source.platform}-{digest}"


def _url_entity(source):
    name = source.identifier.split("/")[-1]
    kind = {"github": "repo", "npm": "npm", "pypi": "pypi"}[source.platform]
    entity = {"name": name, "name_latin": name, "kind": kind, "role": "primary", "evidence_quote": source.value,
              "origin": "url", "resolution": {"status": "from_url", "candidates": []}}
    if source.platform == "github":
        entity["identifier"] = source.identifier
    elif source.platform == "npm":
        entity["npm_package"] = source.identifier
    return entity


def _npm_package(entity):
    if entity.get("npm_package"):
        return entity["npm_package"]
    for candidate in (entity.get("resolution") or {}).get("candidates", [])[:1]:
        for line in candidate["evidence"]:
            if match := NPM_EVIDENCE.match(line):
                return match.group(1)
    return entity["name_latin"].lower() if entity.get("origin") == "url" and entity.get("identifier") else None


def _measure_npm(entity, repo, ctx):
    package = _npm_package(entity)
    if not package:
        return {}
    npm = measure_npm(package, ctx.sources, repo=repo)
    if npm["status"] != "ok" or (repo and not npm["value"]["repository_matches"] and entity.get("origin") == "url"):
        return {}  # a same-named package of someone else is not evidence about this repo
    found = {"npm": npm}
    found["depsdev"] = measure_depsdev("npm", package, npm["value"]["version"], ctx.sources, repo=repo)
    if not repo and npm["value"]["repository"]:
        entity["identifier"] = npm["value"]["repository"]
    return found


def _measure(entity, ctx):
    identifier = entity.get("identifier")
    if identifier and identifier.startswith("https://"):
        return {}
    repo = identifier
    found = _measure_npm(entity, repo, ctx)
    repo = entity.get("identifier")
    if repo and "npm" not in found:
        found.update(_measure_pypi(repo, ctx))
    if repo:
        found["github"] = measure_repo(repo, ctx.sources, now=ctx.now)
    return found


def _measure_pypi(repo, ctx):
    pypi = measure_pypi(repo, ctx.sources)
    if not pypi:
        return {}
    value = pypi["value"]
    return {"pypi": pypi, "depsdev": measure_depsdev("pypi", value["package"], value["version"], ctx.sources, repo=repo)}


def _identify(entity, ctx):
    if entity.get("role") == "mentioned" and entity.get("origin") != "url":
        entity.update(resolution={"status": "skipped_mentioned", "candidates": []}, identifier=None, measurements={})
        return entity
    if entity.get("origin") != "url":
        entity["resolution"] = resolve(entity, ctx.sources)
        entity["resolution"] = samename.settle(entity, ctx.chat, _time_left(ctx))
        top = entity["resolution"]["candidates"][:1]
        entity["identifier"] = top[0]["identifier"] if entity["resolution"]["status"] == "resolved" else None
    entity["measurements"] = _measure(entity, ctx) if entity.get("identifier") or entity.get("npm_package") else {}
    return entity


DM_WATCH_S = 150   # a shared reel; the item deadline can only shorten it


class OutOfTime(Exception):
    pass


def _watch(source, case_dir, ctx, bundle, timeout_s=None):
    problems = preflight.problems(ctx.home, ctx.env)
    if problems:
        raise RuntimeError("preflight 실패: " + "; ".join(problems))
    report = collect(source, case_dir, ctx.runner, ctx.watch_script, ctx.out_root, timeout_s)
    bundle["evidence"] = {"status": report.status, "meta": report.meta, "frames": report.frames,
                          "errors": report.errors}
    bundle["text"] = {"title": report.meta.get("Title", ""), "caption": "", "transcript": report.transcript}
    return report


VIDEO_MARK = ".video-in-progress"


def sweep_video_leftovers(out_root):
    """Frames and watch work dirs of DM reels whose worker was killed mid-way (marker still present)."""
    for mark in Path(out_root).glob(f"*/{VIDEO_MARK}"):
        _remove_video_leftovers(mark.parent)


def _remove_video_leftovers(case_dir):
    """The marker goes only once both dirs are really gone, so a failed delete is retried next pass."""
    for name in ("work", "frames"):
        shutil.rmtree(case_dir / name, ignore_errors=True)
    if not any((case_dir / name).exists() for name in ("work", "frames")):
        (case_dir / VIDEO_MARK).unlink(missing_ok=True)


def _time_left(ctx):
    """The DM item's remaining time (LiveSources.deadline); unlimited for CLI checks and test sources."""
    return getattr(ctx.sources, "time_left", lambda: float("inf"))


def _mask_link(bundle, link):
    """The reel link (watch's Source line, yt-dlp errors) is not kept in the bundle (PRIVACY)."""
    bundle["evidence"] = json.loads(json.dumps(bundle["evidence"], ensure_ascii=False).replace(link, "<reel>"))


def _screen_text(report, ctx, bundle):
    """A failed screen read keeps the transcript; the bundle notes it so the reply is not cached."""
    try:
        return read_screen(report.frames, ctx.vision, _time_left(ctx))
    except Exception as error:  # Claude down, bad frame: the video's speech still counts
        bundle["evidence"]["screen_error"] = type(error).__name__
        return ""


def _shared_video(source, video, case_dir, ctx, bundle):
    """DM reel: caption from the share, transcript and on-screen text from the video, then frames are deleted."""
    (case_dir / VIDEO_MARK).touch()
    try:
        target = Source("url", "instagram", video, needs_download=True) if str(video).startswith("https://") \
            else Source("mp4", "local", str(video))
        limit = ctx.sources.call_limit(DM_WATCH_S) if hasattr(ctx.sources, "call_limit") else DM_WATCH_S
        if limit is None:
            raise OutOfTime("no time left for the video")
        report = _watch(target, case_dir, ctx, bundle, limit)
        _mask_link(bundle, str(video))
        bundle["text"].update(title="", caption=source.value, screen=_screen_text(report, ctx, bundle))
    except Exception as error:  # the caption alone still gets checked
        bundle["evidence"] = {"status": "watch_failed", "frames": [], "errors": [type(error).__name__]}
        bundle["text"].update(title="", transcript="")
        bundle["text"]["caption"] = source.value
    finally:
        _remove_video_leftovers(case_dir)
        bundle["evidence"]["frames"] = []


def _gather_text(source, case_dir, ctx, bundle, video=None):
    if video:
        _shared_video(source, video, case_dir, ctx, bundle)
    elif source.kind == "mp4" or source.needs_download:
        _watch(source, case_dir, ctx, bundle)
    elif source.kind == "text":
        bundle["text"]["caption"] = source.value
    else:
        bundle["notes"].append(WEB_NOTE)


def _entities(source, case_dir, ctx, bundle, video=None):
    if source.platform == "threads":
        bundle["notes"].append(source.note)
        return []
    if source.identifier:
        return [_url_entity(source)]
    _gather_text(source, case_dir, ctx, bundle, video)
    text = bundle["text"]
    if not any(text.values()):
        return []
    extraction = extract_entities(text["transcript"], text["caption"], text["title"], chat=ctx.chat,
                                  screen=text.get("screen", ""), time_left=_time_left(ctx))
    bundle["extraction"] = {"status": extraction.status, "attempts": extraction.attempts, "chunks": extraction.chunks,
                            "error": extraction.error, "model": LLM_MODEL}
    origin = "transcript" if text["transcript"] or text.get("screen") else "caption"
    entities = attach_written_repos(extraction.entities, f"{text['title']}\n{text['caption']}")
    return [{**entity, "origin": origin} for entity in entities]


def _new_bundle(source, case_dir, ctx):
    return {
        "schema_version": 1, "id": case_dir.name, "created_at": ctx.now,
        "source": {"kind": source.kind, "platform": source.platform, "value": source.value},
        "evidence": {"status": "not_run", "frames": []}, "extraction": {"status": "not_run"},
        "text": {"title": "", "caption": "", "transcript": ""}, "notes": [], "entities": [],
        "limitations": list(LIMITATIONS),
        "versions": {"watch": WATCH_VERSION, "llm_model": LLM_MODEL,
                     "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()},
    }


def check(raw, ctx, source=None, tag="", video=None):
    """`video`: a DM-shared reel (local copy, or its public reel page URL); `raw` is then its caption.
    The caller deletes a local copy; watch deletes what it downloads."""
    source = source or classify(raw)
    log_start = len(ctx.runner.log)
    case_dir = Path(ctx.out_root) / _case_id(source, ctx.now, tag)
    case_dir.mkdir(parents=True)
    bundle = _new_bundle(source, case_dir, ctx)
    bundle["entities"] = [_identify(entity, ctx) for entity in _entities(source, case_dir, ctx, bundle, video)]
    bundle["budget"] = {"gh_calls": sum(1 for argv in ctx.runner.log[log_start:] if argv[:2] == ["gh", "api"])}
    write_bundle(case_dir, bundle)
    return case_dir
