import json
import unittest
from pathlib import Path

from hype_checker.measure.github import measure_repo
from hype_checker.measure.npm import measure_npm

FIX = Path(__file__).resolve().parent / "fixtures" / "measure"
NOW = 1792800000  # 2026-10-24


def load(name):
    return json.loads((FIX / name).read_text())


class Sources:
    def __init__(self, gh=None, http=None):
        self.gh_map, self.http_map, self.gh_calls = gh or {}, http or {}, []

    def gh_get(self, path):
        self.gh_calls.append(path)
        return self.gh_map.get(path)

    def http_get(self, url):
        return self.http_map.get(url)


def kordoc_gh():
    return Sources(gh={
        "repos/chrisryugj/kordoc": load("gh_repo_kordoc.json"),
        "repos/chrisryugj/kordoc/contributors?per_page=100": load("gh_contributors_kordoc.json"),
        "repos/chrisryugj/kordoc/stargazers/history?per_page=30&page=1": load("gh_history_kordoc_p1.json"),
        "repos/chrisryugj/kordoc/stargazers/history?per_page=30&page=2": load("gh_history_kordoc_p2.json"),
        "repos/chrisryugj/kordoc/releases?per_page=100": load("gh_releases_kordoc.json"),
    })


class MeasureRepoTest(unittest.TestCase):
    def test_kordoc(self):
        sources = kordoc_gh()
        result = measure_repo("chrisryugj/kordoc", sources, now=NOW)
        meta = result["meta"]["value"]
        self.assertEqual((meta["fork"], meta["archived"], meta["license"]), (False, False, "MIT"))
        self.assertEqual(result["star_history"]["status"], "ok")
        self.assertEqual(result["star_history"]["value"]["pages"], 2)
        self.assertIn("recent_12w", result["star_history"]["value"])
        top = result["contributors"]["value"]
        self.assertEqual(top["top_login"], "chrisryugj")
        self.assertGreater(top["top_share"], 0.8)
        self.assertEqual(result["releases"]["value"], "100+")
        self.assertEqual(result["fork_star"]["value"], "normal")
        self.assertEqual(len(sources.gh_calls), 5)

    def test_missing_repo_fails_without_guessing(self):
        result = measure_repo("nobody/nothing", Sources(), now=NOW)
        self.assertEqual((result["meta"]["value"], result["meta"]["status"]), (None, "failed"))
        self.assertEqual(set(result), {"meta"})


    def test_second_page_failure_is_partial_not_ok(self):
        sources = kordoc_gh()
        del sources.gh_map["repos/chrisryugj/kordoc/stargazers/history?per_page=30&page=2"]
        history = measure_repo("chrisryugj/kordoc", sources, now=NOW)["star_history"]
        self.assertEqual(history["status"], "partial")
        self.assertEqual(history["value"]["pages"], 1)


class MeasureNpmTest(unittest.TestCase):
    def test_kordoc_has_no_install_scripts_and_no_provenance(self):
        sources = Sources(http={
            "https://registry.npmjs.org/kordoc/latest": load("npm_kordoc_latest.json"),
            "https://api.npmjs.org/downloads/point/last-month/kordoc": load("npm_kordoc_downloads.json")})
        result = measure_npm("kordoc", sources, repo="chrisryugj/kordoc")["value"]
        self.assertEqual(result["install_scripts"], {})
        self.assertFalse(result["provenance"])
        self.assertTrue(result["repository_matches"])
        self.assertTrue(result["maintainer_is_owner"])
        self.assertEqual(result["downloads_last_month"], 138725)

    def test_rea_has_provenance_and_only_prepare(self):
        sources = Sources(http={"https://registry.npmjs.org/rea-agents/latest": load("npm_rea_latest.json")})
        result = measure_npm("rea-agents", sources, repo="morluto/rea")["value"]
        self.assertTrue(result["provenance"])
        self.assertEqual(result["install_scripts"], {})
        self.assertIn("prepare", result["git_install_scripts"])
        self.assertIsNone(result["downloads_last_month"])

    def test_renamed_repo_still_matches(self):
        sources = Sources(gh={"repos/ruvnet/claude-flow": load("gh_repo_claude_flow_redirect.json")},
                          http={"https://registry.npmjs.org/ruflo/latest": load("npm_ruflo_latest.json")})
        self.assertTrue(measure_npm("ruflo", sources, repo="ruvnet/ruflo")["value"]["repository_matches"])

    def test_security_holder_is_flagged(self):
        sources = Sources(http={"https://registry.npmjs.org/karakeep/latest": load("npm_karakeep_security_holder.json")})
        value = measure_npm("karakeep", sources, repo="karakeep-app/karakeep")["value"]
        self.assertTrue(value["security_holder"])
        self.assertFalse(value["repository_matches"])

    def test_unknown_package(self):
        self.assertEqual(measure_npm("zz-none", Sources())["status"], "failed")


class DepsDevTest(unittest.TestCase):
    def test_kordoc_has_no_advisories(self):
        from hype_checker.measure.depsdev import measure_depsdev
        sources = Sources(http={
            "https://api.deps.dev/v3/systems/npm/packages/kordoc/versions/4.24.1": load("depsdev_kordoc_version.json"),
            "https://api.deps.dev/v3/projects/github.com%2Fchrisryugj%2Fkordoc": load("depsdev_kordoc_project.json")})
        result = measure_depsdev("npm", "kordoc", "4.24.1", sources, repo="chrisryugj/kordoc")
        self.assertEqual(result["value"]["advisories"], [])
        self.assertIsNone(result["value"]["scorecard"])


if __name__ == "__main__":
    unittest.main()
