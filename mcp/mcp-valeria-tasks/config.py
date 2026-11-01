"""Environment configuration for the user-scoped Valeria MCP client."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Invalid configuration, with messages that never contain credentials."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"Required environment variable is missing: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    url: str
    login: str
    password: str = field(repr=False)
    ca_bundle: Path | None = None
    request_timeout_seconds: float = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if env is None else env
        url = _required(values, "VALERIA_URL")
        try:
            parsed = urlsplit(url)
            port = parsed.port
            valid_url = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and "?" not in url
                and "#" not in url
                and not any(
                    character.isspace() or ord(character) < 32 or ord(character) == 127
                    for character in url
                )
                and (port is None or 1 <= port <= 65535)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ConfigError("VALERIA_URL must be an HTTP(S) URL without credentials or query")

        password_file = values.get("VALERIA_PASSWORD_FILE", "").strip()
        if password_file:
            try:
                password = Path(password_file).expanduser().read_text(encoding="utf-8")
            except (OSError, ValueError, UnicodeError):
                raise ConfigError("Cannot read VALERIA_PASSWORD_FILE") from None
            password = password.rstrip("\r\n")
            if not password:
                raise ConfigError("VALERIA_PASSWORD_FILE must contain a password")
        else:
            password = values.get("VALERIA_PASSWORD", "")
            if not password:
                raise ConfigError("VALERIA_PASSWORD or VALERIA_PASSWORD_FILE is required")

        ca_value = values.get("VALERIA_CA_BUNDLE", "").strip()
        ca_bundle = Path(ca_value).expanduser() if ca_value else None
        if ca_bundle is not None:
            try:
                if not ca_bundle.is_file():
                    raise ConfigError("VALERIA_CA_BUNDLE must be a readable file")
                with ca_bundle.open("rb"):
                    pass
            except (OSError, ValueError):
                raise ConfigError("VALERIA_CA_BUNDLE must be a readable file") from None

        try:
            timeout = float(values.get("VALERIA_REQUEST_TIMEOUT_SECONDS", "30"))
        except (ValueError, TypeError):
            raise ConfigError("VALERIA_REQUEST_TIMEOUT_SECONDS must be a positive number") from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError("VALERIA_REQUEST_TIMEOUT_SECONDS must be a positive finite number")

        return cls(
            url=url.rstrip("/"),
            login=_required(values, "VALERIA_LOGIN"),
            password=password,
            ca_bundle=ca_bundle,
            request_timeout_seconds=timeout,
        )
