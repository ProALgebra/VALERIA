from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Invalid bot configuration; messages never include secret values."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    valeria_url: str
    database_path: Path
    ca_bundle: Path | None = None
    request_timeout_seconds: float = 30
    long_poll_seconds: int = 25
    poll_interval_seconds: float = 3
    allowed_user_ids: frozenset[int] = frozenset()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        return load_settings(env)


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(env.get(name, str(default)))
    except ValueError:
        raise ConfigError(f"{name} must be a positive finite number") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive finite number")
    return value


def _token(env: Mapping[str, str]) -> str:
    token_file = env.get("TELEGRAM_BOT_TOKEN_FILE", "").strip()
    if token_file:
        try:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except (OSError, ValueError, UnicodeError):
            raise ConfigError("Cannot read TELEGRAM_BOT_TOKEN_FILE") from None
    else:
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ConfigError("Set a valid TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN_FILE")
    return token


def _base_url(env: Mapping[str, str]) -> str:
    raw = env.get("VALERIA_BASE_URL", "")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ConfigError("VALERIA_BASE_URL must not contain whitespace or control characters")
    try:
        parsed = urlsplit(raw)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and "?" not in raw
            and "#" not in raw
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError("VALERIA_BASE_URL must be an HTTP(S) URL without credentials or query")
    return raw.rstrip("/")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    values = os.environ if env is None else env
    token = _token(values)
    base_url = _base_url(values)
    try:
        long_poll = int(values.get("TELEGRAM_LONG_POLL_SECONDS", "25"))
    except ValueError:
        raise ConfigError("TELEGRAM_LONG_POLL_SECONDS must be an integer from 1 to 50") from None
    if not 1 <= long_poll <= 50:
        raise ConfigError("TELEGRAM_LONG_POLL_SECONDS must be an integer from 1 to 50")

    allowed: set[int] = set()
    raw_allowed = values.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if raw_allowed:
        for raw_id in raw_allowed.split(","):
            if not re.fullmatch(r"[0-9]+", raw_id.strip()) or int(raw_id) <= 0:
                raise ConfigError("TELEGRAM_ALLOWED_USER_IDS must contain positive numeric IDs")
            allowed.add(int(raw_id))

    ca_raw = values.get("TLS_CA_BUNDLE", "").strip()
    ca_bundle = Path(ca_raw).expanduser().resolve() if ca_raw else None
    if ca_bundle is not None:
        try:
            if not ca_bundle.is_file():
                raise OSError
            with ca_bundle.open("rb"):
                pass
        except OSError:
            raise ConfigError("TLS_CA_BUNDLE must point to a readable file") from None

    database_raw = values.get("TELEGRAM_STATE_DB", "data/telegram-bot.db").strip()
    if not database_raw:
        raise ConfigError("TELEGRAM_STATE_DB must specify a file path")
    return Settings(
        bot_token=token,
        valeria_url=base_url,
        database_path=Path(database_raw).expanduser(),
        ca_bundle=ca_bundle,
        request_timeout_seconds=_positive_float(values, "REQUEST_TIMEOUT_SECONDS", 30),
        long_poll_seconds=long_poll,
        poll_interval_seconds=_positive_float(values, "TELEGRAM_POLL_INTERVAL_SECONDS", 3),
        allowed_user_ids=frozenset(allowed),
    )
