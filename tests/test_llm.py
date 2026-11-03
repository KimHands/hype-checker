import http.client
import json
import stat
import tempfile
import unittest
from pathlib import Path

from hype_checker import llm
from hype_checker.v2.config import ConfigError, load_anthropic_key

OK_BODY = {"content": [{"type": "text", "text": "안녕"}, {"type": "tool_use"}, {"type": "text", "text": "!"}],
           "usage": {"input_tokens": 12, "output_tokens": 3}}


class FakePost:
    def __init__(self, status=200, body=OK_BODY):
        self.status, self.body, self.calls = status, body, []

    def __call__(self, url, body_bytes, headers, timeout_s=60):
        self.calls.append((url, json.loads(body_bytes), headers))
        return self.status, self.body


def call(post, messages=None, **kw):
    messages = messages or [{"role": "user", "content": "hi"}]
    return llm.claude_chat(messages, model="m", api_key="k", post=post, **kw)


class ClaudeChatTest(unittest.TestCase):
    def test_moves_system_prompt_to_system_field(self):
        post = FakePost()
        call(post, [{"role": "system", "content": "A"}, {"role": "system", "content": "B"},
                    {"role": "user", "content": "hi"}])
        _, body, headers = post.calls[0]
        self.assertEqual(body["system"], "A\n\nB")
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])
        self.assertNotIn("temperature", body)
        self.assertEqual(headers["x-api-key"], "k")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")

    def test_low_effort_and_room_for_thinking_by_default(self):
        """Haiku/Sonnet 5.5 think by default and bill it as output; 1024 tokens could leave no text."""
        post = FakePost()
        call(post)
        body = post.calls[0][1]
        self.assertEqual(body["output_config"], {"effort": "low"})
        self.assertEqual(body["max_tokens"], 4096)
        self.assertNotIn("thinking", body)

    def test_make_chat_uses_the_same_defaults(self):
        post = FakePost()
        llm.make_chat("m", "k", post=post)([{"role": "user", "content": "hi"}])
        body = post.calls[0][1]
        self.assertEqual((body["max_tokens"], body["output_config"]), (4096, {"effort": "low"}))

    def test_no_system_field_when_absent(self):
        post = FakePost()
        call(post)
        self.assertNotIn("system", post.calls[0][1])

    def test_429_raises_unavailable(self):
        with self.assertRaises(llm.ClaudeUnavailable):
            call(FakePost(429, {}))

    def test_5xx_raises_unavailable(self):
        with self.assertRaises(llm.ClaudeUnavailable):
            call(FakePost(503, {}))

    def test_credit_exhaustion_raises_unavailable(self):
        body = {"error": {"type": "invalid_request_error", "message": "Your credit balance is too low"}}
        with self.assertRaises(llm.ClaudeUnavailable):
            call(FakePost(400, body))
        with self.assertRaises(llm.ClaudeUnavailable):
            call(FakePost(403, {"error": {"type": "permission_error", "message": "x"}}))

    def test_other_4xx_is_value_error(self):
        with self.assertRaises(ValueError):
            call(FakePost(400, {"error": {"type": "invalid_request_error", "message": "bad"}}))

    def test_connection_error_raises_unavailable(self):
        def post(*a, **k):
            raise TimeoutError()
        with self.assertRaises(llm.ClaudeUnavailable):
            call(post)

    def test_incomplete_read_raises_unavailable(self):
        def post(*a, **k):
            raise http.client.IncompleteRead(b"x")
        with self.assertRaises(llm.ClaudeUnavailable):
            call(post)

    def test_non_dict_error_is_value_error(self):
        with self.assertRaises(ValueError):
            call(FakePost(400, {"error": "string"}))
        with self.assertRaises(ValueError):
            call(FakePost(403, {"error": ["x"]}))

    def test_malformed_success_body_is_value_error(self):
        for body in ({}, {"content": "text"}, {"content": ["x", 3]}, {"content": [{"type": "text", "text": 5}]}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                call(FakePost(200, body))

    def test_malformed_usage_counts_as_zero(self):
        result = call(FakePost(200, {"content": [{"type": "text", "text": "ok"}], "usage": "lots"}))
        self.assertEqual((result["text"], result["input_tokens"], result["output_tokens"]), ("ok", 0, 0))

    def test_non_json_http_body_is_value_error(self):
        self.assertEqual(llm._parse_json(b"<html>"), {})
        self.assertEqual(llm._parse_json(b"\xff\xfe"), {})
        self.assertEqual(llm._parse_json(b"[1]"), {})
        with self.assertRaises(ValueError):
            call(FakePost(400, {}))

    def test_value_error_text_is_trimmed(self):
        body = {"error": {"type": "invalid_request_error", "message": "m" * 500, "secret": "LEAK"}}
        with self.assertRaises(ValueError) as ctx:
            call(FakePost(400, body))
        text = str(ctx.exception)
        self.assertLessEqual(len(text), 250)
        self.assertNotIn("LEAK", text)

    def test_empty_text_is_value_error(self):
        with self.assertRaises(ValueError):
            call(FakePost(200, {"content": [], "usage": {}}))

    def test_returns_usage_tokens(self):
        result = call(FakePost())
        self.assertEqual(result, {"text": "안녕!", "input_tokens": 12, "output_tokens": 3})

    def test_only_anthropic_host(self):
        post = FakePost()
        call(post)
        llm.make_chat("m", "k", post=post)([{"role": "user", "content": "x"}])
        self.assertTrue(all(url == "https://api.anthropic.com/v1/messages" for url, _, _ in post.calls))


class MakeChatTest(unittest.TestCase):
    def test_returns_text_and_records_usage(self):
        chat = llm.make_chat("m", "k", post=FakePost())
        self.assertEqual(chat([{"role": "user", "content": "x"}]), "안녕!")
        chat([{"role": "user", "content": "y"}])
        self.assertEqual(chat.usage, [{"input_tokens": 12, "output_tokens": 3}] * 2)


class LoadAnthropicKeyTest(unittest.TestCase):
    def write(self, text, mode=0o600):
        path = Path(tempfile.mkdtemp()) / "v2.env"
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_reads_key(self):
        self.assertEqual(load_anthropic_key(self.write('ANTHROPIC_API_KEY="sk-1"\n')), "sk-1")

    def test_missing_key_or_file_is_none(self):
        self.assertIsNone(load_anthropic_key(self.write("OTHER=1\n")))
        self.assertIsNone(load_anthropic_key(Path(tempfile.mkdtemp()) / "none.env"))

    def test_broad_permissions_refused(self):
        with self.assertRaises(ConfigError):
            load_anthropic_key(self.write("ANTHROPIC_API_KEY=x\n", 0o644))


if __name__ == "__main__":
    unittest.main()
