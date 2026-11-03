"""Measurements -> readable Korean facts + boolean checks (grading rules read the checks; the DM quotes the texts).

A fact states what was measured, never whether the tool is good. Texts carry no
external free text: only numbers, dates, and identifiers shown elsewhere.
"""

import re
import time
from datetime import datetime, timezone

from hype_checker.labels import located, status_of

ACTIVE_DAYS = 365
STAR_BURST_SHARE = 0.5  # a spike window holding at least half of all stars in it
MEASURED = ("ok", "partial")
VERSION_PATTERN = re.compile(r"^\d+(\.\d+){0,3}([-+][0-9A-Za-z.-]{1,32})?$")
LICENSE_PATTERN = re.compile(r"^[A-Za-z0-9.+-]{1,40}$")
SOURCE_TEXT = {"github_readme": "GitHub README", "npm": "npm 설명", "mcp_registry": "MCP 레지스트리 설명"}


def _fact(fact_id, kind, text):
    return {"id": fact_id, "kind": kind, "text": text}


def _usable(measurement):
    return bool(measurement) and measurement.get("status") in MEASURED and measurement.get("value") is not None


def _parse_time(stamp):
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _days_since(stamp, now):
    pushed = _parse_time(stamp)
    return None if pushed is None else (now - pushed) / 86400


def _identity_facts(entity, official):
    facts = [_fact("identity.located", "identity", "공식 위치 하나를 찾았어요") if located(entity)
             else _fact("identity.not_located", "missing", "공식 위치를 하나로 정하지 못했어요")]
    source = (official or {}).get("source")
    if (official or {}).get("status") in MEASURED and source in SOURCE_TEXT:
        facts.append(_fact("identity.official_text", "identity", f"공식 설명은 {SOURCE_TEXT[source]}에서 가져왔어요"))
    return facts


def _recency_text(days):
    if days <= 30:
        return "최근 30일 안에 업데이트됐어요"
    if days <= ACTIVE_DAYS:
        return f"마지막 업데이트는 약 {int(days)}일 전이에요"
    return f"마지막 업데이트가 {int(days)}일 전이라 1년 넘게 멈춰 있어요"


def _activity_facts(meta, now):
    days = _days_since(meta.get("pushed_at"), now)
    if days is None:
        return [_fact("activity.pushed_unknown", "missing", "마지막 업데이트 날짜를 알 수 없어요")]
    return [_fact("activity.pushed", "activity", _recency_text(days))]


def _contributor_facts(measurement):
    if not _usable(measurement):
        return [_fact("trust.contributors_missing", "missing", "기여자 정보를 가져오지 못했어요")]
    value = measurement["value"]
    sample = "상위 100명 기준 " if "first page" in (measurement.get("note") or "") else ""
    return [_fact("trust.contributors", "trust",
                  f"기여자는 {sample}{value['count']}명이고, 가장 많이 기여한 한 명이 {value['top_share']:.0%}를 맡았어요")]


def _release_fact(measurement):
    if not _usable(measurement):
        return []
    return [_fact("trust.releases", "trust", f"공개된 릴리스는 {measurement['value']}개예요")]


def _repo_facts(meta, github, now):
    license_name = meta.get("license")
    shown_license = license_name if LICENSE_PATTERN.match(license_name or "") else "표기 있음"
    facts = [_fact("trust.stars", "trust", f"스타 {meta.get('stargazers_count') or 0:,}개, 포크 {meta.get('forks_count') or 0:,}개예요"),
             _fact("trust.license", "trust", f"라이선스는 {shown_license}이에요" if shown_license == license_name
                    else "라이선스 표기 있음") if license_name and license_name != "NOASSERTION"
             else _fact("trust.license_missing", "missing", "라이선스 표시를 찾지 못했어요")]
    facts += _activity_facts(meta, now) + _contributor_facts(github.get("contributors"))
    return facts + _release_fact(github.get("releases"))


def _burst_flagged(history):
    if not _usable(history):
        return False
    windows = [history["value"].get("all"), history["value"].get("recent_12w")]
    return any(w and w["spike_days"] > 0 and w["spike_star_share"] >= STAR_BURST_SHARE for w in windows)


def _github_warnings(meta, github):
    warnings = [_fact("warning.fork", "warning", "다른 레포를 복제한 fork 레포예요")] if meta.get("fork") else []
    warnings += [_fact("warning.archived", "warning", "보관(archived) 처리돼 더는 관리되지 않아요")] if meta.get("archived") else []
    return warnings


