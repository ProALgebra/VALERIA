"""Validated settings model for the JSON repair processor.

Settings are stored as one JSON document in the singleton
``json_processor_settings`` row.  The model is pure (no I/O) so it can be used
by the admin API, tests, and the future worker without importing the database.

Security rules:
- there is no secret field; ``api_key_ref`` is an opaque reference only;
- unknown settings (including ``api_key``) are rejected on update and ignored
  when reading a corrupt/legacy row so a secret can never leak back out;
- validation errors never echo the rejected value.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any
from urllib.parse import urlsplit

from .models import (
    ProcessorErrorCode,
    ProcessorFailurePolicy,
    RepairMode,
)

MAX_CONFIGURED_BYTES = 1_048_576
MAX_API_KEY_REF_CHARS = 255
MAX_MODEL_CHARS = 200
MAX_BASE_URL_CHARS = 2_000


class SettingsValidationError(ValueError):
    """One or more settings failed validation.

    ``errors`` contains field-level messages that are safe to show in an admin
    UI.  Values are deliberately never included.
    """

    error_code = ProcessorErrorCode.SETTINGS_INVALID

    def __init__(self, errors: str | list[str] | tuple[str, ...]) -> None:
        items = (errors,) if isinstance(errors, str) else tuple(errors)
        self.errors = items or ("settings are invalid",)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class JsonProcessorSettings:
    """The complete processor configuration supplied by an administrator."""

    enabled: bool = False
    base_url: str = ""
    api_key_ref: str = ""
    model: str = ""
    local_repair_enabled: bool = True
    max_llm_attempts: int = 2
    request_timeout_seconds: int = 45
    max_input_bytes: int = 65_536
    max_output_bytes: int = 65_536
    max_output_tokens: int = 4_096
    temperature: float = 0.0
    repair_mode: RepairMode = RepairMode.SYNTAX_ONLY
    on_processor_failure: ProcessorFailurePolicy = ProcessorFailurePolicy.FAIL
    concurrency_limit: int = 4
    queue_limit: int = 1_000
    raw_output_retention_days: int = 30
    attempt_retention_days: int = 90

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        reject_unknown: bool = True,
    ) -> JsonProcessorSettings:
        """Build a validated settings object.

        ``reject_unknown=True`` is used for updates so an operator cannot smuggle
        a secret or a schema-safe allowance list into global configuration.
        Reads may ignore unknown keys from a manually modified row.
        """

        if not isinstance(data, Mapping):
            raise SettingsValidationError("settings must be a JSON object")

        known = {item.name for item in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown and reject_unknown:
            raise SettingsValidationError(
                [f"unknown setting: {name}" for name in unknown]
            )

        defaults = cls()
        errors: list[str] = []

        def value(name: str) -> Any:
            return data.get(name, getattr(defaults, name))

        def boolean(name: str) -> bool:
            raw = value(name)
            if not isinstance(raw, bool):
                errors.append(f"{name} must be a boolean")
                return getattr(defaults, name)
            return raw

        def integer(name: str, minimum: int, maximum: int) -> int:
            raw = value(name)
            if isinstance(raw, bool) or not isinstance(raw, int):
                errors.append(f"{name} must be an integer")
                return getattr(defaults, name)
            if raw < minimum or raw > maximum:
                errors.append(f"{name} must be between {minimum} and {maximum}")
                return getattr(defaults, name)
            return raw

        def text(name: str, maximum: int, *, required: bool = False) -> str:
            raw = value(name)
            if not isinstance(raw, str):
                errors.append(f"{name} must be a string")
                return getattr(defaults, name)
            normalized = raw.strip()
            if required and not normalized:
                errors.append(f"{name} is required")
            if len(normalized) > maximum:
                errors.append(f"{name} must contain at most {maximum} characters")
            if _has_control(normalized):
                errors.append(f"{name} must not contain control characters")
            return normalized

        enabled = boolean("enabled")
        local_repair_enabled = boolean("local_repair_enabled")
        max_llm_attempts = integer("max_llm_attempts", 0, 3)
        request_timeout_seconds = integer("request_timeout_seconds", 1, 300)
        max_input_bytes = integer("max_input_bytes", 1, MAX_CONFIGURED_BYTES)
        max_output_bytes = integer("max_output_bytes", 1, MAX_CONFIGURED_BYTES)
        max_output_tokens = integer("max_output_tokens", 1, 32_768)
        concurrency_limit = integer("concurrency_limit", 1, 32)
        queue_limit = integer("queue_limit", 1, 100_000)
        raw_output_retention_days = integer("raw_output_retention_days", 1, 3_650)
        attempt_retention_days = integer("attempt_retention_days", 1, 3_650)

        base_url = _normalize_base_url(text("base_url", MAX_BASE_URL_CHARS), errors)
        api_key_ref = text("api_key_ref", MAX_API_KEY_REF_CHARS)
        model = text("model", MAX_MODEL_CHARS)
        repair_mode = _enum(
            "repair_mode",
            value("repair_mode"),
            RepairMode,
            defaults.repair_mode,
            errors,
        )
        on_processor_failure = _enum(
            "on_processor_failure",
            value("on_processor_failure"),
            ProcessorFailurePolicy,
            defaults.on_processor_failure,
            errors,
        )

        raw_temperature = value("temperature")
        if isinstance(raw_temperature, bool) or not isinstance(
            raw_temperature, (int, float)
        ):
            errors.append("temperature must be a number")
            temperature = defaults.temperature
        else:
            temperature = float(raw_temperature)
            if temperature < 0 or temperature > 2:
                errors.append("temperature must be between 0 and 2")

        if enabled and not local_repair_enabled and max_llm_attempts == 0:
            errors.append(
                "an enabled processor needs local repair or at least one LLM attempt"
            )
        if enabled and max_llm_attempts > 0 and not base_url:
            errors.append("base_url is required when LLM repair is enabled")
        if enabled and max_llm_attempts > 0 and not model:
            errors.append("model is required when LLM repair is enabled")

        if errors:
            raise SettingsValidationError(errors)

        return cls(
            enabled=enabled,
            base_url=base_url,
            api_key_ref=api_key_ref,
            model=model,
            local_repair_enabled=local_repair_enabled,
            max_llm_attempts=max_llm_attempts,
            request_timeout_seconds=request_timeout_seconds,
            max_input_bytes=max_input_bytes,
            max_output_bytes=max_output_bytes,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            repair_mode=repair_mode,
            on_processor_failure=on_processor_failure,
            concurrency_limit=concurrency_limit,
            queue_limit=queue_limit,
            raw_output_retention_days=raw_output_retention_days,
            attempt_retention_days=attempt_retention_days,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return only known, non-secret settings in a stable order."""

        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "api_key_ref": self.api_key_ref,
            "model": self.model,
            "local_repair_enabled": self.local_repair_enabled,
            "max_llm_attempts": self.max_llm_attempts,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_input_bytes": self.max_input_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "repair_mode": self.repair_mode.value,
            "on_processor_failure": self.on_processor_failure.value,
            "concurrency_limit": self.concurrency_limit,
            "queue_limit": self.queue_limit,
            "raw_output_retention_days": self.raw_output_retention_days,
            "attempt_retention_days": self.attempt_retention_days,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _enum(
    name: str,
    raw: Any,
    enum_type: Any,
    default: Any,
    errors: list[str],
) -> Any:
    if isinstance(raw, enum_type):
        return raw
    if isinstance(raw, str):
        try:
            return enum_type(raw)
        except ValueError:
            pass
    allowed = ", ".join(item.value for item in enum_type)
    errors.append(f"{name} must be one of: {allowed}")
    return default


def _normalize_base_url(raw: str, errors: list[str]) -> str:
    if not raw:
        return ""
    if _has_control(raw) or any(char.isspace() for char in raw):
        errors.append("base_url must not contain whitespace or control characters")
        return ""
    try:
        parsed = urlsplit(raw)
        parsed.port  # noqa: B018 - force validation of an invalid port
    except ValueError:
        errors.append("base_url is not a valid URL")
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        errors.append("base_url must be an absolute http(s) URL")
        return ""
    if parsed.username is not None or parsed.password is not None:
        errors.append("base_url must not contain credentials")
        return ""
    if parsed.query or parsed.fragment:
        errors.append("base_url must not contain a query or fragment")
        return ""
    return raw.rstrip("/")


def default_settings_json() -> str:
    """Canonical JSON used to seed the singleton settings row."""

    return JsonProcessorSettings().to_json()


__all__ = [
    "MAX_API_KEY_REF_CHARS",
    "MAX_BASE_URL_CHARS",
    "MAX_CONFIGURED_BYTES",
    "MAX_MODEL_CHARS",
    "JsonProcessorSettings",
    "SettingsValidationError",
    "default_settings_json",
]
