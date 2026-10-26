"""Checks that watch → yt-dlp will run anonymously (design-v1 §5-2).

watch falls back to cookie settings from its config when no CLI flag is given,
and calls yt-dlp without --ignore-config, so both sources must be empty.
The watch config is read with watch's own parser (BOM, UTF-16, quoting), so
the two can never disagree about what the file says.
"""

import importlib.util
from pathlib import Path

WATCH_SCRIPTS = Path.home() / ".claude/plugins/cache/claude-video/watch/0.3.2/skills/watch/scripts"
COOKIE_SETTINGS = ("WATCH_COOKIES_FILE", "WATCH_COOKIES_FROM_BROWSER")
YTDLP_CONFIGS = (".config/yt-dlp/config", ".config/yt-dlp.conf", "yt-dlp.conf", ".yt-dlp/config")


def _watch_config_module():
    spec = importlib.util.spec_from_file_location("watch_config_0_3_2", WATCH_SCRIPTS / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cookie_problems(home, env):
    try:
        file_values = _watch_config_module().read_env_file(home / ".config/watch/.env")
    except Exception as error:  # unreadable or malformed: watch would refuse too, but never guess
        return [f"watch config unreadable: {type(error).__name__}"]
    return [f"watch cookie setting is set: {name}" for name in COOKIE_SETTINGS
            if env.get(name, file_values.get(name, "")).strip()]


def _ytdlp_config_problems(home, env):
    candidates = [home / rel for rel in YTDLP_CONFIGS]
    if env.get("XDG_CONFIG_HOME"):
        xdg = Path(env["XDG_CONFIG_HOME"])
        candidates += [xdg / "yt-dlp/config", xdg / "yt-dlp.conf"]
    return [f"yt-dlp config file exists: {path}" for path in candidates if path.exists()]


def problems(home, env):
    home = Path(home)
    return _cookie_problems(home, env) + _ytdlp_config_problems(home, env)
