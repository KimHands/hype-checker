import tempfile
import unittest
from pathlib import Path

from hype_checker.ingest import classify


class ClassifyTest(unittest.TestCase):
    def test_local_mp4(self):
        path = Path(tempfile.mkdtemp()) / "reel.mp4"
        path.write_bytes(b"")
        source = classify(str(path))
        self.assertEqual((source.kind, source.platform), ("mp4", "local"))

    def test_missing_mp4_path_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            classify("/nope/reel.mp4")

    def test_video_platforms(self):
        cases = {
            "https://www.instagram.com/reel/ABC123/": "instagram",
            "https://youtu.be/QWMIQFJye-A": "youtube",
            "https://www.youtube.com/watch?v=55pTFVoclvE": "youtube",
        }
        for url, platform in cases.items():
            with self.subTest(url=url):
                source = classify(url)
                self.assertEqual((source.kind, source.platform), ("url", platform))
                self.assertTrue(source.needs_download)

    def test_threads_asks_for_pasted_caption(self):
        source = classify("https://www.threads.net/@someone/post/XYZ")
        self.assertEqual(source.platform, "threads")
        self.assertFalse(source.needs_download)
        self.assertIn("캡션", source.note)

    def test_github_url_is_normalized(self):
        for url in ["https://github.com/morluto/rea", "https://github.com/morluto/rea/", "github.com/morluto/rea/tree/main/src"]:
            with self.subTest(url=url):
                source = classify(url)
                self.assertEqual((source.platform, source.identifier), ("github", "morluto/rea"))
                self.assertFalse(source.needs_download)

    def test_npm_and_pypi_urls(self):
        self.assertEqual(classify("https://www.npmjs.com/package/rea-agents").identifier, "rea-agents")
        self.assertEqual(classify("https://www.npmjs.com/package/@claude-flow/cli").identifier, "@claude-flow/cli")
        self.assertEqual(classify("https://pypi.org/project/sovereign/").identifier, "sovereign")

    def test_other_text_is_caption(self):
        source = classify("클로드 코드에 Ruflo 붙이면 AI 개발팀이 통째로")
        self.assertEqual((source.kind, source.platform), ("text", "pasted"))


if __name__ == "__main__":
    unittest.main()
