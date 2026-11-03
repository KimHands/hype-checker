"""Per-message DM send state (design-v2 §5, ops-spec §4).

Each message is sent at most once: its attempt time is stored before the call.
A 4xx refusal (SendRejected) clears it; anything else (timeout, crash, 5xx) leaves
the outcome unknown, so that message is never sent again on its own.
HTTP 429 is retried after 30 then 60 seconds; the third 429 for a message (counted
across passes in `rejections`) or any other 4xx sets `gave_up`, and it is never
retried. Messages go out in order: a message that is not sent holds back the rest.
The allow-list and the 24 h window are re-checked with a fresh clock before every attempt.
"""

from hype_checker.v2.reply import SendRejected
from hype_checker.v2.webhook import is_allowed, within_reply_window

WINDOW_ERROR = "24시간 창이 지나 보낼 수 없다"
NOT_ALLOWED_ERROR = "sender not allowed now"
RATE_LIMITED = "HTTP 429"
BACKOFF_S = (30, 60)
MAX_429 = len(BACKOFF_S) + 1


def new_messages(texts):
    return [{"text": text, "sent": False, "attempted_at": None, "error": None, "rejections": 0, "gave_up": False}
            for text in texts]


def messages_of(record):
    """`messages` list, or the one-message view of a v2 record (`reply`/`sent`/`send_attempted_at`)."""
    if "messages" in record:
        return [dict(message) for message in record["messages"] or []]
    if not record.get("reply"):
        return []
    return [{"text": record["reply"], "sent": bool(record.get("sent")), "attempted_at": record.get("send_attempted_at"),
             "error": record.get("send_error"), "rejections": 0, "gave_up": False}]


def _finished(message):
    """Sent, or never to be sent automatically again."""
    return message["sent"] or bool(message.get("attempted_at")) or bool(message.get("gave_up"))


def has_unsent(record):
    return any(not _finished(message) for message in messages_of(record))


def _blocked_reason(share, now_s, config):
    if not is_allowed(share.get("sender"), config):
        return NOT_ALLOWED_ERROR
    if not within_reply_window(share.get("timestamp_ms", 0), now_s):
        return WINDOW_ERROR
    return None


def _rejected(message, error):
    """Records a confirmed refusal; returns the backoff delay, or None when the message is given up."""
    message.update(attempted_at=None, error=str(error))
    if str(error) == RATE_LIMITED:
        message["rejections"] = message.get("rejections", 0) + 1
        if message["rejections"] < MAX_429:
            return BACKOFF_S[message["rejections"] - 1]
    message["gave_up"] = True
    return None


def _deliver(save, message, send, share, config, clock, sleep):
    """Returns True when the message went out."""
    while True:
        reason = _blocked_reason(share, clock(), config)
        if reason:
            if message["error"] != reason:   # unchanged records keep their mtime, so prune still ages them out
                message["error"] = reason
                save()
            return False
        message["attempted_at"] = clock()
        save()
        try:
            send(share["sender"], message["text"])
        except SendRejected as error:
            delay = _rejected(message, error)
            save()
            if delay is None:
                return False
            sleep(delay)
            continue
        except Exception as error:  # outcome unknown (timeout, 5xx): keep the attempt mark, never resend on its own
            message["error"] = f"unknown outcome: {type(error).__name__}"
            save()
            return False
        message.update(sent=True, error=None)
        save()
        return True


def send_messages(inbox, path, record, send, config, clock, sleep):
    """Sends the record's unsent messages in order; returns the record with its updated `messages`."""
    messages, share = messages_of(record), record.get("share") or {}

    def save():
        inbox.update(path, messages=messages)
    for message in messages:
        if message["sent"]:
            continue
        if _finished(message) or not _deliver(save, message, send, share, config, clock, sleep):
            break
    return {**record, "messages": messages}
