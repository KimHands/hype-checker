"""Keeps the Instagram reply token alive (long-lived tokens expire after 60 days).

The token in v2.env is the seed; refreshed tokens live in <inbox>/ig_token.json (600) on the
data volume, because v2.env is mounted read-only. Refresh every week, retry a failure after
6 hours, and never print the token — only the error type.
"""

import json
import sys
import urllib.parse
import urllib.request

from hype_checker.v2.inbox import _write_private

REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
REFRESH_EVERY_S = 7 * 86400
RETRY_AFTER_S = 6 * 3600
WARN_BEFORE_S = 7 * 86400


def _get(url):
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.loads(response.read())


def _state(root):
    try:
        data = json.loads((root / "ig_token.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def current(root, seed):
    return _state(root).get("token") or seed


def _due(state, now):
    if now - state.get("attempted_at", 0) < RETRY_AFTER_S:
        return False
    return now - state.get("refreshed_at", 0) >= REFRESH_EVERY_S


def refresh_if_due(root, seed, now, get=_get):
    """Returns True when a new token was stored. Failures keep the old token."""
    state = _state(root)
    if not seed or not _due(state, now):
        return False
    query = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": current(root, seed)})
    try:
        reply = get(f"{REFRESH_URL}?{query}")
        token, expires_in = reply["access_token"], int(reply["expires_in"])
    except Exception as error:  # expired seed, network, malformed reply
        _write_private(root / "ig_token.json", {**state, "attempted_at": now})
        _warn_if_expiring(state, now, type(error).__name__)
        return False
    _write_private(root / "ig_token.json", {"token": token, "refreshed_at": now, "attempted_at": now,
                                           "expires_at": now + expires_in})
    print(f"ig token refreshed · expires in {expires_in // 86400} days", file=sys.stderr)
    return True


def _warn_if_expiring(state, now, reason):
    left = state.get("expires_at", 0) - now
    detail = f"{left // 86400} days left" if state.get("expires_at") else "expiry unknown"
    if not state.get("expires_at") or left < WARN_BEFORE_S:
        print(f"ig token refresh failed ({reason}) · {detail} — 새 토큰을 v2.env에 넣어야 할 수 있다", file=sys.stderr)
