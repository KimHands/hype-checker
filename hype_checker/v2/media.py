"""Fetches a shared reel from the signed Meta CDN URL the webhook gave us (design-v2 §4, video).

No yt-dlp, no cookies, no Instagram page: only the media link Meta put in the DM event.
Every hop (including redirects) must stay on a Meta media host, the body must be an MP4
(content type and `ftyp` box, so ffmpeg never gets a playlist that points elsewhere),
and size and total time are capped. The caller deletes it once watch has read it.
"""

import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

MEDIA_HOSTS = ("fbsbx.com", "fbcdn.net", "cdninstagram.com")
MAX_BYTES = 80 * 1024 * 1024
TIMEOUT_S = 60          # whole download; overshoot is at most one socket wait
SOCKET_TIMEOUT_S = 15
CHUNK = 1 << 16


class MediaRefused(Exception):
    pass


def is_reel_page(url):
    """The link Meta actually puts in ig_reel events: the public reel page, read by watch through anonymous yt-dlp."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return (parsed.scheme == "https" and host in ("www.instagram.com", "instagram.com")
            and parsed.path.split("/")[1:2] in (["reel"], ["reels"], ["p"]))


def is_media_url(url):
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(host == h or host.endswith("." + h) for h in MEDIA_HOSTS)


class _MetaOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_media_url(newurl):
            raise MediaRefused("redirect left Meta media hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url):
    opener = urllib.request.build_opener(_MetaOnlyRedirects)
    return opener.open(urllib.request.Request(url, headers={"User-Agent": "hype-checker"}), timeout=SOCKET_TIMEOUT_S)


def _copy_capped(response, target, max_bytes, deadline):
    written = 0
    with open(target, "wb") as out:
        while chunk := response.read1(CHUNK):   # one socket read at a time, so a trickle hits the deadline
            if not written and chunk[4:8] != b"ftyp":
                raise MediaRefused("not an MP4")
            written += len(chunk)
            if written > max_bytes:
                raise MediaRefused("media too large")
            if time.monotonic() > deadline:
                raise MediaRefused("download too slow")
            out.write(chunk)
    return written


def fetch_reel(url, dest_dir, open_url=_open, max_bytes=MAX_BYTES, timeout_s=TIMEOUT_S):
    """Downloads to <dest_dir>/reel.mp4 and returns the path; removes the partial file on any failure."""
    if not is_media_url(url):
        raise MediaRefused("not a Meta media URL")
    target = Path(dest_dir) / "reel.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    try:
        with open_url(url) as response:
            if not is_media_url(response.geturl()):
                raise MediaRefused("final URL left Meta media hosts")
            kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not kind.startswith("video/"):
                raise MediaRefused(f"not a video: {kind or 'unknown'}")
            if not _copy_capped(response, target, max_bytes, deadline):
                raise MediaRefused("empty media")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target
