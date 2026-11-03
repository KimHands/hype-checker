"""Minimal Claude Messages API adapter (stdlib only). The only module besides http/extract/reply that opens connections."""

import http.client
import json
import os
import time
import urllib.error
import urllib.request

from hype_checker.http import call_within, read_capped

URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = os.environ.get("HC_CLAUDE_MODEL", "")
CREDIT_ERROR_TYPES = {"billing_error", "permission_error"}
# Haiku/Sonnet 5.5 think by default and bill thinking as output: short classification calls run at low
# effort with room left for the JSON answer, or the text block can come back empty.
EFFORT = "low"
MAX_TOKENS = 4096


class ClaudeUnavailable(Exception):
    pass


def _parse_json(raw):
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _send(request, timeout_s):
    deadline = time.monotonic() + timeout_s
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, _parse_json(read_capped(response, deadline=deadline))
    except urllib.error.HTTPError as error:
        return error.code, _parse_json(read_capped(error, deadline=deadline))


def _post(url, body_bytes, headers, timeout_s=60):
    """Whole request, error bodies included, bounded by `timeout_s` (TookTooLong is an OSError)."""
    request = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    return call_within(lambda: _send(request, timeout_s), timeout_s)


def _build_body(messages, model, max_tokens):
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    body = {"model": model, "max_tokens": max_tokens, "output_config": {"effort": EFFORT},
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]}
    if system:
        body["system"] = system
    return body


def _dict(value):
    return value if isinstance(value, dict) else {}


def _is_credit_error(status, parsed):
    if status not in (400, 403):
        return False
    error = _dict(parsed.get("error"))
    return error.get("type") in CREDIT_ERROR_TYPES or "credit" in str(error.get("message", "")).lower()


def _check_status(status, parsed):
    if status == 429 or status >= 500 or _is_credit_error(status, parsed):
        raise ClaudeUnavailable(f"Claude API {status}")
    if status >= 400:
        error = _dict(parsed.get("error"))
        message = str(error.get("message", ""))[:200]
        raise ValueError(f"Claude API {status}: {error.get('type')}: {message}")


def claude_chat(messages, *, model, api_key, max_tokens=MAX_TOKENS, post=_post):
    headers = {"x-api-key": api_key, "anthropic-version": API_VERSION, "content-type": "application/json"}
    payload = json.dumps(_build_body(messages, model, max_tokens)).encode("utf-8")
    try:
        status, parsed = post(URL, payload, headers)
    except (http.client.HTTPException, OSError) as error:
        raise ClaudeUnavailable(str(error)) from error
    _check_status(status, parsed)
    usage = _dict(parsed.get("usage"))
    return {"text": _reply_text(parsed), "input_tokens": _count(usage, "input_tokens"),
            "output_tokens": _count(usage, "output_tokens")}


def _reply_text(parsed):
    """Malformed bodies (non-list content, non-dict blocks, non-str text) count as an empty reply: ValueError."""
    content = parsed.get("content")
    blocks = content if isinstance(content, list) else []
    texts = [b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    if not texts or not all(isinstance(t, str) for t in texts) or not "".join(texts):
        raise ValueError("Claude API: 빈 응답")
    return "".join(texts)


def _count(usage, key):
    value = usage.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def make_chat(model, api_key, *, max_tokens=MAX_TOKENS, post=_post):
    def chat(messages):
        result = claude_chat(messages, model=model, api_key=api_key, max_tokens=max_tokens, post=post)
        chat.usage.append({"input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"]})
        return result["text"]
    chat.usage = []
    return chat
