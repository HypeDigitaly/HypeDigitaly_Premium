"""AI-powered and legacy pagination detection for GPT Scraper V3.

Migrated from scrape_sitemap_GPT_v2.py (lines 3772-4446) with:
  - C3: Response logging at DEBUG level, truncated to 500 chars
  - M5: Type hints on all public function signatures
  - H3: OpenRouter boilerplate replaced with _call_openrouter()
  - Global variable access replaced with get_config()
  - requests_retry_session() replaced with get_session()
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from gpt_scraper_v3.config import get_config, sanitize_json_response
from gpt_scraper_v3.openrouter_client import _call_openrouter
from gpt_scraper_v3.utilities import get_session

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON Schema for structured pagination output
# ---------------------------------------------------------------------------

_pag_url_props: Dict[str, Any] = {
    "relative_url": {"type": "string", "description": "RELATIVE URL compatible with urljoin()"},
    "page_number": {"type": ["integer", "null"], "description": "Page number or null"},
    "link_text": {"type": "string", "description": "Visible text of the pagination link"},
    "link_type": {
        "type": "string",
        "enum": ["numbered", "navigation", "first", "last", "next", "previous"],
        "description": "Type of pagination link identified",
    },
}
_suburl_props: Dict[str, Any] = {
    "relative_url": {"type": "string", "description": "RELATIVE URL compatible with urljoin()"},
    "link_text": {"type": "string", "description": "Visible text of the suburl link"},
    "url_type": {"type": "string", "description": "Type of suburl (profile, detail, document, etc.)"},
    "is_direct_descendant": {"type": "boolean", "description": "Whether this is a direct child page"},
}

_PAGINATION_SCHEMA: Dict[str, Any] = {
    "name": "pagination_and_suburls_detection",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "has_pagination": {"type": "boolean", "description": "True if legitimate content pagination is detected"},
            "confidence_score": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Confidence 0-100"},
            "content_type_detected": {"type": "string", "description": "Type of content being paginated"},
            "pagination_urls": {
                "type": "array", "description": "Pagination URLs relevant to current page content",
                "items": {"type": "object", "properties": _pag_url_props,
                          "required": list(_pag_url_props), "additionalProperties": False},
            },
            "pagination_indicators": {"type": "array", "description": "Pagination indicators found", "items": {"type": "string"}},
            "suburls": {
                "type": "array", "description": "Direct descendant/child page URLs found",
                "items": {"type": "object", "properties": _suburl_props,
                          "required": list(_suburl_props), "additionalProperties": False},
            },
        },
        "required": ["has_pagination", "confidence_score", "content_type_detected",
                      "pagination_urls", "pagination_indicators", "suburls"],
        "additionalProperties": False,
    },
}

# ---------------------------------------------------------------------------
# Prompt templates (kept identical to V2 for behavioral parity)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT: str = """\
EXPERT CONTENT-AWARE PAGINATION ANALYST: You are a specialist who analyzes HTML content to detect pagination patterns and extrapolate the full sequence of numbered pages with CONTENT RELEVANCE VALIDATION using STRUCTURED OUTPUTS.

## TASK:
Analyze HTML content and return VALIDATED JSON identifying pagination elements ONLY when they paginate the SAME content type as the current page.

## CRITICAL PAGINATION REQUIREMENTS:

**DETECT PAGINATION ONLY FOR:**
- Lists/collections that match the current page content type
- MORE of the SAME content (more news articles, more contacts, more documents, more events, etc.)
- Pagination within the SAME section/category as the current page

**DO NOT DETECT PAGINATION FOR:**
- General site navigation (different website sections)
- Unrelated content pagination (e.g., if on news page but find pagination for products)
- Base domain navigation/menu
- Cross-section pagination (different content types)

## ENHANCED ANALYSIS METHODOLOGY:

**STEP 1: IDENTIFY CURRENT PAGE CONTENT TYPE**
First determine what type of content this page contains:
- News/Articles (aktuality, novinky, clanky, news)
- Contacts/Directory (kontakty, telefonni seznam, seznam, directory)
- Documents/Files (dokumenty, soubory, files, downloads)
- Events/Calendar (udalosti, kalendar, events, calendar)
- Products/Services (produkty, sluzby, products, services)
- Grants/Funding (dotace, granty, funding, grants)
- Meeting/Council pages (zastupitelstvo, usneseni, rada)
- Other list-based content

