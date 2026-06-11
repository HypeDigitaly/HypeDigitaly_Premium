"""Paginated URL orchestration and single URL processing for GPT Scraper V3.

Migrated from scrape_sitemap_GPT_v2.py (lines 4447-5147) with:
  - M5: Type hints on all public function signatures
  - M3: No redundant function-local re-imports
  - Global variable access replaced with get_config()
  - requests_retry_session() replaced with get_session()
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.utilities import canonical_url, count_tokens_approximate
from gpt_scraper_v3.content_fetching import (
    get_html_content_for_pagination_via_jina,
    get_markdown_content,
)
from gpt_scraper_v3.pagination import (
    _construct_pagination_url,
    detect_pagination_in_html,
    extract_suburls_from_ai_data,
    is_url_explicitly_paginated,
    is_url_recursive_enabled,
)
from gpt_scraper_v3.chunking import chunk_content_with_metadata_budget
from gpt_scraper_v3.chunk_processing import process_and_save_chunks_with_metadata_budget
from gpt_scraper_v3.file_saving import save_markdown_to_file
from gpt_scraper_v3.llm_prompts import generate_question_section
from gpt_scraper_v3.openrouter_client import generate_page_summary_via_openrouter
from gpt_scraper_v3.url_processing import (
    find_url_last_modified,
    is_url_already_processed_locally,
    is_url_from_test_urls,
    should_process_url_with_resume,
)
from gpt_scraper_v3.vector_store import create_chunking_strategy

logger: logging.Logger = logging.getLogger(__name__)

# Navigation keywords for legacy pagination detection
_NAV_KEYWORDS = ["next", "previous", "dal\u0161\u00ed", "p\u0159edchoz\u00ed",
                 "vpred", "zpet", "n\u00e1sleduj\u00edc\u00ed"]
_PAG_PARAMS = ["page=", "stranka=", "p=", "pg=", "pagenum="]
_PAG_CSS = ["page", "pagination", "pager", "stranka", "aktivni", "gov-pagination"]

# Shared helper for the common process_single_url_normally call args
_NORMAL_KEYS = ("url", "title", "path", "url_last_modified_map", "last_run_timestamp",
                "local_files_cache", "enable_resume", "vector_store_id",
                "deduplication_enabled", "chunking_strategy", "vector_store_cache",
                "remove_selectors", "rss_metadata")


class VisitedSet:
    """Run-scoped, thread-safe exactly-once guard for URL processing (Bug 14).

    A single instance is created per run in ``cli.process_urls`` and passed by
    reference into every ``process_paginated_url`` call (serial and worker
    paths) and down the pagination/suburl recursion. It prevents a suburl that
    is reachable via two different parent pages from being fetched/processed
    twice -- and, under the worker pool, from being processed CONCURRENTLY by
    two workers.

    NOT a module-level singleton: keeping it run-scoped means repeated
    ``main()`` calls (tests / batch drivers) each start with a fresh set.

    It is purely an in-run exactly-once guard; the ``should_process_url_with_resume``
    gate and all other eligibility checks remain untouched.
    """

    __slots__ = ("_s", "_lock")

    def __init__(self) -> None:
        self._s: Set[str] = set()
        self._lock = threading.Lock()

    def mark(self, url: str) -> bool:
        """Atomically claim *url* for processing.

        Returns ``True`` if this is the first time *url* (by canonical form) is
        seen -- the caller owns it and should process it. Returns ``False`` if
        another caller already claimed it -- the caller should skip.
        """
        key = canonical_url(url)
        with self._lock:
            if key in self._s:
                return False
            self._s.add(key)
            return True


def _resolve_ai_url(base_url: str, relative_url: str) -> Optional[str]:
    """Resolve a relative URL to an absolute one, returning None on failure."""
    try:
        absolute = urljoin(base_url, relative_url)
        p = urlparse(absolute)
        if p.scheme and p.netloc:
            return absolute
        logger.warning("Invalid absolute URL from AI data: %s", absolute)
    except Exception as e:
        logger.error("Error constructing URL: base='%s' + rel='%s': %s", base_url, relative_url, e)
    return None


# ---------------------------------------------------------------------------
# extract_pagination_urls  (V2 lines 4447-4723)
# ---------------------------------------------------------------------------


def extract_pagination_urls(
    html_content: str, base_url: str, url: str = "",
    ai_pagination_data: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Extract pagination URLs from HTML via AI data (preferred) or legacy parsing."""
    logger.info("PAGINATION EXTRACTION: Processing pagination URLs")
    pagination_urls: List[Dict[str, Any]] = []

    # -- PREFERRED: AI-detected pagination data --
    if (ai_pagination_data and ai_pagination_data.get("ai_analysis")
            and ai_pagination_data.get("pagination_urls")):
        logger.info("Using AI-detected pagination data with %d URLs",
                     len(ai_pagination_data["pagination_urls"]))
        seen: Set[str] = set()
        for ai_pag in ai_pagination_data["pagination_urls"]:
            try:
                rel = ai_pag.get("relative_url", "")
                if not rel:
                    continue
                absolute_url = _resolve_ai_url(base_url, rel)
                if not absolute_url or absolute_url in seen or absolute_url == url:
                    continue
                seen.add(absolute_url)
                pagination_urls.append({
                    "url": absolute_url, "page_number": ai_pag.get("page_number"),
                    "link_text": ai_pag.get("link_text", ""),
                    "link_title": "", "link_classes": "",
                    "original_href": rel, "cleaned_href": rel, "constructed_url": rel,
                    "link_type": ai_pag.get("link_type", "unknown"),
                    "element_type": "ai_detected", "ai_source": True,
                })
                logger.info("Added AI pagination URL: %s (page: %s)", absolute_url,
                            ai_pag.get("page_number"))
            except Exception as e:
                logger.error("Error processing AI pagination URL: %s", e)
        logger.info("AI PAGINATION EXTRACTION COMPLETE: %d URLs", len(pagination_urls))
        return pagination_urls

    # -- FALLBACK: Legacy HTML parsing --
    logger.info("Using legacy HTML parsing for pagination URL extraction")
    if not html_content or not html_content.strip():
        return pagination_urls

    try:
        from bs4 import BeautifulSoup  # local import to avoid hard dependency
        soup = BeautifulSoup(html_content, "html.parser")
        seen = set()
        links_href = soup.find_all("a", href=True)
        links_no_href = [l for l in soup.find_all("a") if not l.get("href")]
        buttons = soup.find_all("button")
        all_elements = links_href + links_no_href + buttons
        logger.info("Analyzing %d elements for pagination", len(all_elements))

        for element in all_elements:
            try:
                href = element.get("href", "").strip()
                text = element.get_text(strip=True)
                title_attr = element.get("title", "").strip()
                class_names = " ".join(element.get("class", []))
                onclick = element.get("onclick", "").strip()

                if href:
                    href = href.strip("'\"").replace('\\"', "").replace("\\'", "")
                    href = href.replace("\\", "").replace('"', "").replace("'", "").strip()

                is_pag = False
                page_number: Optional[int] = None
                constructed_url: Optional[str] = None

                # Method 1: Text is a number
                if text.isdigit():
                    page_number = int(text)
                    if href:
                        is_pag, constructed_url = True, href
                    elif url:
                        constructed_url = _construct_pagination_url(url, page_number)
                        is_pag = constructed_url is not None
                # Method 2: Pagination URL parameters
                elif href and any(p in href.lower() for p in _PAG_PARAMS):
                    is_pag, constructed_url = True, href
                    m = re.search(r"(?:page|stranka|p|pg|pagenum)=(\d+)", href, re.IGNORECASE)
                    if m:
                        page_number = int(m.group(1))
                # Method 3: Next/Previous navigation
                elif any(kw in text.lower() or kw in title_attr.lower() for kw in _NAV_KEYWORDS):
                    if href:
                        is_pag, constructed_url = True, href
                    elif onclick:
                        m = re.search(r'["\']([^"\']*(?:page|stranka)[^"\']*)["\']', onclick)
                        if m:
                            is_pag, constructed_url = True, m.group(1)
                # Method 4: CSS classes suggest pagination
                elif any(c in class_names.lower() for c in _PAG_CSS):
                    if href:
                        is_pag, constructed_url = True, href
                    elif text.isdigit() and url:
                        page_number = int(text)
                        constructed_url = _construct_pagination_url(url, page_number)
                        is_pag = constructed_url is not None

                if is_pag and constructed_url and not constructed_url.isspace():
                    absolute_url = _resolve_ai_url(base_url, constructed_url)
                    if not absolute_url or absolute_url in seen or absolute_url == url:
                        continue
                    seen.add(absolute_url)
                    pagination_urls.append({
                        "url": absolute_url, "page_number": page_number,
                        "link_text": text, "link_title": title_attr,
                        "link_classes": class_names,
                        "original_href": element.get("href", ""),
                        "cleaned_href": href, "constructed_url": constructed_url,
                        "link_type": "numbered" if text.isdigit() else "navigation",
                        "element_type": element.name,
                    })
            except Exception as e:
                logger.error("Error processing pagination element: %s", e)

        # Sort: numbered pages first, then navigation pages
        numbered = [u for u in pagination_urls if u["page_number"] is not None]
        nav = [u for u in pagination_urls if u["page_number"] is None]
        numbered.sort(key=lambda x: x["page_number"])
        sorted_urls = numbered + nav
        logger.info("LEGACY EXTRACTION COMPLETE: %d pagination URLs (%d numbered, %d nav)",
                     len(sorted_urls), len(numbered), len(nav))
        return sorted_urls

    except Exception as e:
        logger.error("Error extracting pagination URLs from HTML: %s", e)
        return pagination_urls


