"""Research card draft for one shared content (owner-spec 3-6): one card, its tools listed inside.

Same fields as the research log.md template (bundle.JUDGEMENT_TEMPLATE). 판정 A stays blank and
판정 B carries the automatic owner value: a human approves before anything goes into log.md.
Every externally sourced string is flattened through bundle.inline().
"""

import os
import time
from pathlib import Path

from hype_checker.bundle import inline
from hype_checker.labels import CLAIM_RESULT, GRADE, label

HEADER = "> 자동 초안이에요. 판정 A는 사람이 채우고, 판정 B는 자동값이라 승인 전에 고칠 수 있어요."
NO_LINK = "링크 미기록"
NO_COMMANDS = "없음(자동 측정만)"


def write_private(path, text):
    """Writes `text` to `path` with mode 600 (also when the file already existed)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def _shared_at(bundle):
    shared_ms = (bundle.get("dm") or {}).get("shared_at_ms")
    seconds = shared_ms / 1000 if isinstance(shared_ms, (int, float)) and shared_ms else bundle.get("created_at") or 0
    return time.gmtime(seconds)


def _source_line(bundle, shared):
    media_id = (bundle.get("dm") or {}).get("media_id")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", shared)
    return f"- 출처: DM 공유 · media_id {inline(media_id)} · 공유 시각 {stamp} · {NO_LINK}"


def _claim_lines(tools):
    lines = [f"  - {inline(tool.get('name'))}: {inline(claim.get('claim'))} → {label(CLAIM_RESULT, claim.get('result'))}"
             for tool in tools for claim in tool.get("claims") or []]
    return lines or ["  - (캡션에서 뽑은 주장 없음)"]


def _check_lines(tools):
    lines = []
    for tool in tools:
        where = f" → {inline(tool['identifier'])}" if tool.get("identifier") else ""
        reasons = " · ".join(inline(reason) for reason in tool.get("reasons") or [])
        lines.append(f"  - {inline(tool.get('name'))}{where} · {label(GRADE, tool.get('grade'))}"
                     + (f" — {reasons}" if reasons else ""))
    return lines or ["  - (판정한 도구 없음)"]


def _b_lines(merges):
    lines = []
    for merge in merges:
        value = f"{merge['b']}({inline(merge['match'])})" if merge.get("match") else merge["b"]
        lines.append(f"  - {inline(merge.get('name'))}: {value} — {merge['reason']}")
    return lines or ["  - (비교한 도구 없음)"]


def render_card(bundle, owner):
    tools = (bundle.get("verdict") or {}).get("tools") or []
    shared = _shared_at(bundle)
    title = " · ".join(inline(tool.get("name")) for tool in tools) or inline(bundle.get("id"))
    parts = [HEADER, "", f"### [{time.strftime('%Y-%m-%d', shared)}] {title}", _source_line(bundle, shared),
             "- 주장:", *_claim_lines(tools), "- 검증: (자동 등급 + 1차 출처)", *_check_lines(tools),
             "- 판정 A·진로자산: ", "- 판정 B·워크플로우: (자동값 — 채택은 사람이 정한다)",
             *_b_lines(owner.get("merges") or []), f"- 실행한 명령: {NO_COMMANDS}", ""]
    return "\n".join(parts)


def write_card(case_dir, bundle, owner):
    path = Path(case_dir) / "card.md"
    write_private(path, render_card(bundle, owner))
    return path
