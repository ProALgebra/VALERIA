from __future__ import annotations

import pytest

from opencode_web.filters import matches_search, search_tokens, time_range


def test_search_tokens_trim_split_and_keep_quoted_phrases():
    assert search_tokens('  red "Pinot  Toscana" Château "" "open') == [
        "red", "pinot  toscana", "chateau", "open",
    ]
    assert search_tokens("   ") == [] and search_tokens(None) == []


def test_every_token_must_match_some_field():
    tokens = search_tokens('chateau "ПИНО НУАР"')
    assert matches_search(tokens, "Château Margaux", "пино нуар 2019")
    assert not matches_search(tokens, "Château Margaux", "пино 2019")
    assert matches_search(search_tokens("é"), "café")
    assert matches_search([], None)


def test_time_range_reads_dates_as_whole_days_and_times_exactly():
    assert time_range("2026-03-01", "2026-03-01") == (
        "2026-03-01T00:00:00+00:00", "2026-03-02T00:00:00+00:00",
    )
    assert time_range("2026-03-01", None, "Europe/Moscow") == (
        "2026-02-28T21:00:00+00:00", None,
    )
    assert time_range("2026-03-01T10:00:00 03:00", "2026-03-01T10:00:00Z") == (
        "2026-03-01T07:00:00+00:00", "2026-03-01T10:00:00+00:00",
    )
    assert time_range("", "  ") == (None, None)
    for start, zone in (("2026-02-30", "UTC"), ("soon", "UTC"), (None, "Nowhere/City")):
        with pytest.raises(ValueError):
            time_range(start, None, zone)
