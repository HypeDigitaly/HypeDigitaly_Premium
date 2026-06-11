"""_deduplicate_urls mixed-type sort (Wave 3.2, Bug 2).

Mixed datetime / str / None last_modified values must:
  * never raise TypeError,
  * keep the most-recent entry,
  * break ties on equal timestamps by first-wins (original input order).
"""
from __future__ import annotations

from datetime import datetime, timezone

from gpt_scraper_v3.cli import _deduplicate_urls


def _entry(url, last_modified, tag):
    return {"url": url, "title": tag, "path": "p", "last_modified": last_modified}


def test_mixed_types_no_typeerror_most_recent_wins():
    url = "https://example.com/page"
    older = datetime(2020, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2024, 6, 1, tzinfo=timezone.utc)
    urls = [
        _entry(url, older, "older-dt"),
        _entry(url, "2023-01-01", "str-date"),
        _entry(url, None, "none"),
        _entry(url, newer, "newest-dt"),
    ]
    result = _deduplicate_urls(urls)
    assert len(result) == 1
    assert result[0]["title"] == "newest-dt"


def test_naive_and_aware_datetimes_mixed():
    url = "https://example.com/x"
    naive_new = datetime(2025, 1, 1)  # naive -> treated as UTC
    aware_old = datetime(2021, 1, 1, tzinfo=timezone.utc)
    urls = [
        _entry(url, aware_old, "old"),
        _entry(url, naive_new, "new"),
    ]
    result = _deduplicate_urls(urls)
    assert len(result) == 1
    assert result[0]["title"] == "new"


def test_all_none_first_wins_tiebreak():
    url = "https://example.com/y"
    urls = [
        _entry(url, None, "first"),
        _entry(url, None, "second"),
        _entry(url, None, "third"),
    ]
    result = _deduplicate_urls(urls)
    assert len(result) == 1
    assert result[0]["title"] == "first"


def test_equal_timestamps_first_wins():
    url = "https://example.com/z"
    same = datetime(2024, 3, 3, tzinfo=timezone.utc)
    urls = [
        _entry(url, same, "first"),
        _entry(url, same, "second"),
    ]
    result = _deduplicate_urls(urls)
    assert len(result) == 1
    assert result[0]["title"] == "first"


def test_distinct_urls_all_preserved():
    urls = [
        _entry("https://example.com/a", None, "a"),
        _entry("https://example.com/b", datetime(2024, 1, 1, tzinfo=timezone.utc), "b"),
    ]
    result = _deduplicate_urls(urls)
    assert len(result) == 2
    titles = {e["title"] for e in result}
    assert titles == {"a", "b"}


def test_unparseable_string_treated_as_sentinel():
    url = "https://example.com/u"
    urls = [
        _entry(url, "not-a-date", "garbage"),
        _entry(url, datetime(2024, 1, 1, tzinfo=timezone.utc), "real"),
    ]
    result = _deduplicate_urls(urls)
    assert len(result) == 1
    assert result[0]["title"] == "real"
