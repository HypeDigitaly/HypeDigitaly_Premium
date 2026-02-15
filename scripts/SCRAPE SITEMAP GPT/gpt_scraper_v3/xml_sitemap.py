"""XML sitemap fetching, timestamp management, and local file caching.

Migrated from scrape_sitemap_GPT_v2.py with the following fixes applied:
  - M1: All bare ``except:`` clauses replaced with specific exception types
  - M5: Type hints on all public function signatures
  - Config access via ``get_config()`` singleton (no module-level globals)
  - ``get_session()`` replaces ``requests_retry_session()``
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.utilities import get_session, is_url_blacklisted_by_path, normalize_url_query_params

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency: dateutil
# ---------------------------------------------------------------------------

try:
    import dateutil.parser as dateutil_parser
except ImportError:
    dateutil_parser = None  # type: ignore[assignment]


# ============================================================================
# XML SITEMAP FUNCTIONS
# ============================================================================


def parse_lastmod_date(date_str: str) -> Optional[datetime]:
    """Parse a lastmod date string from the sitemap into a datetime object.

    Tries several ISO-8601 / common date formats in order.  Returns *None*
    when none of them succeed.
    """
    formats = [
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ"),
        lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ"),
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S"),
        lambda s: datetime.strptime(s, "%Y-%m-%d"),
    ]

    for format_func in formats:
        try:
            return format_func(date_str)
        except (ValueError, TypeError):
            continue

    logger.warning("Could not parse lastmod date: %s", date_str)
    return None


def fetch_xml_sitemap() -> Dict[str, Optional[datetime]]:
    """Fetch the XML sitemap and parse URLs with their last modified dates.

    Handles both direct ``<url>`` entries and ``<sitemap>`` index files that
    reference sub-sitemaps (including page-based ``?page=N`` patterns).

    Returns:
        A mapping of URL string to its ``<lastmod>`` datetime (or *None* when
        no ``<lastmod>`` element was present).
    """
    cfg = get_config()
    logger.info("Fetching XML sitemap from %s", cfg.XML_SITEMAP_URL)
    url_last_modified: Dict[str, Optional[datetime]] = {}

    try:
        response = get_session().get(cfg.XML_SITEMAP_URL, timeout=cfg.REQUEST_TIMEOUT)
        response.raise_for_status()

        # -- Detect simple page-based sitemap index format -------------------
        if "page=1" in response.text or "page=2" in response.text:
            logger.info("Detected simple sitemap index format")

            sitemap_page_urls = re.findall(
                r"https://[^/]+/sitemap\.xml\?page=\d+", response.text,
            )
            if sitemap_page_urls:
                logger.info("Found %d sitemap page URLs", len(sitemap_page_urls))

                for page_url in sitemap_page_urls:
                    try:
                        page_response = get_session().get(
                            page_url, timeout=cfg.REQUEST_TIMEOUT,
                        )
                        page_response.raise_for_status()

                        # Match URLs WITH lastmod
                        url_matches = re.findall(
                            r"<url>.*?<loc>(.*?)</loc>.*?<lastmod>(.*?)</lastmod>.*?</url>",
                            page_response.text,
                            re.DOTALL,
                        )

                        for url, lastmod in url_matches:
                            last_modified = parse_lastmod_date(lastmod)
                            url_last_modified[url] = last_modified

                        # Also capture URLs WITHOUT lastmod (store None)
                        loc_only_matches = re.findall(
                            r"<url>.*?<loc>(.*?)</loc>.*?</url>",
                            page_response.text,
                            re.DOTALL,
                        )
                        without_lastmod = 0
                        for url in loc_only_matches:
                            if url not in url_last_modified:
                                url_last_modified[url] = None
                                without_lastmod += 1

                        page_total = len(url_matches) + without_lastmod
                        logger.info(
                            "Extracted %d URLs (%d with lastmod, %d without) from %s",
                            page_total, len(url_matches), without_lastmod, page_url,
                        )

                    except Exception as e:
                        logger.error(
                            "Error processing sitemap page %s: %s", page_url, e,
                        )

        # -- Try parsing with BeautifulSoup ----------------------------------
        # Import here so the module can still load without bs4 installed.
        try:
            from bs4 import BeautifulSoup  # type: ignore[import-untyped]
        except ImportError:
            logger.error(
                "BeautifulSoup (bs4) is not installed; "
                "XML sitemap parsing requires it.",
            )
            return url_last_modified

        for parser in ["lxml-xml", "html.parser", "lxml"]:
            try:
                soup = BeautifulSoup(response.text, parser)

                # -- Sitemap index format (<sitemap> entries) ----------------
                sitemaps = soup.find_all("sitemap")
                if sitemaps:
                    for sitemap in sitemaps:
                        loc = sitemap.find("loc")
                        if loc and loc.text:
                            sitemap_response = get_session().get(
                                loc.text, timeout=cfg.REQUEST_TIMEOUT,
                            )
                            sitemap_response.raise_for_status()

                            sitemap_soup = BeautifulSoup(
                                sitemap_response.text, parser,
                            )
                            for url_entry in sitemap_soup.find_all("url"):
                                loc_elem = url_entry.find("loc")
                                lastmod_elem = url_entry.find("lastmod")

                                if loc_elem and loc_elem.text:
                                    url_str: str = loc_elem.text
                                    last_modified: Optional[datetime] = None

                                    if lastmod_elem and lastmod_elem.text:
                                        last_modified = parse_lastmod_date(
                                            lastmod_elem.text,
                                        )

                                    # CRITICAL: Add ALL URLs, even without
                                    # lastmod (store None).
                                    url_last_modified[url_str] = last_modified
                    break

                # -- Direct URL entries (<url> elements) ---------------------
                url_entries = soup.find_all("url")
                if url_entries:
                    for url_entry in url_entries:
                        loc_elem = url_entry.find("loc")
                        lastmod_elem = url_entry.find("lastmod")

                        if loc_elem and loc_elem.text:
                            url_str = loc_elem.text
                            last_modified = None

                            if lastmod_elem and lastmod_elem.text:
                                last_modified = parse_lastmod_date(
                                    lastmod_elem.text,
                                )

                            # CRITICAL: Add ALL URLs, even without lastmod.
                            # This allows should_process_url_with_resume()
                            # to handle missing dates properly.
                            url_last_modified[url_str] = last_modified
                    break

            except Exception as e:
                logger.error("Error parsing with %s: %s", parser, e)

        logger.info(
            "Successfully extracted %d URLs with last modified dates",
            len(url_last_modified),
        )
        return url_last_modified

    except Exception as e:
        logger.error("Error fetching XML sitemap: %s", e)
        return {}


# ============================================================================
# URL EXTRACTION FROM SITEMAP DATA
# ============================================================================


def extract_urls_from_xml_sitemap(
    url_last_modified_map: Dict[str, Optional[datetime]],
    last_run_timestamp: Optional[datetime] = None,
    local_files_cache: Optional[Dict[str, Any] | Set[str]] = None,
    enable_resume: bool = False,
    vector_store_id: Optional[str] = None,
    vector_store_cache: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Extract URLs from XML sitemap data for processing.

    Iterates over the *url_last_modified_map* produced by
    :func:`fetch_xml_sitemap`, filters against the blacklist, and applies
    timestamp / resume checks via
    :func:`~gpt_scraper_v3.url_processing.should_process_url_with_resume`.

    Returns:
        A list of dicts, each with keys ``url``, ``title``, and ``path``.
    """
    # Import here to avoid circular imports at module level.
    from gpt_scraper_v3.url_processing import should_process_url_with_resume

    cfg = get_config()
    extracted_urls: List[Dict[str, Any]] = []

    logger.info("Extracting URLs from XML sitemap data")

    for url, last_modified in url_last_modified_map.items():
        logger.info("\n=== Processing URL: %s ===", url)

        # Check if URL is blacklisted (exact match or relative path match)
        if url in cfg.BLACKLISTED_URLS or is_url_blacklisted_by_path(
            url, cfg.BLACKLISTED_RELATIVE_PATHS
        ):
            logger.info("Skipping URL %s: blacklisted.", url)
            continue

        # Check if the URL should be processed
        if should_process_url_with_resume(
            url,
            last_modified,
            last_run_timestamp,
            local_files_cache,
            enable_resume,
            rss_published_date=None,
            vector_store_id=vector_store_id,
            vector_store_cache=vector_store_cache,
        ):
            # Extract title from URL path as fallback
            parsed_url = urlparse(url)
            path_parts = [
                part for part in parsed_url.path.split("/") if part
            ]
            title = path_parts[-1] if path_parts else "Homepage"

            # Create a basic path from URL structure
            path = (
                f"XML Sitemap > {' > '.join(path_parts)}"
                if path_parts
                else "XML Sitemap > Homepage"
            )

            extracted_urls.append({
                "url": url,
                "title": title,
                "path": path,
            })
            logger.info("Added URL from XML sitemap: %s", url)
        else:
            logger.info(
                "Skipping URL %s (timestamp check or already processed locally).",
                url,
            )

    logger.info("Extracted %d URLs from XML sitemap", len(extracted_urls))
    return extracted_urls


