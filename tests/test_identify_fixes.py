"""Holdout follow-ups: a repo path written in the post is used as-is; PyPI packages get advisories too."""

import unittest

from hype_checker.facts import build_facts
from hype_checker.measure.pypi import measure_pypi
from hype_checker.resolve import resolve
from tests.test_resolve import FakeSources, candidate

RUFLO = {"full_name": "ruvnet/ruflo", "stargazers_count": 12000, "created_at": "2025-06-01T00:00:00Z", "fork": False}


class RepoPathInPostTest(unittest.TestCase):
    def test_written_repo_path_is_used_without_search(self):
        sources = FakeSources(gh={"repos/ruvnet/ruflo": RUFLO})
        result = resolve(candidate("ruvnet/ruflo"), sources)
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["candidates"][0]["identifier"], "ruvnet/ruflo")
        self.assertFalse(any(path.startswith("search/") for path in sources.gh_calls))

    def test_missing_repo_path_falls_back_to_search(self):
        sources = FakeSources(gh={"search/repositories?q=ruflo+in%3Aname&per_page=5": {"items": []}})
        result = resolve(candidate("ghost-owner/ruflo"), sources)
        self.assertNotEqual(result.get("candidates", [{}])[:1], [{"identifier": "ghost-owner/ruflo"}])
        self.assertIn("repos/ghost-owner/ruflo", sources.gh_calls)

    def test_url_is_not_a_repo_path(self):
        sources = FakeSources()
        resolve(candidate("https://evil.example/a"), sources)
        self.assertNotIn("repos/https:/", " ".join(sources.gh_calls))


PYPI_LITELLM = {"info": {"version": "1.80.0", "home_page": "",
                         "project_urls": {"Source": "https://github.com/BerriAI/litellm"}}}


class PypiTest(unittest.TestCase):
    def test_matching_repository_is_measured(self):
        sources = FakeSources(http={"https://pypi.org/pypi/litellm/json": PYPI_LITELLM})
        result = measure_pypi("BerriAI/litellm", sources)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["value"], {"package": "litellm", "version": "1.80.0", "repository_matches": True})

    def test_someone_elses_package_is_ignored(self):
        other = {"info": {"version": "0.1", "home_page": "https://github.com/someone/else", "project_urls": {}}}
        sources = FakeSources(http={"https://pypi.org/pypi/litellm/json": other})
        self.assertIsNone(measure_pypi("BerriAI/litellm", sources))

    def test_no_package_is_none(self):
        self.assertIsNone(measure_pypi("a/b", FakeSources()))

    def test_unmeasured_advisories_block_no_warning_for_pypi_too(self):
        from tests.test_facts import build, kinds, kordoc_entity
        entity = kordoc_entity()
        del entity["measurements"]["npm"], entity["measurements"]["depsdev"]
        entity["measurements"]["pypi"] = {"status": "ok", "value": {"package": "kordoc", "version": "1",
                                                                    "repository_matches": True}}
        result = build(entity)
        self.assertIn("missing.advisories", kinds(result))
        self.assertFalse(result["checks"]["no_warning"])

if __name__ == "__main__":
    unittest.main()


def ranked(*rows):
    return [{"identifier": i, "source": "github", "score": s, "stars": st, "created_at": c, "evidence": []}
            for i, s, st, c in rows]


class DominantStarsTest(unittest.TestCase):
    """GitHub TOP10 reel: the real repo dwarfs same-named repos even when an unrelated one is older."""

    def test_dominant_repo_resolves_even_if_newer(self):
        from hype_checker.resolve import _status
        rows = ranked(("affaan-m/ECC", 3, 294223, "2026-01-18"), ("andreacorbellini/ecc", 3, 459, "2015-02-24"),
                      ("mys007/ecc", 3, 234, "2017-07-20"))
        self.assertEqual(_status(rows), "resolved")

    def test_close_runner_up_within_margin_counts(self):
        from hype_checker.resolve import _status
        rows = ranked(("tashfeenahmed/freellmapi", 4, 45286, "2026-04-21"), ("sunsky89757/freellmapi", 3, 73, "2025-03-09"))
        self.assertEqual(_status(rows), "resolved")

    def test_no_dominance_stays_ambiguous(self):
        from hype_checker.resolve import _status
        rows = ranked(("a/tool", 3, 5000, "2026-01-01"), ("b/tool", 3, 900, "2025-01-01"))
        self.assertEqual(_status(rows), "ambiguous")

    def test_small_repos_never_resolve_by_stars(self):
        from hype_checker.resolve import _status
        rows = ranked(("a/tool", 3, 600, "2026-01-01"), ("b/tool", 3, 2, "2025-01-01"))
        self.assertEqual(_status(rows), "ambiguous")