**STEP 2: VALIDATE PAGINATION RELEVANCE**
Only detect pagination if it's for MORE of the SAME content type.

**STEP 3: EXTRACT AND EXTRAPOLATE RELEVANT PAGINATION URLS**
For each validated pagination element:
- Extract href attribute AS-IS (relative format like "?page=1")
- Determine page number from link text or URL parameter
- Classify link type (numbered, navigation, etc.)
- **CRITICAL EXTRAPOLATION:** If a numbered sequence is detected (e.g., 1, 2, 3, ..., 9), you MUST extrapolate and include ALL missing numbered pages using the detected URL pattern.

## URL FORMAT REQUIREMENTS:
- **ALWAYS return RELATIVE URLs** compatible with urljoin()
- Examples: "?page=1", "?stranka=2", "/aktuality?page=3"
- **NEVER** include domain/protocol

## EXAMPLE ANALYSIS:

**SCENARIO 1: Contact List Page with Pagination (1, 2, 3, 4, 5, ..., 9)**
```html
<div class="strlistovani"><strong>-1-</strong> <a href="?stranka=2">2</a> <a href="?stranka=3">3</a> ... <a href="?stranka=9">9</a></div>
```
**RESULT: EXTRAPOLATE ALL 9 PAGES** - Return relative URLs for pages 1 through 9.

## CONFIDENCE SCORING:
- High (80-100): Clear content match + strong pagination indicators
- Medium (60-79): Probable content match + pagination indicators
- Low (40-59): Weak content match or unclear pagination
- None (0-39): No content match or no legitimate pagination"""

_USER_PROMPT_TEMPLATE: str = """\
**CRITICAL: Return ONLY valid JSON matching the schema. NO markdown, NO explanations, NO code blocks.**

Analyze this HTML for pagination AND suburls, then return PURE JSON.

Current URL: {url}

## PAGINATION DETECTION:
1. Identify content type (e.g., 'contact list', 'news articles')
2. Find pagination for MORE of SAME content type
3. Extrapolate full numbered sequence if detected (e.g., 1,2,3...9)
4. Extract RELATIVE URLs (e.g., "?page=2")

## SUBURLS DETECTION (CRITICAL):
1. Find links to DETAIL/PROFILE pages of items shown on current page
2. Example: If page lists 55 council members, extract all 55 member profile links
3. Look for patterns: `/person-name`, `/member/123`, `/detail/abc`
4. Include ONLY direct children (detail pages), NOT navigation links
5. Extract RELATIVE URLs (e.g., "/mgr-richard-brabec")

## IMPORTANT DISTINCTIONS:
- **PAGINATION** = More items of same type (page 2, page 3)
- **SUBURLS** = Detail pages for current items (member profiles, document details)

Return ONLY valid JSON. Example:
{{"has_pagination": false, "confidence_score": 0, "content_type_detected": "council_members_directory", "pagination_urls": [], "pagination_indicators": [], "suburls": [{{"relative_url": "/mgr-richard-brabec", "link_text": "Mgr. Richard Brabec", "url_type": "profile", "is_direct_descendant": true}}]}}

