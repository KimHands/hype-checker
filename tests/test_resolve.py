import json
import unittest
from pathlib import Path

from hype_checker.resolve import github_repo_from_url, normalize, resolve

FIX = Path(__file__).resolve().parent / "fixtures" / "resolve"


def load(name):
    return json.loads((FIX / name).read_text())


class FakeSources:
    def __init__(self, gh=None, http=None):
        self.gh_map, self.http_map, self.gh_calls = gh or {}, http or {}, []

    def gh_get(self, path):
        self.gh_calls.append(path)
        return self.gh_map.get(path)

    def http_get(self, url):
        return self.http_map.get(url)


def candidate(name, kind="repo", latin=None):
    return {"name": name, "name_latin": latin or name, "kind": kind, "role": "primary", "evidence_quote": ""}


class HelpersTest(unittest.TestCase):
    def test_core_name(self):
        from hype_checker.resolve import core_name
        self.assertEqual(core_name("Liner Web Search MCP"), "liner")
        self.assertEqual(core_name("Claude Figma Connector"), "figma")
        self.assertEqual(core_name("MCP"), "mcp")

    def test_spellings_only_propose(self):
        sources = FakeSources(http={
            "https://registry.modelcontextprotocol.io/v0/servers?search=mobbin&limit=5": load("mcp_search_mobbin.json")})
        entity = {**candidate("Moblin MCP", kind="mcp"), "spellings": ["Mobbin", "Mobin"]}
        result = resolve(entity, sources)
        self.assertEqual((result["status"], result["term"]), ("ambiguous", "mobbin"))
        self.assertEqual(result["candidates"][0]["identifier"], "https://api.mobbin.com/mcp")

    def test_normalize(self):
        self.assertEqual(normalize("Claude-Flow"), "claudeflow")

    def test_string_repository_shorthand(self):
        from hype_checker.resolve import repository_url
        self.assertEqual(github_repo_from_url(repository_url({"repository": "npm/security-holder"})), "npm/security-holder")
        self.assertEqual(github_repo_from_url(repository_url({"repository": "github:a/b"})), "a/b")

    def test_github_repo_from_url(self):
        self.assertEqual(github_repo_from_url("git+https://github.com/ruvnet/claude-flow.git"), "ruvnet/claude-flow")
        self.assertIsNone(github_repo_from_url("https://gitlab.com/a/b"))


class ResolveRepoTest(unittest.TestCase):
    def ruflo_sources(self):
        return FakeSources(
            gh={"search/repositories?q=ruflo+in%3Aname&per_page=5": load("gh_search_ruflo.json"),
                "repos/ruvnet/claude-flow": load("gh_repo_claude_flow_redirect.json")},
            http={"https://registry.npmjs.org/ruflo/latest": load("npm_ruflo_latest.json")})

    def test_transliterated_name_resolves_with_npm_corroboration(self):
        result = resolve(candidate("루플로", latin="Ruflo"), self.ruflo_sources())
        self.assertEqual(result["status"], "resolved")
        top = result["candidates"][0]
        self.assertEqual(top["identifier"], "ruvnet/ruflo")
        self.assertIn("npm:ruflo → ruvnet/ruflo", top["evidence"])

    def test_misheard_korean_name_needs_corroboration(self):
        sources = FakeSources(gh={"search/repositories?q=moblin+in%3Aname&per_page=5": {"items": [
            {"full_name": "eerimoq/moblin", "fork": False, "archived": False, "stargazers_count": 900}]}})
        entity = {**candidate("모빈 MCP", kind="mcp", latin="Moblin MCP"), "origin": "transcript"}
        self.assertEqual(resolve(entity, sources)["status"], "ambiguous")
        entity["origin"] = "caption"
        self.assertEqual(resolve(entity, sources)["status"], "resolved")

    def test_ruflo_from_transcript_still_resolves_via_npm(self):
        entity = {**candidate("루플로", latin="Ruflo"), "origin": "transcript"}
        self.assertEqual(resolve(entity, self.ruflo_sources())["status"], "resolved")

    def test_keeps_top_three_candidates(self):
        result = resolve(candidate("Ruflo"), self.ruflo_sources())
        self.assertLessEqual(len(result["candidates"]), 3)

    def test_same_name_repos_without_corroboration_are_ambiguous(self):
        sources = FakeSources(gh={"search/repositories?q=graphify+in%3Aname&per_page=5": load("gh_search_graphify.json")})
        result = resolve(candidate("graphify"), sources)
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["candidates"][0]["identifier"], "Graphify-Labs/graphify")

    def test_nothing_found_is_unresolved(self):
        result = resolve(candidate("zz-nothing"), FakeSources(gh={"search/repositories?q=zz-nothing+in%3Aname&per_page=5": {"items": []}}))
        self.assertEqual((result["status"], result["candidates"]), ("unresolved", []))


