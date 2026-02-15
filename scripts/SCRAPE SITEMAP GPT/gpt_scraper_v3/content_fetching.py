"""Content fetching module for GPT Scraper V3.

Jina AI, Firecrawl, and markdown content fetching.  Fixes applied:
C1 (partial) mutable default args, C3 debug-level response logging,
M3 no redundant re-imports, M5 type hints, M8 guarded content preview.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.utilities import get_session, is_url_blacklisted_by_path

logger = logging.getLogger(__name__)

_NONE3: Tuple[None, None, None] = (None, None, None)


def get_html_content_via_jina(
    url: str, remove_selectors: Optional[str] = None,
) -> Optional[str]:
    """Fetch HTML content via Jina AI API for sitemap processing."""
    cfg = get_config()
    api_url = cfg.MARKDOWN_PROVIDERS["jina"]["api_url_template"].format(url=url)
    headers: Dict[str, str] = {
        "Authorization": f"Bearer {cfg.JINA_AI_API_KEY}",
        "X-Return-Format": "html",
        "X-Engine": "browser",
        "X-Proxy": "auto",
    }
    selectors = remove_selectors or cfg.JINA_REMOVE_SELECTORS
    if selectors and selectors.strip():
        headers["X-Remove-Selector"] = selectors
        logger.debug("Using remove selectors for HTML: %s", selectors)
    logger.info("Fetching HTML content with links summary from: %s", url)
    try:
        response = get_session().get(api_url, headers=headers, timeout=cfg.REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.debug("Response preview: %.500s", response.text[:500])
        if response.status_code == 200 and response.text:
            logger.info("Successfully fetched HTML content from %s", url)
            logger.info("HTML content length: %d characters", len(response.text))
            return response.text
        logger.error("Jina API error for %s: HTTP Status %d", url, response.status_code)
        return None
    except requests.exceptions.RequestException as e:
        logger.error("Request error when fetching HTML from %s: %s", url, e)
        return None
    except Exception as e:
        logger.error("Unexpected error fetching HTML from %s: %s", url, e)
        return None


def get_html_content_for_pagination_via_jina(
    url: str, remove_selectors: Optional[str] = None,
) -> Optional[str]:
    """Fetch HTML content via Jina AI API for pagination detection."""
    cfg = get_config()
    api_url = cfg.MARKDOWN_PROVIDERS["jina"]["api_url_template"].format(url=url)
    headers: Dict[str, str] = {
        "Accept": "application/json",
        "Authorization": f"Bearer {cfg.JINA_AI_API_KEY}",
        "X-Engine": "browser",
        "X-Return-Format": "html",
    }
    selectors = remove_selectors or cfg.JINA_REMOVE_SELECTORS
    if selectors and selectors.strip():
        headers["X-Remove-Selector"] = selectors
        logger.debug("Using remove selectors for pagination HTML: %s", selectors)
    logger.info("PAGINATION CHECK: Fetching HTML for pagination detection from: %s", url)
    try:
        response = get_session().get(api_url, headers=headers, timeout=cfg.REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.debug("Response preview: %.500s", response.text[:500])
        if response.status_code == 200 and response.text:
            logger.info("Successfully fetched HTML for pagination check from %s", url)
            logger.info("HTML content length: %d characters", len(response.text))
            return response.text
        logger.error("Jina API error for pagination check %s: HTTP Status %d", url, response.status_code)
        return None
    except requests.exceptions.RequestException as e:
        logger.error("Request error when fetching HTML for pagination from %s: %s", url, e)
        return None
    except Exception as e:
        logger.error("Unexpected error fetching HTML for pagination from %s: %s", url, e)
        return None


def get_markdown_content(
    url: str, remove_selectors: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[Any], Optional[str]]:
    """Fetch markdown content using configured providers in sequence.

    Returns:
        ``(content, title, metadata, provider_used)`` or all-None tuple.
    """
    cfg = get_config()
    logger.info("Fetching markdown content from: %s", url)
    sequence_ids = [p.strip() for p in cfg.MARKDOWN_PROVIDER_SEQUENCE.split(",") if p.strip()]
    logger.info("Using provider sequence: %s", cfg.MARKDOWN_PROVIDER_SEQUENCE)
    if not sequence_ids:
        logger.error("Provider sequence is empty or invalid.")
        return None, None, None, None

    for provider_id in sequence_ids:
        prov_cfg = cfg.MARKDOWN_PROVIDERS.get(provider_id)
        if not prov_cfg:
            logger.warning("Provider ID '%s' not found in configuration. Skipping.", provider_id)
            continue
        provider_name: str = prov_cfg["name"]
        api_key: Optional[str] = prov_cfg.get("api_key")
        logger.info("Attempting to fetch content with provider: %s", provider_name)
        if prov_cfg.get("requires_api_key", True) and not api_key:
            logger.error("Missing API key for provider '%s'. Skipping.", provider_name)
            continue

        for retry_attempt in range(cfg.REQUEST_RETRY_COUNT + 1):
            if retry_attempt > 0:
                logger.warning(
                    "Retry attempt %d/%d for URL: %s with %s",
                    retry_attempt, cfg.REQUEST_RETRY_COUNT, url, provider_name,
                )
                backoff_time = cfg.REQUEST_BACKOFF_FACTOR * (2 ** (retry_attempt - 1))
                logger.info("Waiting %.2f seconds before retry...", backoff_time)
                time.sleep(backoff_time)
            try:
                content: Optional[str] = None
                title: Optional[str] = None
                metadata: Optional[Any] = None
                if provider_name == "jina":
                    content, title, metadata = _fetch_jina_markdown(url, api_key, remove_selectors)
                elif provider_name == "firecrawl":
                    content, title, metadata = _fetch_firecrawl_markdown(url, api_key)
                elif provider_name == "playwright":
                    from gpt_scraper_v3.playwright_provider import is_available, fetch_playwright_markdown
                    if not is_available():
                        logger.warning(
                            "Playwright is not installed. Skipping provider. "
                            "Install with: pip install playwright && playwright install chromium"
                        )
                        break  # Skip to next provider
                    content, title, metadata = fetch_playwright_markdown(url, remove_selectors)
                    # Playwright handles retries internally — break out of retry loop
                    if content:
                        logger.info("Successfully fetched markdown from %s using playwright", url)
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug("Content preview: %.500s", content[:500])
                        return content, title, metadata, provider_name
                    logger.error("Playwright returned no content for %s", url)
                    break  # Try next provider
                if content:
                    logger.info("Successfully fetched markdown from %s using %s", url, provider_name)
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("Content preview: %.500s", content[:500])
                    return content, title, metadata, provider_name
                logger.error("%s returned no content for %s", provider_name, url)
                break  # Try next provider
            except requests.exceptions.Timeout as e:
                logger.error("Timeout with %s for %s (attempt %d): %s", provider_name, url, retry_attempt + 1, e)
                if retry_attempt < cfg.REQUEST_RETRY_COUNT:
                    continue
                logger.error("Max retries reached for %s. Trying next provider.", provider_name)
                break
            except Exception as e:
                logger.error("Error with %s for %s: %s", provider_name, url, e)
                if retry_attempt < cfg.REQUEST_RETRY_COUNT:
                    continue
                logger.error("Max retries reached for %s. Trying next provider.", provider_name)
                break

    logger.error("Failed to fetch markdown content for %s from all providers.", url)
    return None, None, None, None


def _fetch_jina_markdown(
    url: str, api_key: str, remove_selectors: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """Fetch markdown via Jina AI with 5-strategy fallback.

    Strategies 1-2 use ``X-Engine: browser`` + ``X-Proxy: auto`` for
    JavaScript-rendered pages.  Strategy 3 drops those headers and uses
    Jina's default HTTP fetcher for markdown, which is significantly more
    reliable when the headless browser times out or returns empty content
    (common with Czech government sites like litomerice.cz,
    stredoceskykraj.cz, etc.).  Strategy 4 retries HTML via browser engine,
    and strategy 5 uses the default fetcher for HTML as a last resort.
    """
    cfg = get_config()
    api_url = cfg.MARKDOWN_PROVIDERS["jina"]["api_url_template"].format(url=url)

    # -- STRATEGY 1: markdown with browser engine + selectors --
    logger.info("STRATEGY 1: Attempting markdown fetch with selectors for %s", url)
    hdrs: Dict[str, str] = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Return-Format": "markdown",
        "X-Engine": "browser",
        "X-Proxy": "auto",
    }
    selectors = remove_selectors or cfg.JINA_REMOVE_SELECTORS
    if selectors and selectors.strip():
        hdrs["X-Remove-Selector"] = selectors
        logger.debug("Using remove selectors: %s", selectors)
    if cfg.JINA_TARGET_SELECTORS and cfg.JINA_TARGET_SELECTORS.strip():
        hdrs["X-Target-Selector"] = cfg.JINA_TARGET_SELECTORS
        logger.debug("Using target selectors: %s", cfg.JINA_TARGET_SELECTORS)

    try:
        response = get_session().get(api_url, headers=hdrs, timeout=cfg.REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.debug("Response preview: %.500s", response.text[:500])
        data = response.json()
        if response.status_code == 200 and data.get("data"):
            content = data["data"].get("content", "")
            title = data["data"].get("title", "")
            if content:
                logger.info("STRATEGY 1 SUCCESS: Fetched markdown with selectors")
                return content, title, data["data"]
            logger.warning("STRATEGY 1: Jina returned success but content is missing (tokens=%s)",
                           data["data"].get("usage", {}).get("tokens", "?"))
            logger.info("STRATEGY 1 empty response: %.500s", response.text[:500])
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 422:
            logger.warning("STRATEGY 1 FAILED: 422 with selectors - trying without selectors")
        else:
            logger.error("STRATEGY 1 FAILED: HTTP %s - %s", getattr(e.response, "status_code", "?"), e)
            raise
    except Exception as e:
        logger.error("STRATEGY 1 FAILED: %s", e)
        raise

    # -- STRATEGY 2: markdown with browser engine WITHOUT selectors --
    logger.info("STRATEGY 2: Attempting markdown fetch WITHOUT selectors for %s", url)
    hdrs_plain: Dict[str, str] = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Return-Format": "markdown",
        "X-Engine": "browser",
        "X-Proxy": "auto",
    }
    try:
        response = get_session().get(api_url, headers=hdrs_plain, timeout=cfg.REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.debug("Response preview: %.500s", response.text[:500])
        data = response.json()
        if response.status_code == 200 and data.get("data"):
            content = data["data"].get("content", "")
            title = data["data"].get("title", "")
            if content:
                logger.info("STRATEGY 2 SUCCESS: Fetched markdown without selectors")
                return content, title, data["data"]
            logger.warning("STRATEGY 2: Jina returned success but content is missing (tokens=%s)",
                           data["data"].get("usage", {}).get("tokens", "?"))
            logger.info("STRATEGY 2 empty response: %.500s", response.text[:500])
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 422:
            logger.warning("STRATEGY 2 FAILED: 422 without selectors - trying default HTTP fetcher")
        else:
            logger.error("STRATEGY 2 FAILED: HTTP %s - %s", getattr(e.response, "status_code", "?"), e)
            raise
    except Exception as e:
        logger.error("STRATEGY 2 FAILED: %s", e)
        raise

    # -- STRATEGY 3: markdown with DEFAULT HTTP fetcher (no browser engine) --
    # When browser engine returns empty content it usually means the headless
    # browser timed out or the proxy could not render the page.  Jina's default
    # HTTP fetcher is significantly more reliable for static/SSR sites.
    logger.info("STRATEGY 3: Attempting markdown fetch with DEFAULT HTTP fetcher (no browser engine) for %s", url)
    hdrs_default: Dict[str, str] = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Return-Format": "markdown",
    }
    try:
        response = get_session().get(api_url, headers=hdrs_default, timeout=cfg.REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.debug("Response preview: %.500s", response.text[:500])
        data = response.json()
        if response.status_code == 200 and data.get("data"):
            content = data["data"].get("content", "")
            title = data["data"].get("title", "")
            if content:
                logger.info("STRATEGY 3 SUCCESS: Fetched markdown with default HTTP fetcher")
                return content, title, data["data"]
            logger.warning("STRATEGY 3: Jina default fetcher returned success but content is missing")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 422:
            logger.warning("STRATEGY 3 FAILED: 422 with default fetcher - trying HTML fallbacks")
        else:
            logger.error("STRATEGY 3 FAILED: HTTP %s - %s", getattr(e.response, "status_code", "?"), e)
            raise
    except Exception as e:
        logger.error("STRATEGY 3 FAILED: %s", e)
        raise

    # -- STRATEGY 4: HTML with browser engine + text extraction --
    logger.info("STRATEGY 4: Fetching HTML (browser engine) and extracting text content for %s", url)
    hdrs_html: Dict[str, str] = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Return-Format": "html",
        "X-Engine": "browser",
        "X-Proxy": "auto",
    }
    try:
        response = get_session().get(api_url, headers=hdrs_html, timeout=cfg.REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.debug("Response status: %d, length: %d chars", response.status_code, len(response.text))
        data = response.json()
        if response.status_code == 200 and data.get("data"):
            html_content = data["data"].get("html", "")
            title = data["data"].get("title", "")
            if html_content:
                try:
                    from gpt_scraper_v3.utilities import html_to_plain_text
                    cleaned = html_to_plain_text(html_content)
                    if cleaned:
                        logger.info("STRATEGY 4 SUCCESS: Converted browser HTML to text (%d chars)", len(cleaned))
                        logger.warning("NOTE: Using HTML-to-text fallback (not true markdown)")
                        return cleaned, title, data["data"]
                    logger.warning("STRATEGY 4: Browser HTML conversion resulted in empty content")
                except Exception as e:
                    logger.error("STRATEGY 4: Browser HTML-to-text conversion failed: %s", e)
            else:
                logger.warning("STRATEGY 4: Jina browser returned HTML response but content is missing")
    except Exception as e:
        logger.error("STRATEGY 4 FAILED: %s", e)

    # -- STRATEGY 5: HTML with DEFAULT HTTP fetcher + text extraction --
    logger.info("STRATEGY 5: Fetching HTML (default fetcher) and extracting text content for %s", url)
    hdrs_html_default: Dict[str, str] = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Return-Format": "html",
    }
    try:
        response = get_session().get(api_url, headers=hdrs_html_default, timeout=cfg.REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.debug("Response status: %d, length: %d chars", response.status_code, len(response.text))
        data = response.json()
        if response.status_code == 200 and data.get("data"):
            html_content = data["data"].get("html", "")
            title = data["data"].get("title", "")
            if html_content:
                try:
                    from gpt_scraper_v3.utilities import html_to_plain_text
                    cleaned = html_to_plain_text(html_content)
                    if cleaned:
                        logger.info("STRATEGY 5 SUCCESS: Converted default-fetcher HTML to text (%d chars)", len(cleaned))
                        logger.warning("NOTE: Using HTML-to-text fallback with default fetcher (not true markdown)")
                        return cleaned, title, data["data"]
                    logger.error("STRATEGY 5: Default-fetcher HTML conversion resulted in empty content")
                    return _NONE3
                except Exception as e:
                    logger.error("STRATEGY 5: Default-fetcher HTML-to-text conversion failed: %s", e)
                    return _NONE3
            logger.error("STRATEGY 5: Jina default fetcher returned HTML response but content is missing")
            return _NONE3
        error_msg = data.get("error", {}).get("message", f"HTTP Status {response.status_code}")
        logger.error("STRATEGY 5 FAILED: Jina HTML error: %s", error_msg)
        return _NONE3
    except Exception as e:
        logger.error("STRATEGY 5 FAILED: %s", e)
        return _NONE3


def _fetch_firecrawl_markdown(
    url: str, api_key: str,
) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """Fetch markdown content using Firecrawl API."""
    cfg = get_config()
    api_url = "https://api.firecrawl.dev/v1/scrape"
    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload: Dict[str, Any] = {
        "url": url, "formats": ["markdown"],
        "onlyMainContent": True, "parsePDF": True, "maxAge": 14400000,
    }
    response = get_session().post(api_url, headers=headers, json=payload, timeout=cfg.REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if response.status_code == 200 and data.get("success") and data.get("data"):
        content = data["data"].get("markdown", "")
        fc_metadata: Dict[str, Any] = data["data"].get("metadata", {})
        title = fc_metadata.get("title", url.split("/")[-1])
        if content:
            return content, title, fc_metadata
        logger.error("Firecrawl returned success but markdown content is missing")
        return _NONE3
    error_message = data.get("error", f"HTTP Status {response.status_code} or success=false")
    logger.error("Firecrawl API error: %s", error_message)
    return _NONE3


def extract_links_from_jina_summary(
    jina_data: Dict[str, Any],
    url_last_modified_map: Optional[Dict[str, str]] = None,
    last_run_timestamp: Optional[datetime] = None,
    local_files_cache: Optional[Dict[str, Any]] = None,
    enable_resume: bool = False,
) -> List[Dict[str, Any]]:
    """Extract and process links from Jina AI links summary data.

    Args:
        jina_data: Full data response from Jina AI API.
        url_last_modified_map: URL-to-lastmod mapping (C1 fix: None sentinel).
        last_run_timestamp: Last run timestamp for filtering.
        local_files_cache: Cache of already processed URLs for resume.
        enable_resume: Whether resume functionality is enabled.

    Returns:
        List of extracted URL dicts with metadata.
    """
    if url_last_modified_map is None:
        url_last_modified_map = {}

    cfg = get_config()

    # Late import: these functions will be provided by the url_processing module
    from gpt_scraper_v3.url_processing import (  # noqa: E402
        find_url_last_modified,
        should_process_url_with_resume,
    )

    logger.info("Extracting links from Jina AI links summary (generalized approach)")
    extracted_urls: List[Dict[str, Any]] = []
    links_data: List[Any] = jina_data.get("links", [])

    if not links_data:
        logger.warning("No links found in Jina AI response")
        logger.debug("Available keys in jina_data: %s", list(jina_data.keys()))
        return extracted_urls

    logger.info("Processing %d links from Jina AI summary", len(links_data))
    if logger.isEnabledFor(logging.DEBUG) and links_data:
        logger.debug("Structure of first link: %s", type(links_data[0]))
        logger.debug("First link content: %s", links_data[0])
        if len(links_data) > 1:
            logger.debug("Second link content: %s", links_data[1])

    for i, link_info in enumerate(links_data, 1):
        try:
            if not isinstance(link_info, dict):
                logger.error("Link %d is not a dict but %s: %s", i, type(link_info), link_info)
                continue
            link_url: str = link_info.get("url", "").strip()
            text: str = link_info.get("text", "").strip()
            if not link_url:
                logger.debug("Skipping link %d: No URL found in %s", i, link_info)
                continue

            absolute_url = urljoin(cfg.BASE_URL, link_url)
            parsed_url = urlparse(absolute_url)
            if parsed_url.netloc not in (cfg.BASE_NETLOC, cfg.NON_WWW_BASE_NETLOC):
                logger.debug("Skipping external URL: %s", absolute_url)
                continue
            if absolute_url in cfg.BLACKLISTED_URLS or is_url_blacklisted_by_path(
                absolute_url, cfg.BLACKLISTED_RELATIVE_PATHS
            ):
                logger.info("URL %s is blacklisted. Skipping.", absolute_url)
                continue

            title = text if text else (parsed_url.path.split("/")[-1] or "Homepage")
            path = f"HTML Sitemap > {title}"
            logger.info("Processing URL %d/%d: %s", i, len(links_data), absolute_url)
            logger.info("Title: %s  Path: %s", title, path)

            last_modified = find_url_last_modified(absolute_url, url_last_modified_map)
            if should_process_url_with_resume(
                absolute_url, last_modified, last_run_timestamp,
                local_files_cache, enable_resume, rss_published_date=None,
            ):
                extracted_urls.append({
                    "url": absolute_url, "title": title,
                    "path": path, "jina_link_data": link_info,
                })
                logger.info("Added URL: %s", absolute_url)
            else:
                logger.info("Skipping URL %s (timestamp check or already processed).", absolute_url)
        except Exception as e:
            logger.error("Error processing link %d: %s", i, e)
            logger.error("Link data type: %s, content: %s", type(link_info), link_info)
            continue

    logger.info("Successfully extracted %d URLs from Jina AI links summary", len(extracted_urls))
    return extracted_urls
