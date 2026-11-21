"""Recursive secret redaction for VFlow audit and event metadata.

Audit metadata may contain arbitrary dictionaries assembled by callers.  This
module keeps only safe structural information: values stored under a key whose
normalized name looks secret are replaced with :data:`REDACTED`, including
whole nested structures under such a key.  Depth is bounded so hostile or
self-referential objects cannot exhaust the stack.
"""

from __future__ import annotations

from typing import Any

REDACTED = "[REDACTED]"
DEFAULT_MAX_DEPTH = 10

# Keys containing any of these normalized fragments are treated as secrets.
# Markers use underscores; key normalization also converts "-" and spaces to
# underscores so that ``X-API-Key`` and ``apiKey`` are both recognized.
_SECRET_KEY_MARKERS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "authentication",
    "basic_auth",
    "basicauth",
    "auth_",
    "cookie",
    "signature",
    "private_key",
    "privatekey",
    "credential",
    "session_key",
    "sessionkey",
    "access_key",
    "accesskey",
    "client_key",
    "clientkey",
    "signing_key",
    "signingkey",
    "encryption_key",
    "encryptionkey",
    "auth_token",
    "auth_key",
    "bearer",
    "jwt",
)

# Very short generic names are matched exactly to avoid false positives such as
# ordinary prose keys which merely contain the same letters.
_SECRET_EXACT_KEYS = frozenset({"auth", "pass", "pwd"})


def _is_secret_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in _SECRET_EXACT_KEYS:
        return True
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def redact_metadata(value: Any, *, max_depth: int = DEFAULT_MAX_DEPTH) -> Any:
    """Return a redacted copy of ``value`` while preserving its shape.

    Secret-keyed values and any container nested at or beyond ``max_depth`` are
    replaced with ``"[REDACTED]"``.  Scalars under non-secret keys and normal
    dictionaries/lists are preserved.  Tuples stay tuples so Python callers see
    the same structure; JSON serialization naturally renders them as arrays.
    """

    limit = max(0, int(max_depth))
    return _redact(value, depth=0, max_depth=limit)


def _redact(value: Any, *, depth: int, max_depth: int) -> Any:
    if isinstance(value, dict):
        if depth >= max_depth:
            return REDACTED
        return {
            key: (
                REDACTED
                if _is_secret_key(key)
                else _redact(item, depth=depth + 1, max_depth=max_depth)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        if depth >= max_depth:
            return REDACTED
        return [
            _redact(item, depth=depth + 1, max_depth=max_depth) for item in value
        ]
    if isinstance(value, tuple):
        if depth >= max_depth:
            return REDACTED
        return tuple(
            _redact(item, depth=depth + 1, max_depth=max_depth) for item in value
        )
    return value
