"""Query-string filters shared by the admin lists: free-text search and date ranges."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# A quoted phrase (an unclosed quote runs to the end) or a bare word.
SEARCH_TOKEN = re.compile(r'"([^"]*)"?|(\S+)')
COMBINING_MARKS = re.compile(r"[\u0300-\u036f]")
DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
# An unencoded "+03:00" offset arrives as " 03:00".
SPACED_OFFSET = re.compile(r" (\d{2}(?::?\d{2})?)$")


def normalize_for_search(value: str) -> str:
    return COMBINING_MARKS.sub("", unicodedata.normalize("NFKD", value)).casefold()


def search_tokens(query: str | None) -> list[str]:
    """Trimmed query split on whitespace; quoted phrases stay whole."""
    tokens = []
    for phrase, word in SEARCH_TOKEN.findall((query or "").strip()):
        if token := normalize_for_search(phrase or word):
            tokens.append(token)
    return tokens


def matches_search(tokens: list[str], *fields: str | None) -> bool:
    """Every token must appear in at least one field."""
    haystacks = [normalize_for_search(field) for field in fields if field]
    return all(any(token in haystack for haystack in haystacks) for token in tokens)


def time_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Unknown timezone") from None


def time_bound(value: str | None, zone: ZoneInfo, *, end: bool) -> str | None:
    """UTC ISO bound. A bare date covers that whole day in `zone`; times without
    an offset are read in `zone` too. `end` bounds are exclusive."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        if DATE_ONLY.fullmatch(text):
            day = date.fromisoformat(text)
            moment = datetime.combine(day + timedelta(days=1) if end else day, time.min, zone)
        else:
            moment = datetime.fromisoformat(SPACED_OFFSET.sub(r"+\1", text))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=zone)
        return moment.astimezone(UTC).isoformat()
    except (ValueError, OverflowError):
        raise ValueError(f"Invalid date: {text}") from None


def time_range(
    start: str | None, end: str | None, timezone: str = "UTC"
) -> tuple[str | None, str | None]:
    zone = time_zone(timezone)
    return time_bound(start, zone, end=False), time_bound(end, zone, end=True)
