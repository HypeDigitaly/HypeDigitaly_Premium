"""Priority URL collection for GPT Scraper V3.

Deterministic BFS crawl of configured "priority" seed pages (contact hubs,
department/board pages) that must be re-scraped on EVERY run, plus discovery of
links found in their content. Person profile pages are root-level slugs absent
from both the XML and HTML sitemaps and not pattern-matchable by URL, so they
are harvested from anchors on the seed pages.

Configured via the optional ``website.priority_urls`` section:

    "priority_urls": {
      "enabled": 1,
      "seed_urls": ["https://example.cz/kontakty", ...],
      "follow_depth": 2,
      "include_patterns": [],
      "exclude_patterns": [],
      "max_urls": 800,
      "remove_selectors": "header, footer, nav"
    }

The collected URL-info dicts are appended to the run batch by
``cli.resolve_urls`` and force-processed via ``cfg.PRIORITY_URL_SET``
membership in ``url_processing.should_process_url_with_resume``.

URL string convention: emitted ``info["url"]`` values are fragment-stripped but
NOT query-normalized — matching the HTML-sitemap extractor's output — so
``_deduplicate_urls`` and ``VisitedSet`` (both exact-string) collapse overlaps
with sitemap entries. ``PRIORITY_URL_SET`` holds the query-normalized form,
matching what the eligibility gate computes.
"""
from __future__ import annotations

import logging
import re
from collections import deque
from typing import Any, Dict, List, Pattern, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.utilities import (
    is_file_url,
    is_url_blacklisted_by_path,
    normalize_url_query_params,
    strip_url_fragment,
)

logger = logging.getLogger(__name__)


