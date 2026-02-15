"""HTML sitemap link extraction and deprecated menu parsing.

Migrated from scrape_sitemap_GPT_v2.py with the following fixes applied:
  - C1: Mutable default arguments replaced with None sentinel pattern
  - M5: Type hints on all public function signatures
"""
from __future__ import annotations

import logging
import warnings
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.utilities import is_url_blacklisted_by_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Primary HTML sitemap parser (active)
# ---------------------------------------------------------------------------


def extract_links_from_html_sitemap(
    html_content: str,
    url_last_modified_map: Optional[Dict[str, Any]] = None,
    last_run_timestamp: Optional[datetime] = None,
    local_files_cache: Optional[Dict[str, Any]] = None,
    enable_resume: bool = False,
    vector_store_id: Optional[str] = None,
    vector_store_cache: Optional[Dict[str, Any]] = None,
    previous_known_urls: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Extract links from an HTML sitemap by parsing ``<a>`` anchor tags.

    Parses the provided *html_content*, resolves relative URLs against the
    configured ``BASE_URL``, and filters out external, blacklisted, and
    file-type URLs.  Each qualifying URL is checked via
    ``should_process_url_with_resume`` before inclusion.

    Args:
        html_content: Raw HTML string of the sitemap page.
        url_last_modified_map: Mapping of URLs to their last-modified dates
            from the XML sitemap.  Defaults to an empty dict.
        last_run_timestamp: Timestamp of the previous scraper run.
        local_files_cache: Cache of already-processed URLs for resume.
        enable_resume: Whether resume functionality is enabled.
        vector_store_id: OpenAI vector store identifier.
        vector_store_cache: Cache of files already in the vector store.

    Returns:
        List of URL dicts, each containing ``url``, ``title``, ``path``,
        and ``last_modified`` keys.
    """
    # C1 fix: mutable default
    if url_last_modified_map is None:
        url_last_modified_map = {}

    # Late imports to avoid circular dependencies
    from gpt_scraper_v3.url_processing import (
        find_url_last_modified,
        should_process_url_with_resume,
    )

    cfg = get_config()
    logger.info("Extracting links from HTML sitemap by parsing <a> anchor tags")

    extracted_urls: List[Dict[str, Any]] = []

    if not html_content:
        logger.warning("No HTML content provided for link extraction")
        return extracted_urls

    # Content quality warning — early diagnostic for short/blocked pages
    content_length = len(html_content)
    logger.info("HTML sitemap content length: %d characters", content_length)
    if content_length < 200:
        logger.warning(
            "HTML content is suspiciously short (%d chars) — "
            "anchor extraction will likely find few or no links. "
            "This may indicate the page was not fully rendered or was blocked.",
            content_length,
        )

    try:
        soup = BeautifulSoup(html_content, "html.parser")
        anchor_tags = soup.find_all("a", href=True)

        logger.info("Found %d anchor tags with href attributes", len(anchor_tags))

        for i, anchor in enumerate(anchor_tags, 1):
            try:
                url = anchor.get("href", "").strip()
                text = anchor.get_text(strip=True) or anchor.get("title", "").strip()

                if not url:
                    logger.debug("Skipping anchor %d: No URL found", i)
                    continue

                absolute_url = urljoin(cfg.BASE_URL, url)

                # Domain filter
                parsed_url = urlparse(absolute_url)
                if parsed_url.netloc not in (cfg.BASE_NETLOC, cfg.NON_WWW_BASE_NETLOC):
                    logger.debug("Skipping external URL: %s", absolute_url)
                    continue

                # Blacklist filter
                if absolute_url in cfg.BLACKLISTED_URLS or is_url_blacklisted_by_path(
                    absolute_url, cfg.BLACKLISTED_RELATIVE_PATHS
                ):
                    logger.info("URL %s is blacklisted. Skipping.", absolute_url)
                    continue

                title = text if text else (parsed_url.path.split("/")[-1] or "Homepage")
                path = f"HTML Sitemap > {title}"

                logger.info(
                    "\n=== Processing URL %d/%d: %s ===",
                    i, len(anchor_tags), absolute_url,
                )
                logger.info("Title: %s", title)
                logger.info("Path: %s", path)

                last_modified = find_url_last_modified(absolute_url, url_last_modified_map)

                if should_process_url_with_resume(
                    absolute_url,
                    last_modified,
                    last_run_timestamp,
                    local_files_cache,
                    enable_resume,
                    rss_published_date=None,
                    vector_store_id=vector_store_id,
                    vector_store_cache=vector_store_cache,
                    previous_known_urls=previous_known_urls,
                ):
                    extracted_urls.append({
                        "url": absolute_url,
                        "title": title,
                        "path": path,
                        "last_modified": last_modified,
                    })
                else:
                    logger.info(
                        "Skipping URL %s (timestamp check or already processed locally).",
                        absolute_url,
                    )

            except Exception as e:
                logger.error("Error processing anchor tag %d: %s", i, e)
                continue

        logger.info(
            "Successfully extracted %d valid URLs from HTML sitemap",
            len(extracted_urls),
        )
        return extracted_urls

    except Exception as e:
        logger.error("Error parsing HTML sitemap: %s", e)
        return extracted_urls


# ---------------------------------------------------------------------------
# DEPRECATED legacy functions (preserved for backwards compatibility)
# ---------------------------------------------------------------------------


def parse_menu(html_content: str) -> Optional[Tag]:
    """DEPRECATED: Use extract_links_from_html_sitemap instead.

    Parses navigation menus from HTML content by trying a series of CSS
    selectors commonly found on government/CMS sitemap pages.

    Args:
        html_content: Raw HTML string to search for a menu structure.

    Returns:
        The first matching BeautifulSoup ``Tag``, or ``None`` if no menu
        element is found.
    """
    warnings.warn(
        "parse_menu is deprecated, use extract_links_from_html_sitemap instead",
        DeprecationWarning,
        stacklevel=2,
    )
    logger.warning(
        "Using DEPRECATED parse_menu function. "
        "Consider using extract_links_from_html_sitemap() instead."
    )

    soup = BeautifulSoup(html_content, "html.parser")

    selectors_to_try = [
        ".portlet-site-map ul",
        ".gov-container .portlet-site-map ul",
        ".portlet-body ul",
        ".gov-container ul",
        ".sitemap ul",
        "ul",
        ".gov-container",
    ]

    main_menu: Optional[Tag] = None
    for selector in selectors_to_try:
        main_menu = soup.select_one(selector)
        if main_menu:
            logger.info("Found sitemap menu using selector: %s", selector)
            break

    if not main_menu:
        logger.warning("Main menu not found using any selector")
        for tag in soup.find_all(["ul", "div"], limit=10):
            classes = tag.get("class", [])
            logger.info("  %s with classes: %s", tag.name, classes)

    return main_menu


def extract_links(
    menu_item: Tag,
    path: Optional[List[str]] = None,
    url_last_modified_map: Optional[Dict[str, Any]] = None,
    last_run_timestamp: Optional[datetime] = None,
    local_files_cache: Optional[Dict[str, Any]] = None,
    enable_resume: bool = False,
    vector_store_id: Optional[str] = None,
    vector_store_cache: Optional[Dict[str, Any]] = None,
    previous_known_urls: Optional[Set[str]] = None,
    _is_root_call: bool = True,
) -> List[Dict[str, Any]]:
    """DEPRECATED: Use extract_links_from_html_sitemap instead.

    Recursively extract links from a BeautifulSoup menu structure built of
    nested ``<ul>`` / ``<li>`` elements.

    The *path* parameter accumulates breadcrumbs as the function recurses
    into sub-menus.

    Args:
        menu_item: A BeautifulSoup ``Tag`` (typically ``<li>`` or ``<ul>``).
        path: Breadcrumb path segments accumulated during recursion.
            Defaults to an empty list.
        url_last_modified_map: Mapping of URLs to last-modified dates.
            Defaults to an empty dict.
        last_run_timestamp: Timestamp of the previous scraper run.
        local_files_cache: Cache of already-processed URLs for resume.
        enable_resume: Whether resume functionality is enabled.
        vector_store_id: OpenAI vector store identifier.
        vector_store_cache: Cache of files already in the vector store.
        _is_root_call: Internal flag; ``True`` on the first (non-recursive)
            call so the deprecation warning fires only once.

    Returns:
        List of URL dicts with ``url``, ``title``, and ``path`` keys.
    """
    # C1 fix: mutable defaults
    if path is None:
        path = []
    if url_last_modified_map is None:
        url_last_modified_map = {}

    # Only emit the deprecation warning on the top-level call, not on every
    # recursive invocation.
    if _is_root_call:
        warnings.warn(
            "extract_links is deprecated, use extract_links_from_html_sitemap instead",
            DeprecationWarning,
            stacklevel=2,
        )

    # Late imports to avoid circular dependencies
    from gpt_scraper_v3.url_processing import (
        find_url_last_modified,
        should_process_url_with_resume,
    )

    cfg = get_config()
    extracted_urls: List[Dict[str, Any]] = []

    if menu_item.name == "li":
        link_tag: Optional[Tag] = None
        link_text = ""

        selectors_to_try = [
            ("div.views-field span.field-content a", "div.views-field span.field-content"),
            ("a", ""),
            ("span a", "span"),
            ("div a", "div"),
        ]

        for link_selector, text_selector in selectors_to_try:
            link_tag = menu_item.select_one(link_selector)
            if link_tag:
                if text_selector:
                    link_text_element = menu_item.select_one(text_selector)
                    if link_text_element and link_text_element.text.strip():
                        link_text = link_text_element.text.strip()
                logger.debug("Found link using selector: %s", link_selector)
                break

        if not link_tag:
            link_tag = menu_item.find("a")

        # Extract text
        if link_tag and link_tag.text.strip():
            link_text = link_tag.text.strip()
        elif not link_text and menu_item.text.strip():
            link_text = menu_item.get_text(separator=" ", strip=True)
            if menu_item.find():
                direct_text = "".join(
                    menu_item.find_all(text=True, recursive=False)
                ).strip()
                if direct_text:
                    link_text = direct_text

        if link_text:
            current_path = path + [link_text]
            absolute_path = " > ".join(current_path)

            if link_tag and link_tag.has_attr("href"):
                absolute_url = urljoin(cfg.BASE_URL, link_tag["href"])
                logger.info("\n=== Processing URL: %s ===", absolute_url)
                logger.info("Path: %s", absolute_path)

                if absolute_url in cfg.BLACKLISTED_URLS or is_url_blacklisted_by_path(
                    absolute_url, cfg.BLACKLISTED_RELATIVE_PATHS
                ):
                    logger.info("URL %s is blacklisted. Skipping.", absolute_url)
                else:
                    last_modified = find_url_last_modified(
                        absolute_url, url_last_modified_map
                    )

                    if should_process_url_with_resume(
                        absolute_url,
                        last_modified,
                        last_run_timestamp,
                        local_files_cache,
                        enable_resume,
                        rss_published_date=None,
                        vector_store_id=vector_store_id,
                        vector_store_cache=vector_store_cache,
                        previous_known_urls=previous_known_urls,
                    ):
                        extracted_urls.append({
                            "url": absolute_url,
                            "title": link_text,
                            "path": absolute_path,
                        })
                    else:
                        logger.info(
                            "Skipping URL %s (timestamp check or already processed locally).",
                            absolute_url,
                        )

            # Recurse into sub-menu (pass _is_root_call=False to suppress
            # duplicate deprecation warnings)
            sub_menu = menu_item.find("ul", recursive=False)
            if sub_menu:
                extracted_urls.extend(
                    extract_links(
                        sub_menu,
                        current_path,
                        url_last_modified_map,
                        last_run_timestamp,
                        local_files_cache,
                        enable_resume,
                        vector_store_id,
                        vector_store_cache,
                        previous_known_urls=previous_known_urls,
                        _is_root_call=False,
                    )
                )

    elif menu_item.name == "ul":
        for item in menu_item.find_all("li", recursive=False):
            extracted_urls.extend(
                extract_links(
                    item,
                    path,
                    url_last_modified_map,
                    last_run_timestamp,
                    local_files_cache,
                    enable_resume,
                    vector_store_id=vector_store_id,
                    vector_store_cache=vector_store_cache,
                    previous_known_urls=previous_known_urls,
                    _is_root_call=False,
                )
            )

    return extracted_urls
