"""Read-only JSON GET to an allowlist of public registries (design-v1 §4-4·§4-5)."""

import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

ALLOWED_HOSTS = {
    "registry.npmjs.org", "api.npmjs.org", "api.deps.dev",
    "registry.modelcontextprotocol.io", "pypi.org",
}


class HostNotAllowed(Exception):
    pass


def get_json(url, timeout_s=20):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise HostNotAllowed(url)
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "hype-checker"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
