"""URL processing module for GPT Scraper V3.

URL filtering, resume logic, eligibility checking, and last-modified matching.
Migrated from scrape_sitemap_GPT_v2.py with fixes: M5 (type hints), M3 (no
redundant local imports), M6 (eliminated getattr(globals()) pattern).
"""
from __future__ import annotations

import email.utils
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.utilities import is_file_url, is_url_blacklisted_by_path, normalize_url_query_params
from gpt_scraper_v3.vector_store import find_existing_file_by_url_cached, find_existing_files_by_url_cached

logger = logging.getLogger(__name__)

try:
    from dateutil import parser as dateutil_parser
except ImportError:
    dateutil_parser = None  # type: ignore[assignment]


def _status(title: str, lines: List[str]) -> None:
    """Print a bordered status block to stdout (preserves V2 console output)."""
    print(f"\n=== {title} ===")
    for line in lines:
        print(line)
    print("=" * (len(title) + 8) + "\n")


# -- Local resume cache check ------------------------------------------------

def is_url_already_processed_locally(url: str, local_cache: Set[str] | Dict[str, Any]) -> bool:
    """Check if URL has already been processed based on local files cache."""
    return normalize_url_query_params(url) in local_cache


# -- Test URL checks ----------------------------------------------------------

def is_url_from_test_urls(url: str) -> bool:
    """Check if a URL is from the test_urls array in config."""
    cfg = get_config()
    if not cfg.TEST_URLS:
        return False
    normalized = url.rstrip("/")
    return any(normalized == t.rstrip("/") for t in cfg.TEST_URLS)


def is_url_suburl_of_test_recursive_url(url: str) -> bool:
    """Check if *url* is a sub-URL of a URL in BOTH TEST_URLS and RECURSIVE_URLS."""
    cfg = get_config()
    if not cfg.TEST_URLS or not cfg.RECURSIVE_URLS:
        return False
    normalized = url.rstrip("/")
    test_recursive: List[str] = []
    for test_url in cfg.TEST_URLS:
        norm_test = test_url.rstrip("/")
        for rc in cfg.RECURSIVE_URLS:
            rc_url = rc if isinstance(rc, str) else rc.get("url", "")
            if norm_test == rc_url.rstrip("/"):
                test_recursive.append(norm_test)
                break
    for parent in test_recursive:
        if normalized.startswith(parent + "/"):
            logger.info(f"SUBURL OF TEST+RECURSIVE URL: {url} is a suburl of {parent}")
            logger.info("   This suburl will BYPASS last modified check (inherited from TEST_URL parent)")
            return True
    return False


# -- URL last-modified matching -----------------------------------------------

