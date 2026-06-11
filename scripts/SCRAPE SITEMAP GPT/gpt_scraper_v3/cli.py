"""CLI argument parsing and main orchestration for GPT Scraper V3.

Fixes applied: H4 (decompose main), L3 (env-var API key fallback), L4 (print vs logger).
"""
from __future__ import annotations

import argparse, logging, os, queue, threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

from gpt_scraper_v3.config import load_configuration, get_config, ScraperConfig
from gpt_scraper_v3.logging_setup import setup_logging, get_logger
from gpt_scraper_v3.token_tracker import get_token_usage_summary
from gpt_scraper_v3.utilities import canonical_url
from gpt_scraper_v3.vector_store import (
    build_vector_store_cache, create_chunking_strategy, cleanup_removed_urls,
    save_vs_cache_snapshot, snapshot_vs_cache,
)
from gpt_scraper_v3.xml_sitemap import (
    fetch_xml_sitemap, extract_urls_from_xml_sitemap,
    get_last_run_timestamp, save_last_run_timestamp, build_local_files_cache,
    load_known_urls_snapshot, save_known_urls_snapshot, parse_lastmod_date,
    load_html_seen_urls, save_html_seen_urls,
)
from gpt_scraper_v3.rss_feeds import process_rss_feeds
from gpt_scraper_v3.sitemap_parsing import extract_links_from_html_sitemap, parse_menu, extract_links
from gpt_scraper_v3.content_fetching import get_html_content_via_jina, get_raw_html_content
from gpt_scraper_v3.url_processing import process_test_urls
from gpt_scraper_v3.pagination_processing import VisitedSet, process_paginated_url
from gpt_scraper_v3.playwright_provider import close_current_thread_playwright
from gpt_scraper_v3.rate_limiter import reset_rate_limiter
from gpt_scraper_v3.utilities import close_all_sessions

_XS_KEYS = ("last_run_timestamp", "local_files_cache", "enable_resume",
             "vector_store_id", "vector_store_cache", "previous_known_urls")


def _extract_xml_urls(lm_map: Dict[str, Any], **kw: Any) -> List[Dict[str, Any]]:
    """Fetch XML sitemap, merge into *lm_map*, and extract processable URLs."""
    lm_map.update(fetch_xml_sitemap())
    get_logger().info("XML sitemap: %d last-mod entries", len(lm_map))
    result = extract_urls_from_xml_sitemap(lm_map, **{k: kw[k] for k in _XS_KEYS if k in kw})
    get_logger().info("XML sitemap: %d URLs extracted", len(result))
    return result


def _log_last_run_state(cfg: ScraperConfig) -> None:
    """Log the last-run timestamp values + snapshot state loaded from disk.

    Makes startup explicit about WHICH timestamp baseline each mode will use and
    whether a file was actually found (vs. first run). Goes through the logger so
    it lands in both the console and the detailed log.
    """
    logger = get_logger()
    logger.info("--- Last-run state (loaded from disk) ---")
    for label, path in (
        ("Combined last run", cfg.LAST_RUN_FILE),
        ("RSS last run", cfg.RSS_LAST_RUN_FILE),
        ("Sitemap last run", cfg.SITEMAP_LAST_RUN_FILE),
    ):
        basename = os.path.basename(path) if path else "<unset>"
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    val = f.read().strip()
                logger.info("  %s: %s  (%s)", label, val or "<empty file>", basename)
            except OSError as e:
                logger.warning("  %s: ERROR reading %s: %s", label, basename, e)
        else:
            logger.info("  %s: NOT FOUND (first run / will process all) -> %s", label, basename)
    for label, path in (
        ("Known URLs snapshot", cfg.KNOWN_URLS_FILE),
        ("HTML-seen snapshot", cfg.HTML_SEEN_URLS_FILE),
    ):
        basename = os.path.basename(path) if path else "<unset>"
        if path and os.path.exists(path):
            logger.info("  %s: present (%s)", label, basename)
        else:
            logger.info("  %s: NOT FOUND (first run) -> %s", label, basename)
    logger.info("-----------------------------------------")


def build_argument_parser() -> argparse.ArgumentParser:
    """Create the argparse parser preserving every V2 CLI argument."""
    p = argparse.ArgumentParser(description="Universal sitemap scraper with configurable settings")
    # Config
    p.add_argument("--config", type=str, default="config.json",
                   help="Path to configuration file (default: config.json)")
    # URL overrides
    p.add_argument("--base-url", type=str, help="Override base URL from config")
    p.add_argument("--sitemap-url", type=str, help="Override sitemap URL from config")
    p.add_argument("--xml-sitemap-url", type=str, help="Override XML sitemap URL from config")
    # API keys
    p.add_argument("--jina-api-key", type=str, help="Override Jina AI API key from config")
    p.add_argument("--firecrawl-api-key", type=str, help="Override Firecrawl API key from config")
    p.add_argument("--openai-api-key", type=str, help="Override OpenAI API key from config")
    # Processing options
    p.add_argument("--debug", action="store_true", help="Enable debug mode with verbose output")
    p.add_argument("--no-check-modified", action="store_true",
                   help="Disable last modified checking (process all URLs)")
    p.add_argument("--rss-only", action="store_true",
                   help="Process only RSS feeds, skip sitemap processing")
    p.add_argument("--sitemap-only", action="store_true",
                   help="Process only sitemap, skip RSS feeds processing")
    p.add_argument("--xml-only", action="store_true",
                   help="Process only XML sitemap URLs, skip HTML sitemap and RSS feeds")
    p.add_argument("--legacy-html-parsing", action="store_true",
                   help="Use legacy HTML parsing instead of generalized Jina AI links summary")
    p.add_argument("--verbose-url-matching", action="store_true",
                   help="Show detailed URL matching information for all URLs")
    p.add_argument("--resume", action="store_true",
                   help="Resume processing by skipping URLs that already have local files")
    p.add_argument("--test-resume", action="store_true",
                   help="Test resume cache building and show statistics without processing")
    p.add_argument("--test-events-xml", action="store_true",
                   help="Test Events XML parsing functionality with sample data")
    # Vector Store
    p.add_argument("--vector-store-id", type=str,
                   help="OpenAI Vector Store ID to upload processed files to")
    p.add_argument("--disable-deduplication", action="store_true",
                   help="Disable deduplication (allow duplicate files for same URL)")
    p.add_argument("--skip-vector-cache", action="store_true",
                   help="Skip Vector Store cache building (faster but no deduplication)")
    p.add_argument("--chunking-strategy", type=str, choices=["auto", "static"],
                   help="Chunking strategy for Vector Store")
    p.add_argument("--max-chunk-size", type=int, help="Max tokens per chunk for static chunking")
    p.add_argument("--chunk-overlap", type=int, help="Token overlap between chunks")
    # Content processing
    p.add_argument("--jina-remove-selectors", type=str,
                   help="CSS selectors for removing parts of a page (comma-separated)")
    p.add_argument("--output-dir", type=str, help="Override output directory from config")
    # Testing
    p.add_argument("--test-urls", nargs="+", type=str,
                   help="Array of URLs for testing (overrides HTML sitemap processing)")
    # Parallelism / rate limiting
    p.add_argument("--workers", type=int, default=None,
                   help="Number of parallel worker threads (default: from config). "
                        "Live browser concurrency is min(workers, per-domain cap), since "
                        "the scraped-domain politeness cap (not worker count) bounds live "
                        "browsers. --workers 1 = serial escape hatch.")
    p.add_argument("--no-rate-limit", action="store_true",
                   help="Disable the two-tier rate limiter (no-op slots)")
    p.add_argument("--per-domain-interval-ms", type=int, default=None,
                   help="Override politeness-tier minimum start interval per domain (ms)")
    p.add_argument("--per-domain-max-concurrent", type=int, default=None,
                   help="Override politeness-tier max concurrent requests per domain")
    p.add_argument("--api-max-concurrent", type=int, default=None,
                   help="Override API-tier max concurrent calls (openrouter.ai, api.openai.com)")
    return p


