"""Read-only JSON GET to an allowlist of public registries (design-v1 §4-4·§4-5)."""

import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

ALLOWED_HOSTS = {
    "registry.npmjs.org", "api.npmjs.org", "api.deps.dev",
    "registry.modelcontextprotocol.io", "pypi.org",
}


class HostNotAllowed(Exception):
    pass


class TookTooLong(TimeoutError):
    """The whole call (connect, headers, body, error body) did not finish within its limit."""


def call_within(fn, seconds):
    """Runs `fn` with a hard limit on total time. urllib's timeout only bounds each socket wait, so a
    trickling server could hold a call far longer; past the limit the caller moves on and the abandoned
    daemon thread ends when its socket times out. Only for calls whose late result can be discarded."""
    outcome = {}

    def run():
        try:
            outcome["value"] = fn()
        except BaseException as error:  # handed to the caller below
            outcome["error"] = error
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TookTooLong(f"call took over {seconds:.0f}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


MAX_BODY = 8 * 1024 * 1024   # an abandoned call cannot keep reading forever


def read_capped(response, limit=MAX_BODY, deadline=None, chunk=1 << 16):
    """Reads in socket-sized pieces and stops at the size limit or the call's own deadline, so even an
    abandoned call (see call_within) ends after at most one more socket wait."""
    parts, size = [], 0
    while piece := response.read1(chunk):
        size += len(piece)
        if size > limit:
            raise TookTooLong("response body too large")
        if deadline is not None and time.monotonic() > deadline:
            raise TookTooLong("response still arriving past the call's limit")
        parts.append(piece)
    return b"".join(parts)


def _fetch_json(request, timeout_s):
    deadline = time.monotonic() + timeout_s
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(read_capped(response, deadline=deadline))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def get_json(url, timeout_s=20):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise HostNotAllowed(url)
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "hype-checker"})
    return call_within(lambda: _fetch_json(request, timeout_s), timeout_s)
