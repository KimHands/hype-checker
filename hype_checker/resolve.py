"""Entity candidate → identifier candidates (design-v1 §4-4).

Stars never raise a score; they only order ties. 'resolved' needs a clear
margin over the runner-up. A domain- or GitHub-verified MCP registry
namespace is strong corroboration; a same-named npm package is weak, since
generic names are often taken by unrelated packages (live case: Buzz).
"""

import re
from urllib.parse import quote, quote_plus

NO_REPO_KINDS = {"product", "feature"}
NAME_EQUAL, NAME_CONTAINS, OWNER_MATCH = 3, 1, 1
FORK_PENALTY, ARCHIVED_PENALTY = -2, -1
NPM_CORROBORATION, REGISTRY_CORROBORATION = 1, 2
SEGMENT_EQUAL, GITHUB_USER_CONTAINS = 2, 1
RESOLVED_MIN, CLEAR_MARGIN, AMBIGUOUS_MIN = 3, 2, 2
ORIGINAL_STAR_FACTOR = 10  # same-name copies: the original is older AND has 10x the stars of every copy
NPM_NAME = re.compile(r"^(@[a-z0-9-~][a-z0-9-._~]*/)?[a-z0-9-~][a-z0-9-._~]*$")
MCP_SEARCH = "https://registry.modelcontextprotocol.io/v0/servers?search={}&limit=5"
NPM_LATEST = "https://registry.npmjs.org/{}/latest"
GITHUB_URL = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


GENERIC_WORDS = {"mcp", "server", "search", "web", "ai", "skill", "skills", "plugin", "connector",
                 "tool", "tools", "app", "claude", "the", "for", "커넥터", "플러그인", "스킬"}
MAX_TERMS = 3


