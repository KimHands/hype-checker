"""Loads v2 secrets from an env file outside the repo; refuses files others can read (design-v2 §6)."""

import os
import re
import stat
from pathlib import Path

from hype_checker.v2.webhook import Config

DEFAULT_PATH = Path.home() / ".config" / "hype-checker" / "v2.env"
REQUIRED = ("IG_VERIFY_TOKEN", "IG_APP_SECRETS", "IG_ALLOWED_SENDERS")
DEFAULT_WEBHOOK_PATH = "/webhook"
SECRET_WEBHOOK_PATH = re.compile(r"/webhook/[A-Za-z0-9_-]{16,64}")
EXTRACT_BACKENDS = ("local", "claude")


class ConfigError(Exception):
    pass


def _read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _split(value):
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _setting(values, key):
    """HC_* switches: the process env overrides v2.env (handy for one run); IG_* keys come from v2.env only."""
    return os.environ.get(key) or values.get(key) or ""


def _price(text):
    try:
        price = float(text)
    except ValueError:
        return None
    return price if price >= 0 else None


def load_config(path=DEFAULT_PATH):
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"{path} 없음 — docs/design-v2.md §6 형식으로 만든다")
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{path} 권한이 너무 넓다 — chmod 600")
    values = _read_env(path)
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        raise ConfigError("빠진 키: " + ", ".join(missing))
    config = Config(verify_token=values["IG_VERIFY_TOKEN"], app_secrets=_split(values["IG_APP_SECRETS"]),
                    allowed_senders=frozenset(_split(values["IG_ALLOWED_SENDERS"])),
                    account_id=values.get("IG_ACCOUNT_ID") or None,
                    owner_sender=values.get("IG_OWNER_SENDER") or None,
                    unlimited_senders=frozenset(_split(values.get("IG_UNLIMITED_SENDERS", ""))),
                    verdict="on" if _setting(values, "HC_VERDICT").lower() == "on" else "off",
                    video="on" if _setting(values, "HC_VIDEO").lower() == "on" else "off")
    return config, values.get("IG_ACCESS_TOKEN")


def load_anthropic_key(path=DEFAULT_PATH):
    path = Path(path)
    if not path.is_file():
        return None
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{path} 권한이 너무 넓다 — chmod 600")
    return _read_env(path).get("ANTHROPIC_API_KEY") or None


def load_claude_settings(path=DEFAULT_PATH):
    """Model, (price_in, price_out) per MTok and API key for HC_VERDICT=on; any missing → None (fail closed on cost)."""
    path = Path(path)
    if not path.is_file():
        return None
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{path} 권한이 너무 넓다 — chmod 600")
    values = _read_env(path)
    model, key = _setting(values, "HC_CLAUDE_MODEL"), values.get("ANTHROPIC_API_KEY")
    prices = (_price(_setting(values, "HC_PRICE_IN_PER_MTOK")), _price(_setting(values, "HC_PRICE_OUT_PER_MTOK")))
    if not model or not key or None in prices:
        return None
    return {"model": model, "prices": prices, "api_key": key}


def load_server_settings(path=DEFAULT_PATH):
    """Webhook path (plain /webhook or /webhook/<16-64 url-safe chars>) and extraction backend (local LM Studio | claude)."""
    path = Path(path)
    values = _read_env(path) if path.is_file() else {}
    webhook_path = _setting(values, "HC_WEBHOOK_PATH") or DEFAULT_WEBHOOK_PATH
    if webhook_path != DEFAULT_WEBHOOK_PATH and not SECRET_WEBHOOK_PATH.fullmatch(webhook_path):
        raise ConfigError("HC_WEBHOOK_PATH 는 /webhook/<16~64자 영문·숫자·-·_> 형식이어야 한다")
    extract = _setting(values, "HC_EXTRACT") or "local"
    if extract not in EXTRACT_BACKENDS:
        raise ConfigError(f"HC_EXTRACT 는 {'/'.join(EXTRACT_BACKENDS)} 중 하나여야 한다")
    return {"webhook_path": webhook_path, "extract": extract}
