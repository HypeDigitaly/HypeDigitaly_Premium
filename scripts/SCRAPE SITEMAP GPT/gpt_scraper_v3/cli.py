"""CLI argument parsing and main orchestration for GPT Scraper V3.

Fixes applied: H4 (decompose main), L3 (env-var API key fallback), L4 (print vs logger).
"""
from __future__ import annotations

import argparse, logging, os, time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

from gpt_scraper_v3.config import load_configuration, get_config, ScraperConfig
from gpt_scraper_v3.logging_setup import setup_logging, get_logger
from gpt_scraper_v3.token_tracker import get_token_usage_summary
from gpt_scraper_v3.vector_store import build_vector_store_cache, create_chunking_strategy
from gpt_scraper_v3.xml_sitemap import (
    fetch_xml_sitemap, extract_urls_from_xml_sitemap,
    get_last_run_timestamp, save_last_run_timestamp, build_local_files_cache,
)
from gpt_scraper_v3.rss_feeds import process_rss_feeds
from gpt_scraper_v3.sitemap_parsing import extract_links_from_html_sitemap, parse_menu, extract_links
from gpt_scraper_v3.content_fetching import get_html_content_via_jina
from gpt_scraper_v3.url_processing import process_test_urls
from gpt_scraper_v3.pagination_processing import process_paginated_url

_XS_KEYS = ("last_run_timestamp", "local_files_cache", "enable_resume",
             "vector_store_id", "vector_store_cache")


def _extract_xml_urls(lm_map: Dict[str, Any], **kw: Any) -> List[Dict[str, Any]]:
    """Fetch XML sitemap, merge into *lm_map*, and extract processable URLs."""
    lm_map.update(fetch_xml_sitemap())
    get_logger().info("XML sitemap: %d last-mod entries", len(lm_map))
    result = extract_urls_from_xml_sitemap(lm_map, **{k: kw[k] for k in _XS_KEYS if k in kw})
    get_logger().info("XML sitemap: %d URLs extracted", len(result))
    return result


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
            h.setLevel(logging.DEBUG)
        logger.info("Debug mode enabled")
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
) -> List[Dict[str, Any]]:
    """Resolve URLs from test list, HTML/XML sitemap, and RSS feeds."""
    logger = get_logger()
    urls: List[Dict[str, Any]] = []
    xs_kw: Dict[str, Any] = dict(
        last_run_timestamp=last_ts, local_files_cache=local_cache,
        enable_resume=enable_resume, vector_store_id=vs_id, vector_store_cache=vs_cache,
    )

    if not rss_only:
        if cfg.TEST_URLS:
            logger.info("TEST MODE: Using %d test URLs", len(cfg.TEST_URLS))
            lm_map.update(fetch_xml_sitemap())
            urls = process_test_urls(cfg.TEST_URLS, lm_map, last_ts, local_cache,
                                     enable_resume, vector_store_id=vs_id,
                                     vector_store_cache=vs_cache)
            logger.info("TEST MODE: %d test URLs to process", len(urls))
        elif xml_only:
            logger.info("XML-only mode: Skipping HTML sitemap")
            urls = _extract_xml_urls(lm_map, **xs_kw)
        elif cfg.SITEMAP_URL and cfg.SITEMAP_URL.strip():
            urls = _resolve_html_sitemap(cfg, args, lm_map, remove_sel, xs_kw)
        else:
            logger.info("No HTML sitemap URL, using XML-only")
            urls = _extract_xml_urls(lm_map, **xs_kw)
    else:
        logger.info("Skipping sitemap processing (RSS-only mode)")
        lm_map.update(fetch_xml_sitemap())

    # RSS feeds
    if not sitemap_only and not xml_only and cfg.RSS_FEEDS:
        logger.info("Processing RSS feeds")
        rss_urls = process_rss_feeds(lm_map, last_ts, local_cache, enable_resume)
        urls.extend(rss_urls)
        logger.info("Added %d URLs from RSS feeds", len(rss_urls))
    elif sitemap_only or xml_only:
        logger.info("Skipping RSS processing (%s mode)", "sitemap-only" if sitemap_only else "XML-only")
    else:
        logger.info("No RSS feeds configured, skipping")

    return _deduplicate_urls(urls)


