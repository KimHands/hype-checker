"""The only module allowed to start subprocesses (design-v1 §4 invariants 1·3·4)."""

import os
import re
import signal
import subprocess
import sys

API_PATH = re.compile(r"[A-Za-z0-9._~\-/?=&%,+]+")
PYTHONS = {"python3", sys.executable}
COOKIE_FLAG_PREFIX = "--cookies"


TimeoutExpired = subprocess.TimeoutExpired   # so callers can catch it without touching subprocess


class ForbiddenCommand(Exception):
    pass


def build_gh_get(path):
    if path.startswith(("/", "-")) or "://" in path or not API_PATH.fullmatch(path):
        raise ForbiddenCommand(f"gh api path rejected: {path!r}")
    return ["gh", "api", "--method", "GET", path]


def check_argv(argv, watch_script):
    if argv[:2] == ["gh", "api"]:
        if len(argv) != 5 or argv != build_gh_get(argv[-1]):
            raise ForbiddenCommand(f"only plain gh api GET is allowed: {argv!r}")
        return
    if len(argv) >= 2 and argv[0] in PYTHONS and argv[1] == watch_script:
        if any(arg.startswith(COOKIE_FLAG_PREFIX) for arg in argv[2:]):
            raise ForbiddenCommand("watch must run without cookies")
        return
    raise ForbiddenCommand(f"program not in allowlist: {argv[:2]!r}")


class Runner:
    def __init__(self, watch_script, timeout_s=600):
        self.watch_script = watch_script
        self.timeout_s = timeout_s
        self.log = []

    def run(self, argv, timeout_s=None):
        """`timeout_s` can only shorten the runner's own limit (a caller's remaining time budget)."""
        check_argv(argv, self.watch_script)
        self.log.append(list(argv))
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              start_new_session=True) as process:
            try:
                stdout, stderr = process.communicate(timeout=min(self.timeout_s, timeout_s or self.timeout_s))
            except subprocess.TimeoutExpired:
                _kill_group(process)   # watch's ffmpeg children too, not just watch
                raise
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate()
