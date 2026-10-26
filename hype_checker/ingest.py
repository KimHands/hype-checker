"""Classifies a check input into a Source (design-v1 §4-1)."""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

VIDEO_HOSTS = {
    "instagram.com": "instagram",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
}
THREADS_HOSTS = {"threads.net", "threads.com"}
THREADS_NOTE = "Threads는 수집 경로가 없다. 게시물 캡션을 텍스트로 붙여 넣어 달라"
PACKAGE_PATHS = {
    "npmjs.com": ("npm", re.compile(r"/package/((?:@[^/]+/)?[^/]+)")),
    "pypi.org": ("pypi", re.compile(r"/project/([^/]+)")),
}


@dataclass(frozen=True)
class Source:
    kind: str            # mp4 | url | text
    platform: str        # local | instagram | youtube | threads | github | npm | pypi | web | pasted
    value: str
    identifier: str | None = None
    needs_download: bool = False
    note: str = ""


def _host(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _classify_url(url):
    host, path = _host(url), urlparse(url).path
    if host in VIDEO_HOSTS:
        return Source("url", VIDEO_HOSTS[host], url, needs_download=True)
    if host in THREADS_HOSTS:
        return Source("url", "threads", url, note=THREADS_NOTE)
    if host == "github.com":
        parts = [p for p in path.split("/") if p]
        identifier = "/".join(parts[:2]) if len(parts) >= 2 else None
        return Source("url", "github", url, identifier=identifier)
    if host in PACKAGE_PATHS:
        platform, pattern = PACKAGE_PATHS[host]
        match = pattern.match(path)
        return Source("url", platform, url, identifier=match.group(1) if match else None)
    return Source("url", "web", url)


def _looks_like_url(text):
    return bool(re.match(r"^(https?://)?([\w-]+\.)+[a-z]{2,}(/\S*)?$", text, re.IGNORECASE))


def classify(raw):
    text = raw.strip()
    if text.lower().endswith(".mp4"):
        path = Path(text).expanduser()
        if not path.is_file():
            raise FileNotFoundError(text)
        return Source("mp4", "local", str(path.resolve()))
    if _looks_like_url(text):
        return _classify_url(text if "://" in text else f"https://{text}")
    return Source("text", "pasted", text)