class NpmOnlyTest(unittest.TestCase):
    def test_npm_only_repo_is_not_corroborated(self):
        sources = FakeSources(
            gh={"search/repositories?q=liner+in%3Aname&per_page=5": {"items": []},
                "repos/someone/unrelated": {"full_name": "someone/unrelated"}},
            http={"https://registry.npmjs.org/liner/latest": {"repository": {"url": "https://github.com/someone/unrelated"}}})
        result = resolve(candidate("liner"), sources)
        self.assertEqual(result["candidates"][0]["identifier"], "someone/unrelated")
        self.assertEqual(result["candidates"][0]["score"], 0)
        self.assertEqual(result["status"], "unresolved")


class ResolveMcpTest(unittest.TestCase):
    def test_remote_mcp_resolves_to_endpoint_via_verified_namespace(self):
        sources = FakeSources(http={
            "https://registry.modelcontextprotocol.io/v0/servers?search=liner&limit=5": load("mcp_search_liner.json")})
        result = resolve(candidate("Liner Search MCP", kind="mcp", latin="Liner"), sources)
        self.assertEqual(result["status"], "resolved")
        top = result["candidates"][0]
        self.assertEqual(top["identifier"], "https://platform.liner.com/api/v1/mcp")
        self.assertIn("registry namespace com.liner (domain verified)", top["evidence"])

    def test_mobbin(self):
        sources = FakeSources(http={
            "https://registry.modelcontextprotocol.io/v0/servers?search=mobbin&limit=5": load("mcp_search_mobbin.json")})
        result = resolve(candidate("Mobbin", kind="mcp"), sources)
        self.assertEqual(result["candidates"][0]["identifier"], "https://api.mobbin.com/mcp")


class LiveRegressionTest(unittest.TestCase):
    """Cases that went wrong against the live APIs on 2026-10-24."""

    def test_generic_name_with_unrelated_npm_package_stays_ambiguous(self):
        sources = FakeSources(
            gh={"search/repositories?q=buzz+in%3Aname&per_page=5": load("gh_search_buzz.json"),
                "repos/jaysalvat/buzz": load("gh_repo_jaysalvat_buzz.json")},
            http={"https://registry.npmjs.org/buzz/latest": load("npm_buzz_latest.json")})
        self.assertEqual(resolve(candidate("Buzz"), sources)["status"], "ambiguous")

    def test_verified_mcp_namespace_beats_same_name_github_repos(self):
        for term, endpoint in (("liner", "https://platform.liner.com/api/v1/mcp"),
                               ("refero", "https://api.refero.design/mcp")):
            with self.subTest(term=term):
                sources = FakeSources(
                    gh={f"search/repositories?q={term}+in%3Aname&per_page=5": load(f"gh_search_{term}.json")},
                    http={f"https://registry.modelcontextprotocol.io/v0/servers?search={term}&limit=5": load(f"mcp_search_{term}.json")})
                result = resolve(candidate(term.title(), kind="mcp"), sources)
                self.assertEqual((result["status"], result["candidates"][0]["identifier"]), ("resolved", endpoint))

    def test_names_that_are_not_npm_names_skip_npm(self):
        sources = FakeSources(gh={"search/repositories?q=image+code+in%3Aname&per_page=5": {"items": []}})
        sources.http_get = lambda url: self.fail(f"unexpected npm call {url}") if "npmjs" in url else None
        resolve(candidate("Image Code", kind="mcp"), sources)