def _resolve_html_sitemap(
    cfg: ScraperConfig, args: argparse.Namespace,
    lm_map: Dict[str, Any], remove_sel: str, xs_kw: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Fetch HTML sitemap, parse links, with XML/legacy fallback."""
    logger = get_logger()
    logger.info("Fetching HTML sitemap from %s", cfg.SITEMAP_URL)
    html = get_html_content_via_jina(cfg.SITEMAP_URL, remove_sel)

    if not html:
        logger.error("Failed to get HTML sitemap; falling back to XML-only")
        return _extract_xml_urls(lm_map, **xs_kw)

    lm_map.update(fetch_xml_sitemap())
    logger.info("Fetched last modified dates for %d URLs", len(lm_map))

    if getattr(args, "legacy_html_parsing", False):
        logger.info("Using legacy HTML sitemap parsing")
        menu = parse_menu(html)
        if menu:
            result = extract_links(menu, url_last_modified_map=lm_map, **xs_kw)
            logger.info("Found %d URLs from legacy parsing", len(result))
            return result
        logger.error("Legacy parsing failed, falling back to XML-only")
        return _extract_xml_urls(lm_map, **xs_kw)

    logger.info("Extracting URLs from HTML sitemap (<a> anchor tags)")
    result = extract_links_from_html_sitemap(html, url_last_modified_map=lm_map, **xs_kw)
    logger.info("Found %d URLs from HTML sitemap", len(result))

    if not result:
        logger.warning("No links from anchor parsing; trying legacy fallback")
        menu = parse_menu(html)
        if menu:
            result = extract_links(menu, url_last_modified_map=lm_map, **xs_kw)
            logger.info("Found %d URLs from legacy parsing", len(result))
        else:
            logger.warning("Legacy fallback also failed; using XML-only")
            result = _extract_xml_urls(lm_map, **xs_kw)
    return result


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
            best = sorted(enumerate(infos),
                          key=lambda x: (x[1].get("last_modified") or "", x[0]),
                          reverse=True)[0][1]
            deduped.append(best)
        else:
            deduped.append(infos[0])

    if dup_count:
        logger.warning("Removed %d duplicate URL entries", dup_count)
    logger.info("After deduplication: %d unique URLs", len(deduped))
    return deduped


def process_urls(
    urls: List[Dict[str, Any]], cfg: ScraperConfig,
    lm_map: Dict[str, Any], last_ts: Optional[datetime],
    local_cache: Optional[Dict[str, Any]], enable_resume: bool,
    vs_id: str, dedup: bool, chunking: Optional[Dict[str, Any]],
    vs_cache: Optional[Dict[str, Any]], remove_sel: str,
) -> Dict[str, Any]:
    """Process each URL with pagination detection. Returns stats dict."""
    logger = get_logger()
    total = len(urls)
    logger.info("Processing %d URLs with pagination detection", total)

    processed = success = skip_dup = 0
    seen: Set[str] = set()

    for info in urls:
        url, title, path = info["url"], info["title"], info["path"]
        if url in seen:
            skip_dup += 1
            logger.error("DUPLICATE in-run: %s (skipping)", url)
            continue

        logger.info("--- URL %d/%d: %s ---", processed + 1, total, url)

        rss_meta: Optional[Dict[str, Any]] = None
        rss_keys = {"published", "summary", "description", "source_feed", "event_metadata"}
        if any(k in info for k in rss_keys):
            rss_meta = {k: info.get(k) for k in rss_keys}
            logger.info("RSS meta: published=%s, source=%s",
                        rss_meta.get("published"), rss_meta.get("source_feed"))

        try:
            ok, _ = process_paginated_url(
                url, title, path, lm_map, last_ts, local_cache, enable_resume,
                vs_id, dedup, chunking, vs_cache, remove_sel, rss_meta, current_depth=0)
            success += ok
            if ok > 0:
                seen.add(url)
            else:
                logger.error("Failed to process: %s", url)
        except Exception as e:
            logger.error("Error processing URL %s: %s", url, e,
                         exc_info=logger.isEnabledFor(logging.DEBUG))

        processed += 1
        time.sleep(1)

    if skip_dup:
        logger.error("SAFETY NET: Prevented %d duplicate processing", skip_dup)
    else:
        logger.info("Safety net: %d unique URLs processed, no duplicates", len(seen))

    return {"processed_count": processed, "success_count": success,
            "skipped_duplicate_count": skip_dup, "unique_urls_processed": len(seen)}


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


def main(args: Optional[argparse.Namespace] = None) -> None:
    """Top-level orchestration: parse, configure, resolve, process, report."""
    # Step 1: Parse arguments (print OK -- logger not yet set up)
    if args is None:
        args = build_argument_parser().parse_args()

    config_file: str = args.config or "config.json"

    # Step 2: Load configuration
    try:
        cfg = load_configuration(config_file)
    except Exception as e:
        print(f"Error loading configuration: {e}")
        return

    # Validate mutually exclusive modes
    modes = [getattr(args, "rss_only", False), getattr(args, "sitemap_only", False),
             getattr(args, "xml_only", False)]
    if sum(bool(m) for m in modes) > 1:
        print("Error: --rss-only, --sitemap-only, and --xml-only are mutually exclusive")
        return

    # Step 3: Setup logging (last print() above, logger below)
    setup_logging(cfg)
    logger = get_logger()

    # Step 4: Apply CLI overrides
    apply_cli_overrides(args, cfg)

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
        return

    # Step 5: Startup
    start_time = datetime.now()
    logger.info("=== Starting %s at %s ===", cfg.SCRIPT_NAME, start_time)

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
        extracted = resolve_urls(cfg, args, lm_map, local_cache, vs_cache, enable_resume,
                                 last_ts, rss_only, sitemap_only, xml_only, remove_sel, vs_id)
        stats = process_urls(extracted, cfg, lm_map, last_ts, local_cache, enable_resume,
                             vs_id, dedup, chunking, vs_cache, remove_sel)

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
    except Exception as e:
        logger.error("Critical error in main: %s", e,
                     exc_info=logger.isEnabledFor(logging.DEBUG))