def apply_cli_overrides(args: argparse.Namespace, cfg: ScraperConfig) -> None:
    """Override ScraperConfig fields from CLI arguments and env vars."""
    logger = get_logger()

    # API key priority: CLI arg > env var > config file
    if args.jina_api_key:
        cfg.JINA_AI_API_KEY = args.jina_api_key
        cfg.MARKDOWN_PROVIDERS["jina"]["api_key"] = args.jina_api_key
        logger.info("Jina API key overridden from command line")
    elif os.environ.get("JINA_API_KEY"):
        cfg.JINA_AI_API_KEY = os.environ["JINA_API_KEY"]
        cfg.MARKDOWN_PROVIDERS["jina"]["api_key"] = cfg.JINA_AI_API_KEY
        logger.info("Jina API key loaded from JINA_API_KEY environment variable")

    if args.firecrawl_api_key:
        cfg.FIRECRAWL_API_KEY = cfg.MARKDOWN_PROVIDERS["firecrawl"]["api_key"] = args.firecrawl_api_key
        logger.info("Firecrawl API key overridden from command line")
    elif os.environ.get("FIRECRAWL_API_KEY"):
        cfg.FIRECRAWL_API_KEY = cfg.MARKDOWN_PROVIDERS["firecrawl"]["api_key"] = os.environ["FIRECRAWL_API_KEY"]
        logger.info("Firecrawl API key from FIRECRAWL_API_KEY env var")

    if args.openai_api_key:
        cfg.OPENAI_API_KEY = args.openai_api_key
        logger.info("OpenAI API key overridden from command line")
    elif os.environ.get("OPENAI_API_KEY"):
        cfg.OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
        logger.info("OpenAI API key from OPENAI_API_KEY env var")

    if not cfg.OPENROUTER_API_KEY and os.environ.get("OPENROUTER_API_KEY"):
        cfg.OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
        logger.info("OpenRouter API key from OPENROUTER_API_KEY env var")

    # URL / directory overrides
    for attr, cfg_attr in [("base_url", "BASE_URL"), ("sitemap_url", "SITEMAP_URL"),
                            ("xml_sitemap_url", "XML_SITEMAP_URL"), ("output_dir", "OUTPUT_DIR")]:
        val = getattr(args, attr, None)
        if val:
            setattr(cfg, cfg_attr, val)
            logger.info("%s overridden: %s", cfg_attr, val)

    # Recompute derived URL fields if BASE_URL was overridden
    if getattr(args, "base_url", None):
        cfg.PARSED_BASE_URL = urlparse(cfg.BASE_URL)
        cfg.BASE_NETLOC = cfg.PARSED_BASE_URL.netloc
        cfg.NON_WWW_BASE_NETLOC = (
            cfg.BASE_NETLOC[4:] if cfg.BASE_NETLOC.startswith("www.") else cfg.BASE_NETLOC
        )
        cfg.BASE_SCHEME = cfg.PARSED_BASE_URL.scheme
        logger.info("Recomputed derived URL fields for new BASE_URL: netloc=%s, scheme=%s",
                     cfg.BASE_NETLOC, cfg.BASE_SCHEME)

        # Recompute CANONICAL_BASE_URLS for homepage detection
        canonical: Set[str] = set()
        canonical.add(cfg.BASE_URL)
        normalized_base = cfg.BASE_URL.rstrip("/")
        canonical.add(normalized_base)
        canonical.add(normalized_base + "/")

        if cfg.BASE_NETLOC.startswith("www."):
            non_www_url = cfg.BASE_URL.replace("www.", "", 1)
            canonical.add(non_www_url)
            canonical.add(non_www_url.rstrip("/"))
            canonical.add(non_www_url.rstrip("/") + "/")
        else:
            www_url = cfg.BASE_URL.replace(
                cfg.BASE_SCHEME + "://", cfg.BASE_SCHEME + "://www.", 1
            )
            canonical.add(www_url)
            canonical.add(www_url.rstrip("/"))
            canonical.add(www_url.rstrip("/") + "/")

        if cfg.PARSED_BASE_URL.path.strip("/") == "":
            root_no_path = urlunparse((cfg.BASE_SCHEME, cfg.BASE_NETLOC, "", "", "", ""))
            default_pages = ["index.html", "index.htm", "default.aspx", "home.html"]
            for page in default_pages:
                canonical.add(urljoin(root_no_path, page))
            if cfg.BASE_NETLOC.startswith("www."):
                non_www_netloc = cfg.BASE_NETLOC[4:]
                root_non_www = urlunparse((cfg.BASE_SCHEME, non_www_netloc, "", "", "", ""))
                for page in default_pages:
                    canonical.add(urljoin(root_non_www, page))

        cfg.CANONICAL_BASE_URLS = canonical
        logger.info("Recomputed %d CANONICAL_BASE_URLS for new BASE_URL", len(canonical))

    if args.output_dir:
        os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)  # type: ignore[arg-type]

    # Debug / flags
    if args.debug:
        logger.setLevel(logging.DEBUG)
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.DEBUG)
        logger.info("Debug mode enabled (console=DEBUG, file=INFO)")
    if args.no_check_modified:
        cfg.CHECK_LAST_MODIFIED = 0
        logger.info("Last modified checking disabled - will process all URLs")
    if args.verbose_url_matching:
        cfg.VERBOSE_URL_MATCHING = True
        logger.info("Verbose URL matching enabled")
    if args.test_urls:
        cfg.TEST_URLS = args.test_urls
        logger.info("Test URLs overridden from command line: %d URLs", len(cfg.TEST_URLS))
        for i, url in enumerate(cfg.TEST_URLS, 1):
            logger.info("  %d. %s", i, url)

    # Parallelism / rate-limiting overrides
    if getattr(args, "workers", None) is not None:
        cfg.PARALLEL_WORKERS = max(1, args.workers)
        logger.info("PARALLEL_WORKERS overridden: %d", cfg.PARALLEL_WORKERS)
    if getattr(args, "no_rate_limit", False):
        cfg.RATE_LIMIT_ENABLED = False
        logger.info("Rate limiter disabled (--no-rate-limit)")
    if getattr(args, "per_domain_interval_ms", None) is not None:
        cfg.RATE_LIMIT_MIN_INTERVAL_MS = args.per_domain_interval_ms
        logger.info("RATE_LIMIT_MIN_INTERVAL_MS overridden: %d", cfg.RATE_LIMIT_MIN_INTERVAL_MS)
    if getattr(args, "per_domain_max_concurrent", None) is not None:
        cfg.RATE_LIMIT_MAX_CONCURRENT_PER_DOMAIN = args.per_domain_max_concurrent
        logger.info("RATE_LIMIT_MAX_CONCURRENT_PER_DOMAIN overridden: %d",
                    cfg.RATE_LIMIT_MAX_CONCURRENT_PER_DOMAIN)
    if getattr(args, "api_max_concurrent", None) is not None:
        cfg.API_MAX_CONCURRENT = args.api_max_concurrent
        logger.info("API_MAX_CONCURRENT overridden: %d", cfg.API_MAX_CONCURRENT)


