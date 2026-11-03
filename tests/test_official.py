import base64
import unittest

from hype_checker.official import MAX_BYTES, collect_official

NOW = 1792800000


class Sources:
    def __init__(self, gh=None, http=None):
        self.gh_map, self.http_map, self.calls = gh or {}, http or {}, []

    def gh_get(self, path):
        self.calls.append(path)
        return self.gh_map.get(path)

    def http_get(self, url):
        self.calls.append(url)
        return self.http_map.get(url)


def encoded(raw: bytes):
    return {"content": base64.encodebytes(raw).decode(), "encoding": "base64"}


class OfficialTest(unittest.TestCase):
    def test_github_readme_split_and_capped(self):
        body = "\n\n".join(f"문단 {i} " + "가" * 200 for i in range(40))
        self.assertGreater(len(body.encode()), 10_000)
        sources = Sources(gh={
            "repos/o/r/readme": encoded(body.encode()),
            "repos/o/r": {"description": "Repo desc"},
        })
        result = collect_official({"identifier": "o/r"}, sources, NOW)
        self.assertEqual((result["status"], result["source"]), ("ok", "github_readme"))
        self.assertEqual(result["paragraphs"][0], "Repo desc")
        self.assertLessEqual(sum(len(p.encode()) for p in result["paragraphs"]), MAX_BYTES)
        self.assertGreater(len(result["paragraphs"]), 3)
        self.assertEqual(result["fetched_at"], NOW)

    def test_single_huge_paragraph_cut_on_codepoint(self):
        sources = Sources(gh={"repos/o/r/readme": encoded(("한" * 5000).encode())})
        result = collect_official({"identifier": "o/r"}, sources, NOW)
        text = "".join(result["paragraphs"])
        self.assertLessEqual(len(text.encode()), MAX_BYTES)
        self.assertTrue(text and set(text) == {"한"})

    def test_non_utf8_readme_fails_softly(self):
        sources = Sources(gh={"repos/o/r/readme": encoded(b"\xff\xfe\x00bad")})
        result = collect_official({"identifier": "o/r"}, sources, NOW)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["note"])

    def test_readme_fetch_failure(self):
        sources = Sources()
        sources.errors = []
        original = sources.gh_get
        sources.gh_get = lambda path: sources.errors.append(f"{path}: failed") or original(path)
        result = collect_official({"identifier": "o/r"}, sources, NOW)
        self.assertEqual(result["status"], "failed")

    def test_missing_readme_uses_repo_description(self):
        sources = Sources(gh={"repos/o/r": {"description": "A desktop agent"}})
        sources.errors = []
        result = collect_official({"identifier": "o/r"}, sources, NOW)
        self.assertEqual((result["status"], result["paragraphs"]), ("ok", ["A desktop agent"]))

    def test_wrong_shaped_readme_reply_is_failed(self):
        for reply in ({}, [], {"content": None}):
            sources = Sources(gh={"repos/o/r/readme": reply, "repos/o/r": {"description": "d"}})
            sources.errors = []
            self.assertEqual(collect_official({"identifier": "o/r"}, sources, NOW)["status"], "failed", reply)

    def test_readme_ok_but_description_lookup_failed_is_partial(self):
        sources = Sources(gh={"repos/o/r/readme": encoded(b"Hello tool")})
        sources.errors = []
        original = sources.gh_get
        sources.gh_get = lambda path: (sources.errors.append("desc: failed") if path == "repos/o/r" else original(path))
        result = collect_official({"identifier": "o/r"}, sources, NOW)
        self.assertEqual((result["status"], result["paragraphs"]), ("partial", ["Hello tool"]))

    def test_empty_readme_file_uses_repo_description(self):
        sources = Sources(gh={"repos/o/r/readme": {"content": "", "encoding": "base64", "size": 0},
                              "repos/o/r": {"description": "fallback"}})
        sources.errors = []
        result = collect_official({"identifier": "o/r"}, sources, NOW)
        self.assertEqual((result["status"], result["paragraphs"]), ("ok", ["fallback"]))

    def test_no_readme_no_description_is_none(self):
        sources = Sources()
        sources.errors = []
        self.assertEqual(collect_official({"identifier": "o/r"}, sources, NOW)["status"], "none")

    def test_npm_description(self):
        sources = Sources(http={"https://registry.npmjs.org/pkg/latest": {"description": "An npm tool"}})
        result = collect_official({"identifier": None, "npm_package": "pkg"}, sources, NOW)
        self.assertEqual((result["status"], result["source"], result["paragraphs"]), ("ok", "npm", ["An npm tool"]))

    def test_mcp_uses_preserved_registry_description(self):
        url = "https://mcp.example.com/mcp"
        entity = {"identifier": url, "resolution": {"status": "resolved", "candidates": [
            {"identifier": "https://other", "official": {"description": "no"}},
            {"identifier": url, "official": {"description": "Registry says hi", "website": None, "repository": None}},
        ]}}
        sources = Sources()
        result = collect_official(entity, sources, NOW)
        self.assertEqual((result["status"], result["source"], result["paragraphs"]),
                         ("ok", "mcp_registry", ["Registry says hi"]))
        self.assertEqual(sources.calls, [])

    def test_unresolved_entity_returns_none(self):
        sources = Sources()
        result = collect_official({"identifier": None, "resolution": {"status": "unresolved", "candidates": []}}, sources, NOW)
        self.assertEqual((result["status"], result["source"], result["paragraphs"]), ("none", None, []))
        self.assertEqual(sources.calls, [])


if __name__ == "__main__":
    unittest.main()
