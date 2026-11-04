"""File queue between `serve` and `work` (design-v2 §3·§4).

Dedupes on message id (Meta retries for 36 h), publishes atomically, quarantines
broken files, allows one worker at a time, keeps files private to the user.
"""

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

DIRS = ("queue", "done", "failed")
ARRIVAL_STEP_NS = 1000


class WorkerBusy(Exception):
    pass


def _write_private(path, data, mtime_ns=None):
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)  # mode 600, unique
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, ensure_ascii=False)
    if mtime_ns is not None:
        os.utime(temp, ns=(mtime_ns, mtime_ns))
    os.replace(temp, path)


def _raw_without_link(item):
    """Unparseable queue text is kept for debugging only when it cannot hold a media link."""
    raw = item.read_text(errors="replace")
    return None if "media_url" in raw else raw[:5000]


def without_media_url(share):
    return {key: value for key, value in share.items() if key != "media_url"}


class Inbox:
    def __init__(self, root):
        self.root = Path(root)
        self._publish = threading.Lock()   # serve is threaded: check-then-publish must be one step
        self._last_arrival_ns = 0
        for name in DIRS:
            (self.root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
            (self.root / name).chmod(0o700)
        self.root.chmod(0o700)

    def key(self, mid):
        return hashlib.sha256(mid.encode()).hexdigest()[:32] + ".json"

    def _seen(self, key):
        return any((self.root / name / key).exists() for name in DIRS)

    def enqueue(self, share):
        key = self.key(share.mid)
        with self._publish:
            if self._seen(key):
                return False
            _write_private(self.root / "queue" / key, asdict(share), mtime_ns=self._next_arrival_ns())
            return True

    def _next_arrival_ns(self):
        """Filesystem clocks tick coarsely, so two quick arrivals could tie and lose their order."""
        self._last_arrival_ns = max(time.time_ns(), self._last_arrival_ns + ARRIVAL_STEP_NS)
        return self._last_arrival_ns

    def pending(self):
        return sorted((self.root / "queue").glob("[!.]*.json"), key=lambda p: p.stat().st_mtime_ns)

    def read(self, item):
        try:
            data = json.loads(item.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def quarantine(self, item, reason):
        share = self.read(item)
        try:
            raw = json.dumps(without_media_url(share)) if share else _raw_without_link(item)
        except OSError:
            raw = None
        _write_private(self.root / "failed" / item.name, {"reason": reason, "raw": raw})
        try:
            item.unlink()
        except OSError:
            pass

    def already_done(self, item):
        """True when finish() wrote done/ but crashed before removing the queue file; the stale queue file is dropped."""
        if not (self.root / "done" / item.name).exists():
            return False
        try:
            item.unlink()
        except OSError:
            pass
        return True

    def finish(self, item, share, result):
        _write_private(self.root / "done" / item.name, {"share": without_media_url(share), **result})
        item.unlink()

    def done(self):
        return [(path, self.read(path)) for path in sorted((self.root / "done").glob("[!.]*.json"))]

    def update(self, path, **fields):
        _write_private(path, {**(self.read(path) or {}), **fields})

    def prune(self, days, now=None):
        cutoff = (now or time.time()) - days * 86400
        # a week-old queue item is past the 24 h reply window anyway; limits and disputes hold sender ids
        for name in ("queue", "done", "failed", "limits", "disputes"):
            for path in (self.root / name).glob("*.json"):
                if path.stat().st_mtime < cutoff:
                    path.unlink()

    def sweep_media(self):
        """Reel copies left by a killed worker; call under the worker lock, when nothing is in progress."""
        shutil.rmtree(self.root / "media", ignore_errors=True)

    @contextmanager
    def worker_lock(self):
        fd = os.open(self.root / "worker.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WorkerBusy("다른 work가 실행 중") from None
            yield
        finally:
            os.close(fd)