def _find_domain_variation_match(normalized_url: str, lm_map: Dict[str, Any]) -> Optional[str]:
    parsed = urlparse(normalized_url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        alt_netloc = domain[4:]
    else:
        alt_netloc = f"www.{domain}"
    alt = urlunparse(parsed._replace(netloc=alt_netloc))
    return alt if alt in lm_map else None


def _find_path_based_match(input_path: str, input_domain_nw: str, lm_map: Dict[str, Any]) -> Optional[str]:
    for surl in lm_map:
        try:
            p = urlparse(surl)
            sd = p.netloc.lower()
            sd_nw = sd[4:] if sd.startswith("www.") else sd
            if sd_nw == input_domain_nw and p.path.lower() == input_path:
                return surl
        except Exception:
            continue
    return None


def _find_flexible_substring_match(
    normalized_url: str, input_domain_nw: str, input_path: str, lm_map: Dict[str, Any],
) -> Optional[str]:
    best, best_score = None, 0
    for surl in lm_map:
        try:
            p = urlparse(surl)
            sd = p.netloc.lower()
            sd_nw = sd[4:] if sd.startswith("www.") else sd
            sp = p.path.lower()
            score = 0
            if sd_nw == input_domain_nw:
                score += 100
            elif input_domain_nw in sd_nw or sd_nw in input_domain_nw:
                score += 50
            if sp == input_path:
                score += 200
            elif input_path in sp:
                score += 100
            elif sp in input_path:
                score += 80
            else:
                common = set(s for s in input_path.split("/") if s) & set(s for s in sp.split("/") if s)
                score += len(common) * 10
            if score > best_score and score >= 150:
                best_score, best = score, surl
        except Exception:
            continue
    return best


def _find_legacy_substring_match(normalized_url: str, lm_map: Dict[str, Any]) -> Optional[str]:
    matches = [s for s in lm_map if normalized_url in s or s in normalized_url]
    return matches[0] if matches else None


_STRATEGY_NAMES = [
    "Exact match", "Normalized exact match", "Domain variation match",
    "Path-based match", "Flexible substring match", "Legacy substring match",
]


def find_url_last_modified(url: str, url_last_modified_map: Dict[str, Any]) -> Optional[datetime]:
    """Find the last modified date for *url* using multi-strategy matching."""
    cfg = get_config()
    logger.debug(f"Finding last modified date for URL: {url}")
    norm = url.rstrip("/")
    pi = urlparse(norm)
    idom = pi.netloc.lower()
    ipath = pi.path.lower()
    idom_nw = idom[4:] if idom.startswith("www.") else idom
    lm = url_last_modified_map

    strategies = [
        lambda: url if url in lm else None,
        lambda: norm if norm in lm else None,
        lambda: _find_domain_variation_match(norm, lm),
        lambda: _find_path_based_match(ipath, idom_nw, lm),
        lambda: _find_flexible_substring_match(norm, idom_nw, ipath, lm),
        lambda: _find_legacy_substring_match(norm, lm),
    ]
    matched, last_modified = None, None
    for i, strat in enumerate(strategies, 1):
        try:
            result = strat()
            if result:
                matched = result
                last_modified = lm.get(matched)
                logger.info(f"URL matched using strategy {i}: {matched}")
                break
        except Exception as e:
            logger.debug(f"Strategy {i} failed: {e}")
    if logger.isEnabledFor(logging.DEBUG) or not matched or cfg.VERBOSE_URL_MATCHING:
        print(f"\n=== URL MATCHING ===")
        print(f"SITEMAP_URL: {url}")
        if matched:
            print(f"XML_SITEMAP_URL: {matched}")
            print(f"Last modified date: {last_modified}")
            for j, s in enumerate(strategies, 1):
                try:
                    if s() == matched:
                        print(f"Matching strategy: {j}. {_STRATEGY_NAMES[j - 1]}")
                        break
                except Exception:
                    continue
        else:
            print("XML_SITEMAP_URL: No matching URL found")
            print(f"Available XML URLs count: {len(lm)}")
            if lm:
                print(f"Sample XML URLs: {list(lm.keys())[:3]}")
        print("===================\n")
    return last_modified


# -- RSS URL eligibility ------------------------------------------------------

def should_process_rss_url(
    url: str, rss_published_date: Any, rss_last_run_timestamp: Optional[datetime],
    xml_last_modified: Optional[datetime], sitemap_last_run_timestamp: Optional[datetime],
    local_cache: Optional[Dict[str, Any] | Set[str]] = None,
    vector_store_id: Optional[str] = None,
    vector_store_cache: Optional[Dict[str, Any]] = None,
) -> bool:
    """Decide whether an RSS URL should be processed."""
    cfg = get_config()
    logger.debug(f"Checking if RSS URL should be processed: {url}")

    # Step 1: Parse RSS published date
    pub: Optional[datetime] = None
    if rss_published_date:
        if isinstance(rss_published_date, str):
            try:
                if dateutil_parser:
                    pub = dateutil_parser.parse(rss_published_date)
                else:
                    try:
                        pub = datetime.fromtimestamp(
                            email.utils.mktime_tz(email.utils.parsedate_tz(rss_published_date)))  # type: ignore[arg-type]
                    except Exception:
                        logger.warning(f"Could not parse RSS published date: {rss_published_date}")
                        return True
            except Exception as e:
                logger.warning(f"Error parsing RSS published date {rss_published_date}: {e}")
                return True
        else:
            pub = rss_published_date

    if pub is None:
        logger.info(f"No RSS published date found for {url}. Processing to be safe.")
        _status("RSS URL PROCESSING STATUS", [
            f"URL: {url}", f"RSS Published: Unknown",
            f"Last Run: {rss_last_run_timestamp}", "Processing: WILL PROCEED (safety)"])
        return True

    pub_utc = pub.replace(tzinfo=timezone.utc) if pub.tzinfo is None else pub.astimezone(timezone.utc)

    # Step 2: RSS_DATE_THRESHOLD
    if cfg.RSS_DATE_THRESHOLD:
        if pub_utc < cfg.RSS_DATE_THRESHOLD:
            logger.info(f"RSS URL {url} published {pub_utc} BEFORE threshold {cfg.RSS_DATE_THRESHOLD}. Skipping.")
            _status("RSS URL PROCESSING STATUS (THRESHOLD)", [
                f"URL: {url}", f"RSS Published: {pub}",
                f"Date Threshold: {cfg.RSS_DATE_THRESHOLD.strftime('%d-%m-%Y %H:%M:%S UTC')}",
                "Processing: SKIPPED (BELOW THRESHOLD)"])
            return False
        logger.info(f"RSS URL {url} passed RSS Date Threshold check.")

    # Step 3: RSS last run timestamp  (0=off, 1=on, 2=partial)
    if cfg.CHECK_LAST_MODIFIED == 0:
        logger.info(f"CHECK_LAST_MODIFIED is 0 (off). Processing RSS URL {url}")
        _status("RSS URL PROCESSING STATUS", [
            f"URL: {url}", "CHECK_LAST_MODIFIED: 0 (off)", "Processing: WILL PROCEED"])
        return True

    if rss_last_run_timestamp:
        is_newer = pub_utc > rss_last_run_timestamp
        _status("RSS URL PROCESSING STATUS (RSS LAST RUN)", [
            f"URL: {url}", f"RSS Published: {pub}", f"RSS Last Run: {rss_last_run_timestamp}",
            f"Is Published Since RSS Last Run: {'YES' if is_newer else 'NO'}",
            f"Processing: {'PASSED RSS TIMESTAMP CHECK' if is_newer else 'SKIPPED (RSS TIMESTAMP)'}"])
        if not is_newer:
            logger.info(f"RSS URL {url} was published before RSS last run. Skipping.")
            return False
        logger.info(f"RSS URL {url} was published after RSS last run. Proceeding to XML check.")
    else:
        logger.info("No RSS last run timestamp found. Proceeding to XML check.")

    # Step 4: XML sitemap last modified
    if xml_last_modified is None:
        # Mode 2 (partial): always process URLs without XML lastmod
        if cfg.CHECK_LAST_MODIFIED == 2:
            logger.info(f"No XML lastmod for {url}. Processing (PARTIAL MODE).")
            _status("RSS URL PROCESSING STATUS (MISSING XML LASTMOD - PARTIAL)", [
                f"URL: {url}", "XML Last Modified: Unknown", f"Sitemap Last Run: {sitemap_last_run_timestamp}",
                "CHECK_LAST_MODIFIED: 2 (partial)",
                "Processing: WILL PROCEED (Partial mode - missing lastmod always processed)"])
            return True
        # Mode 1: skip if exists locally or in vector store
        loc = is_url_already_processed_locally(url, local_cache) if local_cache is not None else False
        vs = bool(vector_store_id and vector_store_cache is not None
                   and find_existing_files_by_url_cached(url, vector_store_cache))
        if loc or vs:
            logger.info(f"No XML lastmod for {url}. Skipping (local={loc}, VS={vs}).")
            _status("RSS URL PROCESSING STATUS (MISSING XML LASTMOD)", [
                f"URL: {url}", "XML Last Modified: Unknown", f"Sitemap Last Run: {sitemap_last_run_timestamp}",
                "Local/Vector Store Exists: YES", "Processing: SKIPPED (Missing XML lastmod + Exists)"])
            return False
        logger.info(f"No XML lastmod for {url}. Processing (NEW URL).")
        _status("RSS URL PROCESSING STATUS (MISSING XML LASTMOD)", [
            f"URL: {url}", "XML Last Modified: Unknown", f"Sitemap Last Run: {sitemap_last_run_timestamp}",
            "Local/Vector Store Exists: NO", "Processing: WILL PROCEED (Missing XML lastmod + New URL)"])
        return True

    if sitemap_last_run_timestamp is None:
        logger.info("No Sitemap last run timestamp found. Processing based on RSS checks only.")
        return True

    xml_utc = (xml_last_modified.replace(tzinfo=timezone.utc) if xml_last_modified.tzinfo is None
               else xml_last_modified.astimezone(timezone.utc))
    is_mod = xml_utc > sitemap_last_run_timestamp
    _status("RSS URL PROCESSING STATUS (XML LAST MOD)", [
        f"URL: {url}", f"XML Last Modified: {xml_last_modified}",
        f"Sitemap Last Run: {sitemap_last_run_timestamp}",
        f"Is Modified Since Sitemap Last Run: {'YES' if is_mod else 'NO'}",
        f"Processing: {'WILL PROCEED' if is_mod else 'SKIPPED'}"])
    if is_mod:
        logger.info(f"RSS URL {url} XML lastmod newer than Sitemap last run. Processing.")
    else:
        logger.info(f"RSS URL {url} XML lastmod NOT newer than Sitemap last run. Skipping.")
    return is_mod


# -- Main URL eligibility function -------------------------------------------

def should_process_url_with_resume(
    url: str, last_modified: Optional[datetime], last_run_timestamp: Optional[datetime],
    local_cache: Optional[Dict[str, Any] | Set[str]] = None, enable_resume: bool = False,
    rss_published_date: Any = None, sitemap_last_run_timestamp: Optional[datetime] = None,
    vector_store_id: Optional[str] = None, vector_store_cache: Optional[Dict[str, Any]] = None,
) -> bool:
    """Decide whether *url* should be scraped (main eligibility entry point)."""
    cfg = get_config()
    logger.debug(f"Checking if URL should be processed (with resume): {url}")

    # Skip BASE_URL / domain itself
    pu = urlparse(url)
    norm_base = urlunparse((pu.scheme, pu.netloc, pu.path, "", "", ""))
    if norm_base in cfg.CANONICAL_BASE_URLS:
        if cfg.PROCESS_BASE_URL_ONCE:
            if cfg.BASE_URL_PROCESSED_IN_RUN:
                logger.info(f"Skipping URL {url}: Base URL already processed once.")
                _status("URL PROCESSING STATUS (BASE URL)", [
                    f"URL: {url}", "Processing: SKIPPED (BASE URL ALREADY PROCESSED)"])
                return False
            cfg.BASE_URL_PROCESSED_IN_RUN = True
            logger.info(f"IncludeBaseURL=1 - allowing BASE URL processing once: {url}")
            _status("URL PROCESSING STATUS (BASE URL)", [
                f"URL: {url}", "IncludeBaseURL: ENABLED (processing once)",
                "Processing: CONTINUES (BASE URL ALLOWED)"])
        else:
            logger.info(f"Skipping URL {url}: Detected as BASE_URL/domain itself.")
            _status("URL PROCESSING STATUS (BASE URL)", [
                f"URL: {url}", "Processing: SKIPPED (BASE URL)"])
            return False

    # Skip file URLs
    if is_file_url(url):
        logger.info(f"Skipping URL {url}: Detected as file URL.")
        _status("URL PROCESSING STATUS (FILE URL)", [
            f"URL: {url}", "Processing: SKIPPED (FILE URL)"])
        return False

    # Test URLs bypass last modified check
    if is_url_from_test_urls(url):
        logger.info(f"TEST URL DETECTED: {url} - BYPASSING last modified check")
        _status("TEST URL PROCESSING STATUS", [
            f"URL: {url}", "Source: test_urls array in config",
            "Last modified check: BYPASSED", "Processing: WILL PROCEED (TEST URL)"])
        if enable_resume and local_cache is not None:
            if is_url_already_processed_locally(url, local_cache):
                logger.info(f"URL {url} already processed locally. Skipping (RESUME).")
                _status("TEST URL PROCESSING STATUS (RESUME)", [
                    f"URL: {url}", "Local file exists: YES", "Processing: SKIPPED (RESUME)"])
                return False
            logger.info(f"Test URL {url} not found in local cache. Will process.")
        return True

    # Sub-URLs of TEST+RECURSIVE URLs
    if is_url_suburl_of_test_recursive_url(url):
        logger.info(f"SUBURL OF TEST+RECURSIVE URL DETECTED: {url} - BYPASSING last modified check")
        _status("SUBURL OF TEST+RECURSIVE URL PROCESSING STATUS", [
            f"URL: {url}", "Source: Suburl of TEST+RECURSIVE URL",
            "Last modified check: BYPASSED (inherited from parent)",
            "Processing: WILL PROCEED (TEST+RECURSIVE SUBURL)"])
        if enable_resume and local_cache is not None:
            if is_url_already_processed_locally(url, local_cache):
                logger.info(f"URL {url} already processed locally. Skipping (RESUME).")
                _status("SUBURL PROCESSING STATUS (RESUME)", [
                    f"URL: {url}", "Local file exists: YES", "Processing: SKIPPED (RESUME)"])
                return False
            logger.info(f"Suburl of test+recursive URL {url} not found in local cache. Will process.")
        return True

    # Step 1: Local resume cache
    if enable_resume and local_cache is not None:
        if is_url_already_processed_locally(url, local_cache):
            logger.info(f"URL {url} already processed locally. Skipping (RESUME).")
            _status("URL PROCESSING STATUS (RESUME)", [
                f"URL: {url}", "Local file exists: YES", "Processing: SKIPPED (RESUME)"])
            return False
        logger.info(f"URL {url} not found in local cache. Will process.")

    # Step 2: RSS URLs
    if rss_published_date is not None:
        return should_process_rss_url(
            url, rss_published_date, last_run_timestamp, last_modified,
            sitemap_last_run_timestamp, local_cache, vector_store_id, vector_store_cache)

    # Step 3: Timestamp-based logic  (0=off, 1=on, 2=partial)
    if cfg.CHECK_LAST_MODIFIED == 0:
        logger.info(f"CHECK_LAST_MODIFIED is 0 (off). Processing {url}")
        _status("URL PROCESSING STATUS", [
            f"URL: {url}", "CHECK_LAST_MODIFIED: 0 (off)", "Processing: WILL PROCEED"])
        return True

    if not last_run_timestamp:
        logger.info(f"No last run timestamp. Processing {url}")
        _status("URL PROCESSING STATUS", [
            f"URL: {url}", "Last Run Timestamp: None (first run)", "Processing: WILL PROCEED"])
        return True

    # Handle missing last_modified
    if last_modified is None:
        # Mode 2 (partial): always process URLs without lastmod
        if cfg.CHECK_LAST_MODIFIED == 2:
            logger.info(f"No lastmod for {url}. Processing (PARTIAL MODE - always process missing lastmod).")
            _status("URL PROCESSING STATUS (MISSING LASTMOD - PARTIAL)", [
                f"URL: {url}", "Last Modified: Unknown", f"Last Run: {last_run_timestamp}",
                "CHECK_LAST_MODIFIED: 2 (partial)",
                "Processing: WILL PROCEED (Partial mode - missing lastmod always processed)"])
            return True
        # Mode 1: skip if exists locally or in vector store
        loc = is_url_already_processed_locally(url, local_cache) if local_cache is not None else False
        vs = bool(vector_store_id and vector_store_cache is not None
                   and find_existing_files_by_url_cached(url, vector_store_cache))
        if loc or vs:
            logger.info(f"No lastmod for {url}. Skipping (local={loc}, VS={vs}).")
            _status("URL PROCESSING STATUS (MISSING LASTMOD)", [
                f"URL: {url}", "Last Modified: Unknown", f"Last Run: {last_run_timestamp}",
                "Local/Vector Store Exists: YES", "Processing: SKIPPED (Missing lastmod + Exists)"])
            return False
        logger.info(f"No lastmod for {url}. Processing (NEW).")
        _status("URL PROCESSING STATUS (MISSING LASTMOD)", [
            f"URL: {url}", "Last Modified: Unknown", f"Last Run: {last_run_timestamp}",
            "Local/Vector Store Exists: NO", "Processing: WILL PROCEED (Missing lastmod + New URL)"])
        return True

    # Compare last_modified against last_run_timestamp
    lm_utc = (last_modified.replace(tzinfo=timezone.utc) if last_modified.tzinfo is None
              else last_modified.astimezone(timezone.utc))
    is_modified = lm_utc > last_run_timestamp
    _status("URL PROCESSING STATUS", [
        f"URL: {url}", f"Last Modified: {last_modified}", f"Last Run: {last_run_timestamp}",
        f"Is Modified Since Last Run: {'YES' if is_modified else 'NO'}",
        f"Processing: {'WILL PROCEED' if is_modified else 'SKIPPED'}"])
    logger.info(f"URL {url} {'has' if is_modified else 'has NOT'} been modified. "
                f"{'Processing' if is_modified else 'Skipping'}.")
    return is_modified


# -- Test URL validation and processing ---------------------------------------

def validate_test_urls(test_urls: List[str]) -> Tuple[List[str], List[str]]:
    """Validate test URLs for proper format and domain compatibility."""
    cfg = get_config()
    logger.info(f"Validating {len(test_urls)} test URLs")
    valid: List[str] = []
    errors: List[str] = []
    for i, url in enumerate(test_urls, 1):
        try:
            if not url or not url.strip():
                errors.append(f"URL {i}: Empty URL"); continue
            url = url.strip()
            if not url.startswith(("http://", "https://")):
                errors.append(f"URL {i}: Missing protocol (http:// or https://): {url}"); continue
            try:
                p = urlparse(url)
                if not p.netloc:
                    errors.append(f"URL {i}: Invalid domain: {url}"); continue
                if p.netloc not in (cfg.BASE_NETLOC, cfg.NON_WWW_BASE_NETLOC):
                    logger.warning(f"URL {i} is from different domain ({p.netloc}) than base ({cfg.BASE_NETLOC})")
                if url in cfg.BLACKLISTED_URLS or is_url_blacklisted_by_path(url, cfg.BLACKLISTED_RELATIVE_PATHS):
                    errors.append(f"URL {i}: URL is blacklisted: {url}"); continue
                valid.append(url)
                logger.info(f"Valid test URL {i}: {url}")
            except Exception as e:
                errors.append(f"URL {i}: Failed to parse URL '{url}': {e}")
        except Exception as e:
            errors.append(f"URL {i}: Validation error: {e}")
    logger.info(f"Test URL validation complete: {len(valid)} valid, {len(errors)} errors")
    return valid, errors


def process_test_urls(
    test_urls: List[str], url_last_modified_map: Optional[Dict[str, Any]] = None,
    last_run_timestamp: Optional[datetime] = None,
    local_files_cache: Optional[Dict[str, Any] | Set[str]] = None,
    enable_resume: bool = False, vector_store_id: Optional[str] = None,
    vector_store_cache: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Process test URLs for testing purposes, overriding HTML sitemap processing."""
    cfg = get_config()
    lm_map = url_last_modified_map or {}
    logger.info(f"TEST MODE: Processing {len(test_urls)} test URLs (overriding HTML sitemap)")
    print(f"TEST MODE ACTIVATED: Processing {len(test_urls)} test URLs")
    print("HTML sitemap processing is BYPASSED")

    valid_urls, validation_errors = validate_test_urls(test_urls)
    if validation_errors:
        logger.warning(f"Test URL validation found {len(validation_errors)} errors:")
        print("Test URL validation errors:")
        for err in validation_errors:
            logger.warning(f"  - {err}")
            print(f"   {err}")
    if not valid_urls:
        logger.error("No valid test URLs found after validation")
        print("No valid test URLs to process")
        return []

    logger.info(f"Processing {len(valid_urls)} valid test URLs")
    print(f"Processing {len(valid_urls)} valid test URLs")
    extracted: List[Dict[str, Any]] = []
    for i, url in enumerate(valid_urls, 1):
        try:
            abs_url = urljoin(cfg.BASE_URL, url)
            parts = [p for p in urlparse(abs_url).path.split("/") if p]
            title = parts[-1] if parts else "Homepage"
            path = f"TEST URL {i}/{len(valid_urls)} > {title}"
            logger.info(f"\n=== Processing Test URL {i}/{len(valid_urls)}: {abs_url} ===")
            logger.info(f"Title: {title}"); logger.info(f"Path: {path}")
            lm = find_url_last_modified(abs_url, lm_map)
            if should_process_url_with_resume(
                abs_url, lm, last_run_timestamp, local_files_cache, enable_resume,
                rss_published_date=None, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache,
            ):
                extracted.append({"url": abs_url, "title": title, "path": path, "last_modified": lm, "test_url": True})
                logger.info(f"Added test URL: {abs_url}")
            else:
                logger.info(f"Skipping test URL {abs_url} (timestamp check or already processed locally).")
        except Exception as e:
            logger.error(f"Error processing test URL {i} ({url}): {e}")
    logger.info(f"TEST MODE: Successfully prepared {len(extracted)} test URLs for processing")
    print(f"Test URL preparation complete: {len(extracted)} URLs ready for processing")
    return extracted
