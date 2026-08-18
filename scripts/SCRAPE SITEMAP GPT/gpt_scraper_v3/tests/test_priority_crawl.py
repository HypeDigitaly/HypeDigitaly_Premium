"""Tests for priority_crawl (website.priority_urls feature).

Covers: link extraction filters (domain, blacklist, file, include/exclude,
remove_selectors), URL-form contract (fragment-stripped output, normalized
gate keying), BFS depth/max_urls fetch caps, seed filtering + normalization,
config parsing (section absent -> disabled, type edge cases), and the
always-process gate branch in should_process_url_with_resume.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import patch

from gpt_scraper_v3.priority_crawl import (
    _compile_patterns,
    _split_selectors,
    collect_priority_urls,
    extract_priority_links,
)
from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.url_processing import should_process_url_with_resume
from gpt_scraper_v3.utilities import normalize_url_query_params


BASE = "https://example.com"


def _page(*bodies: str) -> str:
    return (
        "<html><head></head><body>"
        "<nav><a href='/navigace'>Nav</a></nav>"
        "<header><a href='/header-link'>H</a></header>"
        + "".join(bodies)
        + "<footer><a href='/paticka'>F</a></footer></body></html>"
    )


def _prio_cfg(make_config, **overrides: Any):
    defaults: Dict[str, Any] = dict(
        PRIORITY_URLS_ENABLED=True,
        PRIORITY_SEED_URLS=[f"{BASE}/kontakty"],
        PRIORITY_FOLLOW_DEPTH=1,
        PRIORITY_MAX_URLS=100,
        PRIORITY_REMOVE_SELECTORS="nav, header, footer",
    )
    defaults.update(overrides)
    return make_config(**defaults)


def _extract(html: str) -> List[tuple]:
    """Call extract_priority_links with args derived from the active config."""
    cfg = get_config()
    return extract_priority_links(
        html,
        _compile_patterns(cfg.PRIORITY_INCLUDE_PATTERNS),
        _compile_patterns(cfg.PRIORITY_EXCLUDE_PATTERNS),
        _split_selectors(cfg.PRIORITY_REMOVE_SELECTORS or cfg.JINA_REMOVE_SELECTORS),
    )


# -- extract_priority_links ----------------------------------------------------

def test_extract_strips_nav_and_keeps_content_links(make_config):
    _prio_cfg(make_config)
    html = _page("<a href='/mgr-jan-novak'>Mgr. Jan Novák</a>")
    urls = [u for u, _ in _extract(html)]
    assert urls == [f"{BASE}/mgr-jan-novak"]


def test_extract_filters_external_file_mailto_and_fragments(make_config):
    _prio_cfg(make_config)
    html = _page(
        "<a href='https://other.example.org/x'>ext</a>"
        "<a href='/doc.pdf'>pdf</a>"
        "<a href='mailto:a@b.cz'>mail</a>"
        "<a href='tel:+420123'>tel</a>"
        "<a href='#kotva'>anchor</a>"
        "<a href='/osoba#sekce'>person</a>"
    )
    urls = [u for u, _ in _extract(html)]
    assert urls == [f"{BASE}/osoba"]  # fragment stripped, rest filtered


def test_extract_applies_blacklist_and_exclude_patterns(make_config):
    _prio_cfg(
        make_config,
        BLACKLISTED_URLS=[f"{BASE}/tajne"],
        BLACKLISTED_RELATIVE_PATHS=["/Account"],
        PRIORITY_EXCLUDE_PATTERNS=[r"^/rss"],
    )
    html = _page(
        "<a href='/tajne'>b1</a>"
        "<a href='/Account/Login'>b2</a>"
        "<a href='/rss/feed'>b3</a>"
        "<a href='/odbor-dopravy'>ok</a>"
    )
    urls = [u for u, _ in _extract(html)]
    assert urls == [f"{BASE}/odbor-dopravy"]


def test_extract_include_patterns_allowlist(make_config):
    _prio_cfg(make_config, PRIORITY_INCLUDE_PATTERNS=[r"^/oddeleni-"])
    html = _page("<a href='/oddeleni-x'>x</a><a href='/jina-stranka'>y</a>")
    urls = [u for u, _ in _extract(html)]
    assert urls == [f"{BASE}/oddeleni-x"]


def test_extract_url_form_contract(make_config):
    """Output is fragment-stripped but NOT query-normalized (extractor parity);
    the gate keys on the normalized form of the same string."""
    _prio_cfg(make_config)
    html = _page("<a href='/osoba?b=2&a=1#x'>p</a>")
    (url, _), = _extract(html)
    assert url == f"{BASE}/osoba?b=2&a=1"  # fragment gone, query order preserved
    assert normalize_url_query_params(url) == f"{BASE}/osoba?a=1&b=2"


def test_extract_skips_homepage_links(make_config):
    cfg = _prio_cfg(make_config)
    cfg.CANONICAL_BASE_URLS = {BASE, f"{BASE}/"}
    html = _page(f"<a href='{BASE}'>logo</a><a href='/'>home</a><a href='/osoba'>p</a>")
    urls = [u for u, _ in _extract(html)]
    assert urls == [f"{BASE}/osoba"]


def test_extract_dedupes_query_permutations_within_page(make_config):
    _prio_cfg(make_config)
    html = _page("<a href='/osoba?b=2&a=1'>p1</a><a href='/osoba?a=1&b=2'>p2</a>")
    assert len(_extract(html)) == 1


# -- collect_priority_urls (BFS) -----------------------------------------------

def _run_collect(pages: Dict[str, str], fetcher=None) -> tuple:
    """Run collect_priority_urls with a fake fetcher; returns (infos, fetched)."""
    fetched: List[str] = []

    def fake_fetch(url: str):
        fetched.append(url)
        return pages.get(url, "")

    with patch("gpt_scraper_v3.content_fetching.get_raw_html_content", fetcher or fake_fetch):
        infos = collect_priority_urls()
    return infos, fetched


def test_collect_disabled_flag_returns_empty_even_with_seeds(make_config):
    _prio_cfg(make_config, PRIORITY_URLS_ENABLED=False)
    infos, fetched = _run_collect({})
    assert infos == [] and fetched == []


def test_collect_depth1_discovers_persons_but_does_not_fetch_them(make_config):
    _prio_cfg(make_config)
    pages = {f"{BASE}/kontakty": _page("<a href='/mgr-jan-novak'>Mgr. Jan Novák</a>")}
    infos, fetched = _run_collect(pages)
    urls = {i["url"] for i in infos}
    assert urls == {f"{BASE}/kontakty", f"{BASE}/mgr-jan-novak"}
    assert fetched == [f"{BASE}/kontakty"]  # depth-1 page NOT fetched (depth cap)
    by_url = {i["url"]: i for i in infos}
    assert by_url[f"{BASE}/mgr-jan-novak"]["title"] == "Mgr. Jan Novák"
    assert by_url[f"{BASE}/mgr-jan-novak"]["last_modified"] is None


def test_collect_depth0_seeds_only_no_fetches(make_config):
    _prio_cfg(make_config, PRIORITY_FOLLOW_DEPTH=0)
    infos, fetched = _run_collect({f"{BASE}/kontakty": _page("<a href='/x'>x</a>")})
    assert [i["url"] for i in infos] == [f"{BASE}/kontakty"]
    assert fetched == []


def test_collect_depth2_follows_oddeleni_to_person(make_config):
    _prio_cfg(make_config, PRIORITY_FOLLOW_DEPTH=2)
    pages = {
        f"{BASE}/kontakty": _page("<a href='/oddeleni-x'>Oddělení X</a>"),
        f"{BASE}/oddeleni-x": _page("<a href='/katerina-kozarova'>Kateřina</a>"),
    }
    infos, fetched = _run_collect(pages)
    urls = {i["url"] for i in infos}
    assert f"{BASE}/katerina-kozarova" in urls
    assert set(fetched) == {f"{BASE}/kontakty", f"{BASE}/oddeleni-x"}


def test_collect_seed_normalization_and_dedup(make_config):
    """Seeds are fragment-stripped; duplicate seeds (by normalized form) collapse."""
    _prio_cfg(make_config, PRIORITY_SEED_URLS=[
        f"{BASE}/kontakty?b=2&a=1#x", f"{BASE}/kontakty?a=1&b=2"])
    infos, fetched = _run_collect({})
    assert [i["url"] for i in infos] == [f"{BASE}/kontakty?b=2&a=1"]


def test_collect_seed_filtered_by_blacklist_and_file(make_config):
    _prio_cfg(make_config, PRIORITY_SEED_URLS=[
        f"{BASE}/kontakty", f"{BASE}/doc.pdf", f"{BASE}/Account/Login"],
        BLACKLISTED_RELATIVE_PATHS=["/Account"])
    infos, _ = _run_collect({})
    assert [i["url"] for i in infos] == [f"{BASE}/kontakty"]


def test_collect_max_urls_caps_set_and_fetches(make_config):
    _prio_cfg(make_config, PRIORITY_FOLLOW_DEPTH=3, PRIORITY_MAX_URLS=3)
    pages = {
        f"{BASE}/kontakty": _page(
            "<a href='/a'>a</a><a href='/b'>b</a><a href='/c'>c</a><a href='/d'>d</a>"),
        f"{BASE}/a": _page("<a href='/e'>e</a>"),
    }
    infos, fetched = _run_collect(pages)
    assert len(infos) == 3  # seed + 2 discovered, /c /d dropped
    assert fetched == [f"{BASE}/kontakty"]  # cap hit -> no further fetches
    assert f"{BASE}/e" not in {i["url"] for i in infos}


def test_collect_max_fetch_pages_budget(make_config):
    _prio_cfg(make_config, PRIORITY_FOLLOW_DEPTH=3, PRIORITY_MAX_FETCH_PAGES=1,
              PRIORITY_SEED_URLS=[f"{BASE}/kontakty", f"{BASE}/rada"])
    pages = {
        f"{BASE}/kontakty": _page("<a href='/osoba-a'>a</a>"),
        f"{BASE}/rada": _page("<a href='/osoba-b'>b</a>"),
    }
    infos, fetched = _run_collect(pages)
    assert fetched == [f"{BASE}/kontakty"]  # budget stops after 1 fetch
    urls = {i["url"] for i in infos}
    # links from the fetched page kept; unfetched seed still in the set
    assert urls == {f"{BASE}/kontakty", f"{BASE}/rada", f"{BASE}/osoba-a"}


def test_collect_max_urls_smaller_than_seed_list(make_config):
    _prio_cfg(make_config, PRIORITY_MAX_URLS=1,
              PRIORITY_SEED_URLS=[f"{BASE}/kontakty", f"{BASE}/rada"])
    infos, fetched = _run_collect({})
    assert len(infos) == 1


def test_collect_empty_html_skips_page_without_raising(make_config):
    _prio_cfg(make_config, PRIORITY_SEED_URLS=[f"{BASE}/kontakty", f"{BASE}/rada"])
    pages = {f"{BASE}/rada": _page("<a href='/clen'>Člen</a>")}  # kontakty -> "" (no HTML)
    infos, _ = _run_collect(pages)
    urls = {i["url"] for i in infos}
    assert urls == {f"{BASE}/kontakty", f"{BASE}/rada", f"{BASE}/clen"}


def test_collect_raising_fetcher_skips_page_without_raising(make_config):
    _prio_cfg(make_config, PRIORITY_SEED_URLS=[f"{BASE}/kontakty", f"{BASE}/rada"])

    def raising_fetch(url: str):
        if url == f"{BASE}/kontakty":
            raise RuntimeError("boom")
        return _page("<a href='/clen'>Člen</a>")

    infos, _ = _run_collect({}, fetcher=raising_fetch)
    urls = {i["url"] for i in infos}
    assert urls == {f"{BASE}/kontakty", f"{BASE}/rada", f"{BASE}/clen"}


# -- gate branch ---------------------------------------------------------------

def test_gate_priority_url_always_processed_even_under_resume(make_config):
    cfg = _prio_cfg(make_config)
    url = f"{BASE}/mgr-jan-novak"
    cfg.PRIORITY_URL_SET.add(normalize_url_query_params(url))
    # Even with resume enabled + file present locally + no lastmod, the
    # priority branch wins (Step 0b sits above the resume check).
    assert should_process_url_with_resume(
        url, None, datetime(2026, 1, 1, tzinfo=timezone.utc),
        local_cache={normalize_url_query_params(url)}, enable_resume=True,
    ) is True


def test_gate_non_priority_url_unaffected(make_config):
    cfg = _prio_cfg(make_config)
    cfg.PRIORITY_URL_SET.add(normalize_url_query_params(f"{BASE}/mgr-jan-novak"))
    # A different URL with lastmod older than last run is still skipped.
    assert should_process_url_with_resume(
        f"{BASE}/stara-stranka", datetime(2025, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    ) is False


# -- config parsing ------------------------------------------------------------

def test_config_section_absent_disables_feature(loaded_config):
    cfg = loaded_config()
    assert cfg.PRIORITY_URLS_ENABLED is False
    assert cfg.PRIORITY_SEED_URLS == []
    assert cfg.PRIORITY_URL_SET == set()


def test_config_section_parsed(loaded_config):
    cfg = loaded_config({
        "website": {
            "priority_urls": {
                "enabled": 1,
                "seed_urls": ["https://example.com/kontakty", "  ", 42],
                "follow_depth": 2,
                "max_urls": 50,
                "include_patterns": ["^/odbor-"],
                "exclude_patterns": ["^/rss"],
                "remove_selectors": "nav",
            }
        }
    })
    assert cfg.PRIORITY_URLS_ENABLED is True
    assert cfg.PRIORITY_SEED_URLS == ["https://example.com/kontakty"]
    assert cfg.PRIORITY_FOLLOW_DEPTH == 2
    assert cfg.PRIORITY_MAX_URLS == 50
    assert cfg.PRIORITY_INCLUDE_PATTERNS == ["^/odbor-"]
    assert cfg.PRIORITY_EXCLUDE_PATTERNS == ["^/rss"]
    assert cfg.PRIORITY_REMOVE_SELECTORS == "nav"


def test_config_enabled_without_seeds_is_disabled(loaded_config):
    cfg = loaded_config({"website": {"priority_urls": {"enabled": 1, "seed_urls": []}}})
    assert cfg.PRIORITY_URLS_ENABLED is False


def test_config_type_edge_cases_do_not_crash_or_misparse(loaded_config):
    cfg = loaded_config({
        "website": {
            "priority_urls": {
                "enabled": "yes",           # bad -> disabled, no crash
                "seed_urls": "https://example.com/a",  # string, not list -> []
                "follow_depth": True,       # bool is not a valid depth -> 1
                "max_urls": True,           # bool is not a valid cap -> 500
                "include_patterns": None,   # -> []
                "exclude_patterns": "x",    # string, not list -> []
                "remove_selectors": 123,    # non-str -> ""
            }
        }
    })
    assert cfg.PRIORITY_URLS_ENABLED is False
    assert cfg.PRIORITY_SEED_URLS == []
    assert cfg.PRIORITY_FOLLOW_DEPTH == 1
    assert cfg.PRIORITY_MAX_URLS == 500
    assert cfg.PRIORITY_INCLUDE_PATTERNS == []
    assert cfg.PRIORITY_EXCLUDE_PATTERNS == []
    assert cfg.PRIORITY_REMOVE_SELECTORS == ""


def test_config_section_non_dict_disables_feature(loaded_config):
    cfg = loaded_config({"website": {"priority_urls": []}})
    assert cfg.PRIORITY_URLS_ENABLED is False
