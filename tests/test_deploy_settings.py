"""Server-move settings: secret webhook path, extraction backend choice (design-deploy)."""

import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from hype_checker import cli
from hype_checker.v2.config import ConfigError, load_server_settings
from hype_checker.v2.inbox import Inbox
from hype_checker.v2.server import make_handler
from tests.test_v2 import CONFIG, NOW_S

SECRET_PATH = "/webhook/Zx9-abcdEFGHijkl0123456789_qrstu"


def env_file(text):
    path = Path(tempfile.mkdtemp()) / "v2.env"
    path.write_text(text)
    path.chmod(0o600)
    return path


class ServerSettingsTest(unittest.TestCase):
    def test_defaults(self):
        settings = load_server_settings(env_file("IG_VERIFY_TOKEN=t\n"))
        self.assertEqual(settings, {"webhook_path": "/webhook", "extract": "local"})

    def test_secret_path_and_claude_extraction(self):
        settings = load_server_settings(env_file(f"HC_WEBHOOK_PATH={SECRET_PATH}\nHC_EXTRACT=claude\n"))
        self.assertEqual(settings, {"webhook_path": SECRET_PATH, "extract": "claude"})

    def test_rejects_weak_or_odd_paths(self):
        for bad in ("/hook", "/webhook/short", "/webhook/../x/abcdefghijklmnop", "webhook/abcdefghijklmnopqrst"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                load_server_settings(env_file(f"HC_WEBHOOK_PATH={bad}\n"))

    def test_rejects_unknown_extract_backend(self):
        with self.assertRaises(ConfigError):
            load_server_settings(env_file("HC_EXTRACT=openai\n"))


class SecretPathHandlerTest(unittest.TestCase):
    def setUp(self):
        handler = make_handler(CONFIG, Inbox(Path(tempfile.mkdtemp())), clock=lambda: NOW_S, path=SECRET_PATH)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.root = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        url = f"{self.root}{path}?hub.mode=subscribe&hub.verify_token=tok&hub.challenge=9"
        try:
            return urllib.request.urlopen(url).read()
        except urllib.error.HTTPError as error:
            return error.code

    def test_only_the_secret_path_answers(self):
        self.assertEqual(self.get(SECRET_PATH), b"9")
        self.assertEqual(self.get("/webhook"), 403)


class ExtractionChatTest(unittest.TestCase):
    def test_local_by_default(self):
        self.assertIs(cli.extraction_chat({"extract": "local"}, claude=None), cli.lm_studio_chat)

    def test_claude_when_configured(self):
        claude = {"model": "claude-haiku-5-5", "api_key": "k", "prices": (0.1, 0.5)}
        chat = cli.extraction_chat({"extract": "claude"}, claude=claude)
        self.assertTrue(hasattr(chat, "usage"))

    def test_claude_without_settings_fails_loudly(self):
        with self.assertRaises(SystemExit):
            cli.extraction_chat({"extract": "claude"}, claude=None)


if __name__ == "__main__":
    unittest.main()


class CrashRecoveryTest(unittest.TestCase):
    """finish() writes done/ then deletes queue/: a crash in between must not reprocess (and resend) the item."""

    def test_item_already_done_is_not_reprocessed(self):
        import json as _json
        from hype_checker.v2.worker import process_one
        from tests.test_v2 import fixture
        from hype_checker.v2.webhook import parse_shares
        inbox = Inbox(Path(tempfile.mkdtemp()))
        inbox.enqueue(parse_shares(fixture("ig_post.json"), CONFIG, NOW_S)[0])
        [item] = inbox.pending()
        done = {"share": inbox.read(item), "messages": [{"text": "hi", "sent": True, "attempted_at": NOW_S, "error": None}]}
        (inbox.root / "done" / item.name).write_text(_json.dumps(done))
        sends = []
        with mock.patch("hype_checker.v2.worker.check") as check:
            process_one(inbox, item, ctx=None, config=CONFIG, send=lambda r, t: sends.append(t), now_s=NOW_S)
        check.assert_not_called()
        self.assertEqual(sends, [])
        self.assertEqual(inbox.pending(), [])
        self.assertTrue(_json.loads((inbox.root / "done" / item.name).read_text())["messages"][0]["sent"])


class PublicBindGuardTest(unittest.TestCase):
    def test_public_host_requires_secret_path(self):
        with self.assertRaises(SystemExit):
            cli.require_secret_path_for_public("0.0.0.0", "/webhook")
        cli.require_secret_path_for_public("0.0.0.0", "/webhook/abcdefghijklmnopqrstu")
        cli.require_secret_path_for_public("127.0.0.1", "/webhook")