def repo_item(full_name, stars, created, fork=False):
    return {"full_name": full_name, "fork": fork, "archived": False, "stargazers_count": stars, "created_at": created}


class OriginalVersusCopiesTest(unittest.TestCase):
    def search(self, term, items):
        return FakeSources(gh={f"search/repositories?q={term}+in%3Aname&per_page=5": {"items": items}})

    def test_oldest_with_ten_times_stars_is_the_original(self):
        sources = self.search("notebooklm-py", [
            repo_item("teng-lin/notebooklm-py", 19870, "2025-01-01T00:00:00Z"),
            repo_item("wx-b/notebooklm-py", 40, "2025-06-01T00:00:00Z"),
            repo_item("patriarchal-boothose896/notebooklm-py", 3, "2026-04-01T00:00:00Z")])
        result = resolve(candidate("notebooklm-py"), sources)
        self.assertEqual((result["status"], result["candidates"][0]["identifier"]), ("resolved", "teng-lin/notebooklm-py"))
        self.assertTrue(any("원본" in e for e in result["candidates"][0]["evidence"]))

    def test_newer_repo_with_more_stars_is_not_assumed_original(self):
        sources = self.search("ghidramcp", [
            repo_item("LaurieWired/GhidraMCP", 11505, "2025-03-01T00:00:00Z"),
            repo_item("bethington/ghidramcp", 900, "2024-12-01T00:00:00Z")])
        self.assertEqual(resolve(candidate("GhidraMCP"), sources)["status"], "ambiguous")

    def test_two_real_projects_with_close_stars_stay_ambiguous(self):
        sources = self.search("agent-skills", [
            repo_item("addyosmani/agent-skills", 109954, "2025-01-01T00:00:00Z"),
            repo_item("vercel-labs/agent-skills", 32905, "2025-02-01T00:00:00Z")])
        self.assertEqual(resolve(candidate("agent-skills"), sources)["status"], "ambiguous")

    def test_misheard_names_still_need_corroboration(self):
        sources = self.search("moblin", [repo_item("eerimoq/moblin", 900, "2023-01-01T00:00:00Z"),
                                         repo_item("x/moblin", 2, "2025-01-01T00:00:00Z")])
        entity = {**candidate("모빈", kind="mcp", latin="Moblin"), "origin": "transcript"}
        self.assertEqual(resolve(entity, sources)["status"], "ambiguous")


class PacingTest(unittest.TestCase):
    def test_search_calls_are_spaced(self):
        import subprocess
        from hype_checker.resolve import SEARCH_INTERVAL_S, LiveSources

        class R:
            log = []
            def run(self, argv):
                return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

        waits, now = [], [0.0]
        sources = LiveSources(R(), sleep=waits.append, clock=lambda: now[0])
        sources.gh_get("search/repositories?q=a")
        sources.gh_get("repos/a/b")
        sources.gh_get("search/repositories?q=b")
        self.assertEqual(waits, [SEARCH_INTERVAL_S])


class ResolveProductTest(unittest.TestCase):
    def test_products_and_features_are_not_searched(self):
        sources = FakeSources()
        for kind in ("product", "feature"):
            with self.subTest(kind=kind):
                result = resolve(candidate("Aside", kind=kind), sources)
                self.assertEqual(result["status"], "no_repo_expected")
        self.assertEqual(sources.gh_calls, [])


if __name__ == "__main__":
    unittest.main()