HTML content to analyze:
{html_content}"""


# ---------------------------------------------------------------------------
# Main AI-powered pagination detection
# ---------------------------------------------------------------------------

def detect_pagination_in_html(html_content: str, url: str = "") -> Dict[str, Any]:
    """AI-powered pagination detection via OpenRouter Structured Outputs.

    Falls back to legacy pattern-matching when the API key is missing or on error.
    """
    logger.info("AI-POWERED PAGINATION DETECTION: Analyzing HTML content via OpenRouter Structured Outputs")

    if not html_content or not html_content.strip():
        logger.warning("No HTML content provided for pagination detection")
        return {"has_pagination": False, "indicators": [], "confidence": 0, "reason": "No HTML content provided"}

    cfg = get_config()
    if not cfg.OPENROUTER_API_KEY:
        logger.warning("OpenRouter API key not configured, falling back to legacy pagination detection")
        return _detect_pagination_legacy_fallback(html_content, url)

    try:
        user_prompt = _USER_PROMPT_TEMPLATE.format(url=url, html_content=html_content)
        response_format: Dict[str, Any] = {"type": "json_schema", "json_schema": _PAGINATION_SCHEMA}

        ai_response = _call_openrouter(
            messages=[{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
            max_tokens=20000, call_name="AI_PAGINATION_DETECTION", url=url,
            temperature=0.1, response_format=response_format,
        )

        if not ai_response:
            logger.error("Empty AI response for pagination detection")
            return _detect_pagination_legacy_fallback(html_content, url)

        try:
            ai_response = sanitize_json_response(ai_response)
            pagination_data: Dict[str, Any] = json.loads(ai_response)

            has_pagination = pagination_data.get("has_pagination", False)
            confidence = pagination_data.get("confidence_score", 0)
            content_type = pagination_data.get("content_type_detected", "unknown")
            pagination_urls = pagination_data.get("pagination_urls", [])
            suburls = pagination_data.get("suburls", [])
            indicators = pagination_data.get("pagination_indicators", [])

            logger.info("AI PAGINATION ANALYSIS COMPLETE: pagination=%s confidence=%d%% type=%s "
                        "pag_urls=%d suburls=%d indicators=%s",
                        has_pagination, confidence, content_type,
                        len(pagination_urls), len(suburls), indicators)

            if has_pagination:
                logger.info("AI DETECTED CONTENT-RELEVANT PAGINATION with %d%% confidence", confidence)
                for i, pu in enumerate(pagination_urls, 1):
                    logger.info("   %d. %s: '%s' -> %s", i, pu.get("link_type", "?"),
                                pu.get("link_text", ""), pu.get("relative_url", ""))
            else:
                logger.info("AI: NO CONTENT-RELEVANT PAGINATION (confidence: %d%%)", confidence)

            if suburls:
                logger.info("AI DETECTED %d SUBURLS", len(suburls))
                for i, su in enumerate(suburls, 1):
                    logger.info("   %d. %s: '%s' -> %s", i, su.get("url_type", "?"),
                                su.get("link_text", ""), su.get("relative_url", ""))

            return {"has_pagination": has_pagination, "indicators": indicators, "confidence": confidence,
                    "content_type_detected": content_type, "pagination_urls": pagination_urls,
                    "suburls": suburls, "ai_analysis": True}

        except json.JSONDecodeError as exc:
            logger.error("Failed to parse structured AI response as JSON: %s", exc)
            # C3 fix: debug level, truncated to 500 chars
            logger.debug("Response preview: %.500s", ai_response[:500])
            logger.info("Falling back to legacy pagination detection...")
            return _detect_pagination_legacy_fallback(html_content, url)

    except Exception as exc:
        logger.error("Unexpected error in AI pagination detection: %s", exc)
        return _detect_pagination_legacy_fallback(html_content, url)


# ---------------------------------------------------------------------------
# Legacy fallback pagination detection
# ---------------------------------------------------------------------------

_PAGINATION_CSS = ["pagination", "pager", "gov-pagination", "strlistovani"]
_PAGINATION_PARAMS = ["page=", "stranka=", "p=", "pg=", "pagenum="]
_NEXT_PREV_KW = [
    "next", "previous", "prev", "last", "first",
    "dal\u0161\u00ed", "p\u0159edchoz\u00ed", "n\u00e1sleduj\u00edc\u00ed",
    "posledn\u00ed", "prvn\u00ed",
]


def _detect_pagination_legacy_fallback(html_content: str, url: str = "") -> Dict[str, Any]:
    """Legacy fallback pagination detection using pattern matching.

    Used when the OpenRouter API is not available or fails.
    """
    from bs4 import BeautifulSoup  # local import to avoid hard dependency

    logger.info("LEGACY PAGINATION DETECTION: Using pattern matching fallback")
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        pagination_indicators: List[str] = []
        confidence_score = 0

        all_links = soup.find_all("a")
        page_number_count = href_page_count = no_href_page_count = 0
        logger.info("ANALYZING %d <a> tags for numbered pages", len(all_links))

        for link in all_links:
            href: str = link.get("href", "")
            text: str = link.get_text(strip=True)
            cls: str = " ".join(link.get("class", []))

            # Clean malformed href attributes
            if href:
                href = href.strip("'\"").replace('\\"', "").replace("\\'", "") \
                    .replace("\\", "").replace('"', "").replace("'", "").strip()

            if (any(c in cls.lower() for c in _PAGINATION_CSS[:3])
                    or any(p in href.lower() for p in ["page=", "stranka="])
                    or text.isdigit() or "page" in text.lower()):
                logger.info("POTENTIAL PAGINATION LINK: text='%s' href='%s' classes='%s'", text, href, cls)

            # Determine page number from link text
            page_num: Optional[int] = None
            if text.isdigit():
                page_num = int(text)
            elif "page" in text.lower():
                m = re.search(r"page\s*(\d+)", text.lower())
                if m:
                    page_num = int(m.group(1))
            elif re.search(r"\d+$", text):
                m = re.search(r"(\d+)$", text)
                if m:
                    page_num = int(m.group(1))

            if page_num is not None:
                logger.info("FOUND PAGE NUMBER %d - checking context...", page_num)
                cls_lower = cls.lower()
                if href and any(p in href.lower() for p in _PAGINATION_PARAMS):
                    page_number_count += 1; href_page_count += 1
                    logger.info("COUNTED: numbered page link with href: %s -> %s", text, href)
                elif not href and any(c in cls_lower for c in _PAGINATION_CSS):
                    page_number_count += 1; no_href_page_count += 1
                    logger.info("COUNTED: numbered page element without href: %s (class: %s)", text, cls)
                elif any(c in cls_lower for c in _PAGINATION_CSS):
                    page_number_count += 1
                    if href:
                        href_page_count += 1
                    else:
                        no_href_page_count += 1
                    logger.info("COUNTED: pagination CSS element: %s (class: %s) href: %s", text, cls, bool(href))

        logger.info("NUMBERED PAGE DETECTION COMPLETE: %d elements (%d href, %d no-href)",
                     page_number_count, href_page_count, no_href_page_count)

        # Look for next/previous navigation
        next_prev_count = 0
        for el in soup.find_all(["a", "button"]):
            el_text = el.get_text(strip=True).lower()
            el_title = el.get("title", "").lower()
            if any(kw in el_text or kw in el_title for kw in _NEXT_PREV_KW):
                next_prev_count += 1

        # Calculate confidence
        if page_number_count >= 3:
            pagination_indicators.append(f"numbered_page_elements: {page_number_count}")
            confidence_score += 40
        elif page_number_count >= 1:
            pagination_indicators.append(f"partial_numbered_page_elements: {page_number_count}")
            confidence_score += 20
        if next_prev_count >= 1:
            pagination_indicators.append(f"next_prev_navigation: {next_prev_count} elements")
            confidence_score += 25

        has_pagination = confidence_score >= 60
        result: Dict[str, Any] = {"has_pagination": has_pagination, "indicators": pagination_indicators,
                                   "confidence": confidence_score, "ai_analysis": False, "legacy_fallback": True}
        if has_pagination:
            logger.info("LEGACY PAGINATION DETECTED: Confidence %d%%", confidence_score)
        else:
            logger.info("LEGACY: NO PAGINATION (confidence: %d%%)", confidence_score)
        return result

    except Exception as exc:
        logger.error("Error in legacy pagination detection: %s", exc)
        return {"has_pagination": False, "indicators": [], "confidence": 0,
                "reason": f"Legacy detection error: {exc}"}


# ---------------------------------------------------------------------------
# URL recursive / pagination configuration checks
# ---------------------------------------------------------------------------

def is_url_recursive_enabled(url: str) -> Tuple[bool, int]:
    """Check if a URL is configured for recursive suburls processing.

    Returns:
        Tuple of (is_enabled, max_recursive_level). (False, 0) when not configured.
    """
    cfg = get_config()
    if not cfg.RECURSIVE_URLS:
        return False, 0

    normalized_url = url.rstrip("/")
    for rc in cfg.RECURSIVE_URLS:
        if isinstance(rc, str):
            recursive_url, recursive_level = rc, 1
        else:
            recursive_url = rc.get("url", "")
            recursive_level = rc.get("recursive_level", 1)

        rc_norm = recursive_url.rstrip("/")
        if normalized_url == rc_norm or normalized_url.startswith(rc_norm + "/"):
            logger.info("URL %s is enabled for recursive suburls (matches: %s, max level: %d)",
                        url, recursive_url, recursive_level)
            return True, recursive_level

    logger.debug("URL %s is not configured for recursive suburls processing", url)
    return False, 0


def is_url_explicitly_paginated(url: str) -> bool:
    """Check if a URL is configured for explicit pagination processing."""
    cfg = get_config()
    if not cfg.PAGINATED_URLS:
        return False
    normalized_url = url.rstrip("/")
    if normalized_url in cfg.PAGINATED_URLS:
        logger.info("URL %s is explicitly enabled for pagination processing.", url)
        return True
    logger.debug("URL %s is not explicitly configured for pagination processing.", url)
    return False


# ---------------------------------------------------------------------------
# Sub-URL extraction from AI data
# ---------------------------------------------------------------------------

def extract_suburls_from_ai_data(
    suburls_data: List[Dict[str, Any]], base_url: str, current_url: str = "",
) -> List[Dict[str, Any]]:
    """Extract and process suburls from AI-detected data.

    Converts relative sub-URL information into absolute URLs, validates and deduplicates.
    """
    logger.info("SUBURLS EXTRACTION: Processing %d AI-detected suburls", len(suburls_data))
    suburl_list: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()

    for i, si in enumerate(suburls_data, 1):
        try:
            relative_url = si.get("relative_url", "")
            link_text = si.get("link_text", "")
            url_type = si.get("url_type", "other")
            is_direct = si.get("is_direct_descendant", False)

            if not relative_url:
                logger.warning("Skipping AI suburl with empty relative_url")
                continue
            try:
                absolute_url = urljoin(base_url, relative_url)
                logger.debug("AI suburl: base='%s' + rel='%s' = '%s'", base_url, relative_url, absolute_url)
                pr = urlparse(absolute_url)
                if not pr.scheme or not pr.netloc:
                    logger.warning("Invalid absolute URL from AI suburl data: %s, skipping", absolute_url)
                    continue
            except Exception as exc:
                logger.error("Error constructing URL from AI suburl: base='%s' + rel='%s': %s",
                             base_url, relative_url, exc)
                continue

            if absolute_url in seen_urls:
                continue
            seen_urls.add(absolute_url)
            if absolute_url == current_url:
                continue

            suburl_list.append({"url": absolute_url, "link_text": link_text, "url_type": url_type,
                                "is_direct_descendant": is_direct, "original_relative_url": relative_url,
                                "ai_source": True})
            logger.info("Added AI-detected suburl: %s (type: %s, text: '%s')", absolute_url, url_type, link_text)
        except Exception as exc:
            logger.error("Error processing AI suburl %d: %s", i, exc)

    logger.info("AI SUBURLS EXTRACTION COMPLETE: Found %d valid suburls", len(suburl_list))
    return suburl_list


# ---------------------------------------------------------------------------
# Pagination URL construction
# ---------------------------------------------------------------------------

def _construct_pagination_url(current_url: str, page_number: int) -> Optional[str]:
    """Construct a pagination URL from *current_url* and *page_number*.

    Detects existing pagination query parameters or falls back to Czech-site defaults.
    """
    try:
        if not current_url or not page_number:
            return None
        parsed = urlparse(current_url)
        qp: Dict[str, List[str]] = parse_qs(parsed.query)

        param_names = ["stranka", "page", "p", "pg", "pagenum"]
        existing = next((p for p in param_names if p in qp), None)

        if existing:
            param_name = existing
        elif "stranka=" in current_url.lower():
            param_name = "stranka"
        elif "page=" in current_url.lower():
            param_name = "page"
        else:
            param_name = "stranka"

        qp[param_name] = [str(page_number)]
        new_query = urlencode(qp, doseq=True)
        new_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path,
                              parsed.params, new_query, parsed.fragment))
        logger.debug("Constructed pagination URL: %s + page %d -> %s", current_url, page_number, new_url)
        return new_url
    except Exception as exc:
        logger.error("Error constructing pagination URL: %s", exc)
        return None
