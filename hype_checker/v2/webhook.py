"""Instagram Messaging webhook: subscription check, signature check, share parsing (design-v2 §3).

Shapes follow Meta docs as of 2026-10-26: `ig_post` (ig_post_media_id or id) and
`ig_reel` (reel_video_id) attachments carry title (= caption) and a signed CDN url.
"""

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from hype_checker.v2.media import is_media_url, is_reel_page

SHARE_TYPES = {"ig_post", "ig_reel"}
MAX_CAPTION = 5000
PENDING = "pending"
PUBLIC = "*"   # IG_ALLOWED_SENDERS=*: anyone may use it, within the daily limits
DISPUTE_WORD = "정정"
REPLY_WINDOW_MS = 24 * 3600 * 1000  # Send API: reply only within 24 h of the user's message


@dataclass(frozen=True)
class Config:
    verify_token: str
    app_secrets: tuple
    allowed_senders: frozenset
    account_id: str | None = None   # my IG professional account; events for any other recipient are dropped
    owner_sender: str | None = None  # my own IG user id: owner mode, implicitly allowed
    verdict: str = "off"             # HC_VERDICT: "on" sends verdict replies, anything else the v2 measurement reply
    video: str = "off"               # HC_VIDEO: "on" reads shared reels (transcript + on-screen text), else caption only
    unlimited_senders: frozenset = frozenset()  # never limited, and not counted against the shared daily limits


@dataclass(frozen=True)
class Share:
    mid: str
    sender: str
    kind: str            # ig_post | ig_reel | text | dispute
    caption: str
    media_id: str | None = None
    has_media_url: bool = False
    timestamp_ms: int = 0
    media_url: str | None = None   # signed Meta CDN link; lives only in the queue item, never in done/failed records


def is_public(config):
    return PUBLIC in config.allowed_senders


def is_allowed(sender, config):
    if not sender or sender == config.account_id:
        return False
    return (is_public(config) or sender in config.allowed_senders or sender in config.unlimited_senders
            or sender == config.owner_sender)


def verify_subscription(params, config):
    if params.get("hub.mode") != "subscribe":
        return None
    if not hmac.compare_digest(str(params.get("hub.verify_token", "")), config.verify_token):
        return None
    return params.get("hub.challenge")


def matched_secret(body, header, config):
    """1-based position of the secret that signed the body, or None. Kept so §7-1 can shrink to one secret."""
    if not header or not header.startswith("sha256="):
        return None
    given = header[len("sha256="):]
    for position, secret in enumerate(config.app_secrets, start=1):
        if secret and hmac.compare_digest(hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), given):
            return position
    return None


def signature_shaped(header):
    """Cheap pre-check so unsigned requests are refused before their body is read."""
    return bool(header) and re.fullmatch(r"sha256=[0-9a-f]{64}", header) is not None


def valid_signature(body, header, config):
    return matched_secret(body, header, config) is not None


def within_reply_window(timestamp_ms, now_s):
    return 0 <= now_s * 1000 - timestamp_ms <= REPLY_WINDOW_MS


def _attachment_share(base, attachment, config):
    payload = attachment.get("payload") or {}
    media_id = payload.get("ig_post_media_id") or payload.get("id") or payload.get("reel_video_id")
    url = payload.get("url")
    return Share(**base, kind=attachment["type"], caption=str(payload.get("title") or "")[:MAX_CAPTION],
                 media_id=str(media_id) if media_id is not None else None, has_media_url=bool(url),
                 media_url=url if config.video == "on" and attachment["type"] == "ig_reel"
                 and (is_media_url(url) or is_reel_page(url)) else None)


def _accepted(event, message, config, now_s):
    sender = str((event.get("sender") or {}).get("id", ""))
    recipient = str((event.get("recipient") or {}).get("id", ""))
    timestamp = event.get("timestamp")
    return (is_allowed(sender, config) and message.get("mid") and isinstance(timestamp, int)
            and within_reply_window(timestamp, now_s)
            and (config.account_id is None or recipient == config.account_id)
            and not message.get("is_echo") and not message.get("is_unsupported"))


def _shares_in(event, config, now_s):
    message = event.get("message") or {}
    if not _accepted(event, message, config, now_s):
        return []
    base = {"mid": str(message["mid"]), "sender": str(event["sender"]["id"]), "timestamp_ms": event["timestamp"]}
    shares = [_attachment_share(base, a, config) for a in message.get("attachments") or [] if a.get("type") in SHARE_TYPES]
    if not shares and message.get("text"):
        text = str(message["text"])[:MAX_CAPTION]
        shares = [Share(**base, kind="dispute" if text.strip() == DISPUTE_WORD else "text", caption=text)]
    return shares[:1]


def media_link_shape(payload):
    """Scheme and host of shared media links only (never path or signature), to see why a link was refused."""
    events = [e for entry in payload.get("entry") or [] for e in entry.get("messaging") or []]
    links = [(a.get("payload") or {}).get("url") for e in events for a in (e.get("message") or {}).get("attachments") or []]
    return ", ".join(f"{urlparse(u).scheme}://{urlparse(u).hostname}" for u in links if isinstance(u, str)) or "none"


def diagnosing(config):
    """`IG_ALLOWED_SENDERS=pending`: nobody is allowed yet, and the first DM's sender id is logged once so I can fill it in."""
    return config.allowed_senders == frozenset({PENDING})


def unknown_senders(payload, config):
    """Sender ids of non-echo DMs to my account, only while diagnosing — otherwise strangers are dropped unlogged (ADR 0011)."""
    if not diagnosing(config) or not isinstance(payload, dict) or payload.get("object") != "instagram":
        return []
    events = [event for entry in payload.get("entry") or [] for event in entry.get("messaging") or []]
    return [str(e["sender"]["id"]) for e in events
            if (e.get("sender") or {}).get("id") and e.get("message") and not e["message"].get("is_echo")
            and str((e.get("recipient") or {}).get("id", "")) == config.account_id]


def parse_shares(payload, config, now_s=None):
    if not isinstance(payload, dict) or payload.get("object") != "instagram":
        return []
    now_s = time.time() if now_s is None else now_s
    return [share for entry in payload.get("entry") or [] for event in entry.get("messaging") or []
            for share in _shares_in(event, config, now_s)]