def build_caches(
    cfg: ScraperConfig, args: argparse.Namespace,
    vector_store_id: str, deduplication_enabled: bool,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Build vector-store and local-file caches. Returns (vs_cache, local_cache)."""
    logger = get_logger()
    vs_cache: Optional[Dict[str, Any]] = None
    skip = getattr(args, "skip_vector_cache", False)

    if vector_store_id and deduplication_enabled and not skip:
        logger.info("Building Vector Store cache for optimized deduplication...")
        vs_cache = build_vector_store_cache(vector_store_id)
        logger.info("Vector Store cache built with %d files", len(vs_cache))
    elif skip and vector_store_id:
        logger.info("Skipping Vector Store cache (--skip-vector-cache); slower API lookups")

    local_cache: Optional[Dict[str, Any]] = None
    resume = getattr(args, "resume", False)
    need_existence = bool(vector_store_id) or bool(cfg.RSS_FEEDS)
    if resume or need_existence:
        logger.info("Building local files cache (%s)...",
                     "resume mode" if resume else "existence checks")
        local_cache = build_local_files_cache(cfg.OUTPUT_DIR or "")
        logger.info("Local cache built with %d processed URLs", len(local_cache))

    return vs_cache, local_cache


def resolve_urls(
    cfg: ScraperConfig, args: argparse.Namespace,
    lm_map: Dict[str, Any], local_cache: Optional[Dict[str, Any]],
    vs_cache: Optional[Dict[str, Any]], enable_resume: bool,
    last_ts: Optional[datetime], rss_only: bool, sitemap_only: bool,
    xml_only: bool, remove_sel: str, vs_id: str,
    previous_known_urls: Optional[Set[str]] = None,
    previous_html_seen: Optional[Set[str]] = None,
) -> Tuple[List[Dict[str, Any]], Set[str]]:
    """Resolve URLs from test list, HTML/XML sitemap, and RSS feeds.

    Returns a tuple of (deduplicated URL dicts, html_seen_sink) where
    ``html_seen_sink`` is the full set of canonical HTML-discovered URLs seen
    this run (always a set, never None).
    """
    logger = get_logger()
    urls: List[Dict[str, Any]] = []
    # Collect ALL canonical HTML-discovered URLs (even gate-skipped ones).
    # Allocated unconditionally so callers never have to None-check it.
    html_seen_sink: Set[str] = set()
    xs_kw: Dict[str, Any] = dict(
        last_run_timestamp=last_ts, local_files_cache=local_cache,
        enable_resume=enable_resume, vector_store_id=vs_id, vector_store_cache=vs_cache,
        previous_known_urls=previous_known_urls,
    )

    if not rss_only:
        if cfg.TEST_URLS:
            logger.info("TEST MODE: Using %d test URLs", len(cfg.TEST_URLS))
            lm_map.update(fetch_xml_sitemap())
            urls = process_test_urls(cfg.TEST_URLS, lm_map, last_ts, local_cache,
                                     enable_resume, vector_store_id=vs_id,
                                     vector_store_cache=vs_cache,
                                     previous_known_urls=previous_known_urls)
            logger.info("TEST MODE: %d test URLs to process", len(urls))
        elif xml_only:
            logger.info("XML-only mode: Skipping HTML sitemap")
            urls = _extract_xml_urls(lm_map, **xs_kw)
        elif cfg.SITEMAP_URL and cfg.SITEMAP_URL.strip():
            urls = _resolve_html_sitemap(
                cfg, args, lm_map, remove_sel, xs_kw,
                previous_html_seen, html_seen_sink,
            )
        else:
            logger.info("No HTML sitemap URL, using XML-only")
            urls = _extract_xml_urls(lm_map, **xs_kw)
    else:
        logger.info("Skipping sitemap processing (RSS-only mode)")
        lm_map.update(fetch_xml_sitemap())

    # RSS feeds
    if not sitemap_only and not xml_only and cfg.RSS_FEEDS:
        logger.info("Processing RSS feeds")
        # F1: RSS must be gated against the RSS-specific baseline, not the
        # combined `last_ts`. In --rss-only mode `last_ts` already equals the
        # rss timestamp, so re-reading it here is idempotent.
        rss_ts = get_last_run_timestamp("rss")
        logger.info("RSS last-run baseline: %s",
                    rss_ts.isoformat() if rss_ts else "none (first RSS run)")
        rss_urls = process_rss_feeds(lm_map, rss_ts, local_cache, enable_resume)
        urls.extend(rss_urls)
        logger.info("Added %d URLs from RSS feeds", len(rss_urls))
    elif sitemap_only or xml_only:
        logger.info("Skipping RSS processing (%s mode)", "sitemap-only" if sitemap_only else "XML-only")
    else:
        logger.info("No RSS feeds configured, skipping")

    return _deduplicate_urls(urls), html_seen_sink


def _resolve_html_sitemap(
    cfg: ScraperConfig, args: argparse.Namespace,
    lm_map: Dict[str, Any], remove_sel: str, xs_kw: Dict[str, Any],
    previous_html_seen: Optional[Set[str]] = None,
    html_seen_sink: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Fetch HTML sitemap, parse links, with XML/legacy fallback.

    ``html_seen_urls``/``html_seen_sink`` are threaded ONLY into the two live
    HTML extractors (anchor + legacy), never into ``_extract_xml_urls`` (whose
    XML extractor does not accept them).
    """
    logger = get_logger()
    logger.info("Fetching HTML sitemap from %s (provider-sequence-aware)", cfg.SITEMAP_URL)
    html = get_raw_html_content(cfg.SITEMAP_URL)

    if not html:
        logger.error("Failed to get HTML sitemap; falling back to XML-only")
        return _extract_xml_urls(lm_map, **xs_kw)

    lm_map.update(fetch_xml_sitemap())
    logger.info("Fetched last modified dates for %d URLs", len(lm_map))

    if getattr(args, "legacy_html_parsing", False):
        logger.info("Using legacy HTML sitemap parsing")
        menu = parse_menu(html)
        if menu:
            result = extract_links(
                menu, url_last_modified_map=lm_map,
                html_seen_urls=previous_html_seen, html_seen_sink=html_seen_sink,
                **xs_kw,
            )
            logger.info("Found %d URLs from legacy parsing", len(result))
            return result
        logger.error("Legacy parsing failed, falling back to XML-only")
        return _extract_xml_urls(lm_map, **xs_kw)

    logger.info("Extracting URLs from HTML sitemap (<a> anchor tags)")
    result = extract_links_from_html_sitemap(
        html, url_last_modified_map=lm_map,
        html_seen_urls=previous_html_seen, html_seen_sink=html_seen_sink,
        **xs_kw,
    )
    logger.info("Found %d URLs from HTML sitemap", len(result))

    if not result:
        logger.warning("No links from anchor parsing; trying legacy fallback")
        menu = parse_menu(html)
        if menu:
            result = extract_links(
                menu, url_last_modified_map=lm_map,
                html_seen_urls=previous_html_seen, html_seen_sink=html_seen_sink,
                **xs_kw,
            )
            logger.info("Found %d URLs from legacy parsing", len(result))
        else:
            logger.warning("Legacy fallback also failed; using XML-only")
            result = _extract_xml_urls(lm_map, **xs_kw)
    return result


_DEDUP_SENTINEL_DT = datetime.min.replace(tzinfo=timezone.utc)


def _dedup_sort_dt(value: Any) -> datetime:
    """Coerce a ``last_modified`` value to an aware-UTC datetime for sorting.

    Bug 2 helper. ``last_modified`` entries arrive as :class:`datetime`
    (possibly naive), as date strings, or as ``None``. To compare them safely:
      * ``datetime`` -> returned as-is if aware, else assumed UTC (naive→UTC).
      * ``str`` -> parsed via :func:`parse_lastmod_date`; naive results→UTC.
      * missing / unparseable -> ``datetime.min`` (UTC) sentinel so they sort
        below every real timestamp.
    """
    dt: Optional[datetime] = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        dt = parse_lastmod_date(value)

    if dt is None:
        return _DEDUP_SENTINEL_DT
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _deduplicate_urls(urls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove duplicate URL entries, keeping the most recent by lastmod."""
    if not urls:
        return urls
    logger = get_logger()
    logger.info("Deduplicating %d URLs", len(urls))

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for info in urls:
        groups.setdefault(info["url"], []).append(info)

    deduped: List[Dict[str, Any]] = []
    dup_count = 0
    for url, infos in groups.items():
        if len(infos) > 1:
            dup_count += len(infos) - 1
            logger.warning("Duplicate: %s (%d entries)", url, len(infos))
            # Bug 2: the old key ``(x[1].get("last_modified") or "", x[0])`` mixed
            # datetime / str / None and string-sorted them — wrong "most recent
            # wins" plus a TypeError risk when datetimes and strings collide.
            # Coerce every last_modified to an aware-UTC datetime first
            # (sentinel for missing/unparseable), then stable-tiebreak by the
            # original index. ``max`` over ``(dt, -index)`` makes the most-recent
            # entry win, and the *first* input entry win on equal timestamps.
            best = max(enumerate(infos),
                       key=lambda x: (_dedup_sort_dt(x[1].get("last_modified")), -x[0]))[1]
            deduped.append(best)
        else:
            deduped.append(infos[0])

    if dup_count:
        logger.warning("Removed %d duplicate URL entries", dup_count)
    logger.info("After deduplication: %d unique URLs", len(deduped))
    return deduped


def _build_rss_meta(info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract RSS metadata from a URL info dict (H2: preserve rss_meta)."""
    rss_keys = {"published", "summary", "description", "source_feed", "event_metadata"}
    if any(k in info for k in rss_keys):
        rss_meta = {k: info.get(k) for k in rss_keys}
        get_logger().info("RSS meta: published=%s, source=%s",
                          rss_meta.get("published"), rss_meta.get("source_feed"))
        return rss_meta
    return None


def _process_one_url(
    info: Dict[str, Any], cfg: ScraperConfig,
    lm_map: Dict[str, Any], last_ts: Optional[datetime],
    local_cache: Optional[Dict[str, Any]], enable_resume: bool,
    vs_id: str, dedup: bool, chunking: Optional[Dict[str, Any]],
    vs_cache: Optional[Dict[str, Any]], remove_sel: str,
    visited: VisitedSet,
) -> Dict[str, Any]:
    """Process a single URL info dict, returning a per-URL result record.

    Never raises -- any exception is captured into the result so the worker
    loop / serial loop keeps going. The full per-URL body (rss_meta extraction,
    current_depth=0) is preserved here.

    Result keys: ``url``, ``success`` (int success_count from
    process_paginated_url), ``status`` (``"ok"`` / ``"skipped"`` / ``"failed"``
    -- Bug 10), and ``error`` (exception text or None).
    """
    logger = get_logger()
    url, title, path = info["url"], info["title"], info["path"]
    rss_meta = _build_rss_meta(info)
    try:
        ok, _total, status = process_paginated_url(
            url, title, path, lm_map, last_ts, local_cache, enable_resume,
            vs_id, dedup, chunking, vs_cache, remove_sel, rss_meta,
            current_depth=0, visited=visited)
        return {"url": url, "success": ok, "status": status, "error": None}
    except Exception as e:  # pragma: no cover - defensive; one URL crash is contained
        logger.error("Error processing URL %s: %s", url, e,
                     exc_info=logger.isEnabledFor(logging.DEBUG))
        return {"url": url, "success": 0, "status": "failed", "error": str(e)}


def _fold_result(result: Dict[str, Any], seen: Set[str]) -> int:
    """Fold a single per-URL result into the success counters (single-threaded).

    Bug 10: intentional skips (status ``"skipped"``) log at INFO, genuine
    failures (status ``"failed"`` with zero success) log at ERROR.
    Returns the success count contributed by this result.
    """
    logger = get_logger()
    url = result["url"]
    ok = result["success"]
    status = result["status"]
    if ok > 0:
        seen.add(url)
    elif status == "skipped":
        logger.info("Skipped (not modified / already processed): %s", url)
    else:
        logger.error("Failed to process: %s", url)
    return ok


def _tally_status(
    result: Dict[str, Any], ok_n: int, skip_n: int, fail_n: int,
) -> Tuple[int, int, int]:
    """Fold one result's status into the (ok, skip, fail) tallies (F3/F12).

    Derives the bucket from ``result["status"]`` so both the serial and the
    parallel consume paths produce identical counts. Any non-skipped, non-ok
    status (including the defensive ``"failed"``) counts as a failure.
    """
    status = result.get("status")
    if status == "ok":
        ok_n += 1
    elif status == "skipped":
        skip_n += 1
    else:
        fail_n += 1
    return ok_n, skip_n, fail_n


def process_urls(
    urls: List[Dict[str, Any]], cfg: ScraperConfig,
    lm_map: Dict[str, Any], last_ts: Optional[datetime],
    local_cache: Optional[Dict[str, Any]], enable_resume: bool,
    vs_id: str, dedup: bool, chunking: Optional[Dict[str, Any]],
    vs_cache: Optional[Dict[str, Any]], remove_sel: str,
) -> Dict[str, Any]:
    """Process each URL with pagination detection. Returns stats dict.

    Dispatches to a serial loop when ``cfg.PARALLEL_WORKERS == 1`` (escape
    hatch, current semantics minus the old ``sleep(1)``) or to a queue-fed
    worker-thread pool otherwise. In-run duplicate URLs are filtered up front.
    """
    logger = get_logger()

    # -- Pre-submission in-run dedup (moved out of the processing loop) --------
    work_items: List[Dict[str, Any]] = []
    submitted: Set[str] = set()
    skip_dup = 0
    for info in urls:
        url = info["url"]
        if url in submitted:
            skip_dup += 1
            logger.error("DUPLICATE in-run: %s (skipping)", url)
            continue
        submitted.add(url)
        work_items.append(info)

    total = len(work_items)
    workers = max(1, int(getattr(cfg, "PARALLEL_WORKERS", 1)))
    logger.info("Processing %d URLs with pagination detection (workers=%d)", total, workers)

    seen: Set[str] = set()
    processed = success = 0
    # F3/F12: per-status tallies, derived identically in both serial and
    # parallel paths so the returned stats shape matches regardless of workers.
    ok_n = skip_n = fail_n = 0

    # Bug 14: ONE run-scoped, thread-safe visited set shared by the serial path
    # and every worker (via *common -> _process_one_url -> process_paginated_url).
    # Guarantees a suburl reachable via two parents is processed exactly once,
    # even concurrently. Run-scoped (not module-level) so repeated main() is safe.
    visited = VisitedSet()

    common = (cfg, lm_map, last_ts, local_cache, enable_resume,
              vs_id, dedup, chunking, vs_cache, remove_sel, visited)

    if workers == 1 or total <= 1:
        # -- Serial escape hatch: no threads --------------------------------
        for i, info in enumerate(work_items, 1):
            logger.info("--- URL %d/%d: %s ---", i, total, info["url"])
            result = _process_one_url(info, *common)
            success += _fold_result(result, seen)
            processed += 1
            # F3/F12: same status tally as the parallel consume loop.
            ok_n, skip_n, fail_n = _tally_status(result, ok_n, skip_n, fail_n)
    else:
        # -- Queue-fed worker pool ------------------------------------------
        success, processed, ok_n, skip_n, fail_n = _process_urls_parallel(
            work_items, workers, common, seen)

    if skip_dup:
        logger.error("SAFETY NET: Prevented %d duplicate processing", skip_dup)
    else:
        logger.info("Safety net: %d unique URLs processed, no duplicates", len(seen))

    return {"processed_count": processed, "success_count": success,
            "skipped_duplicate_count": skip_dup, "unique_urls_processed": len(seen),
            # F3/F12: new keys (existing keys above unchanged for compat).
            "ok_count": ok_n, "skipped_count": skip_n, "failed_count": fail_n}


def _process_urls_parallel(
    work_items: List[Dict[str, Any]], workers: int,
    common: Tuple[Any, ...], seen: Set[str],
) -> Tuple[int, int, int, int, int]:
    """Run the per-URL processing across *workers* queue-fed threads.

    Returns ``(success_count, processed_count, ok_count, skipped_count,
    failed_count)``. The main thread consumes the results queue single-threaded
    and folds counters (no locks on counters).

    On ``KeyboardInterrupt`` it sets a shared ``stop_event`` (workers check it
    between URLs and notice via the timed ``work_q.get``), drains the queue,
    joins workers with a generous timeout, logs a settle note, and re-raises so
    ``main()`` can return a nonzero exit code. Worker ``finally`` blocks still
    run, tearing down per-thread Playwright/browser resources.
    """
    logger = get_logger()
    total = len(work_items)
    # F2: batch-start banner through the logger (lands in both console + file).
    # cfg is the FIRST element of the common tuple assembled in process_urls:
    # (cfg, lm_map, last_ts, local_cache, enable_resume, vs_id, dedup, chunking,
    #  vs_cache, remove_sel, visited).
    cfg = common[0]
    per_domain_cap = max(1, int(getattr(cfg, "RATE_LIMIT_MAX_CONCURRENT_PER_DOMAIN", 4)))
    api_cap = max(1, int(getattr(cfg, "API_MAX_CONCURRENT", 12)))
    live_browser_cap = min(workers, per_domain_cap)
    rate_limit_on = "on" if bool(getattr(cfg, "RATE_LIMIT_ENABLED", True)) else "off"
    logger.info(
        "=== BATCH START: %d URLs | %d workers | live-browser cap=%d | "
        "api cap=%d | rate_limit=%s ===",
        total, workers, live_browser_cap, api_cap, rate_limit_on)
    work_q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
    results_q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
    stop_event = threading.Event()

    for info in work_items:
        work_q.put(info)
    for _ in range(workers):
        work_q.put(None)  # one sentinel per worker

    def _worker() -> None:
        try:
            logger.info("worker online: %s", threading.current_thread().name)
            while True:
                try:
                    item = work_q.get(timeout=0.5)
                except queue.Empty:
                    if stop_event.is_set():
                        break
                    continue
                if item is None:  # sentinel -> drain done
                    break
                if stop_event.is_set():
                    # Interrupted: do not start new work; just acknowledge it.
                    results_q.put({"url": item["url"], "success": 0,
                                   "status": "skipped", "error": "interrupted"})
                    continue
                result = _process_one_url(item, *common)
                results_q.put(result)
        finally:
            # PRIMARY teardown: same-thread, greenlet-safe (close THIS thread's
            # Playwright/browser). Own HTTP session is closed by the backstop
            # close_all_sessions() in main()'s finally.
            try:
                close_current_thread_playwright()
            except Exception as e:  # pragma: no cover - defensive teardown
                logger.error("Worker teardown error: %s", e)

    threads = [threading.Thread(target=_worker, name=f"url-worker-{i}")
               for i in range(workers)]
    for t in threads:
        t.start()

    success = processed = 0
    ok_n = skip_n = fail_n = 0
    try:
        # Consume results until every expected result arrives OR all workers die.
        while processed < total:
            try:
                result = results_q.get(timeout=0.5)
            except queue.Empty:
                if not any(t.is_alive() for t in threads):
                    break  # workers gone; no more results will come
                continue
            success += _fold_result(result, seen)
            processed += 1
            # F3: running ok/skip/fail tallies on every per-URL completion line.
            ok_n, skip_n, fail_n = _tally_status(result, ok_n, skip_n, fail_n)
            logger.info("[%d/%d] %s: %s (ok=%d skip=%d fail=%d)",
                        processed, total, str(result.get("status", "")).upper(),
                        result["url"], ok_n, skip_n, fail_n)
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt: stopping workers...")
        stop_event.set()
        # Drain remaining queued work so sentinels/loops unwind quickly.
        try:
            while True:
                work_q.get_nowait()
        except queue.Empty:
            pass
        for t in threads:
            t.join(timeout=60)
        logger.warning(
            "Interrupted; in-flight navigation may take up to the nav-timeout "
            "to settle. Teardown ran in each worker.")
        raise

    for t in threads:
        t.join(timeout=60)
    # F2: matching batch-complete banner with totals.
    logger.info(
        "=== BATCH COMPLETE: %d/%d processed | ok=%d skip=%d fail=%d | success=%d ===",
        processed, total, ok_n, skip_n, fail_n, success)
    return success, processed, ok_n, skip_n, fail_n


def report_summary(
    stats: Dict[str, Any], cfg: ScraperConfig, start_time: datetime,
    config_file: str, test_urls: List[str], rss_only: bool,
    sitemap_only: bool, xml_only: bool, enable_resume: bool,
    local_cache: Optional[Dict[str, Any]], vs_id: str,
    args: argparse.Namespace,
) -> None:
    """Log the final processing summary."""
    logger = get_logger()
    end = datetime.now()
    p, s = stats["processed_count"], stats["success_count"]

    logger.info("=== PROCESSING COMPLETE ===")
    logger.info("Duration: %s | URLs: %d | Success: %d", end - start_time, p, s)
    # F12: ok/skip/fail breakdown threaded through the stats dict (new keys,
    # .get() defaults so older callers/stats shapes don't KeyError).
    logger.info("URLs: %d | OK: %d | Skipped: %d | Failed: %d",
                p, stats.get("ok_count", 0), stats.get("skipped_count", 0),
                stats.get("failed_count", 0))
    if p > 0:
        logger.info("Success rate: %.1f%%", s / p * 100)
    logger.info("Output: %s | Config: %s | Target: %s",
                os.path.abspath(cfg.OUTPUT_DIR or ""), config_file, cfg.BASE_URL)

    if test_urls:
        logger.info("Mode: TEST (%d URLs)", len(test_urls))
    elif rss_only:
        logger.info("Mode: RSS-only")
    elif sitemap_only:
        logger.info("Mode: Sitemap-only")
    elif xml_only:
        logger.info("Mode: XML-only")
    else:
        logger.info("Mode: Combined (RSS + Sitemap)")

    if enable_resume:
        logger.info("Resume: skipped %d already-processed URLs",
                     len(local_cache) if local_cache else 0)
    if vs_id:
        logger.info("Vector Store: %s", vs_id)

    tu = get_token_usage_summary()
    if tu["api_calls_count"] > 0:
        logger.info("=== TOKEN USAGE: %d calls, %s prompt, %s completion, %s total ===",
                     tu["api_calls_count"], f"{tu['total_prompt_tokens']:,}",
                     f"{tu['total_completion_tokens']:,}", f"{tu['total_tokens']:,}")


def main(args: Optional[argparse.Namespace] = None) -> int:
    """Top-level orchestration: parse, configure, resolve, process, report.

    Returns a process exit code: ``0`` on success, ``1`` on config/runtime
    error, ``130`` on Ctrl+C. (Full exit-code propagation through
    ``run_gpt_scraper_v3.py`` is a later wave; this function already returns the
    int codes where touched.)
    """
    # Step 1: Parse arguments (print OK -- logger not yet set up)
    if args is None:
        args = build_argument_parser().parse_args()

    config_file: str = args.config or "config.json"

    # Step 2: Load configuration
    try:
        cfg = load_configuration(config_file)
    except Exception as e:
        print(f"Error loading configuration: {e}")
        return 1

    # Validate mutually exclusive modes
    modes = [getattr(args, "rss_only", False), getattr(args, "sitemap_only", False),
             getattr(args, "xml_only", False)]
    if sum(bool(m) for m in modes) > 1:
        print("Error: --rss-only, --sitemap-only, and --xml-only are mutually exclusive")
        return 1

    # Step 3: Setup logging (last print() above, logger below)
    setup_logging(cfg)
    os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)
    logger = get_logger()

    # Step 4: Apply CLI overrides
    apply_cli_overrides(args, cfg)

    # Reset the rate-limiter singleton so a repeated main() call (tests / batch
    # drivers) rebuilds it from the freshly-loaded + overridden config.
    reset_rate_limiter()

    # Handle --test-resume early exit
    if getattr(args, "test_resume", False):
        logger.info("Testing resume cache for %s", cfg.OUTPUT_DIR)
        if cfg.OUTPUT_DIR and os.path.exists(cfg.OUTPUT_DIR):
            cache = build_local_files_cache(cfg.OUTPUT_DIR)
            logger.info("Cache: %d URLs in %s", len(cache), os.path.abspath(cfg.OUTPUT_DIR))
            for i, (u, inf) in enumerate(list(cache.items())[:5], 1):
                logger.info("  %d. %s -> %s", i, u, inf["filename"])
            if len(cache) > 5:
                logger.info("  ... and %d more", len(cache) - 5)
        else:
            logger.error("Output directory does not exist: %s", cfg.OUTPUT_DIR)
        return 0

    # The post-config body owns network sessions (cache build, fetches, uploads).
    # Wrap it so close_all_sessions() always runs (backstop; per-worker Playwright
    # teardown happens in-worker). atexit also sweeps any leftover Playwright.
    exit_code = 0
    # Pre-bound so the finally block can persist the run's mutated VS cache even
    # if an exception fires before build_caches() assigns the real cache.
    vs_cache: Optional[Dict[str, Any]] = None
    try:
        # Step 5: Startup
        start_time = datetime.now()
        logger.info("=== Starting %s at %s ===", cfg.SCRIPT_NAME, start_time)
        _log_last_run_state(cfg)

        remove_sel: str = cfg.JINA_REMOVE_SELECTORS
        if getattr(args, "jina_remove_selectors", None):
            remove_sel = args.jina_remove_selectors
            logger.info("Custom remove selectors: %s", remove_sel)

        vs_id: str = (args.vector_store_id if getattr(args, "vector_store_id", None)
                      else cfg.OPENAI_VECTOR_STORE_ID)
        dedup: bool = (not args.disable_deduplication if getattr(args, "disable_deduplication", False)
                       else cfg.ENABLE_DEDUPLICATION)

        chunking: Optional[Dict[str, Any]] = None
        if vs_id:
            st = args.chunking_strategy if getattr(args, "chunking_strategy", None) else cfg.DEFAULT_CHUNKING_STRATEGY
            mc = args.max_chunk_size if getattr(args, "max_chunk_size", None) else cfg.DEFAULT_MAX_CHUNK_SIZE
            co = args.chunk_overlap if getattr(args, "chunk_overlap", None) else cfg.DEFAULT_CHUNK_OVERLAP
            chunking = create_chunking_strategy(st, mc, co)

        # Step 6: Build caches
        vs_cache, local_cache = build_caches(cfg, args, vs_id, dedup)

        # Step 6b: Load known URLs + HTML-seen snapshots for new-URL detection
        previous_known_urls: Optional[Set[str]] = None
        previous_html_seen: Set[str] = set()
        if cfg.CHECK_LAST_MODIFIED != 0:
            previous_known_urls = load_known_urls_snapshot()
            if previous_known_urls:
                logger.info("Loaded %d known URLs from previous snapshot", len(previous_known_urls))
            else:
                logger.info("No previous known URLs snapshot (first run or empty)")
            previous_html_seen = load_html_seen_urls()
            if previous_html_seen:
                logger.info("Loaded %d HTML-seen URLs from previous snapshot", len(previous_html_seen))
            else:
                logger.info("No previous HTML-seen URLs snapshot (first run or empty)")

        # Step 7: Processing mode and timestamps
        rss_only = getattr(args, "rss_only", False)
        sitemap_only = getattr(args, "sitemap_only", False)
        xml_only = getattr(args, "xml_only", False)
        enable_resume = getattr(args, "resume", False)

        ts_type = "rss" if rss_only else ("sitemap" if (sitemap_only or xml_only) else "combined")
        last_ts = get_last_run_timestamp(ts_type)
        logger.info("Mode: %s | Last run: %s", ts_type, last_ts.isoformat() if last_ts else "none")

        # Steps 8-11: Resolve, process, save timestamps, report
        try:
            lm_map: Dict[str, Any] = {}
            extracted, html_seen_sink = resolve_urls(
                cfg, args, lm_map, local_cache, vs_cache, enable_resume,
                last_ts, rss_only, sitemap_only, xml_only, remove_sel, vs_id,
                previous_known_urls=previous_known_urls,
                previous_html_seen=previous_html_seen)
            stats = process_urls(extracted, cfg, lm_map, last_ts, local_cache, enable_resume,
                                 vs_id, dedup, chunking, vs_cache, remove_sel)

            # Step 9b: Known URLs snapshot — save current state and cleanup removed URLs
            if cfg.CHECK_LAST_MODIFIED != 0:
                # Build current URL set from lm_map keys (all URLs seen in this run's sitemap)
                current_known_urls: Set[str] = set()
                for url_key in lm_map:
                    current_known_urls.add(canonical_url(url_key))
                logger.info("Current sitemap contains %d URLs", len(current_known_urls))

                if not current_known_urls:
                    logger.warning(
                        "Current sitemap returned 0 URLs — skipping snapshot save and cleanup "
                        "(possible fetch failure)")
                else:
                    # Detect removed URLs
                    removed_urls: Set[str] = set()
                    if previous_known_urls:
                        removed_urls = previous_known_urls - current_known_urls
                        new_urls = current_known_urls - previous_known_urls
                        logger.info(
                            "URL diff: %d new, %d removed, %d unchanged",
                            len(new_urls), len(removed_urls),
                            len(current_known_urls & previous_known_urls))

                    # Safety guard: >50% removal threshold
                    if previous_known_urls and len(removed_urls) > len(previous_known_urls) * 0.5:
                        logger.warning(
                            "SAFETY: >50%% of URLs removed (%d of %d) — skipping BOTH "
                            "cleanup AND snapshot save (possible sitemap fetch failure)",
                            len(removed_urls), len(previous_known_urls))
                    else:
                        # Cleanup removed URLs from vector store
                        if removed_urls and vs_id and vs_cache is not None:
                            logger.info("Cleaning up %d removed URLs from vector store", len(removed_urls))
                            deleted, failed_urls = cleanup_removed_urls(removed_urls, vs_id, vs_cache)
                            logger.info("Cleanup complete: %d files deleted", deleted)
                            if failed_urls:
                                logger.warning(
                                    "Adding %d URLs with failed deletions back to snapshot for retry on next run",
                                    len(failed_urls))
                                current_known_urls.update(failed_urls)
                        elif removed_urls:
                            # Bug 7: cleanup did NOT run (no vs_id, or vs_cache is None —
                            # e.g. --skip-vector-cache). If we saved the snapshot without these
                            # URLs they would never be re-detected as removed and their vector
                            # store files would be orphaned forever. Keep them in the snapshot
                            # (mirror the failed_urls retry pattern above) so a future run with a
                            # vector store cache available cleans them up.
                            logger.info(
                                "%d URLs removed from sitemap but no vector store cleanup "
                                "(no vector store ID or cache) — keeping them in snapshot "
                                "pending cleanup on a future run", len(removed_urls))
                            current_known_urls.update(removed_urls)

                        # Save current snapshot
                        save_known_urls_snapshot(current_known_urls)

            # Step 9c: HTML-seen snapshot — monotonic union, saved INDEPENDENTLY of
            # the XML empty/>50% guard nest above. Governed only by its own guard:
            # an HTML-fetch failure yields an empty sink, leaving the prior file
            # intact. HTML-seen is append-only (XML cleanup handles real removals).
            if cfg.CHECK_LAST_MODIFIED != 0 and html_seen_sink:
                save_html_seen_urls(previous_html_seen | html_seen_sink)

            # Save timestamps
            if rss_only:
                save_last_run_timestamp("rss")
            elif sitemap_only or xml_only:
                save_last_run_timestamp("sitemap")
            else:
                for t in ("combined", "rss", "sitemap"):
                    save_last_run_timestamp(t)

            report_summary(stats, cfg, start_time, config_file,
                           cfg.TEST_URLS or [], rss_only, sitemap_only, xml_only,
                           enable_resume, local_cache, vs_id, args)
        except KeyboardInterrupt:
            # process_urls re-raises Ctrl+C after stopping/joining workers.
            logger.warning("Run interrupted by user (Ctrl+C) — exiting with code 130")
            exit_code = 130
        except Exception as e:
            logger.error("Critical error in main: %s", e,
                         exc_info=logger.isEnabledFor(logging.DEBUG))
            exit_code = 1
    finally:
        # Persist the run's final VS cache snapshot so uploads/deletions that
        # mutated the in-memory cache during processing are reflected on the next
        # warm start. build_vector_store_cache() already wrote a fresh snapshot,
        # but the cache is mutated AFTER that during URL processing + cleanup, so
        # we re-save the latest state here. Take a shallow copy under the lock so
        # serialization is not racing any straggling mutation.
        if vs_cache is not None:
            try:
                save_vs_cache_snapshot(snapshot_vs_cache(vs_cache))
            except Exception as e:  # pragma: no cover - defensive teardown
                logger.error("Error saving VS cache snapshot: %s", e)

        # Backstop teardown: close any thread-local HTTP sessions and clear the
        # registry (repeated-main() safety). Per-worker Playwright teardown ran
        # in each worker's finally; an atexit sweep covers any stragglers.
        try:
            close_all_sessions()
        except Exception as e:  # pragma: no cover - defensive teardown
            logger.error("Error closing HTTP sessions: %s", e)

    return exit_code
