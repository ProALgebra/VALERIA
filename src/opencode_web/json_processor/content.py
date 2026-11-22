"""Content-preservation checks for repaired JSON.

``syntax_only`` requires the decoded value to be identical before and after a
repair.  ``schema_safe`` may only allow deterministic changes that a caller
explicitly allow-lists (for example inserting a known placeholder at a missing
schema field).  There is no implicit semantic rewriting.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .models import (
    ProcessorError,
    ProcessorErrorCode,
    RepairMode,
    ValidationDiagnostic,
    field_path,
    index_path,
    json_type_name,
    json_values_equal,
)

MAX_CONTENT_DIAGNOSTICS = 100


class ContentChangeKind(StrEnum):
    VALUE_CHANGED = "value_changed"
    TYPE_CHANGED = "type_changed"
    KEY_ADDED = "key_added"
    KEY_REMOVED = "key_removed"
    ITEM_ADDED = "item_added"
    ITEM_REMOVED = "item_removed"


class ContentOperation(StrEnum):
    """Operations that a later deterministic transformation may request."""

    SET_VALUE = "set_value"
    ADD = "add"
    REMOVE = "remove"


@dataclass(frozen=True, slots=True)
class ContentChange:
    """One path-level difference between decoded values."""

    path: str
    kind: ContentChangeKind
    before_type: str | None
    after_type: str | None


@dataclass(frozen=True, slots=True)
class ContentAllowance:
    """Explicit permission for deterministic schema-safe changes.

    ``subtree=True`` applies the allowance to the path and all descendants.
    ``syntax_only`` ignores allowances entirely.
    """

    path: str
    operations: frozenset[ContentOperation]
    reason: str = ""
    subtree: bool = False

    def matches_path(self, path: str) -> bool:
        if path == self.path:
            return True
        if not self.subtree:
            return False
        return path.startswith(f"{self.path}.") or path.startswith(f"{self.path}[")

    def allows(self, change: ContentChange) -> bool:
        if not self.matches_path(change.path):
            return False
        return _operation_for(change.kind) in self.operations


def placeholder_allowance(
    path: str,
    *,
    subtree: bool = False,
    reason: str = "",
) -> ContentAllowance:
    """Allow a known placeholder to fill an absent or invalid field.

    This only describes permission.  Applying the transformation remains the
    responsibility of a later deterministic phase.
    """

    return ContentAllowance(
        path=path,
        operations=frozenset(
            {ContentOperation.SET_VALUE, ContentOperation.ADD}
        ),
        reason=reason,
        subtree=subtree,
    )


@dataclass(frozen=True, slots=True)
class ContentCheckResult:
    """Outcome of comparing two decoded JSON values."""

    ok: bool
    changes: tuple[ContentChange, ...] = ()
    unauthorized: tuple[ContentChange, ...] = ()
    error: ProcessorError | None = None


def _content_type(value: Any) -> str:
    name = json_type_name(value)
    return "number" if name in {"integer", "number"} else name


def _operation_for(kind: ContentChangeKind) -> ContentOperation:
    if kind in {ContentChangeKind.KEY_ADDED, ContentChangeKind.ITEM_ADDED}:
        return ContentOperation.ADD
    if kind in {ContentChangeKind.KEY_REMOVED, ContentChangeKind.ITEM_REMOVED}:
        return ContentOperation.REMOVE
    return ContentOperation.SET_VALUE


def diff_json_values(
    before: Any,
    after: Any,
    *,
    path: str = "$",
) -> tuple[ContentChange, ...]:
    """Describe all value, type, key, and item differences recursively."""

    if json_values_equal(before, after):
        return ()
    before_type = _content_type(before)
    after_type = _content_type(after)
    if before_type != after_type:
        return (
            ContentChange(path, ContentChangeKind.TYPE_CHANGED, before_type, after_type),
        )

    changes: list[ContentChange] = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in before:
            if key not in after:
                changes.append(
                    ContentChange(
                        field_path(path, key),
                        ContentChangeKind.KEY_REMOVED,
                        _content_type(before[key]),
                        None,
                    )
                )
        for key in after:
            if key not in before:
                changes.append(
                    ContentChange(
                        field_path(path, key),
                        ContentChangeKind.KEY_ADDED,
                        None,
                        _content_type(after[key]),
                    )
                )
        for key in before:
            if key in after:
                changes.extend(
                    diff_json_values(
                        before[key],
                        after[key],
                        path=field_path(path, key),
                    )
                )
        return tuple(changes)

    if isinstance(before, list) and isinstance(after, list):
        common = min(len(before), len(after))
        for item_index in range(common):
            changes.extend(
                diff_json_values(
                    before[item_index],
                    after[item_index],
                    path=index_path(path, item_index),
                )
            )
        for item_index in range(common, len(before)):
            changes.append(
                ContentChange(
                    index_path(path, item_index),
                    ContentChangeKind.ITEM_REMOVED,
                    _content_type(before[item_index]),
                    None,
                )
            )
        for item_index in range(common, len(after)):
            changes.append(
                ContentChange(
                    index_path(path, item_index),
                    ContentChangeKind.ITEM_ADDED,
                    None,
                    _content_type(after[item_index]),
                )
            )
        return tuple(changes)

    return (
        ContentChange(path, ContentChangeKind.VALUE_CHANGED, before_type, after_type),
    )


def check_content_preserved(
    before: Any,
    after: Any,
    mode: RepairMode | str = RepairMode.SYNTAX_ONLY,
    *,
    allowances: Iterable[ContentAllowance] = (),
) -> ContentCheckResult:
    """Fail closed when a repair changed content outside explicit allowances."""

    resolved_mode = mode if isinstance(mode, RepairMode) else RepairMode(mode)
    changes = diff_json_values(before, after)
    if not changes:
        return ContentCheckResult(ok=True)

    if resolved_mode is RepairMode.SYNTAX_ONLY:
        unauthorized = changes
    else:
        allowed = tuple(allowances)
        unauthorized = tuple(
            change
            for change in changes
            if not any(item.allows(change) for item in allowed)
        )
    if not unauthorized:
        return ContentCheckResult(ok=True, changes=changes)

    first = unauthorized[0]
    diagnostics = tuple(
        ValidationDiagnostic(
            code="CONTENT_CHANGED",
            message=f"{change.kind.value} at {change.path}",
            path=change.path,
        )
        for change in unauthorized[:MAX_CONTENT_DIAGNOSTICS]
    )
    error = ProcessorError(
        ProcessorErrorCode.CONTENT_CHANGED,
        f"content changed at {first.path} ({first.kind.value})",
        first.path,
        diagnostics,
    )
    return ContentCheckResult(
        ok=False,
        changes=changes,
        unauthorized=unauthorized,
        error=error,
    )


def ensure_content_preserved(
    before: Any,
    after: Any,
    mode: RepairMode | str = RepairMode.SYNTAX_ONLY,
    *,
    allowances: Iterable[ContentAllowance] = (),
) -> None:
    """Raise :class:`ProcessorError` unless content is preserved."""

    result = check_content_preserved(before, after, mode, allowances=allowances)
    if result.error is not None:
        raise result.error


__all__ = [
    "MAX_CONTENT_DIAGNOSTICS",
    "ContentAllowance",
    "ContentChange",
    "ContentChangeKind",
    "ContentCheckResult",
    "ContentOperation",
    "check_content_preserved",
    "diff_json_values",
    "ensure_content_preserved",
    "placeholder_allowance",
]
