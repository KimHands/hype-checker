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
DOMINANT_FACTOR, DOMINANT_MIN_STARS = 50, 1000  # stars-only tie break: the real repo dwarfs namesakes
NPM_NAME = re.compile(r"^(@[a-z0-9-~][a-z0-9-._~]*/)?[a-z0-9-~][a-z0-9-._~]*$")
MCP_SEARCH = "https://registry.modelcontextprotocol.io/v0/servers?search={}&limit=5"
NPM_LATEST = "https://registry.npmjs.org/{}/latest"
GITHUB_URL = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


GENERIC_WORDS = {"mcp", "server", "search", "web", "ai", "skill", "skills", "plugin", "connector",
                 "tool", "tools", "app", "claude", "the", "for", "커넥터", "플러그인", "스킬"}
MAX_TERMS = 3
DESCRIPTION_CHARS = 200


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
                           "description": str(repo.get("description") or "")[:DESCRIPTION_CHARS],
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


def _mcp_official(server, repo):
    return {"description": server.get("description") or "", "website": server.get("websiteUrl"),
            "repository": repo}


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
                               "stars": 0, "evidence": evidence, "official": _mcp_official(server, repo)})
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
    if top["score"] >= RESOLVED_MIN and (margin >= CLEAR_MARGIN or _original_among_copies(ranked)
                                         or _dominant_by_stars(ranked)):
        return "resolved"
    return "ambiguous"


def _dominant_by_stars(ranked):
    """Same-named repos within the score margin, but the top one has DOMINANT_FACTOR x everyone's stars and is
    well established. An unrelated older repo can share the name (ECC 2015 vs 2026); a copy cannot buy that lead."""
    top = ranked[0]
    contenders = [c for c in ranked[1:] if top["score"] - c["score"] < CLEAR_MARGIN]
    if top.get("source") != "github" or top.get("stars", 0) < DOMINANT_MIN_STARS or not contenders:
        return False
    if any(top["stars"] < DOMINANT_FACTOR * max(c.get("stars", 0), 1) for c in contenders):
        return False
    top["evidence"].append(f"같은 이름 중 스타가 압도적({DOMINANT_FACTOR}배 이상)")
    return True


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


GITHUB_LINK = re.compile(r"(?<![\w.-])(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)", re.IGNORECASE)
GITHUB_PAGES = {"topics", "orgs", "sponsors", "marketplace", "features", "collections", "settings", "apps", "search",
                "trending"}
PACKAGE_KINDS = {"npm", "pypi"}   # a written package and a same-named repo may be different tools


def _written_links(text):
    links = [f"{owner}/{re.sub(r'(?:.git)?[.]*$', '', repo)}" for owner, repo in GITHUB_LINK.findall(text or "")
             if owner.lower() not in GITHUB_PAGES]
    return list(dict.fromkeys(links))


def _spelled_repos(entities):
    return {(e.get(field) or "").strip().lower() for e in entities for field in ("name", "name_latin")
            if SHORTHAND.match((e.get(field) or "").strip())}


def _name_keys(text):
    return {key for key in (normalize(text), normalize(core_name(re.sub(r"[-_.]", " ", text)))) if key}


def _names_link(entity, link):
    repo = link.split("/")[1]
    names = [entity.get("name") or "", entity.get("name_latin") or ""]
    return any(_name_keys(name) & _name_keys(repo) for name in names)


def _with_link(entity, link):
    kind = "repo" if entity.get("kind") in (*NO_REPO_KINDS, "unknown") else entity.get("kind")
    return {**entity, "written_repo": link, "kind": kind}


def attach_written_repos(entities, text):
    """A github.com link in the post beats any name search: it goes to the tool it names, or to the only tool
    the post is about when it is the only link. Links a tool already spells out are that tool's."""
    links = _written_links(text)
    free = [link for link in links if link.lower() not in _spelled_repos(entities)]
    open_for_link = lambda e: e.get("kind") not in PACKAGE_KINDS and not _spelled_repos([e])
    named = [next((link for link in free if open_for_link(e) and _names_link(e, link)), None) for e in entities]
    linked = [_with_link(e, link) if link else e for e, link in zip(entities, named)]
    primaries = [e for e in linked if e.get("role") == "primary"]
    if len(links) == 1 and free and len(primaries) == 1 and not any(named) and open_for_link(primaries[0]) \
            and primaries[0].get("kind") not in NO_REPO_KINDS:
        return [_with_link(e, free[0]) if e is primaries[0] else e for e in linked]
    return linked


def _listed_repo(entity, text):
    name = (entity.get("name") or "").strip()
    return entity.get("kind") == "repo" and SHORTHAND.match(name) and name.lower() in text.lower()


def promote_listed_repos(entities, text):
    """A post that only lists repos ('주간 TOP 10') recommends none of them, yet the owner/repo it wrote is
    what the reader is asking about. A post with its own tool keeps the listed ones as mentions."""
    if any(e.get("role") == "primary" for e in entities):
        return entities
    return [{**e, "role": "primary"} if _listed_repo(e, text) else e for e in entities]


