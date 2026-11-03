"""Daily caps that survive restarts (ops-spec §4): per sender, all senders, estimated Claude dollars.

One JSON file per UTC day. Every processed item counts, failures included. A file
that cannot be read or has the wrong shape fails closed: nothing more is measured
that day until it is fixed. `notified` lists senders already told they hit the cap
(at most one notice per sender per day).
"""

import json
from pathlib import Path

from hype_checker.v2.inbox import _write_private


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return (_is_int(value) or isinstance(value, float)) and not isinstance(value, bool)


def _valid(counts):
    if not isinstance(counts, dict) or not _is_int(counts.get("total")) or not _is_number(counts.get("usd")):
        return False
    senders, notified = counts.get("senders"), counts.get("notified", [])
    return (isinstance(senders, dict) and all(isinstance(k, str) and _is_int(v) for k, v in senders.items())
            and isinstance(notified, list) and all(isinstance(n, str) for n in notified))


class Limits:
    def __init__(self, root, per_sender=2, total=60, usd=2.0):
        self.root = Path(root)
        self.per_sender, self.total, self.usd = per_sender, total, usd
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def _path(self, today):
        return self.root / f"{today}.json"

    def _read(self, today):
        path = self._path(today)
        if not path.exists():
            return {"total": 0, "usd": 0.0, "senders": {}, "notified": []}
        try:
            counts = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return {"notified": [], **counts} if _valid(counts) else None

    def allow(self, sender, today):
        counts = self._read(today)
        if counts is None:
            return False
        return (counts["senders"].get(sender, 0) < self.per_sender and counts["total"] < self.total
                and counts["usd"] < self.usd)

    def first_notice(self, sender, today):
        """True once per sender per day (and records it); False when already told or the file is unreadable."""
        counts = self._read(today)
        if counts is None or sender in counts["notified"]:
            return False
        counts["notified"].append(sender)
        _write_private(self._path(today), counts)
        return True

    def record(self, sender, today, usd):
        counts = self._read(today) or {"total": self.total, "usd": self.usd, "senders": {}, "notified": []}
        counts["senders"][sender] = counts["senders"].get(sender, 0) + 1
        counts["total"] += 1
        counts["usd"] = round(counts["usd"] + (usd or 0), 6)
        _write_private(self._path(today), counts)
