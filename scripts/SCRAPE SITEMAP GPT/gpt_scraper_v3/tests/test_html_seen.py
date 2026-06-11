"""Tests for the two-file HTML-seen snapshot (F2) and the new-URL gate matrix.

Covers:
  * ``should_process_url_with_resume`` new-URL gate with the added
    ``html_seen_urls`` parameter:
      - first run: URL in neither set -> NEW -> process
      - second run: URL in html_seen_urls -> not NEW -> falls through to the
        missing-lastmod + VS-exists branch -> skipped
      - ``html_seen_urls=None`` -> legacy behaviour (XML snapshot only)
  * ``load_html_seen_urls`` / ``save_html_seen_urls`` round-trip and the
    monotonic union semantics used at the save site.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gpt_scraper_v3 import url_processing
from gpt_scraper_v3 import xml_sitemap


_LAST_RUN = datetime(2026, 6, 1, tzinfo=timezone.utc)


# -- New-URL gate matrix ------------------------------------------------------

def test_first_run_url_in_neither_set_is_new(make_config):
    """/rss in neither the XML snapshot nor html_seen -> NEW -> process."""
    make_config(BASE_URL="https://e.com", CHECK_LAST_MODIFIED=1)
    assert url_processing.should_process_url_with_resume(
        "https://e.com/rss",
        last_modified=None,
        last_run_timestamp=_LAST_RUN,
        previous_known_urls=set(),          # XML snapshot (empty)
        html_seen_urls=set(),               # html-seen (empty) -> first run
    ) is True


def test_second_run_url_in_html_seen_not_new_then_skipped(make_config):
    """/rss already in html_seen -> not NEW. With no lastmod and a vector-store
    hit, it falls to the missing-lastmod + VS-exists branch and is skipped."""
    make_config(BASE_URL="https://e.com", CHECK_LAST_MODIFIED=1)

    norm = url_processing.normalize_url_query_params("https://e.com/rss")
    # Simulate the page already living in the vector store. The cache shape
    # mirrors build_vector_store_cache: {normalized_url: {file_id, file_info}}.
    vs_cache = {norm: {"file_id": "file-1", "file_info": {"id": "file-1"}}}

    result = url_processing.should_process_url_with_resume(
        "https://e.com/rss",
        last_modified=None,
        last_run_timestamp=_LAST_RUN,
        previous_known_urls=set(),
        html_seen_urls={norm},              # seen on a previous run
        vector_store_id="vs_test",
        vector_store_cache=vs_cache,
    )
    assert result is False


def test_html_seen_none_is_legacy(make_config):
    """html_seen_urls=None -> legacy: a URL absent from the XML snapshot is
    still flagged NEW (no regression for callers that don't pass html-seen)."""
    make_config(BASE_URL="https://e.com", CHECK_LAST_MODIFIED=1)
    assert url_processing.should_process_url_with_resume(
        "https://e.com/rss",
        last_modified=None,
        last_run_timestamp=_LAST_RUN,
        previous_known_urls=set(),
        html_seen_urls=None,                # legacy
    ) is True


def test_url_in_xml_snapshot_not_new(make_config):
    """A URL present in the XML known-URLs snapshot is not NEW even with an
    empty html-seen set (the two sets are OR'd)."""
    make_config(BASE_URL="https://e.com", CHECK_LAST_MODIFIED=1)
    norm = url_processing.normalize_url_query_params("https://e.com/page")
    vs_cache = {norm: {"file_id": "file-1", "file_info": {"id": "file-1"}}}
    result = url_processing.should_process_url_with_resume(
        "https://e.com/page",
        last_modified=None,
        last_run_timestamp=_LAST_RUN,
        previous_known_urls={norm},
        html_seen_urls=set(),
        vector_store_id="vs_test",
        vector_store_cache=vs_cache,
    )
    assert result is False


# -- load/save round-trip + monotonic union -----------------------------------

def test_html_seen_save_load_round_trip(make_config, tmp_path):
    path = tmp_path / "site_html_seen_urls.json"
    make_config(BASE_URL="https://e.com", HTML_SEEN_URLS_FILE=str(path))

    urls = {"https://e.com/a", "https://e.com/b"}
    xml_sitemap.save_html_seen_urls(urls)
    assert path.exists()

    loaded = xml_sitemap.load_html_seen_urls()
    assert loaded == urls


def test_html_seen_load_missing_file_returns_empty(make_config, tmp_path):
    path = tmp_path / "does_not_exist_html_seen.json"
    make_config(BASE_URL="https://e.com", HTML_SEEN_URLS_FILE=str(path))
    assert xml_sitemap.load_html_seen_urls() == set()


def test_html_seen_monotonic_union(make_config, tmp_path):
    """The save site stores ``previous | sink`` — the set only ever grows."""
    path = tmp_path / "site_html_seen_urls.json"
    make_config(BASE_URL="https://e.com", HTML_SEEN_URLS_FILE=str(path))

    previous = {"https://e.com/a"}
    xml_sitemap.save_html_seen_urls(previous)

    sink = {"https://e.com/b"}            # this run discovered a new URL
    merged = xml_sitemap.load_html_seen_urls() | sink
    xml_sitemap.save_html_seen_urls(merged)

    assert xml_sitemap.load_html_seen_urls() == {
        "https://e.com/a", "https://e.com/b",
    }


def test_known_and_html_seen_use_separate_files(make_config, tmp_path):
    """The two snapshots are independent files and never cross-contaminate."""
    known_path = tmp_path / "site_known_urls.json"
    html_path = tmp_path / "site_html_seen_urls.json"
    make_config(
        BASE_URL="https://e.com",
        KNOWN_URLS_FILE=str(known_path),
        HTML_SEEN_URLS_FILE=str(html_path),
    )

    xml_sitemap.save_known_urls_snapshot({"https://e.com/xml-only"})
    xml_sitemap.save_html_seen_urls({"https://e.com/html-only"})

    assert xml_sitemap.load_known_urls_snapshot() == {"https://e.com/xml-only"}
    assert xml_sitemap.load_html_seen_urls() == {"https://e.com/html-only"}