def _written_repo(entity, sources):
    """A post that spells out owner/repo names the repo itself; searching by name could pick a copy instead."""
    for text in (entity.get("written_repo"), entity.get("name"), entity.get("name_latin")):
        match = SHORTHAND.match((text or "").strip())
        if not match or "://" in (text or ""):
            continue
        repo = sources.gh_get(f"repos/{match.group(1)}/{match.group(2)}")
        if repo and repo.get("full_name"):
            return {"status": "resolved", "term": text, "candidates": [{
                "identifier": repo["full_name"], "source": "github", "score": NAME_EQUAL + REGISTRY_CORROBORATION,
                "stars": repo.get("stargazers_count", 0), "created_at": repo.get("created_at") or "",
                "evidence": ["게시물에 적힌 레포 경로 그대로"]}]}
    return None


def resolve(entity, sources):
    if entity.get("kind") in NO_REPO_KINDS:
        return {"status": "no_repo_expected", "candidates": []}
    written = _written_repo(entity, sources)
    if written:
        return written
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


def _registry_name(candidate):
    return next((e[len("mcp registry "):] for e in candidate["evidence"] if e.startswith("mcp registry ")), "")


def _main_server(ranked, term):
    """One verified domain can list several servers (Lazyweb: main and discovery); the one named like the tool is it."""
    tied = [c for c in ranked if c["score"] == ranked[0]["score"]] if ranked else []
    if len(tied) < 2 or not all(c.get("source") == "mcp_registry" and _corroborated(c) for c in tied):
        return None
    if len({_registry_name(c).partition("/")[0] for c in tied}) != 1:
        return None
    named = [c for c in tied if normalize(_registry_name(c).partition("/")[2]) == normalize(term)]
    return named[0] if len(named) == 1 else None


def _resolve_term(term, entity, sources):
    candidates = _mcp_candidates(term, sources) if entity.get("kind") == "mcp" else []
    candidates += _npm_corroborate(term, _github_candidates(term, sources), sources)
    for candidate in candidates:
        candidate["evidence"].append(f"term '{term}'")
    ranked = sorted(candidates, key=lambda c: (c["score"], c["stars"]), reverse=True)
    main = _main_server(ranked, term)
    if main:
        return {"status": "resolved", "candidates": [main, *[c for c in ranked if c is not main]][:3], "term": term}
    return {"status": _status(ranked, _misheard_risk(entity)), "candidates": ranked[:3], "term": term}


SEARCH_INTERVAL_S = 2.2  # GitHub search API: 30 requests/minute
GH_CALL_S = 30           # one gh api call; GitHub answers in seconds
HTTP_CALL_S = 20
MIN_CALL_S = 1


class LiveSources:
    """gh api through the runner allowlist, registries through http.get_json."""

    def __init__(self, runner, sleep=None, clock=None):
        import time
        self.runner = runner
        self.errors = []
        self._sleep, self._clock = sleep or time.sleep, clock or time.monotonic
        self._last_search = None
        self.deadline = None   # monotonic time; set per DM item so no call outlives the item's budget

    def time_left(self):
        return float("inf") if self.deadline is None else self.deadline - self._clock()

    def call_limit(self, cap, asked=None):
        """Seconds this call may take, or None when the item's time is already up."""
        limit = min(cap, asked) if asked else cap
        if self.deadline is None:
            return limit
        left = self.deadline - self._clock()
        return min(limit, left) if left >= MIN_CALL_S else None

    def _pace_search(self, path):
        """Waits for the search rate limit; False (no wait, no search) when that wait would pass the deadline."""
        if not path.startswith("search/"):
            return True
        wait = 0 if self._last_search is None else SEARCH_INTERVAL_S - (self._clock() - self._last_search)
        if self.deadline is not None and self.deadline - self._clock() - max(wait, 0) < MIN_CALL_S:
            return False
        if wait > 0:
            self._sleep(wait)
        self._last_search = self._clock()
        return True

    def gh_get(self, path, timeout_s=None):
        """`timeout_s`: the caller's remaining time; running out counts as a failed request (None)."""
        import json
        from hype_checker.runner import TimeoutExpired, build_gh_get
        paced = self._pace_search(path)
        limit = self.call_limit(GH_CALL_S, timeout_s) if paced else None   # measured after pacing
        if limit is None:
            self.errors.append(f"{path}: out of time")
            return None
        try:
            result = self.runner.run(build_gh_get(path), timeout_s=limit)
        except TimeoutExpired:
            self.errors.append(f"{path}: timed out")
            return None
        stderr = (result.stderr or "").lower()
        if result.returncode != 0 and not ("404" in stderr or "not found" in stderr):
            self.errors.append(f"{path}: {'rate limited' if 'rate limit' in stderr else 'failed'}")
        if result.returncode != 0:
            return None
        try:
            value = json.loads(result.stdout)
        except ValueError:   # empty or broken body on success: a failed lookup, not 'not found'
            value = None
        if value is None:     # includes a literal `null`: only a 404 may mean 'not there'
            self.errors.append(f"{path}: unreadable reply")
        return value

    def http_get(self, url):
        from hype_checker.http import get_json
        limit = self.call_limit(HTTP_CALL_S)
        if limit is None:
            self.errors.append(f"{url}: out of time")
            return None
        try:
            return get_json(url, timeout_s=limit)
        except (OSError, ValueError) as error:  # network, HTTP, empty or broken JSON: record, never guess
            self.errors.append(f"{url}: {type(error).__name__}")
            return None