def _compile_patterns(patterns: List[str]) -> List[Pattern[str]]:
    """Compile regex patterns, warning on (and skipping) invalid ones."""
    compiled: List[Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as e:
            logger.warning("PRIORITY: invalid regex pattern %r skipped (%s)", pattern, e)
    return compiled


def _split_selectors(selectors: Any) -> List[str]:
    """Split a comma-separated CSS selector string defensively (never raises)."""
    if not isinstance(selectors, str):
        return []
    return [s.strip() for s in selectors.split(",") if s.strip()]


def _strip_selectors(soup: BeautifulSoup, selector_list: List[str]) -> None:
    """Remove nav/header/footer elements client-side before link extraction.

    The playwright raw-HTML fetch deliberately ignores remove_selectors
    (``fetch_playwright_html`` returns unfiltered ``page.content()``), so the
    stripping MUST happen here, not in the fetch layer.
    """
    for sel in selector_list:
        try:
            for el in soup.select(sel):
                el.decompose()
        except Exception as e:  # soupsieve SelectorSyntaxError and friends
            logger.warning("PRIORITY: invalid remove selector %r skipped (%s)", sel, e)


def _passes_filters(
    absolute_url: str, path: str,
    include_res: List[Pattern[str]], exclude_res: List[Pattern[str]],
    *, apply_include: bool = True,
) -> bool:
    """Shared URL filters: blacklists, file URLs, include/exclude patterns.

    ``apply_include=False`` for seeds: they are explicitly configured, so the
    discovery allowlist does not apply to them (blacklist/exclude/file still do).
    """
    cfg = get_config()
    if absolute_url in cfg.BLACKLISTED_URLS or is_url_blacklisted_by_path(
        absolute_url, cfg.BLACKLISTED_RELATIVE_PATHS
    ):
        return False
    if is_file_url(absolute_url):
        return False
    if apply_include and include_res and not any(p.search(path) for p in include_res):
        return False
    if any(p.search(path) for p in exclude_res):
        return False
    return True


def extract_priority_links(
    html_content: str,
    include_res: List[Pattern[str]],
    exclude_res: List[Pattern[str]],
    remove_selector_list: List[str],
) -> List[Tuple[str, str]]:
    """Extract candidate (url, anchor_text) pairs from page HTML.

    Mirrors the HTML-sitemap anchor pipeline (sitemap_parsing.py): skip
    fragment-only hrefs, ``strip_url_fragment(urljoin(BASE_URL, href))``,
    same-domain netloc check, blacklist filters — plus priority-specific
    filters (file URLs, include/exclude regex on the URL path). Emitted URLs
    are fragment-stripped but NOT query-normalized (extractor parity); per-page
    dedup keys on the normalized form.
    """
    cfg = get_config()
    try:
        soup = BeautifulSoup(html_content, "html.parser")
    except Exception as e:
        logger.warning("PRIORITY: HTML parse failed (%s)", e)
        return []

    _strip_selectors(soup, remove_selector_list)

    links: List[Tuple[str, str]] = []
    seen_here: set = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if not href or href.startswith("#"):
            continue
        if href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue

        absolute_url = strip_url_fragment(urljoin(cfg.BASE_URL, href))
        parsed = urlparse(absolute_url)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc not in (cfg.BASE_NETLOC, cfg.NON_WWW_BASE_NETLOC):
            continue
        # Skip the homepage/base URL (logo links etc.) - discovered links only;
        # explicit seeds may still target it deliberately.
        if absolute_url in cfg.CANONICAL_BASE_URLS or absolute_url.rstrip("/") in cfg.CANONICAL_BASE_URLS:
            continue
        if not _passes_filters(absolute_url, parsed.path, include_res, exclude_res):
            continue

        normalized = normalize_url_query_params(absolute_url)
        if normalized in seen_here:
            continue
        seen_here.add(normalized)
        text = anchor.get_text(strip=True) or anchor.get("title", "").strip()
        links.append((absolute_url, text))
    return links


def collect_priority_urls() -> List[Dict[str, Any]]:
    """BFS-collect the priority URL set: seeds + content links up to follow_depth.

    Fetches raw HTML only for pages at depth < ``PRIORITY_FOLLOW_DEPTH``. Both
    the URL set AND the fetch queue are capped by ``PRIORITY_MAX_URLS`` — seeds
    included — and dropped/unfetched URLs are counted and logged (no silent
    truncation). Never raises: fetch or parse failures are logged and that page
    is skipped.

    Returns URL-info dicts shaped like the sitemap extractors' output:
    ``{"url", "title", "path", "last_modified": None}`` with ``url``
    fragment-stripped (not query-normalized; see module docstring).
    """
    cfg = get_config()
    if not cfg.PRIORITY_URLS_ENABLED or not cfg.PRIORITY_SEED_URLS:
        return []

    # Lazy import: content_fetching pulls in the provider stack; keep module
    # import light and cycle-safe (also lets tests patch the symbol easily).
    from gpt_scraper_v3.content_fetching import get_raw_html_content

    include_res = _compile_patterns(cfg.PRIORITY_INCLUDE_PATTERNS)
    exclude_res = _compile_patterns(cfg.PRIORITY_EXCLUDE_PATTERNS)
    remove_selector_list = _split_selectors(
        cfg.PRIORITY_REMOVE_SELECTORS or cfg.JINA_REMOVE_SELECTORS)

    max_urls = cfg.PRIORITY_MAX_URLS
    max_fetch = cfg.PRIORITY_MAX_FETCH_PAGES  # 0 = unlimited
    depth_cap = cfg.PRIORITY_FOLLOW_DEPTH
    # Keyed by normalized URL; info dicts carry the fragment-stripped form.
    collected: Dict[str, Dict[str, Any]] = {}
    queue: deque = deque()
    dropped = 0

    for seed in cfg.PRIORITY_SEED_URLS:
        stripped = strip_url_fragment(seed.strip())
        normalized = normalize_url_query_params(stripped)
        if not normalized or normalized in collected:
            continue
        if len(collected) >= max_urls:
            dropped += 1
            continue
        parsed = urlparse(stripped)
        if not _passes_filters(stripped, parsed.path, include_res, exclude_res,
                               apply_include=False):
            logger.warning("PRIORITY: seed rejected by blacklist/file/exclude filters: %s", stripped)
            continue
        if normalized in cfg.CANONICAL_BASE_URLS and not cfg.PROCESS_BASE_URL_ONCE:
            logger.warning(
                "PRIORITY: seed %s is the base URL itself with IncludeBaseURL=0 - it WILL "
                "be scraped via the priority batch (the base-URL gate does not apply here).",
                normalized)
        slug = parsed.path.rstrip("/").split("/")[-1] or "Homepage"
        collected[normalized] = {
            "url": stripped, "title": slug,
            "path": f"Priority > {slug}", "last_modified": None,
        }
        queue.append((stripped, 0))
    seed_count = len(collected)
    if dropped:
        logger.warning(
            "PRIORITY: max_urls=%d smaller than seed list - %d seeds DROPPED", max_urls, dropped)

    logger.info("PRIORITY: starting collection from %d seeds (depth=%d, max_urls=%d)",
                seed_count, depth_cap, max_urls)

    fetched = 0
    while queue:
        if len(collected) >= max_urls:
            logger.warning(
                "PRIORITY: max_urls=%d reached with %d queued pages left unfetched "
                "(raise website.priority_urls.max_urls for deeper coverage)",
                max_urls, len(queue))
            break
        # Fetch budget: without it, a URL count sitting just below max_urls
        # makes the BFS grind through the ENTIRE remaining queue (hundreds of
        # profile pages adding 0-2 links each) at politeness-limited speed.
        if max_fetch and fetched >= max_fetch:
            logger.warning(
                "PRIORITY: max_fetch_pages=%d reached with %d queued pages left "
                "unfetched - links already collected are kept "
                "(raise website.priority_urls.max_fetch_pages for deeper coverage)",
                max_fetch, len(queue))
            break
        url, depth = queue.popleft()
        if depth >= depth_cap:
            continue
        try:
            html = get_raw_html_content(url)
        except Exception as e:  # defensive: providers already swallow most errors
            logger.warning("PRIORITY: fetch failed for %s (%s)", url, e)
            continue
        fetched += 1
        if not html:
            logger.warning("PRIORITY: no HTML for %s - links from this page skipped", url)
            continue

        try:
            page_links = extract_priority_links(
                html, include_res, exclude_res, remove_selector_list)
        except Exception as e:  # never let one bad page kill the run
            logger.warning("PRIORITY: link extraction failed for %s (%s)", url, e)
            continue

        for link_url, link_text in page_links:
            normalized = normalize_url_query_params(link_url)
            if normalized in collected:
                continue
            if len(collected) >= max_urls:
                dropped += 1
                continue
            title = link_text or (urlparse(link_url).path.rstrip("/").split("/")[-1] or "Homepage")
            collected[normalized] = {
                "url": link_url, "title": title,
                "path": f"Priority > {title}", "last_modified": None,
            }
            queue.append((link_url, depth + 1))

    if dropped:
        logger.warning(
            "PRIORITY: max_urls=%d reached - %d discovered URLs were DROPPED "
            "(raise website.priority_urls.max_urls to include them)", max_urls, dropped)
    logger.info("PRIORITY: collected %d URLs (seeds=%d, discovered=%d, fetched_pages=%d, dropped=%d)",
                len(collected), seed_count, len(collected) - seed_count, fetched, dropped)
    return list(collected.values())