# ---------------------------------------------------------------------------
# process_paginated_url  (V2 lines 4725-5007)
# ---------------------------------------------------------------------------


def process_paginated_url(
    url: str, title: str, path: str, url_last_modified_map: Dict[str, Any],
    last_run_timestamp: Any, local_files_cache: Optional[Dict[str, Any] | Set[str]],
    enable_resume: bool, vector_store_id: Optional[str],
    deduplication_enabled: bool, chunking_strategy: Optional[Dict[str, Any]],
    vector_store_cache: Optional[Dict[str, Any]], remove_selectors: str,
    rss_metadata: Optional[Dict[str, Any]] = None, current_depth: int = 0,
    visited: Optional[VisitedSet] = None,
) -> Tuple[int, int, str]:
    """Check a URL for pagination/suburls, then process all discovered pages.

    Args:
        visited: Run-scoped, thread-safe :class:`VisitedSet` (Bug 14) ensuring
            each URL/suburl/pagination subpage is fetched and processed exactly
            once across all workers within a run. ``None`` (the default) creates
            a fresh local instance for backward compatibility / standalone calls.

    Returns:
        Tuple of ``(success_count, total_processed, status)`` where *status* is
        one of:

        * ``"ok"`` -- at least one page was fetched, saved (and uploaded).
        * ``"failed"`` -- a genuine fetch/save failure occurred and nothing
          succeeded. Callers should log this at ERROR level (Bug 10).
        * ``"skipped"`` -- nothing succeeded but the only "non-success" was an
          intentional gate decision (not-modified / resume-skip / already
          visited this run), NOT a real failure. Callers should log this at
          INFO level (Bug 10).
    """
    if visited is None:
        # Backward compat: a standalone call gets its own one-shot guard.
        visited = VisitedSet()

    # Bug 14: claim this URL for the run. If another parent page already
    # processed it (or it is both a sitemap entry and someone's suburl), skip
    # exactly once. This is an in-run guard only -- it does not replace the
    # should_process_url_with_resume eligibility gate below.
    if not visited.mark(url):
        logger.info("VISITED SKIP: %s already processed/claimed this run", url)
        return (0, 0, "skipped")

    logger.info("PAGINATION PROCESSING: Starting for %s", url)
    success_count = 0
    total_processed = 0
    # Bug 10: track whether any genuine fetch/save failure happened versus only
    # intentional gate-skips, so the top-level caller can log INFO vs ERROR.
    had_failure = False
    had_skip = False

    def _single_status(result: Tuple[int, int]) -> str:
        """Map a process_single_url_normally result to an "ok"/"failed" status."""
        return "ok" if result[0] > 0 else "failed"

    def _finalize_status() -> str:
        """Derive the aggregate status from accumulated counters/flags."""
        if success_count > 0:
            return "ok"
        if had_failure:
            return "failed"
        if had_skip:
            return "skipped"
        # Nothing succeeded, nothing explicitly skipped, no recorded failure:
        # treat as failed so it is never silently swallowed.
        return "failed"
    # Shorthand for delegating to normal processing
    _norm = dict(url=url, title=title, path=path,
                 url_last_modified_map=url_last_modified_map,
                 last_run_timestamp=last_run_timestamp,
                 local_files_cache=local_files_cache, enable_resume=enable_resume,
                 vector_store_id=vector_store_id,
                 deduplication_enabled=deduplication_enabled,
                 chunking_strategy=chunking_strategy,
                 vector_store_cache=vector_store_cache,
                 remove_selectors=remove_selectors, rss_metadata=rss_metadata)

    try:
        url_recursive_enabled, max_recursive_level = is_url_recursive_enabled(url)
        url_explicitly_paginated = is_url_explicitly_paginated(url)
        should_run = url_recursive_enabled or url_explicitly_paginated

        html_content: Optional[str] = None
        pag_result: Dict[str, Any] = {
            "has_pagination": False, "suburls": [], "confidence": 0, "indicators": []}
        has_suburls = False

        if should_run:
            logger.info("Fetching HTML for pagination detection (Explicit/Recursive)")
            html_content = get_html_content_for_pagination_via_jina(url, remove_selectors)
            if not html_content:
                logger.warning("No HTML for pagination check, processing normally")
                r = process_single_url_normally(**_norm)
                return (r[0], r[1], _single_status(r))
            pag_result = detect_pagination_in_html(html_content, url)
            has_suburls = bool(pag_result.get("suburls", [])) and url_recursive_enabled
        else:
            logger.info("Skipping AI detection: not configured for pagination/recursion")

        if not pag_result["has_pagination"] and not has_suburls:
            logger.info("NO PAGINATION/SUBURLS: confidence=%s%% suburls=%d recursive=%s depth=%d/%d",
                        pag_result["confidence"], len(pag_result.get("suburls", [])),
                        url_recursive_enabled, current_depth, max_recursive_level)
            r = process_single_url_normally(**_norm)
            return (r[0], r[1], _single_status(r))

        # Step 3: Extract pagination URLs and suburls
        pagination_urls: List[Dict[str, Any]] = []
        suburls: List[Dict[str, Any]] = []

        if pag_result["has_pagination"]:
            logger.info("PAGINATION DETECTED: %s%% confidence", pag_result["confidence"])
            print(f"PAGINATION FOUND: {url} ({pag_result['confidence']}%)")
            pagination_urls = extract_pagination_urls(
                html_content, url, url, ai_pagination_data=pag_result)
            if pagination_urls:
                logger.info("Found %d pagination URLs", len(pagination_urls))
                print(f"Found {len(pagination_urls)} pages to process")

        if has_suburls:
            logger.info("SUBURLS DETECTED: %d for recursive URL", len(pag_result["suburls"]))
            print(f"SUBURLS FOUND: {len(pag_result['suburls'])} for {url}")
            suburls = extract_suburls_from_ai_data(pag_result["suburls"], url, url)
            if suburls:
                logger.info("Found %d valid suburls", len(suburls))

        if not pagination_urls and not suburls:
            logger.warning("Detected but no URLs extracted, processing normally")
            r = process_single_url_normally(**_norm)
            return (r[0], r[1], _single_status(r))

        logger.info("Additional URLs: %d pag + %d sub", len(pagination_urls), len(suburls))

        # Step 4: Process the original URL (main page / page 1)
        main_lm = find_url_last_modified(url, url_last_modified_map)
        if should_process_url_with_resume(url, main_lm, last_run_timestamp,
                                          local_files_cache, enable_resume,
                                          rss_published_date=None,
                                          vector_store_id=vector_store_id,
                                          vector_store_cache=vector_store_cache):
            main_result = process_single_url_normally(**_norm)
            success_count += main_result[0]
            total_processed += main_result[1]
            if main_result[0] == 0:
                had_failure = True  # genuine fetch/save failure of the main page
        else:
            logger.info("Skipping main URL %s (already processed)", url)
            total_processed += 1
            had_skip = True  # intentional gate decision, not a failure

        # Step 5: Process pagination subpages
        if pagination_urls:
            for i, pi in enumerate(pagination_urls, 1):
                pn = pi.get("page_number")
                pt = pi.get("link_text", "")
                sp_title = f"{title}_PAGE{pn}" if pn is not None else f"{title}_PAGE{i}"
                sp_path = (f"{path} > Page {pn}" if pn is not None
                           else f"{path} > {pt or f'Page {i}'}")
                if not visited.mark(pi["url"]):
                    logger.info("VISITED SKIP subpage: %s already processed/claimed this run",
                                pi["url"])
                    total_processed += 1
                    had_skip = True
                    continue
                logger.info("Subpage %d/%d: %s (%s)", i, len(pagination_urls), pi["url"], sp_title)
                r = process_single_url_normally(
                    pi["url"], sp_title, sp_path, url_last_modified_map, last_run_timestamp,
                    local_files_cache, enable_resume, vector_store_id,
                    deduplication_enabled, chunking_strategy, vector_store_cache,
                    remove_selectors, rss_metadata)
                success_count += r[0]; total_processed += r[1]
                if r[0] == 0:
                    had_failure = True  # a subpage genuinely failed to fetch/save
            logger.info("PAGINATION COMPLETE: %d URLs processed", len(pagination_urls))

        # Step 6: Process suburls (recursive, within depth limit)
        if suburls and url_recursive_enabled:
            if current_depth >= max_recursive_level:
                logger.info("DEPTH LIMIT: %d/%d - skipping %d suburls",
                            current_depth, max_recursive_level, len(suburls))
            else:
                logger.info("Processing %d suburls at depth %d/%d",
                            len(suburls), current_depth + 1, max_recursive_level)
                parent_is_test = is_url_from_test_urls(url)
                if parent_is_test:
                    logger.info("CONTEXT-AWARE BYPASS: parent in TEST_URLS, %d suburls bypass "
                                "timestamp", len(suburls))

                for i, si in enumerate(suburls, 1):
                    su_url = si["url"]
                    su_type = si.get("url_type", "other")
                    su_text = si.get("link_text", "")
                    su_title = f"{title}_SUB_{su_type.upper()}_{i}"
                    su_path = f"{path} > Suburl: {su_text or f'Link {i}'}"
                    logger.info("Suburl %d/%d depth %d: %s",
                                i, len(suburls), current_depth + 1, su_url)

                    should_process = False
                    if parent_is_test:
                        if enable_resume and local_files_cache is not None:
                            if is_url_already_processed_locally(su_url, local_files_cache):
                                total_processed += 1; had_skip = True; continue
                        should_process = True
                    else:
                        su_lm = find_url_last_modified(su_url, url_last_modified_map)
                        should_process = should_process_url_with_resume(
                            su_url, su_lm, last_run_timestamp, local_files_cache,
                            enable_resume, rss_published_date=None,
                            vector_store_id=vector_store_id,
                            vector_store_cache=vector_store_cache)

                    if not should_process:
                        logger.info("Skipping suburl %s", su_url)
                        total_processed += 1; had_skip = True; continue

                    r = process_paginated_url(
                        su_url, su_title, su_path, url_last_modified_map,
                        last_run_timestamp, local_files_cache, enable_resume,
                        vector_store_id, deduplication_enabled, chunking_strategy,
                        vector_store_cache, remove_selectors, rss_metadata,
                        current_depth=current_depth + 1, visited=visited)
                    success_count += r[0]; total_processed += r[1]
                    # r[2] is the recursive status; only "failed" marks a real failure.
                    if len(r) > 2 and r[2] == "failed":
                        had_failure = True
                logger.info("SUBURLS COMPLETE: %d suburls at depth %d",
                            len(suburls), current_depth + 1)
        elif suburls and not url_recursive_enabled:
            logger.info("SUBURLS SKIPPED: %d (not in recursive_urls config)", len(suburls))
            print(f"Suburls found but skipped (not recursive): {len(suburls)}")

        proc_sub = (len(suburls) if url_recursive_enabled
                     and current_depth < max_recursive_level else 0)
        logger.info("ENHANCED COMPLETE: %d/%d processed", success_count, total_processed)
        print(f"Enhanced processing: {success_count}/{total_processed} "
              f"(1 main + {len(pagination_urls)} pag + {proc_sub} sub, "
              f"depth {current_depth}/{max_recursive_level})")
        return (success_count, total_processed, _finalize_status())

    except Exception as e:
        logger.error("Error in pagination processing for %s: %s", url, e)
        r = process_single_url_normally(**_norm)
        return (r[0], r[1], _single_status(r))