def _burst_notice(github):
    """Going viral looks the same as buying stars, so this is only pointed out: no penalty, no blocked 👍."""
    if not _burst_flagged(github.get("star_history")):
        return []
    return [_fact("notice.star_burst", "notice", "스타가 짧은 기간에 많이 늘었어요. 화제가 됐거나 홍보 영향일 수 있어요")]


def _github_facts(github, now):
    if not _usable(github.get("meta")):
        return [_fact("measure.github_failed", "missing", "GitHub 레포를 측정하지 못했어요")], []
    meta = github["meta"]["value"]
    return _repo_facts(meta, github, now) + _burst_notice(github), _github_warnings(meta, github)


def _npm_facts(npm):
    if not _usable(npm):
        return [_fact("measure.npm_failed", "missing", "npm 패키지를 측정하지 못했어요")], []
    value = npm["value"]
    if value.get("security_holder"):
        return [], [_fact("warning.security_holder", "warning", "npm이 보안상 내리고 자리만 남긴 이름이에요")]
    warnings = []
    if value.get("install_scripts"):
        warnings.append(_fact("warning.install_scripts", "warning", "npm 설치 때 자동으로 실행되는 스크립트가 있어요"))
    if not value.get("repository_matches"):
        warnings.append(_fact("warning.repo_mismatch", "warning", "npm 패키지가 가리키는 레포가 이 레포와 달라요"))
    version = value.get("version")
    shown = f"npm 패키지 최신 버전은 {version}이에요" if VERSION_PATTERN.match(str(version)) else "npm 패키지를 확인했어요"
    return [_fact("trust.npm_version", "trust", shown)], warnings


def _advisory_warnings(measurements):
    advisories = ((measurements.get("depsdev") or {}).get("value") or {}).get("advisories")
    if not advisories:
        return []
    return [_fact("warning.advisories", "warning", f"알려진 보안 권고가 {len(advisories)}건 있어요")]


def _unmeasured_fact(entity):
    if (entity.get("identifier") or "").startswith("https://"):
        return _fact("measure.remote", "missing", "원격 MCP 주소라 코드는 측정하지 않았어요")
    if status_of(entity) == "no_repo_expected":
        return _fact("measure.product", "missing", "상용 제품이라 공개 코드는 측정하지 않았어요")
    return _fact("measure.none", "missing", "측정한 코드 정보가 없어요")


def _measurement_facts(entity, now):
    measurements = entity.get("measurements") or {}
    facts, warnings = [], []
    for part, build in (("github", lambda m: _github_facts(m, now)), ("npm", _npm_facts)):
        if part in measurements:
            found, flagged = build(measurements[part])
            facts, warnings = facts + found, warnings + flagged
    return facts, warnings + _advisory_warnings(measurements)


def _is_measured(measurements):
    return any(_usable((measurements.get(part) or {}).get("meta") if part == "github" else measurements.get(part))
               for part in ("github", "npm"))


def _is_active(measurements, now):
    meta = (measurements.get("github") or {}).get("meta")
    days = _days_since(meta["value"].get("pushed_at"), now) if _usable(meta) else None
    return days is not None and days <= ACTIVE_DAYS


def _missing_safety_facts(measurements):
    """Unmeasured is never read as safe: star history (or advisories, when npm/PyPI was measured) not ok blocks no_warning."""
    if not _is_measured(measurements):
        return []
    missing = []
    history = (measurements.get("github") or {}).get("star_history")
    if "github" in measurements and not (history and history.get("status") == "ok"):
        missing.append(_fact("missing.star_history", "missing", "스타 증가 기록을 다 확인하지 못했어요"))
    depsdev = measurements.get("depsdev")
    if ("npm" in measurements or "pypi" in measurements) and not (depsdev and depsdev.get("status") == "ok" and depsdev.get("value") is not None):
        missing.append(_fact("missing.advisories", "missing", "보안 권고 정보를 확인하지 못했어요"))
    return missing


def build_facts(entity, official, now=None):
    now = time.time() if now is None else now
    measurements = entity.get("measurements") or {}
    measured_facts, warnings = _measurement_facts(entity, now)
    if not measurements:
        measured_facts = [_unmeasured_fact(entity)]
    unchecked = _missing_safety_facts(measurements)
    facts = _identity_facts(entity, official) + measured_facts + warnings + unchecked
    checks = {"located": located(entity), "measured": _is_measured(measurements),
              "active_1y": _is_active(measurements, now), "no_warning": not warnings and not unchecked}
    return {"facts": facts, "checks": checks}
