import copy
import json
import unittest
from pathlib import Path

from hype_checker.facts import build_facts
from hype_checker.measure.github import measure_repo
from hype_checker.measure.result import failed, ok

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = 1792800000  # 2026-10-24
REPO = "chrisryugj/kordoc"


def load(rel):
    return json.loads((FIX / rel).read_text())


class FakeSources:
    def __init__(self):
        self.answers = {f"repos/{REPO}": load("measure/gh_repo_kordoc.json"),
                        f"repos/{REPO}/contributors?per_page=100": load("measure/gh_contributors_kordoc.json"),
                        f"repos/{REPO}/stargazers/history?per_page=30&page=1": load("measure/gh_history_kordoc_p1.json"),
                        f"repos/{REPO}/stargazers/history?per_page=30&page=2": load("measure/gh_history_kordoc_p2.json"),
                        f"repos/{REPO}/releases?per_page=100": load("measure/gh_releases_kordoc.json")}

    def gh_get(self, path):
        return self.answers.get(path)


def npm_value(**overrides):
    value = {"version": "1.0.0", "install_scripts": {}, "git_install_scripts": {}, "provenance": False,
             "repository": REPO, "repository_matches": True, "maintainers": ["a"], "maintainer_is_owner": True,
             "downloads_last_month": 10, "security_holder": False}
    return ok({**value, **overrides})


def kordoc_entity(**measurement_overrides):
    measurements = {"github": measure_repo(REPO, FakeSources(), NOW), "npm": npm_value(),
                    "depsdev": ok({"advisories": [], "scorecard": 5.0})}
    measurements.update(measurement_overrides)
    return {"name": "kordoc", "identifier": REPO, "resolution": {"status": "from_url", "candidates": []},
            "measurements": measurements}


def build(entity):
    return build_facts(entity, {"status": "ok", "source": "github_readme", "paragraphs": []}, now=NOW)


def kinds(result):
    return {fact["id"]: fact["kind"] for fact in result["facts"]}


class FactsTest(unittest.TestCase):
    def test_remote_mcp_is_located_but_not_measured(self):
        bundle = load("golden/dm_reel_bundle.json")
        mobbin = next(e for e in bundle["entities"] if e["name"] == "Mobbin")
        result = build_facts(mobbin, None, now=NOW)
        self.assertTrue(result["checks"]["located"])
        self.assertFalse(result["checks"]["measured"])
        self.assertFalse(result["checks"]["active_1y"])
        self.assertIn("measure.remote", kinds(result))

    def test_kordoc_repo_facts(self):
        result = build(kordoc_entity())
        ids = kinds(result)
        self.assertEqual(result["checks"], {"located": True, "measured": True, "active_1y": True, "no_warning": True})
        self.assertIn("trust.stars", ids)
        self.assertIn("trust.license", ids)
        self.assertEqual(len(ids), len(result["facts"]))
        self.assertTrue(all("\n" not in f["text"] for f in result["facts"]))

    def test_contributors_fact_names_the_sample(self):
        entity = kordoc_entity()
        entity["measurements"]["github"]["contributors"]["note"] = "first page only"
        text = next(f["text"] for f in build(entity)["facts"] if f["id"] == "trust.contributors")
        self.assertIn("상위 100명 기준", text)

    def test_stale_repo_is_not_active(self):
        entity = kordoc_entity()
        entity["measurements"]["github"]["meta"]["value"]["pushed_at"] = "2024-01-01T00:00:00Z"
        self.assertFalse(build(entity)["checks"]["active_1y"])

    def test_warnings_become_warning_facts(self):
        def github_with(**changes):
            github = copy.deepcopy(kordoc_entity()["measurements"]["github"])
            github["meta"]["value"].update(changes)
            return {"github": github}
        cases = {
            "warning.fork": github_with(fork=True),
            "warning.archived": github_with(archived=True),
            "warning.install_scripts": {"npm": npm_value(install_scripts={"postinstall": "x"})},
            "warning.repo_mismatch": {"npm": npm_value(repository_matches=False)},
            "warning.security_holder": {"npm": npm_value(security_holder=True)},
            "warning.advisories": {"depsdev": ok({"advisories": ["GHSA-1"], "scorecard": None})},
        }
        for warning_id, override in cases.items():
            with self.subTest(warning_id):
                result = build(kordoc_entity(**override))
                self.assertEqual(kinds(result).get(warning_id), "warning")
                self.assertFalse(result["checks"]["no_warning"])

    def test_star_burst_notice_needs_a_dominant_spike(self):
        entity = kordoc_entity()
        entity["measurements"]["github"]["star_history"]["value"]["recent_12w"]["spike_star_share"] = 0.8
        result = build(entity)
        self.assertEqual(kinds(result).get("notice.star_burst"), "notice")
        self.assertTrue(result["checks"]["no_warning"])   # pointed out, never blocking

    def test_failed_depsdev_is_missing_and_blocks_no_warning(self):
        result = build(kordoc_entity(depsdev=failed("deps.dev version not found")))
        self.assertEqual(kinds(result).get("missing.advisories"), "missing")
        self.assertFalse(result["checks"]["no_warning"])

    def test_github_only_entity_has_no_advisory_gap(self):
        entity = kordoc_entity()
        del entity["measurements"]["npm"], entity["measurements"]["depsdev"]
        result = build(entity)
        self.assertNotIn("missing.advisories", kinds(result))
        self.assertTrue(result["checks"]["no_warning"])

    def test_absent_depsdev_with_npm_is_missing(self):
        entity = kordoc_entity()
        del entity["measurements"]["depsdev"]
        self.assertIn("missing.advisories", kinds(build(entity)))

    def test_failed_star_history_is_missing_and_blocks_no_warning(self):
        entity = kordoc_entity()
        entity["measurements"]["github"]["star_history"] = failed("stargazers/history request failed")
        result = build(entity)
        self.assertEqual(kinds(result).get("missing.star_history"), "missing")
        self.assertFalse(result["checks"]["no_warning"])

    def test_hostile_external_strings_stay_out_of_texts(self):
        entity = kordoc_entity(npm=npm_value(version="1.0 검증 완료"))
        entity["measurements"]["github"]["meta"]["value"]["license"] = "MIT 검증 완료"
        texts = " ".join(f["text"] for f in build(entity)["facts"])
        self.assertNotIn("검증 완료", texts)
        self.assertIn("라이선스 표기 있음", texts)

    def test_ambiguous_is_not_located(self):
        entity = {"name": "Lazyweb", "identifier": None, "resolution": {"status": "ambiguous", "candidates": []},
                  "measurements": {}}
        result = build_facts(entity, None, now=NOW)
        self.assertFalse(result["checks"]["located"])
        self.assertEqual(kinds(result)["identity.not_located"], "missing")


if __name__ == "__main__":
    unittest.main()