# ---------------------------------------------------------------------------
# process_single_url_normally  (V2 lines 5009-5147)
# ---------------------------------------------------------------------------


def process_single_url_normally(
    url: str, title: str, path: str, url_last_modified_map: Dict[str, Any],
    last_run_timestamp: Any, local_files_cache: Optional[Dict[str, Any] | Set[str]],
    enable_resume: bool, vector_store_id: Optional[str],
    deduplication_enabled: bool, chunking_strategy: Optional[Dict[str, Any]],
    vector_store_cache: Optional[Dict[str, Any]], remove_selectors: str,
    rss_metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int]:
    """Process a single URL: fetch content, chunk if needed, save and upload.

    Returns:
        (success_count, total_processed) -- (1,1) success, (0,1) failure.
    """
    cfg = get_config()

    # Override chunking strategy for upload: max chunk size, zero overlap
    upload_cs = chunking_strategy
    if vector_store_id:
        try:
            upload_cs = create_chunking_strategy("static", 4096, 0)
            logger.info("Upload chunking overridden to STATIC 4096/0")
        except Exception as e:
            logger.error("Failed to create override chunking strategy: %s", e)
            upload_cs = chunking_strategy

    try:
        content, api_title, metadata, provider_used = get_markdown_content(url, remove_selectors)
        if not content:
            logger.error("Failed to fetch content for: %s", url)
            return (0, 1)

        # Determine final title
        if "_PAGE" in title:
            final_title = title
            logger.info("Preserving pagination title: %s", title)
        elif (rss_metadata and rss_metadata.get("event_metadata")
              and "eventId=" in url and api_title == "Detail akce"):
            final_title = title
            logger.info("EVENTS RSS: Preserving RSS title '%s' over '%s'", title, api_title)
        else:
            final_title = api_title if api_title and api_title.strip() else title

        page_summary: Optional[str] = None
        if cfg.OPENROUTER_API_KEY:
            page_summary = generate_page_summary_via_openrouter(
                content, final_title, url, cfg.OPENROUTER_TARGET_LANGUAGE)

        question_text = generate_question_section(
            url, final_title, cfg.OPENROUTER_TARGET_LANGUAGE,
            page_summary=page_summary, rss_metadata=rss_metadata)
        last_modified = find_url_last_modified(url, url_last_modified_map)

        content_tokens = count_tokens_approximate(content)
        logger.info("Content size: %s tokens", f"{content_tokens:,}")
        working_space = cfg.DEFAULT_MAX_CHUNK_SIZE - cfg.DEFAULT_CONTENT_TOKEN_OFFSET
        content_budget = int(working_space * cfg.DEFAULT_CONTENT_RATIO)

        if content_tokens > content_budget:
            # Chunked processing with metadata budget management
            logger.info("CONTENT SPLITTING: %s tokens > budget %s",
                        f"{content_tokens:,}", f"{content_budget:,}")
            chunks = chunk_content_with_metadata_budget(
                content, cfg.DEFAULT_MAX_CHUNK_SIZE, final_title, url,
                cfg.OPENROUTER_TARGET_LANGUAGE)
            logger.info("Created %d chunks", len(chunks))
            saved_files = process_and_save_chunks_with_metadata_budget(
                chunks, final_title, url,
                target_language=cfg.OPENROUTER_TARGET_LANGUAGE,
                upload_to_vector_store=bool(vector_store_id),
                vector_store_id=vector_store_id,
                enable_deduplication=deduplication_enabled,
                chunking_strategy=upload_cs, vector_store_cache=vector_store_cache,
                last_modified=last_modified, path=path,
                provider_used=provider_used, rss_metadata=rss_metadata)
            if saved_files:
                logger.info("Chunked URL saved: %s (%d files)", url, len(saved_files))
                return (len(saved_files), 1)
            logger.error("Failed to save chunked files for: %s", url)
            return (0, 1)

        # Single file processing
        saved_file = save_markdown_to_file(
            content, final_title, url, question_text=question_text,
            upload_to_vector_store=bool(vector_store_id),
            vector_store_id=vector_store_id,
            enable_deduplication=deduplication_enabled,
            chunking_strategy=upload_cs, vector_store_cache=vector_store_cache,
            last_modified=last_modified, path=path,
            provider_used=provider_used, rss_metadata=rss_metadata,
            source_page_summary=page_summary)
        if saved_file:
            logger.info("Single URL saved: %s", url)
            return (1, 1)
        logger.error("Failed to save file for: %s", url)
        return (0, 1)

    except Exception as e:
        logger.error("Error processing single URL %s: %s", url, e)
        return (0, 1)
