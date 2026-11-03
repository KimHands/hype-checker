"""PyPI package that belongs to the identified GitHub repo, so deps.dev advisories cover Python tools too."""

from urllib.parse import quote

from hype_checker.measure.result import ok
from hype_checker.resolve import github_repo_from_url

PYPI = "https://pypi.org/pypi/{}/json"


def _linked_repos(info):
    urls = [info.get("home_page") or "", *((info.get("project_urls") or {}).values())]
    return {repo.lower() for repo in (github_repo_from_url(str(url)) for url in urls) if repo}


def measure_pypi(repo, sources):
    """Same-named package whose metadata links back to `repo`, or None (someone else's package is not evidence)."""
    name = repo.split("/")[-1].lower()
    info = (sources.http_get(PYPI.format(quote(name, safe=""))) or {}).get("info") or {}
    if repo.lower() not in _linked_repos(info) or not info.get("version"):
        return None
    return ok({"package": name, "version": str(info["version"]), "repository_matches": True})
