"""Read-only npm registry measurements; never downloads the tarball (design-v1 §4-5)."""

from urllib.parse import quote

from hype_checker.measure.result import failed, ok
from hype_checker.resolve import github_repo_from_url, repository_url

SECURITY_HOLDER = "npm/security-holder"

INSTALL_HOOKS = ("preinstall", "install", "postinstall")
GIT_INSTALL_HOOKS = ("prepare",)
LATEST = "https://registry.npmjs.org/{}/latest"
DOWNLOADS = "https://api.npmjs.org/downloads/point/last-month/{}"
DOWNLOADS_NOTE = "npx 실행·미러 수집도 집계되므로 사용자 수가 아니다"


def _pick(scripts, names):
    return {name: scripts[name] for name in names if name in scripts}


def _same_repo(linked, repo, sources):
    if not (linked and repo):
        return False
    if linked.lower() == repo.lower():
        return True
    canonical = (sources.gh_get(f"repos/{linked}") or {}).get("full_name", "")
    return canonical.lower() == repo.lower()


def measure_npm(package, sources, repo=None):
    manifest = sources.http_get(LATEST.format(quote(package, safe="@/")))
    if not manifest:
        return failed("package not found")
    scripts = manifest.get("scripts") or {}
    raw_repository = repository_url(manifest)
    linked = github_repo_from_url(raw_repository)
    maintainers = [m.get("name") for m in manifest.get("maintainers", [])]
    downloads = sources.http_get(DOWNLOADS.format(quote(package, safe="@/")))
    owner = repo.split("/")[0].lower() if repo else None
    return ok({
        "version": manifest.get("version"),
        "install_scripts": _pick(scripts, INSTALL_HOOKS),
        "git_install_scripts": _pick(scripts, GIT_INSTALL_HOOKS),
        "provenance": bool((manifest.get("dist") or {}).get("attestations")),
        "repository": linked,
        "repository_matches": _same_repo(linked, repo, sources),
        "maintainers": maintainers,
        "maintainer_is_owner": bool(owner and owner in (m.lower() for m in maintainers if m)),
        "downloads_last_month": (downloads or {}).get("downloads"),
        "security_holder": raw_repository == SECURITY_HOLDER or "security holding" in (manifest.get("description") or "").lower(),
    }, DOWNLOADS_NOTE)
