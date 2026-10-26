"""deps.dev advisories and OpenSSF Scorecard (design-v1 §4-5)."""

from urllib.parse import quote

from hype_checker.measure.result import failed, ok

VERSION = "https://api.deps.dev/v3/systems/{}/packages/{}/versions/{}"
PROJECT = "https://api.deps.dev/v3/projects/{}"


def measure_depsdev(system, package, version, sources, repo=None):
    detail = sources.http_get(VERSION.format(system, quote(package, safe=""), quote(version, safe="")))
    if not detail:
        return failed("deps.dev version not found")
    project = sources.http_get(PROJECT.format(quote(f"github.com/{repo}", safe=""))) if repo else None
    scorecard = (project or {}).get("scorecard") or {}
    return ok({
        "advisories": [a.get("id") for a in detail.get("advisoryKeys", [])],
        "scorecard": scorecard.get("overallScore"),
    })
