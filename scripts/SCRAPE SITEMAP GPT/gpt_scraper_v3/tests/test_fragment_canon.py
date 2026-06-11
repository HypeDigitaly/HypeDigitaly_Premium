"""Tests for F2/F3 fragment canonicalization.

Covers:
  * ``strip_url_fragment`` truth table (including the ``/a#`` -> ``/a`` edge case
    that a naive ``if not parsed.fragment`` early-return would get wrong).
  * The same-page-anchor guard: a bare ``#frag`` href is skipped entirely.
  * ``extract_links_from_html_sitemap`` collapses the three ``/o-webu#*``
    fragment variants to a single canonical URL and populates ``html_seen_sink``
    with every discovered URL — including ones the gate skips.
"""
from __future__ import annotations

import pytest

from gpt_scraper_v3 import sitemap_parsing
from gpt_scraper_v3.utilities import strip_url_fragment


# -- strip_url_fragment table -------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://e.com/o-webu#zou", "https://e.com/o-webu"),
        ("https://e.com/a#", "https://e.com/a"),
        ("https://e.com/a", "https://e.com/a"),
        ("https://e.com/p?x=1#f", "https://e.com/p?x=1"),
        ("", ""),
        # %23 is NOT a fragment delimiter -> preserved verbatim.
        ("https://e.com/a%23b", "https://e.com/a%23b"),
    ],
)
def test_strip_url_fragment_table(raw, expected):
    assert strip_url_fragment(raw) == expected


# -- extract_links_from_html_sitemap collapse + sink --------------------------

_O_WEBU_HTML = """
<html><body>
  <a href="/o-webu#zou">O webu zou</a>
  <a href="/o-webu#accessibility">O webu accessibility</a>
  <a href="/o-webu#cookies">O webu cookies</a>
  <a href="#top">Same-page anchor (must be skipped)</a>
  <a href="/rss">RSS listing</a>
</body></html>
"""


def test_html_sitemap_collapses_fragments_and_populates_sink(make_config):
    """Three #-variants of /o-webu collapse to ONE canonical URL; the sink
    captures every discovered canonical URL (incl. gate-skipped ones); the
    bare ``#top`` anchor is skipped (no page identity)."""
    make_config(
        BASE_URL="https://e.com",
        CHECK_LAST_MODIFIED=0,  # gate off -> every non-fragment link processed
    )

    sink: set = set()
    result = sitemap_parsing.extract_links_from_html_sitemap(
        _O_WEBU_HTML,
        url_last_modified_map={},
        last_run_timestamp=None,
        html_seen_sink=sink,
    )

    urls = {info["url"] for info in result}
    # /o-webu#zou, #accessibility, #cookies all collapse to one canonical URL.
    assert "https://e.com/o-webu" in urls
    assert urls == {"https://e.com/o-webu", "https://e.com/rss"}
    # No fragment survives in any extracted URL.
    assert not any("#" in u for u in urls)
    # Bare same-page anchor produced no entry.
    assert "https://e.com/" not in urls

    # Sink holds the canonical (fragment-free, normalized) discovered URLs.
    assert sink == {"https://e.com/o-webu", "https://e.com/rss"}


def test_sink_captures_gate_skipped_urls(make_config, monkeypatch):
    """A URL skipped by should_process_url_with_resume must STILL be added to
    the sink (so the HTML-seen snapshot is complete and suppression works)."""
    make_config(BASE_URL="https://e.com", CHECK_LAST_MODIFIED=1)

    # extract_links_from_html_sitemap resolves should_process_url_with_resume
    # via a late import from url_processing inside the function body, so the
    # patch must target that source module.
    import gpt_scraper_v3.url_processing as up
    monkeypatch.setattr(up, "should_process_url_with_resume", lambda *a, **k: False)

    sink: set = set()
    result = sitemap_parsing.extract_links_from_html_sitemap(
        '<a href="/o-webu#zou">x</a><a href="/rss">y</a>',
        url_last_modified_map={},
        last_run_timestamp=None,
        html_seen_sink=sink,
    )

    assert result == []  # all gate-skipped
    # ...but the sink still recorded the canonical URLs.
    assert sink == {"https://e.com/o-webu", "https://e.com/rss"}


def test_anchor_guard_skips_fragment_only_href(make_config):
    """A document whose only links are bare ``#frag`` hrefs yields nothing and
    an empty sink (no collapse to the current page)."""
    make_config(BASE_URL="https://e.com", CHECK_LAST_MODIFIED=0)
    sink: set = set()
    result = sitemap_parsing.extract_links_from_html_sitemap(
        '<a href="#zou">a</a><a href="#cookies">b</a>',
        url_last_modified_map={},
        last_run_timestamp=None,
        html_seen_sink=sink,
    )
    assert result == []
    assert sink == set()
