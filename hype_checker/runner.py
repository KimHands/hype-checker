"""The only module allowed to start subprocesses (design-v1 §4 invariants 1·3·4)."""

import re
import subprocess
import sys

API_PATH = re.compile(r"[A-Za-z0-9._~\-/?=&%,+]+")
PYTHONS = {"python3", sys.executable}
COOKIE_FLAG_PREFIX = "--cookies"


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

    def run(self, argv):
        check_argv(argv, self.watch_script)
        self.log.append(list(argv))
        return subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout_s, check=False)
