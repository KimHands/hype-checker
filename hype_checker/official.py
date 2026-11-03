"""Collect the official description text of an identified tool (README / npm / MCP registry).

Text is stored as-is: it is untrusted data and is escaped where it is rendered or sent.
Status: ok · failed (a fetch went wrong, worth retrying) · none (there is no such text).
"""

import base64
import binascii
from urllib.parse import quote

MAX_BYTES = 8192
NPM_LATEST = "https://registry.npmjs.org/{}/latest"


def _result(status, source=None, paragraphs=None, now=0.0, note=""):
    return {"status": status, "source": source, "paragraphs": paragraphs or [],
            "fetched_at": now, "note": note}


def _cut_to_bytes(text, limit):
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _cap(paragraphs):
    kept, used = [], 0
    for paragraph in paragraphs:
        room = MAX_BYTES - used
        if room <= 0:
            break
        piece = paragraph if len(paragraph.encode("utf-8")) <= room else _cut_to_bytes(paragraph, room)
        if piece:
            kept.append(piece)
            used += len(piece.encode("utf-8"))
    return kept


def _split(text):
    return [part.strip() for part in text.replace("\r\n", "\n").split("\n\n") if part.strip()]


def _lookup_failed(sources, before):
    return len(getattr(sources, "errors", [])) > before


def _description_only(repo, sources, now, before):
    """No README (404): the repo description is the official text; a failed lookup stays 'failed'."""
    description = ((sources.gh_get(f"repos/{repo}") or {}).get("description") or "").strip()
    if _lookup_failed(sources, before):
        return _result("failed", "github_readme", now=now, note="README를 가져오지 못함")
    if not description:
        return _result("none", "github_readme", now=now, note="README·설명 없음")
    return _result("ok", "github_readme", _cap([description]), now)


def _github_readme(repo, sources, now):
    before = len(getattr(sources, "errors", []))
    readme = sources.gh_get(f"repos/{repo}/readme")
    if readme is None and not _lookup_failed(sources, before):
        return _description_only(repo, sources, now, before)          # 404: there is no README
    if not isinstance(readme, dict) or not isinstance(readme.get("content"), str):
        return _result("failed", "github_readme", now=now, note="README 응답을 읽지 못함")
    if not readme["content"]:                                           # an empty README file
        return _description_only(repo, sources, now, before)
    try:
        text = base64.b64decode(readme["content"]).decode("utf-8")
    except (binascii.Error, ValueError):
        return _result("failed", "github_readme", now=now, note="README가 UTF-8이 아님")
    description = ((sources.gh_get(f"repos/{repo}") or {}).get("description") or "").strip()
    if _lookup_failed(sources, before):   # the README alone is incomplete: use it now, never cache it
        return _result("partial", "github_readme", _cap(_split(text)), now, note="저장소 설명 조회 실패")
    paragraphs = ([description] if description else []) + _split(text)
    return _result("ok", "github_readme", _cap(paragraphs), now)


def _npm(package, sources, now):
    manifest = sources.http_get(NPM_LATEST.format(quote(package, safe="@/")))
    if manifest is None:
        return _result("failed", "npm", now=now, note="npm 설명을 가져오지 못함")
    description = (manifest.get("description") or "").strip()
    if not description:
        return _result("none", "npm", now=now, note="npm 설명 없음")
    return _result("ok", "npm", _cap([description]), now)


def _mcp_description(entity):
    for candidate in (entity.get("resolution") or {}).get("candidates", []):
        if candidate.get("identifier") == entity["identifier"]:
            return ((candidate.get("official") or {}).get("description") or "").strip()
    return ""


def collect_official(entity, sources, now):
    identifier = entity.get("identifier")
    if identifier and not identifier.startswith("https://"):
        return _github_readme(identifier, sources, now)
    if entity.get("npm_package"):
        return _npm(entity["npm_package"], sources, now)
    if identifier:
        description = _mcp_description(entity)
        if description:
            return _result("ok", "mcp_registry", _cap([description]), now)
        return _result("none", "mcp_registry", now=now, note="레지스트리 설명 없음")
    return _result("none", now=now, note="공식 위치 없음")
