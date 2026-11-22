"""Strict, conservative JSON parsing for untrusted model output.

The parser starts with ``json.loads`` and only then applies syntax-level,
value-preserving repairs.  It never fills in missing fields and never guesses
semantic content.  If more than one independent JSON candidate is present the
result is ambiguous rather than arbitrarily choosing one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .models import (
    ParseResult,
    ParseStrategy,
    ProcessorError,
    ProcessorErrorCode,
)

MAX_RAW_OUTPUT_BYTES = 1_048_576

_SUPPORTED_FENCE_LANGUAGES = {"", "json"}
_SINGLE_QUOTE_ESCAPES = {
    "'": "'",
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_HEX_RE = re.compile(r"[0-9A-Fa-f]{4}\Z")


class _NormalizationError(ValueError):
    """A conservative normalization could not be applied safely."""


@dataclass(frozen=True, slots=True)
class _Fence:
    content: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    value: Any
    start: int
    end: int
    normalizations: tuple[str, ...]


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-JSON constant {value!r}")


def _loads(text: str) -> Any:
    """Strict ``json.loads`` including rejection of NaN/Infinity constants."""

    return json.loads(text, parse_constant=_reject_constant)


def _try_loads(text: str) -> tuple[Any, bool]:
    try:
        return _loads(text), True
    except (TypeError, ValueError):
        return None, False


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _remove_trailing_commas(text: str) -> tuple[str, bool]:
    """Remove commas directly before ``}`` or ``]`` outside strings."""

    output: list[str] = []
    index = 0
    quote: str | None = None
    escaped = False
    changed = False
    while index < len(text):
        char = text[index]
        if quote is not None:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                changed = True
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output), changed


def _read_single_quoted(text: str, start: int) -> tuple[str, int]:
    """Decode one JS/Python-like single-quoted string conservatively."""

    chars: list[str] = []
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "'":
            return "".join(chars), index + 1
        if char in "\r\n":
            raise _NormalizationError("single-quoted string contains a raw newline")
        if char != "\\":
            chars.append(char)
            index += 1
            continue
        index += 1
        if index >= len(text):
            raise _NormalizationError("single-quoted string ends with a backslash")
        escape = text[index]
        if escape in _SINGLE_QUOTE_ESCAPES:
            chars.append(_SINGLE_QUOTE_ESCAPES[escape])
            index += 1
            continue
        if escape == "u":
            digits = text[index + 1 : index + 5]
            if len(digits) != 4 or _HEX_RE.fullmatch(digits) is None:
                raise _NormalizationError("invalid unicode escape in single-quoted string")
            chars.append(chr(int(digits, 16)))
            index += 5
            continue
        raise _NormalizationError(f"unsupported escape \\{escape} in single-quoted string")
    raise _NormalizationError("unterminated single-quoted string")


def _convert_single_quotes(text: str) -> tuple[str, bool]:
    """Convert single-quoted JSON-like strings while preserving their values."""

    output: list[str] = []
    index = 0
    in_double = False
    escaped = False
    changed = False
    while index < len(text):
        char = text[index]
        if in_double:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_double = False
            index += 1
            continue
        if char == '"':
            in_double = True
            output.append(char)
            index += 1
            continue
        if char == "'":
            value, next_index = _read_single_quoted(text, index)
            output.append(json.dumps(value, ensure_ascii=True))
            index = next_index
            changed = True
            continue
        output.append(char)
        index += 1
    return "".join(output), changed


def _convert_python_literals(text: str) -> tuple[str, bool]:
    """Convert ``True``/``False``/``None`` tokens outside strings only."""

    replacements = {"True": "true", "False": "false", "None": "null"}
    output: list[str] = []
    index = 0
    quote: str | None = None
    escaped = False
    changed = False
    while index < len(text):
        char = text[index]
        if quote is not None:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            output.append(char)
            index += 1
            continue
        for token, replacement in replacements.items():
            if not text.startswith(token, index):
                continue
            before = text[index - 1] if index > 0 else ""
            after_index = index + len(token)
            after = text[after_index] if after_index < len(text) else ""
            if (before and _is_word_char(before)) or (after and _is_word_char(after)):
                continue
            output.append(replacement)
            index = after_index
            changed = True
            break
        else:
            output.append(char)
            index += 1
    return "".join(output), changed


def _normalize_json_syntax(text: str) -> tuple[str, tuple[str, ...]]:
    """Apply the conservative normalization pipeline, recording each step."""

    applied: list[str] = []
    normalized = text
    steps = (
        ("trailing_commas", _remove_trailing_commas),
        ("single_quotes", _convert_single_quotes),
        ("python_literals", _convert_python_literals),
    )
    for name, step in steps:
        normalized, changed = step(normalized)
        if changed:
            applied.append(name)
    return normalized, tuple(applied)


def _parse_clean_text(text: str) -> tuple[Any, tuple[str, ...]] | None:
    """Parse text directly, then via conservative normalization."""

    parsed, ok = _try_loads(text)
    if ok:
        return parsed, ()
    try:
        normalized, applied = _normalize_json_syntax(text)
    except _NormalizationError:
        return None
    if not applied:
        return None
    parsed, ok = _try_loads(normalized)
    if not ok:
        return None
    return parsed, applied


def _skip_string(text: str, start: int) -> int:
    quote = text[start]
    index = start + 1
    escaped = False
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            return index + 1
        index += 1
    return len(text)


def _matching_end(text: str, start: int) -> int | None:
    expected = {"{": "}", "[": "]"}[text[start]]
    stack = [expected]
    index = start + 1
    while index < len(text):
        char = text[index]
        if char in {'"', "'"}:
            index = _skip_string(text, index)
            continue
        if char in "{[":
            stack.append("}" if char == "{" else "]")
            index += 1
            continue
        if char in "}]":
            if not stack or char != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return index + 1
        index += 1
    return None


def _fence_blocks(text: str) -> list[_Fence]:
    """Find complete markdown fences with no information string or ``json``."""

    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)

    fences: list[_Fence] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped.startswith("```"):
            index += 1
            continue
        language = stripped[3:].strip().lower()
        content_start = offsets[index] + len(lines[index])
        close_index = index + 1
        while close_index < len(lines) and lines[close_index].strip() != "```":
            close_index += 1
        if close_index >= len(lines):
            break
        content_end = offsets[close_index]
        if language in _SUPPORTED_FENCE_LANGUAGES:
            fences.append(
                _Fence(text[content_start:content_end], content_start, content_end)
            )
        index = close_index + 1
    return fences


def _collect_candidates(text: str) -> list[_Candidate]:
    """Collect independent, valid object/array candidates outside strings."""

    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        char = text[index]
        # Only JSON double-quoted strings are skipped here.  Apostrophes in
        # surrounding prose ("Here's the result") must not hide the candidate;
        # braces inside single-quoted candidates are handled by containment
        # checks after parsing.
        if char == '"':
            index = _skip_string(text, index)
            continue
        if char in "{[":
            end = _matching_end(text, index)
            if end is not None:
                spans.append((index, end))
        index += 1

    parsed_candidates: list[_Candidate] = []
    seen: set[tuple[int, int]] = set()
    for start, end in spans:
        if (start, end) in seen:
            continue
        seen.add((start, end))
        parsed = _parse_clean_text(text[start:end])
        if parsed is None:
            continue
        value, applied = parsed
        parsed_candidates.append(_Candidate(value, start, end, applied))

    independent: list[_Candidate] = []
    for candidate in parsed_candidates:
        contained = any(
            start <= candidate.start
            and end >= candidate.end
            and (start, end) != (candidate.start, candidate.end)
            for start, end in spans
        )
        if not contained:
            independent.append(candidate)
    return independent


def _not_repairable(
    raw_output: str,
    message: str = "raw output does not contain one unambiguous JSON value",
) -> ParseResult:
    return ParseResult.failure(
        ProcessorError(ProcessorErrorCode.NOT_REPAIRABLE, message),
        raw_output=raw_output,
    )


def parse_json_output(
    raw_output: str,
    *,
    max_bytes: int = MAX_RAW_OUTPUT_BYTES,
) -> ParseResult:
    """Parse raw model output into exactly one JSON value.

    Recovery order:

    1. strict ``json.loads`` over the whole output;
    2. one complete markdown code fence containing JSON;
    3. conservative normalization over the whole output;
    4. exactly one independent JSON object/array candidate with prose around it.

    Any failure is returned as a fail-closed :class:`ParseResult`; callers must
    not pass ``value`` downstream when ``ok`` is false.
    """

    if not isinstance(raw_output, str):
        return ParseResult.failure(
            ProcessorError(
                ProcessorErrorCode.INVALID_INPUT,
                "raw output must be a string",
            ),
            raw_output="",
        )

    try:
        size = len(raw_output.encode("utf-8"))
    except UnicodeEncodeError:
        return _not_repairable(raw_output, "raw output is not valid Unicode text")
    if size > max_bytes:
        return ParseResult.failure(
            ProcessorError(
                ProcessorErrorCode.RAW_OUTPUT_TOO_LARGE,
                f"raw output exceeds {max_bytes} bytes",
            ),
            raw_output=raw_output,
        )

    stripped = raw_output.strip()
    if not stripped:
        return ParseResult.failure(
            ProcessorError(ProcessorErrorCode.EMPTY_OUTPUT, "raw output is empty"),
            raw_output=raw_output,
        )

    parsed, ok = _try_loads(raw_output)
    if ok:
        return ParseResult.success(
            parsed,
            raw_output=raw_output,
            strategy=ParseStrategy.DIRECT,
        )

    cleaned = _parse_clean_text(stripped)
    if cleaned is not None:
        value, applied = cleaned
        return ParseResult.success(
            value,
            raw_output=raw_output,
            strategy=ParseStrategy.NORMALIZED,
            normalizations=applied,
        )

    valid_fences: list[_Candidate] = []
    for fence in _fence_blocks(raw_output):
        parsed_fence = _parse_clean_text(fence.content.strip())
        if parsed_fence is None:
            continue
        value, applied = parsed_fence
        valid_fences.append(
            _Candidate(value, fence.start, fence.end, applied)
        )
    if len(valid_fences) > 1:
        return ParseResult.failure(
            ProcessorError(
                ProcessorErrorCode.AMBIGUOUS_JSON,
                f"raw output contains {len(valid_fences)} ambiguous JSON candidates",
            ),
            raw_output=raw_output,
            candidate_count=len(valid_fences),
        )
    if valid_fences:
        fence = valid_fences[0]
        return ParseResult.success(
            fence.value,
            raw_output=raw_output,
            strategy=ParseStrategy.FENCED,
            normalizations=fence.normalizations,
        )

    candidates = _collect_candidates(raw_output)
    if len(candidates) > 1:
        return ParseResult.failure(
            ProcessorError(
                ProcessorErrorCode.AMBIGUOUS_JSON,
                f"raw output contains {len(candidates)} ambiguous JSON candidates",
            ),
            raw_output=raw_output,
            candidate_count=len(candidates),
        )
    if not candidates:
        return _not_repairable(raw_output)

    candidate = candidates[0]
    leading = len(raw_output) - len(raw_output.lstrip())
    trailing = len(raw_output.rstrip())
    is_whole_text = candidate.start == leading and candidate.end == trailing
    return ParseResult.success(
        candidate.value,
        raw_output=raw_output,
        strategy=(
            ParseStrategy.NORMALIZED if is_whole_text else ParseStrategy.PROSE_EXTRACTED
        ),
        normalizations=candidate.normalizations,
    )


__all__ = ["MAX_RAW_OUTPUT_BYTES", "parse_json_output"]