# ============================================================================
# TIMESTAMP FUNCTIONS
# ============================================================================


def _resolve_timestamp_file(timestamp_type: str) -> str:
    """Return the filesystem path for the given *timestamp_type*.

    Args:
        timestamp_type: One of ``"combined"``, ``"rss"``, or ``"sitemap"``.

    Returns:
        Absolute or relative path to the timestamp file.
    """
    cfg = get_config()
    if timestamp_type == "rss":
        return cfg.RSS_LAST_RUN_FILE
    if timestamp_type == "sitemap":
        return cfg.SITEMAP_LAST_RUN_FILE
    return cfg.LAST_RUN_FILE


def get_last_run_timestamp(
    timestamp_type: str = "combined",
) -> Optional[datetime]:
    """Get the timestamp of the last script run.

    Args:
        timestamp_type: ``"combined"``, ``"rss"``, or ``"sitemap"``.

    Returns:
        A timezone-aware datetime, or *None* if the file does not exist or
        cannot be parsed.
    """
    try:
        timestamp_file = _resolve_timestamp_file(timestamp_type)

        if os.path.exists(timestamp_file):
            with open(timestamp_file, "r", encoding="utf-8") as f:
                timestamp_str = f.read().strip()
                return datetime.fromisoformat(timestamp_str)
        return None
    except (ValueError, OSError, TypeError) as e:
        logger.error("Error reading %s timestamp: %s", timestamp_type, e)
        return None


