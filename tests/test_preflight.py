import tempfile
import unittest
from pathlib import Path

from hype_checker.preflight import problems


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def write(self, rel, text=""):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_clean_home_passes(self):
        self.write(".config/watch/.env", "WATCH_ENGINE=local\nGROQ_API_KEY=x\n")
        self.assertEqual(problems(self.home, env={}), [])

    def test_cookie_settings_in_watch_env_file_block(self):
        self.write(".config/watch/.env", "WATCH_COOKIES_FROM_BROWSER=chrome\n")
        self.assertIn("WATCH_COOKIES_FROM_BROWSER", " ".join(problems(self.home, env={})))

    def test_commented_or_empty_cookie_setting_is_ignored(self):
        self.write(".config/watch/.env", "# WATCH_COOKIES_FILE=/x\nWATCH_COOKIES_FILE=\n")
        self.assertEqual(problems(self.home, env={}), [])

    def test_bom_and_utf16_files_are_read_like_watch_reads_them(self):
        for encoding, prefix in (("utf-8", "\ufeff"), ("utf-16", "")):
            with self.subTest(encoding=encoding):
                home = Path(tempfile.mkdtemp())
                path = home / ".config/watch/.env"
                path.parent.mkdir(parents=True)
                path.write_text(prefix + "WATCH_COOKIES_FROM_BROWSER=chrome\n", encoding=encoding)
                self.assertTrue(problems(home, env={}))

    def test_unreadable_config_blocks(self):
        self.write(".config/watch/.env", "this is not dotenv ===\n\x00")
        self.assertTrue(problems(self.home, env={}))

    def test_cookie_settings_in_environment_block(self):
        self.assertTrue(problems(self.home, env={"WATCH_COOKIES_FILE": "/tmp/c.txt"}))

    def test_any_ytdlp_config_file_blocks(self):
        for rel in [".config/yt-dlp/config", ".config/yt-dlp.conf", "yt-dlp.conf", ".yt-dlp/config"]:
            with self.subTest(rel=rel):
                home = Path(tempfile.mkdtemp())
                (home / rel).parent.mkdir(parents=True, exist_ok=True)
                (home / rel).write_text("--cookies-from-browser chrome\n")
                self.assertTrue(problems(home, env={}))


if __name__ == "__main__":
    unittest.main()
