"""check: input → evidence → entities → identifiers → measurements → bundle (design-v1 §2)."""

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path

from hype_checker import preflight
from hype_checker.bundle import write_bundle
from hype_checker.evidence import collect
from hype_checker.extract import LLM_MODEL, SYSTEM_PROMPT, extract_entities
from hype_checker.ingest import classify
from hype_checker.measure.depsdev import measure_depsdev
from hype_checker.measure.github import measure_repo
from hype_checker.measure.npm import measure_npm
from hype_checker.resolve import resolve

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


def _case_id(source, now):
    digest = hashlib.sha1(source.value.encode()).hexdigest()[:8]
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
    if repo:
        found["github"] = measure_repo(repo, ctx.sources, now=ctx.now)
    return found


def _identify(entity, ctx):
    if entity.get("role") == "mentioned" and entity.get("origin") != "url":
        entity.update(resolution={"status": "skipped_mentioned", "candidates": []}, identifier=None, measurements={})
        return entity
    if entity.get("origin") != "url":
        entity["resolution"] = resolve(entity, ctx.sources)
        top = entity["resolution"]["candidates"][:1]
        entity["identifier"] = top[0]["identifier"] if entity["resolution"]["status"] == "resolved" else None
    entity["measurements"] = _measure(entity, ctx) if entity.get("identifier") or entity.get("npm_package") else {}
    return entity


def _gather_text(source, case_dir, ctx, bundle):
    if source.kind == "mp4" or source.needs_download:
        problems = preflight.problems(ctx.home, ctx.env)
        if problems:
            raise RuntimeError("preflight 실패: " + "; ".join(problems))
        report = collect(source, case_dir, ctx.runner, ctx.watch_script, ctx.out_root)
        bundle["evidence"] = {"status": report.status, "meta": report.meta, "frames": report.frames,
                              "errors": report.errors}
        bundle["text"] = {"title": report.meta.get("Title", ""), "caption": "", "transcript": report.transcript}
    elif source.kind == "text":
        bundle["text"]["caption"] = source.value
    else:
        bundle["notes"].append(WEB_NOTE)


def _entities(source, case_dir, ctx, bundle):
    if source.platform == "threads":
        bundle["notes"].append(source.note)
        return []
    if source.identifier:
        return [_url_entity(source)]
    _gather_text(source, case_dir, ctx, bundle)
    text = bundle["text"]
    if not any(text.values()):
        return []
    extraction = extract_entities(text["transcript"], text["caption"], text["title"], chat=ctx.chat)
    bundle["extraction"] = {"status": extraction.status, "attempts": extraction.attempts, "chunks": extraction.chunks,
                            "error": extraction.error, "model": LLM_MODEL}
    origin = "transcript" if text["transcript"] else "caption"
    return [{**entity, "origin": origin} for entity in extraction.entities]


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


def check(raw, ctx):
    source = classify(raw)
    log_start = len(ctx.runner.log)
    case_dir = Path(ctx.out_root) / _case_id(source, ctx.now)
    case_dir.mkdir(parents=True)
    bundle = _new_bundle(source, case_dir, ctx)
    bundle["entities"] = [_identify(entity, ctx) for entity in _entities(source, case_dir, ctx, bundle)]
    bundle["budget"] = {"gh_calls": sum(1 for argv in ctx.runner.log[log_start:] if argv[:2] == ["gh", "api"])}
    write_bundle(case_dir, bundle)
    return case_dir
