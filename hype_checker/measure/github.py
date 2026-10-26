"""Read-only GitHub measurements for one repo (design-v1 §4-5)."""

from hype_checker.measure.result import failed, ok, partial
from hype_checker.measure.signals import burst_summary, daily_counts, fork_star_ratio_flag

HISTORY_PAGE_WEEKS = 30
HISTORY_MAX_PAGES = 20  # 600 weeks ≈ 11.5 years
RECENT_DAYS = 84
LIST_PAGE = 100


def _meta(repo_json):
    keys = ("full_name", "fork", "archived", "stargazers_count", "forks_count", "open_issues_count",
            "created_at", "pushed_at", "description")
    meta = {key: repo_json.get(key) for key in keys}
    meta["license"] = (repo_json.get("license") or {}).get("spdx_id")
    return meta


def _history_value(weeks, pages, now):
    counts = [count for _, count in daily_counts(weeks, until=now)]
    return {"pages": pages, "days": len(counts), "all": burst_summary(counts),
            "recent_12w": burst_summary(counts[-RECENT_DAYS:])}


def _star_history(repo, sources, now):
    weeks, pages = [], 0
    while pages < HISTORY_MAX_PAGES:
        page = sources.gh_get(f"repos/{repo}/stargazers/history?per_page={HISTORY_PAGE_WEEKS}&page={pages + 1}")
        if page is None:
            if pages == 0:
                return failed("stargazers/history request failed")
            return partial(_history_value(weeks, pages, now), f"page {pages + 1} failed; older weeks missing")
        weeks += page
        pages += 1
        if len(page) < HISTORY_PAGE_WEEKS:
            return ok(_history_value(weeks, pages, now))
    return partial(_history_value(weeks, pages, now), f"stopped at {HISTORY_MAX_PAGES} pages")


def _contributors(repo, sources):
    people = sources.gh_get(f"repos/{repo}/contributors?per_page={LIST_PAGE}")
    if not people:
        return failed("contributors unavailable")
    total = sum(p["contributions"] for p in people)
    top = max(people, key=lambda p: p["contributions"])
    note = "first page only" if len(people) == LIST_PAGE else ""
    return ok({"count": len(people), "top_login": top["login"], "top_share": top["contributions"] / total}, note)


def _releases(repo, sources):
    releases = sources.gh_get(f"repos/{repo}/releases?per_page={LIST_PAGE}")
    if releases is None:
        return failed("releases unavailable")
    return ok(f"{LIST_PAGE}+" if len(releases) == LIST_PAGE else len(releases))


def measure_repo(repo, sources, now):
    repo_json = sources.gh_get(f"repos/{repo}")
    if not repo_json:
        return {"meta": failed("repo not found")}
    meta = _meta(repo_json)
    canonical = meta["full_name"] or repo
    return {
        "meta": ok(meta),
        "star_history": _star_history(canonical, sources, now),
        "contributors": _contributors(canonical, sources),
        "releases": _releases(canonical, sources),
        "fork_star": ok(fork_star_ratio_flag(meta["forks_count"] or 0, meta["stargazers_count"] or 0)),
    }