def normalize(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def core_name(text):
    """Drops generic words so 'Liner Web Search MCP' searches as 'liner'."""
    words = re.findall(r"[A-Za-z0-9가-힣.+-]+", text or "")
    core = [w for w in words if w.lower() not in GENERIC_WORDS]
    return " ".join(core or words).strip().lower()


SHORTHAND = re.compile(r"^(?:github:)?([\w.-]+)/([\w.-]+)$")


def github_repo_from_url(url):
    match = GITHUB_URL.search(url or "") or SHORTHAND.match(url or "")
    return f"{match.group(1)}/{match.group(2)}" if match else None


def repository_url(manifest):
    """npm 'repository' is either {"url": ...} or a shorthand string like "owner/repo"."""
    repository = (manifest or {}).get("repository")
    return repository if isinstance(repository, str) else (repository or {}).get("url")


def _search_terms(entity):
    raw = [entity.get("name_latin") or entity["name"], *(entity.get("spellings") or [])]
    terms = []
    for term in (core_name(t) for t in raw if isinstance(t, str)):
        if term and term not in terms:
            terms.append(term)
    return terms[:MAX_TERMS]


def _name_score(name, term):
    name, term = normalize(name), normalize(term)
    if name == term:
        return NAME_EQUAL
    return NAME_CONTAINS if term and term in name else 0


def _github_candidates(term, sources):
    found = sources.gh_get(f"search/repositories?q={quote_plus(term + ' in:name')}&per_page=5") or {}
    candidates = []
    for repo in found.get("items", []):
        owner, name = repo["full_name"].split("/")
        score = _name_score(name, term) + (OWNER_MATCH if normalize(term) in normalize(owner) else 0)
        score += FORK_PENALTY if repo.get("fork") else 0
        score += ARCHIVED_PENALTY if repo.get("archived") else 0
        candidates.append({"identifier": repo["full_name"], "source": "github", "score": score,
                           "stars": repo.get("stargazers_count", 0), "created_at": repo.get("created_at") or "",
                           "evidence": [f"github name match {score}"]})
    return candidates


def _npm_corroborate(term, candidates, sources):
    if not NPM_NAME.match(term):
        return candidates
    package = sources.http_get(NPM_LATEST.format(quote(term, safe="@/")))
    if repository_url(package) == "npm/security-holder":
        return candidates
    repo = github_repo_from_url(repository_url(package))
    if not repo:
        return candidates
    canonical = (sources.gh_get(f"repos/{repo}") or {}).get("full_name", repo)
    note = f"npm:{term} → {canonical}"
    for candidate in candidates:
        if candidate["identifier"].lower() == canonical.lower():
            candidate["score"] += NPM_CORROBORATION
            candidate["evidence"].append(note)
            return candidates
    # npm-only repo: a same-named package may belong to someone else, so score the repo name only
    return candidates + [{"identifier": canonical, "source": "npm", "score": _name_score(canonical.split("/")[1], term),
                          "stars": 0, "evidence": [note]}]


def _namespace_score(server_name, term):
    namespace, _, segment = server_name.partition("/")
    labels = namespace.split(".")
    segment_equal = normalize(segment) == normalize(term)
    if labels[:2] == ["io", "github"]:
        user_contains = normalize(term) in normalize(labels[-1])
        score = (SEGMENT_EQUAL if segment_equal else 0) + (GITHUB_USER_CONTAINS if user_contains else 0)
        if segment_equal and user_contains:
            return score + REGISTRY_CORROBORATION, f"registry namespace {namespace} (GitHub verified)"
        return score, None
    if len(labels) >= 2 and normalize(labels[1]) == normalize(term):
        return NAME_EQUAL + REGISTRY_CORROBORATION, f"registry namespace {namespace} (domain verified)"
    return (SEGMENT_EQUAL if segment_equal else 0), None


def _mcp_candidates(term, sources):
    found = sources.http_get(MCP_SEARCH.format(quote(term))) or {}
    candidates, seen = [], set()
    for entry in found.get("servers", []):
        server = entry["server"]
        if server["name"] in seen:
            continue
        seen.add(server["name"])
        score, note = _namespace_score(server["name"], term)
        remotes = [r["url"] for r in server.get("remotes", []) if r.get("url")]
        repo = github_repo_from_url(repository_url(server))
        identifier = remotes[0] if remotes else repo
        if identifier:
            evidence = [f"mcp registry {server['name']}"] + ([note] if note else [])
            candidates.append({"identifier": identifier, "source": "mcp_registry", "score": score,
                               "stars": 0, "evidence": evidence})
    return candidates


HANGUL = re.compile(r"[가-힣]")


def _corroborated(candidate):
    return any(e.startswith(("npm:", "registry namespace")) for e in candidate["evidence"])


def _status(ranked, needs_corroboration=False):
    if not ranked or ranked[0]["score"] < AMBIGUOUS_MIN:
        return "unresolved"
    top = ranked[0]
    margin = top["score"] - (ranked[1]["score"] if len(ranked) > 1 else 0)
    if needs_corroboration and not _corroborated(top):
        return "ambiguous"
    if top["score"] >= RESOLVED_MIN and (margin >= CLEAR_MARGIN or _original_among_copies(ranked)):
        return "resolved"
    return "ambiguous"


def _original_among_copies(ranked):
    """Among candidates tied on name score, the oldest one with 10x everyone's stars is the original.
    A newer impersonator cannot pass: being older cannot be bought."""
    top = ranked[0]
    tied = [c for c in ranked[1:] if c["score"] == top["score"]]
    if top.get("source") != "github" or not top.get("created_at") or not tied:
        return False
    for other in tied:
        if other.get("source") != "github" or not other.get("created_at"):
            return False
        if top["stars"] < ORIGINAL_STAR_FACTOR * max(other["stars"], 1) or top["created_at"] >= other["created_at"]:
            return False
    top["evidence"].append(f"원본 추정: 같은 이름 중 가장 오래됐고 스타가 {ORIGINAL_STAR_FACTOR}배 이상")
    return True


def _misheard_risk(entity):
    """A Korean spoken name re-spelled by the LLM may be wrong (live case: 모빈 → Moblin)."""
    return bool(HANGUL.search(entity.get("name") or "")) and entity.get("origin") == "transcript"


def resolve(entity, sources):
    if entity.get("kind") in NO_REPO_KINDS:
        return {"status": "no_repo_expected", "candidates": []}
    best = {"status": "unresolved", "candidates": []}
    for index, term in enumerate(_search_terms(entity)):
        result = _resolve_term(term, entity, sources)
        if index > 0 and result["status"] == "resolved":
            result["status"] = "ambiguous"  # a guessed spelling may only propose, never confirm
        if _better(result, best):
            best = result
        if best["status"] == "resolved":
            break
    return best


STATUS_RANK = {"unresolved": 0, "ambiguous": 1, "resolved": 2}


def _better(result, best):
    top = lambda r: r["candidates"][0]["score"] if r["candidates"] else -99
    return (STATUS_RANK[result["status"]], top(result)) > (STATUS_RANK[best["status"]], top(best))


def _resolve_term(term, entity, sources):
    candidates = _mcp_candidates(term, sources) if entity.get("kind") == "mcp" else []
    candidates += _npm_corroborate(term, _github_candidates(term, sources), sources)
    for candidate in candidates:
        candidate["evidence"].append(f"term '{term}'")
    ranked = sorted(candidates, key=lambda c: (c["score"], c["stars"]), reverse=True)
    return {"status": _status(ranked, _misheard_risk(entity)), "candidates": ranked[:3], "term": term}


SEARCH_INTERVAL_S = 2.2  # GitHub search API: 30 requests/minute


class LiveSources:
    """gh api through the runner allowlist, registries through http.get_json."""

    def __init__(self, runner, sleep=None, clock=None):
        import time
        self.runner = runner
        self.errors = []
        self._sleep, self._clock = sleep or time.sleep, clock or time.monotonic
        self._last_search = None

    def _pace_search(self, path):
        if not path.startswith("search/"):
            return
        if self._last_search is not None:
            wait = SEARCH_INTERVAL_S - (self._clock() - self._last_search)
            if wait > 0:
                self._sleep(wait)
        self._last_search = self._clock()

    def gh_get(self, path):
        import json
        from hype_checker.runner import build_gh_get
        self._pace_search(path)
        result = self.runner.run(build_gh_get(path))
        if result.returncode != 0 and "rate limit" in (result.stderr or "").lower():
            self.errors.append(f"{path}: rate limited")
        return json.loads(result.stdout) if result.returncode == 0 and result.stdout else None

    def http_get(self, url):
        from hype_checker.http import get_json
        try:
            return get_json(url)
        except OSError as error:  # HTTPError/URLError: record, never guess
            self.errors.append(f"{url}: {error}")
            return None
