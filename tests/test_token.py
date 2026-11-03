import json
import tempfile
import unittest
from pathlib import Path

from hype_checker.v2 import token

DAY = 86400


class TokenRefreshTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.urls = []

    def ok(self, url):
        self.urls.append(url)
        return {"access_token": f"new{len(self.urls)}", "token_type": "bearer", "expires_in": 60 * DAY}

    def broken(self, url):
        self.urls.append(url)
        raise OSError("down")

    def test_seed_is_used_until_refreshed(self):
        self.assertEqual(token.current(self.root, "seed"), "seed")

    def test_first_pass_refreshes_and_stores_privately(self):
        self.assertTrue(token.refresh_if_due(self.root, "seed", 1000 * DAY, get=self.ok))
        self.assertIn("access_token=seed", self.urls[0])
        self.assertEqual(token.current(self.root, "seed"), "new1")
        self.assertEqual((self.root / "ig_token.json").stat().st_mode & 0o777, 0o600)

    def test_not_again_within_a_week_then_uses_the_refreshed_token(self):
        token.refresh_if_due(self.root, "seed", 1000 * DAY, get=self.ok)
        self.assertFalse(token.refresh_if_due(self.root, "seed", 1006 * DAY, get=self.ok))
        self.assertTrue(token.refresh_if_due(self.root, "seed", 1007 * DAY, get=self.ok))
        self.assertIn("access_token=new1", self.urls[1])

    def test_failure_keeps_old_token_and_waits_before_retry(self):
        self.assertFalse(token.refresh_if_due(self.root, "seed", 1000 * DAY, get=self.broken))
        self.assertEqual(token.current(self.root, "seed"), "seed")
        token.refresh_if_due(self.root, "seed", 1000 * DAY + 3600, get=self.broken)
        self.assertEqual(len(self.urls), 1)
        token.refresh_if_due(self.root, "seed", 1000 * DAY + 7 * 3600, get=self.broken)
        self.assertEqual(len(self.urls), 2)

    def test_malformed_reply_is_a_failure(self):
        self.assertFalse(token.refresh_if_due(self.root, "seed", 1000 * DAY, get=lambda url: {"error": "x"}))
        self.assertEqual(token.current(self.root, "seed"), "seed")

    def test_token_never_printed(self):
        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        with redirect_stderr(err):
            token.refresh_if_due(self.root, "seed-secret", 1000 * DAY, get=self.ok)
            token.refresh_if_due(Path(tempfile.mkdtemp()), "seed-secret", 1000 * DAY, get=self.broken)
        self.assertNotIn("seed-secret", err.getvalue())
        self.assertNotIn("new1", err.getvalue())


if __name__ == "__main__":
    unittest.main()