def save_last_run_timestamp(timestamp_type: str = "combined") -> None:
    """Save the current timestamp as the last run timestamp.

    The timestamp is stored in the local timezone UTC+02:00 (Central European
    Summer Time) to match the V2 behaviour.

    Args:
        timestamp_type: ``"combined"``, ``"rss"``, or ``"sitemap"``.
    """
    try:
        timestamp_file = _resolve_timestamp_file(timestamp_type)

        with open(timestamp_file, "w", encoding="utf-8") as f:
            f.write(
                datetime.now()
                .astimezone(timezone(timedelta(hours=2)))
                .isoformat()
            )
        logger.info("Saved current UTC+02:00 timestamp to %s", timestamp_file)
    except (OSError, TypeError) as e:
        logger.error("Error saving %s timestamp: %s", timestamp_type, e)


# ============================================================================
# RESUME FUNCTIONALITY
# ============================================================================


def build_local_files_cache(
    output_dir: str,
) -> Dict[str, Dict[str, str]]:
    """Build a cache of already-processed URLs from local ``.txt`` files.

    Scans *output_dir* for ``.txt`` files and extracts the source URL from the
    metadata header using several format patterns (current, legacy, and
    broad fallback).

    Args:
        output_dir: Path to the directory containing scraped output files.

    Returns:
        A mapping of normalised URL to a dict with ``filepath`` and
        ``filename`` keys.
    """
    logger.info("Building local files cache from %s", output_dir)
    print("Building local files cache...")
    start_time = time.time()

    url_to_file_cache: Dict[str, Dict[str, str]] = {}
    processed_files: int = 0
    format_stats: Dict[str, int] = {
        "current_format": 0,
        "legacy_format": 0,
        "filename_based": 0,
        "no_url_found": 0,
    }

    if not os.path.exists(output_dir):
        logger.info("Output directory doesn't exist, no local files to cache")
        return {}

    # URL extraction patterns ordered by specificity (most specific first)
    url_patterns: List[str] = [
        # Current format
        r"## \*\*ZDROJOV\u00c1 URL:\*\*\s*\n### \*\*(.+?)\*\*",
        # Legacy formats
        r"ZDROJOV\u00c1 URL:\s*\n.*?(\bhttps?://[^\s\n]+)",
        r"Source URL:\s*(\bhttps?://[^\s\n]+)",
        r"URL:\s*(\bhttps?://[^\s\n]+)",
        # Very broad fallback: any HTTP URL in first 3 KB
        r"(\bhttps?://[^\s<>\"']+)",
    ]

    # Scan all .txt files in output directory
    for filename in os.listdir(output_dir):
        if not filename.endswith(".txt"):
            continue

        filepath = os.path.join(output_dir, filename)
        source_url: Optional[str] = None

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                # Read first 3 KB to find URL in metadata
                content = f.read(3000)

                # Try each pattern in order of specificity
                for i, pattern in enumerate(url_patterns):
                    url_matches = re.findall(
                        pattern, content, re.IGNORECASE | re.MULTILINE,
                    )

                    if url_matches:
                        # Take the first URL that passes basic validation
                        for url_candidate in url_matches:
                            url_candidate = url_candidate.strip().rstrip(".,;)")

                            if (
                                url_candidate.startswith(("http://", "https://"))
                                and len(url_candidate) > 10
                                and "." in url_candidate
                            ):
                                source_url = url_candidate

                                # Track which pattern succeeded
                                if i == 0:
                                    format_stats["current_format"] += 1
                                elif i < 4:
                                    format_stats["legacy_format"] += 1
                                else:
                                    format_stats["filename_based"] += 1
                                break

                        if source_url:
                            break

                # If we found a URL, add to cache
                if source_url:
                    normalized_source_url = normalize_url_query_params(source_url)
                    url_to_file_cache[normalized_source_url] = {
                        "filepath": filepath,
                        "filename": filename,
                    }
                    processed_files += 1
                    logger.debug(
                        "Found URL in %s: %s (Normalized: %s)",
                        filename,
                        source_url,
                        normalized_source_url,
                    )
                else:
                    format_stats["no_url_found"] += 1
                    logger.debug("No URL found in %s", filename)

        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Error reading file %s: %s", filepath, e)
            format_stats["no_url_found"] += 1
            continue

    elapsed_time = time.time() - start_time
    logger.info("Built local files cache in %.2fs", elapsed_time)
    logger.info(
        "Cache contains %d processed URLs from %d files",
        len(url_to_file_cache),
        processed_files,
    )

    # Log format statistics
    total_files = sum(format_stats.values())
    if total_files > 0:
        logger.info("File format breakdown:")
        logger.info("  Current format: %d files", format_stats["current_format"])
        logger.info("  Legacy format: %d files", format_stats["legacy_format"])
        logger.info("  Filename-based: %d files", format_stats["filename_based"])
        logger.info("  No URL found: %d files", format_stats["no_url_found"])

        print(
            "Local cache built: %d URLs indexed in %.1fs"
            % (len(url_to_file_cache), elapsed_time),
        )
        print(
            "Format compatibility: %d/%d files had extractable URLs"
            % (processed_files, total_files),
        )

    return url_to_file_cache
