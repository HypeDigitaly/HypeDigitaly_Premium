"""Tests for RSS feed processing fixes F1 (RSS-specific timestamp baseline)
and F4 (cross-feed item deduplication).

F1 is implemented at the *caller* (``cli.resolve_urls``): the RSS branch sources
``get_last_run_timestamp("rss")`` and passes that into ``process_rss_feeds``,
instead of the combined ``last_ts``. We exercise the seam two ways:

  * Directly: call ``process_rss_feeds`` with the RSS baseline and assert items
    straddling it are gated correctly, and that the value reaches
    ``should_process_url_with_resume`` as the ``last_run_timestamp`` argument.
  * Via the caller: drive ``resolve_urls`` with distinct sentinel timestamps per
    type (rss/sitemap/combined) and assert the RSS gate sees the *rss* one.

F4 patches ``parse_rss_feed`` so multiple configured feeds return the same item
and asserts the result collapses to one occurrence (first wins), with the
cross-feed collapse logged.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gpt_scraper_v3 import rss_feeds
from gpt_scraper_v3 import url_processing
from gpt_scraper_v3 import xml_sitemap


# -- Helpers ------------------------------------------------------------------

def _patch_timestamps(monkeypatch, mapping):
    """Patch get_last_run_timestamp at its source module (xml_sitemap).

    ``process_rss_feeds`` resolves ``get_last_run_timestamp`` via a *late* import
    from ``xml_sitemap`` inside the function body, so patching must target the
    source module, not the ``rss_feeds`` namespace. *mapping* maps
    timestamp_type -> datetime|None.
    """
    monkeypatch.setattr(
        xml_sitemap, "get_last_run_timestamp",
        lambda t="combined": mapping.get(t),
    )

def _item(url: str, published: datetime) -> dict:
    """Build a minimal RSS url_info dict as parse_rss_feed would emit."""
    return {"url": url, "published": published, "title": url}


def _patch_feeds(monkeypatch, mapping):
    """Patch parse_rss_feed so each feed URL returns the supplied item list.

    *mapping* maps feed_url -> list[url_info]. Unknown feeds yield [].
    """
    def _fake(feed_url):
        return list(mapping.get(feed_url, []))

    monkeypatch.setattr(rss_feeds, "parse_rss_feed", _fake)


def _spy_should_process(monkeypatch, return_value=True):
    """Patch should_process_url_with_resume (consumed via late import from
    url_processing) to record its calls and return a fixed value.

    Returns the list of recorded call kwargs/positional snapshots.
    """
    calls = []

    def _fake(url, last_modified, last_run_timestamp, *args, **kwargs):
        calls.append({
            "url": url,
            "last_modified": last_modified,
            "last_run_timestamp": last_run_timestamp,
            "rss_published_date": kwargs.get("rss_published_date"),
            "sitemap_last_run_timestamp": kwargs.get("sitemap_last_run_timestamp"),
        })
        return return_value

    monkeypatch.setattr(url_processing, "should_process_url_with_resume", _fake)
    return calls


# -- Sentinels ----------------------------------------------------------------

T_RSS = datetime(2025, 6, 1, tzinfo=timezone.utc)
T_SITE = datetime(2026, 1, 1, tzinfo=timezone.utc)
T_COMB = datetime(2026, 6, 9, tzinfo=timezone.utc)


# =============================================================================
# F1 -- RSS gated against the RSS-specific baseline
# =============================================================================

def test_f1_process_rss_feeds_receives_rss_baseline(make_config, monkeypatch):
    """The timestamp passed to process_rss_feeds reaches should_process as the
    per-item last_run_timestamp (proving the RSS baseline, not combined, flows
    through). Real gating (Step 3) is exercised separately below."""
    make_config(
        RSS_FEEDS=["https://example.com/rss"],
        CHECK_LAST_MODIFIED=1,
        RSS_DATE_THRESHOLD=None,
    )
    # sitemap baseline used only by the Step-4 sub-check -- keep it distinct.
    _patch_timestamps(monkeypatch, {"sitemap": T_SITE})
    _patch_feeds(monkeypatch, {
        "https://example.com/rss": [_item("https://example.com/a", T_COMB)],
    })
    calls = _spy_should_process(monkeypatch, return_value=True)

    result = rss_feeds.process_rss_feeds(
        url_last_modified_map={}, rss_last_run_timestamp=T_RSS,
    )

    assert len(result) == 1
    assert len(calls) == 1
    # The RSS baseline (T_RSS), not the combined one, is what gates the item.
    assert calls[0]["last_run_timestamp"] == T_RSS
    # The Step-4 XML sub-check still uses the sitemap timestamp, untouched.
    assert calls[0]["sitemap_last_run_timestamp"] == T_SITE


def test_f1_item_between_rss_and_combined_is_included(make_config, monkeypatch):
    """An item newer than the RSS baseline but OLDER than the combined baseline
    is INCLUDED -- the real proof that the rss baseline (not combined) gates RSS.

    Uses the real should_process_url_with_resume / should_process_rss_url chain.
    """
    make_config(
        RSS_FEEDS=["https://example.com/rss"],
        CHECK_LAST_MODIFIED=2,  # partial: no XML lastmod -> proceeds on date alone
        RSS_DATE_THRESHOLD=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    _patch_timestamps(monkeypatch, {"sitemap": None})
    # Published 2025-09-01: after T_RSS (2025-06-01), before T_COMB (2026-06-09).
    pub = datetime(2025, 9, 1, tzinfo=timezone.utc)
    _patch_feeds(monkeypatch, {
        "https://example.com/rss": [_item("https://example.com/mid", pub)],
    })

    # With the RSS baseline the item passes...
    included = rss_feeds.process_rss_feeds({}, rss_last_run_timestamp=T_RSS)
    assert [u["url"] for u in included] == ["https://example.com/mid"]

    # ...but had the (buggy) combined baseline been used, it would be dropped.
    excluded = rss_feeds.process_rss_feeds({}, rss_last_run_timestamp=T_COMB)
    assert excluded == []


def test_f1_first_run_none_baseline_passes_post_threshold_items(make_config, monkeypatch):
    """First RSS run: get_last_run_timestamp('rss') is None -> all items on/after
    the RSS_DATE_THRESHOLD pass (Step 2 gate), none dropped by the timestamp gate."""
    make_config(
        RSS_FEEDS=["https://example.com/rss"],
        CHECK_LAST_MODIFIED=2,
        RSS_DATE_THRESHOLD=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    _patch_timestamps(monkeypatch, {})  # all types -> None (first run)
    items = [
        _item("https://example.com/old", datetime(2024, 1, 1, tzinfo=timezone.utc)),
        _item("https://example.com/new1", datetime(2025, 3, 1, tzinfo=timezone.utc)),
        _item("https://example.com/new2", datetime(2026, 2, 1, tzinfo=timezone.utc)),
    ]
    _patch_feeds(monkeypatch, {"https://example.com/rss": items})

    result = rss_feeds.process_rss_feeds({}, rss_last_run_timestamp=None)
    urls = {u["url"] for u in result}
    # Pre-threshold item dropped; both post-threshold items pass.
    assert urls == {"https://example.com/new1", "https://example.com/new2"}


def test_f1_caller_passes_rss_timestamp_not_combined(make_config, monkeypatch):
    """End-to-end at the caller: resolve_urls in combined mode sources the rss
    timestamp for the RSS branch even though last_ts is the combined one."""
    from gpt_scraper_v3 import cli
    import argparse

    make_config(
        RSS_FEEDS=["https://example.com/rss"],
        CHECK_LAST_MODIFIED=1,
        RSS_DATE_THRESHOLD=None,
        SITEMAP_URL="",  # force XML-only sitemap path, isolating the RSS branch
    )
    # Distinct sentinel per timestamp type, consulted by BOTH cli and rss_feeds.
    def _ts(t="combined"):
        return {"rss": T_RSS, "sitemap": T_SITE, "combined": T_COMB}.get(t)

    monkeypatch.setattr(cli, "get_last_run_timestamp", _ts)
    monkeypatch.setattr(xml_sitemap, "get_last_run_timestamp", _ts)
    # No XML sitemap fetch / HTML extraction in this isolated path.
    monkeypatch.setattr(cli, "fetch_xml_sitemap", lambda: {})

    _patch_feeds(monkeypatch, {
        "https://example.com/rss": [_item("https://example.com/x", T_COMB)],
    })
    calls = _spy_should_process(monkeypatch, return_value=True)

    args = argparse.Namespace(resume=False, legacy_html_parsing=False)
    cli.resolve_urls(
        cfg=cli.get_config(), args=args, lm_map={}, local_cache=None,
        vs_cache=None, enable_resume=False,
        last_ts=T_COMB,  # combined mode passes the COMBINED ts in
        rss_only=False, sitemap_only=False, xml_only=False,
        remove_sel="", vs_id="",
    )

    # Exactly the RSS item went through the gate, with the RSS baseline.
    rss_calls = [c for c in calls if c["url"] == "https://example.com/x"]
    assert len(rss_calls) == 1
    assert rss_calls[0]["last_run_timestamp"] == T_RSS
    assert rss_calls[0]["last_run_timestamp"] != T_COMB


def test_f1_rss_only_mode_idempotent(make_config, monkeypatch):
    """--rss-only: last_ts already equals the rss timestamp; re-reading it in the
    RSS branch yields the same value -> identical result (no double-apply)."""
    from gpt_scraper_v3 import cli
    import argparse

    make_config(
        RSS_FEEDS=["https://example.com/rss"],
        CHECK_LAST_MODIFIED=1,
        RSS_DATE_THRESHOLD=None,
    )

    def _ts(t="combined"):
        return {"rss": T_RSS, "sitemap": T_SITE, "combined": T_COMB}.get(t)

    monkeypatch.setattr(cli, "get_last_run_timestamp", _ts)
    monkeypatch.setattr(xml_sitemap, "get_last_run_timestamp", _ts)
    monkeypatch.setattr(cli, "fetch_xml_sitemap", lambda: {})
    _patch_feeds(monkeypatch, {
        "https://example.com/rss": [_item("https://example.com/x", T_COMB)],
    })
    calls = _spy_should_process(monkeypatch, return_value=True)

    args = argparse.Namespace(resume=False, legacy_html_parsing=False)
    cli.resolve_urls(
        cfg=cli.get_config(), args=args, lm_map={}, local_cache=None,
        vs_cache=None, enable_resume=False,
        last_ts=T_RSS,  # rss-only: last_ts IS already the rss ts
        rss_only=True, sitemap_only=False, xml_only=False,
        remove_sel="", vs_id="",
    )

    rss_calls = [c for c in calls if c["url"] == "https://example.com/x"]
    assert len(rss_calls) == 1
    assert rss_calls[0]["last_run_timestamp"] == T_RSS


# =============================================================================
# F4 -- cross-feed item dedup
# =============================================================================

def test_f4_same_item_two_feeds_collapses_to_one(make_config, monkeypatch, caplog):
    """Two configured feeds return the SAME item -> exactly one result, and the
    cross-feed collapse is logged."""
    make_config(
        RSS_FEEDS=["https://example.com/rss", "https://example.com/rss/?21"],
        CHECK_LAST_MODIFIED=0,  # gate always passes; isolates dedup behavior
    )
    _patch_timestamps(monkeypatch, {})
    same = _item("https://example.com/article", T_COMB)
    _patch_feeds(monkeypatch, {
        "https://example.com/rss": [dict(same)],
        "https://example.com/rss/?21": [dict(same)],
    })
    _spy_should_process(monkeypatch, return_value=True)

    with caplog.at_level("INFO"):
        result = rss_feeds.process_rss_feeds({}, rss_last_run_timestamp=None)

    assert len(result) == 1
    assert result[0]["url"] == "https://example.com/article"
    assert any("Collapsed 1 cross-feed duplicate" in r.message for r in caplog.records)


def test_f4_keeps_first_occurrence(make_config, monkeypatch):
    """First occurrence wins: feed order determines which url_info is retained."""
    make_config(
        RSS_FEEDS=["https://example.com/feedA", "https://example.com/feedB"],
        CHECK_LAST_MODIFIED=0,
    )
    _patch_timestamps(monkeypatch, {})
    first = _item("https://example.com/dup", datetime(2026, 1, 1, tzinfo=timezone.utc))
    first["title"] = "FROM_A"
    second = _item("https://example.com/dup", datetime(2026, 5, 1, tzinfo=timezone.utc))
    second["title"] = "FROM_B"
    _patch_feeds(monkeypatch, {
        "https://example.com/feedA": [first],
        "https://example.com/feedB": [second],
    })
    _spy_should_process(monkeypatch, return_value=True)

    result = rss_feeds.process_rss_feeds({}, rss_last_run_timestamp=None)
    assert len(result) == 1
    assert result[0]["title"] == "FROM_A"


def test_f4_query_order_variant_collapses(make_config, monkeypatch):
    """URLs differing only in query-parameter ORDER normalize equal -> collapse."""
    make_config(
        RSS_FEEDS=["https://example.com/feedA", "https://example.com/feedB"],
        CHECK_LAST_MODIFIED=0,
    )
    _patch_timestamps(monkeypatch, {})
    _patch_feeds(monkeypatch, {
        "https://example.com/feedA": [
            _item("https://example.com/p?a=1&b=2", T_COMB)],
        "https://example.com/feedB": [
            _item("https://example.com/p?b=2&a=1", T_COMB)],
    })
    _spy_should_process(monkeypatch, return_value=True)

    result = rss_feeds.process_rss_feeds({}, rss_last_run_timestamp=None)
    assert len(result) == 1


def test_f4_distinct_urls_both_kept(make_config, monkeypatch):
    """Genuinely distinct article URLs across feeds are both retained."""
    make_config(
        RSS_FEEDS=["https://example.com/feedA", "https://example.com/feedB"],
        CHECK_LAST_MODIFIED=0,
    )
    _patch_timestamps(monkeypatch, {})
    _patch_feeds(monkeypatch, {
        "https://example.com/feedA": [_item("https://example.com/one", T_COMB)],
        "https://example.com/feedB": [_item("https://example.com/two", T_COMB)],
    })
    _spy_should_process(monkeypatch, return_value=True)

    result = rss_feeds.process_rss_feeds({}, rss_last_run_timestamp=None)
    assert {u["url"] for u in result} == {
        "https://example.com/one", "https://example.com/two"}
