"""Playwright-based content provider for GPT Scraper V3.

Provides headless browser automation for fetching and converting web pages
to markdown.  Designed as a drop-in provider alongside Jina AI and Firecrawl,
integrated via the ``provider_sequence`` mechanism.

Optional dependency: if ``playwright`` is not installed, ``is_available()``
returns ``False`` and the provider is silently skipped.

.. note:: The singleton browser pattern is NOT thread-safe.  This is
   acceptable for the current single-threaded scraper architecture.
"""
from __future__ import annotations

import atexit
import copy
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from gpt_scraper_v3.config import get_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency check
# ---------------------------------------------------------------------------

_PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.sync_api import (
        sync_playwright,
        Playwright,
        Browser,
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeout,
        Error as PlaywrightError,
    )
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass


def is_available() -> bool:
    """Return ``True`` if the Playwright Python package is installed."""
    return _PLAYWRIGHT_AVAILABLE


# ---------------------------------------------------------------------------
# Singleton browser lifecycle
# ---------------------------------------------------------------------------

_playwright: Optional["Playwright"] = None
_browser: Optional["Browser"] = None
_context: Optional["BrowserContext"] = None
_cleanup_registered: bool = False
_context_page_count: int = 0


def _cleanup_playwright() -> None:
    """Atexit handler: tear down Playwright resources in order."""
    global _context, _browser, _playwright

    logger.info("Playwright cleanup: shutting down browser resources")

    if _context is not None:
        try:
            _context.close()
        except Exception as exc:
            logger.warning("Playwright cleanup: failed to close context: %s", exc)
        _context = None

    if _browser is not None:
        try:
            _browser.close()
        except Exception as exc:
            logger.warning("Playwright cleanup: failed to close browser: %s", exc)
        _browser = None

    if _playwright is not None:
        try:
            _playwright.stop()
        except Exception as exc:
            logger.warning("Playwright cleanup: failed to stop playwright: %s", exc)
        _playwright = None


def get_browser() -> "Browser":
    """Return the singleton Browser instance, launching it lazily if needed."""
    global _playwright, _browser, _cleanup_registered

    if _browser is not None:
        return _browser

    if not _PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "Playwright is not installed. Install with: pip install playwright && playwright install chromium"
        )

    cfg = get_config()
    pw_cfg = cfg.PLAYWRIGHT_CONFIG

    _playwright = sync_playwright().start()

    # Register atexit cleanup exactly once
    if not _cleanup_registered:
        atexit.register(_cleanup_playwright)
        _cleanup_registered = True

    # Select browser engine (chromium, firefox, webkit)
    browser_type_name = pw_cfg.get("browser", "chromium")
    browser_type = getattr(_playwright, browser_type_name)

    # Build launch kwargs
    launch_kwargs: Dict[str, Any] = {
        "headless": pw_cfg.get("headless", True),
    }
    executable_path = pw_cfg.get("executable_path", "")
    if executable_path:
        launch_kwargs["executable_path"] = executable_path

    try:
        _browser = browser_type.launch(**launch_kwargs)
    except PlaywrightError as exc:
        if "executable doesn't exist" in str(exc).lower():
            logger.error(
                "PLAYWRIGHT: Browser executable not found. "
                "Run: playwright install %s",
                browser_type_name,
            )
        raise

    logger.info(
        "Playwright %s browser launched (headless=%s)",
        browser_type_name,
        launch_kwargs["headless"],
    )
    return _browser


def _get_context() -> "BrowserContext":
    """Return the singleton BrowserContext, recreating it when page limit is reached."""
    global _context, _context_page_count

    cfg = get_config()
    pw_cfg = cfg.PLAYWRIGHT_CONFIG
    context_pages_limit = pw_cfg.get("context_pages_limit", 500)

    # Recycle context if page count exceeds limit
    if _context is not None and _context_page_count >= context_pages_limit:
        try:
            _context.close()
        except Exception:
            pass
        _context = None
        _context_page_count = 0
        logger.info("Playwright context recreated after %d pages", context_pages_limit)

    if _context is not None:
        return _context

    browser = get_browser()

    # Build context kwargs
    context_kwargs: Dict[str, Any] = {
        "viewport": pw_cfg.get("viewport", {"width": 1920, "height": 1080}),
        "ignore_https_errors": pw_cfg.get("ignore_https_errors", False),
    }

    if context_kwargs["ignore_https_errors"]:
        logger.warning(
            "Playwright: ignore_https_errors is enabled — TLS certificate "
            "verification is DISABLED. Use only for development/testing."
        )

    user_agent = pw_cfg.get("user_agent", "")
    if user_agent:
        context_kwargs["user_agent"] = user_agent

    locale = pw_cfg.get("locale", "")
    if locale:
        context_kwargs["locale"] = locale

    timezone_id = pw_cfg.get("timezone_id", "")
    if timezone_id:
        context_kwargs["timezone_id"] = timezone_id

    extra_http_headers = pw_cfg.get("extra_http_headers", {})
    if extra_http_headers:
        context_kwargs["extra_http_headers"] = extra_http_headers

    storage_state = pw_cfg.get("storage_state", "")
    if storage_state:
        if os.path.exists(storage_state):
            context_kwargs["storage_state"] = storage_state
        else:
            logger.warning(
                "Playwright: storage_state path '%s' does not exist. Ignoring.",
                storage_state,
            )

    _context = browser.new_context(**context_kwargs)

    # Route-based resource blocking
    block_resources: List[str] = pw_cfg.get("block_resources", [])
    if block_resources:
        resource_set = set(block_resources)

        def _block_handler(route, request):  # type: ignore[no-untyped-def]
            if request.resource_type in resource_set:
                route.abort()
            else:
                route.continue_()

        _context.route("**/*", _block_handler)

    # URL pattern blocking
    block_urls: List[str] = pw_cfg.get("block_urls", [])
    for pattern in block_urls:
        _context.route(pattern, lambda route, _request: route.abort())

    # Inject cookies
    cookies: List[Dict[str, Any]] = pw_cfg.get("cookies", [])
    if cookies:
        _context.add_cookies(cookies)

    # Apply stealth anti-detection
    if pw_cfg.get("stealth", True):
        _apply_stealth_scripts(_context)

    return _context


def _reset_playwright() -> None:
    """Full teardown for crash recovery -- reset all singletons."""
    global _context, _browser, _playwright, _context_page_count

    logger.warning("Playwright: performing full reset of browser resources")

    if _context is not None:
        try:
            _context.close()
        except Exception:
            pass
        _context = None

    if _browser is not None:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None

    if _playwright is not None:
        try:
            _playwright.stop()
        except Exception:
            pass
        _playwright = None

    _context_page_count = 0


# ---------------------------------------------------------------------------
# Stealth anti-detection
# ---------------------------------------------------------------------------

_MANUAL_STEALTH_JS = """\
// Mask navigator.webdriver
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// Add window.chrome
window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};

// Mock navigator.plugins with realistic objects
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            {name: 'Chrome PDF Plugin', description: 'Portable Document Format', filename: 'internal-pdf-viewer'},
            {name: 'Chrome PDF Viewer', description: '', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
            {name: 'Native Client', description: '', filename: 'internal-nacl-plugin'}
        ];
        plugins.length = 3;
        return plugins;
    }
});

// Set navigator.languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['cs-CZ', 'cs', 'en-US', 'en']
});

// Override Permissions.query
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : originalQuery(parameters);
"""


def _apply_stealth_scripts(context: "BrowserContext") -> None:
    """Inject stealth scripts into *context* to avoid bot detection."""
    try:
        from playwright_stealth import stealth_sync  # type: ignore[import-untyped]

        # playwright_stealth provides a comprehensive stealth setup
        context.add_init_script(
            """
            // playwright-stealth integration marker
            window.__stealth_injected = true;
            """
        )
        # Apply stealth to each new page via the context
        stealth_sync(context)
        logger.debug("Playwright stealth applied via playwright-stealth library")
    except ImportError:
        logger.warning(
            "playwright-stealth not installed, applying manual stealth scripts. "
            "Install with: pip install playwright-stealth"
        )
        context.add_init_script(_MANUAL_STEALTH_JS)
        logger.debug("Manual stealth scripts injected into browser context")


# ---------------------------------------------------------------------------
# HTML-to-markdown conversion
# ---------------------------------------------------------------------------


def _parse_selectors(selector_string: str) -> List[str]:
    """Split a comma-separated CSS selector string into a list of selectors.

    Args:
        selector_string: Comma-separated CSS selectors, e.g.
            ``"nav, footer, .sidebar"``.

    Returns:
        List of stripped, non-empty selector strings.  Empty list for
        empty / ``None`` input.
    """
    if not selector_string:
        return []
    return [s.strip() for s in selector_string.split(",") if s.strip()]


def _html_to_markdown(
    html: str,
    remove_selectors: Optional[List[str]] = None,
    target_selectors: Optional[List[str]] = None,
    pw_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Convert raw HTML to markdown with optional DOM filtering.

    Args:
        html: Full HTML source of the page.
        remove_selectors: CSS selectors for elements to remove before conversion.
        target_selectors: CSS selectors for elements to extract.  If provided,
            only matching elements are converted.  Falls back to the whole
            document when nothing matches.
        pw_cfg: Playwright config dict controlling image and table behaviour.
            Recognised keys:

            - ``convert_images_to_alt_text`` (bool, default ``True``): when
              ``True``, images are converted to ``[Image: alt text]`` markers;
              when ``False``, ``<img>`` tags are stripped entirely.
            - ``table_infer_header`` (bool, default ``True``): when ``True``,
              markdownify receives ``table_infer_header=True`` so headerless
              HTML tables get an inferred header row.

    Returns:
        A ``(markdown_content, title)`` tuple.
    """
    if pw_cfg is None:
        pw_cfg = {}

    from bs4 import BeautifulSoup  # type: ignore[import-untyped]

    soup = BeautifulSoup(html, "html.parser")

    # Extract title before any DOM manipulation
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    # Remove always-unwanted elements
    for tag_name in ("script", "style", "noscript", "iframe", "svg"):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Target selector extraction
    working_soup = soup
    if target_selectors:
        extracted_parts = []
        for selector in target_selectors:
            extracted_parts.extend(soup.select(selector))
        if extracted_parts:
            # Build a new soup from the extracted elements
            combined_html = "".join(str(el) for el in extracted_parts)
            working_soup = BeautifulSoup(combined_html, "html.parser")
        else:
            logger.debug(
                "Playwright: target_selectors matched nothing, using whole document"
            )

    # Remove selector filtering
    if remove_selectors:
        for selector in remove_selectors:
            for el in working_soup.select(selector):
                el.decompose()

    # Convert to markdown
    try:
        from markdownify import markdownify as md  # type: ignore[import-untyped]

        strip_tags = ["svg"]  # Always strip SVG
        if not pw_cfg.get("convert_images_to_alt_text", True):
            strip_tags.append("img")

        md_kwargs: Dict[str, Any] = {
            "heading_style": "ATX",
            "bullets": "-",
            "strip": strip_tags,
        }
        if pw_cfg.get("table_infer_header", True):
            md_kwargs["table_infer_header"] = True

        markdown = md(str(working_soup), **md_kwargs)
    except ImportError:
        logger.warning(
            "markdownify not installed, falling back to plain text extraction. "
            "Install with: pip install markdownify"
        )
        from gpt_scraper_v3.utilities import html_to_plain_text

        markdown = html_to_plain_text(str(working_soup))

    # Post-process: convert image markdown to simplified [Image: alt] markers
    if pw_cfg.get("convert_images_to_alt_text", True):

        def _simplify_image(match: re.Match) -> str:  # type: ignore[type-arg]
            alt = match.group(1).strip()
            if len(alt) < 3:
                return ""
            return f"[Image: {alt}]"

        markdown = re.sub(
            r'!\[([^\]]*)\]\([^)]*(?:\s+"[^"]*")?\)', _simplify_image, markdown
        )

    # Post-process: collapse excessive blank lines (max 2 consecutive)
    markdown = re.sub(r"\n{4,}", "\n\n\n", markdown)

    return (markdown, title)


# ---------------------------------------------------------------------------
# Page preparation helper
# ---------------------------------------------------------------------------


def _prepare_page(
    page: "Page",
    url: str,
    pw_cfg: dict,
    include_content_actions: bool = True,
) -> Optional[str]:
    """Navigate to *url* and prepare the page for content extraction.

    Centralises the navigation, wait-strategy, cookie-banner dismissal,
    content-action (scroll / expand), custom JS execution, and HTML capture
    logic that was previously duplicated across ``fetch_playwright_markdown``
    and ``fetch_playwright_html``.

    Args:
        page: An already-created Playwright ``Page`` (navigation timeout
            should be set by the caller).
        url: The URL to navigate to.
        pw_cfg: Playwright configuration dict (from ``ScraperConfig``).
        include_content_actions: When ``True``, scroll for lazy content and
            expand collapsible sections after navigation.  ``False`` is used
            by ``fetch_playwright_html`` which only needs raw HTML.

    Returns:
        The HTML string on success, or ``None`` if the page returned
        minimal / empty content (caller should retry).
    """
    # -- Navigate -----------------------------------------------------------
    wait_until = pw_cfg.get("wait_until", "domcontentloaded")
    page.goto(url, wait_until=wait_until)

    # -- Wait strategy ------------------------------------------------------
    if pw_cfg.get("smart_wait_enabled", False):
        from gpt_scraper_v3.playwright_page_actions import wait_for_content_ready

        wait_for_content_ready(page, pw_cfg)
        # smart wait handles wait_for_selector and wait_for_timeout_ms internally
    else:
        # Basic wait -- existing behaviour
        wait_sel = pw_cfg.get("wait_for_selector", "")
        if wait_sel:
            page.wait_for_selector(wait_sel, timeout=10000)
        wait_ms = pw_cfg.get("wait_for_timeout_ms", 0)
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)

    # -- Cookie banner dismissal (always, regardless of content actions) ----
    from gpt_scraper_v3.playwright_page_actions import dismiss_cookie_banners

    dismiss_cookie_banners(page, pw_cfg)

    # -- Content actions (gated) --------------------------------------------
    if include_content_actions:
        from gpt_scraper_v3.playwright_page_actions import (
            scroll_for_lazy_content,
            expand_collapsible_sections,
        )

        did_scroll = scroll_for_lazy_content(page, pw_cfg)
        did_expand = expand_collapsible_sections(page, pw_cfg)
        if did_scroll or did_expand:
            page.wait_for_timeout(300)

    # -- Custom JS execution ------------------------------------------------
    js_code = pw_cfg.get("javascript_to_execute", "")
    if js_code:
        logger.warning("Executing custom JavaScript from config (security: verify source)")
        page.evaluate(js_code)

    # -- Capture and validate HTML ------------------------------------------
    html = page.content()
    if not html or len(html.strip()) < 200:
        logger.warning(
            "PLAYWRIGHT: Minimal HTML for %s (%d chars)",
            url,
            len(html) if html else 0,
        )
        return None

    return html


# ---------------------------------------------------------------------------
# Content quality assessment + escalation
# ---------------------------------------------------------------------------


def _assess_content_quality(markdown: str, html_length: int, pw_cfg: dict) -> str:
    """Assess whether markdown extraction produced sufficient content.

    The quality gate is controlled by ``quality_gate_enabled`` in *pw_cfg*.
    When disabled (the default), this function unconditionally returns
    ``"good"`` so the caller incurs no overhead.

    Args:
        markdown: The extracted markdown text.
        html_length: The length of the raw HTML that was converted.
        pw_cfg: Playwright configuration dict.

    Returns:
        ``"good"``, ``"suspect"``, or ``"empty"``.
    """
    if not pw_cfg.get("quality_gate_enabled", False):
        return "good"

    md_len = len(markdown.strip())
    if md_len == 0:
        return "empty"

    min_len = pw_cfg.get("quality_gate_min_markdown_length", 100)
    min_ratio = pw_cfg.get("quality_gate_min_md_html_ratio", 0.02)

    # If HTML itself is small, page is genuinely sparse — accept
    if html_length < 2000:
        return "good"

    if md_len < min_len:
        return "suspect"

    ratio = md_len / html_length if html_length > 0 else 0
    if ratio < min_ratio:
        return "suspect"

    return "good"


def _escalate_pw_cfg(pw_cfg: dict) -> dict:
    """Create an escalated copy of *pw_cfg* with more aggressive settings.

    Enables smart wait, auto-scroll, and collapsible expansion, and adds
    3 000 ms to the existing ``wait_for_timeout_ms``.

    Args:
        pw_cfg: The original Playwright configuration dict.

    Returns:
        A deep-copied dict with escalated values.
    """
    escalated = copy.deepcopy(pw_cfg)
    escalated["smart_wait_enabled"] = True
    escalated["auto_scroll_enabled"] = True
    escalated["expand_collapsibles_enabled"] = True
    current_wait = escalated.get("wait_for_timeout_ms", 0)
    escalated["wait_for_timeout_ms"] = min(current_wait + 3000, 15000)
    return escalated


# ---------------------------------------------------------------------------
# Main fetch function
# ---------------------------------------------------------------------------


def fetch_playwright_markdown(
    url: str,
    remove_selectors: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """Fetch *url* via Playwright and convert HTML to markdown.

    This function NEVER raises exceptions.  All errors are caught internally
    and logged; on failure the return value is ``(None, None, None)``.

    The inner ``for`` loop retries up to 2 times on ``PlaywrightTimeout``
    only.  Other Playwright errors are not retried.

    An outer quality-gate loop (max 1 escalation) re-fetches with more
    aggressive page-interaction settings when the extracted markdown looks
    suspiciously thin relative to the HTML size.  The quality gate is only
    active when ``quality_gate_enabled`` is ``True`` in ``PLAYWRIGHT_CONFIG``.

    Args:
        url: The URL to fetch.
        remove_selectors: Optional comma-separated CSS selectors for elements
            to remove from the page before markdown conversion.  Overrides the
            config-level ``remove_selectors`` when provided.

    Returns:
        A ``(markdown, title, metadata)`` tuple on success, or
        ``(None, None, None)`` on any failure.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        logger.error(
            "Playwright is not installed. Cannot fetch markdown. "
            "Install with: pip install playwright && playwright install chromium"
        )
        return (None, None, None)

    # Reject non-HTTP(S) URLs to prevent SSRF (e.g. file://, javascript:)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        logger.error(
            "PLAYWRIGHT: Refusing to fetch URL with scheme '%s' — "
            "only http and https are allowed: %s",
            parsed.scheme, url,
        )
        return (None, None, None)

    cfg = get_config()
    pw_cfg = cfg.PLAYWRIGHT_CONFIG
    active_pw_cfg = pw_cfg  # Will be escalated by quality gate if needed
    max_retries = 2
    quality_retried = False

    for _quality_attempt in range(2):  # Outer loop for quality gate (max 1 escalation)
        result_markdown: Optional[str] = None
        result_title: Optional[str] = None
        result_metadata: Optional[Dict[str, Any]] = None
        last_html_length: int = 0

        for attempt in range(max_retries + 1):
            if attempt > 0:
                backoff = 2 ** (attempt - 1)
                logger.info(
                    "PLAYWRIGHT: Retry %d/%d for %s (waiting %ds)",
                    attempt, max_retries, url, backoff,
                )
                time.sleep(backoff)

            page: Optional["Page"] = None
            try:
                context = _get_context()
                global _context_page_count
                _context_page_count += 1
                page = context.new_page()

                try:
                    nav_timeout = active_pw_cfg.get("navigation_timeout_ms", 30000)
                    page.set_default_navigation_timeout(nav_timeout)

                    logger.info(
                        "PLAYWRIGHT: Navigating to %s (wait_until=%s, timeout=%dms)",
                        url,
                        active_pw_cfg.get("wait_until", "domcontentloaded"),
                        nav_timeout,
                    )

                    html = _prepare_page(page, url, active_pw_cfg, include_content_actions=True)
                    if html is None:
                        continue  # retry

                    last_html_length = len(html)

                    # Determine selectors
                    r_sel_str = remove_selectors if remove_selectors else active_pw_cfg.get("remove_selectors", "")
                    t_sel_str = active_pw_cfg.get("target_selectors", "")
                    r_sels = _parse_selectors(r_sel_str)
                    t_sels = _parse_selectors(t_sel_str)

                    markdown, title = _html_to_markdown(html, r_sels, t_sels, pw_cfg=active_pw_cfg)

                    if not markdown or not markdown.strip():
                        logger.warning(
                            "PLAYWRIGHT: HTML-to-markdown produced empty result for %s",
                            url,
                        )
                        logger.debug("Quality gate: skipped (empty markdown)")
                        return (None, None, None)

                    result_metadata = {
                        "provider": "playwright",
                        "browser": active_pw_cfg.get("browser", "chromium"),
                        "url": url,
                        "content_length": len(markdown),
                    }

                    result_markdown = markdown
                    result_title = title

                    logger.info(
                        "PLAYWRIGHT SUCCESS: %s (%d chars markdown)",
                        url, len(markdown),
                    )
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("Content preview: %.500s", markdown[:500])
                    break  # Success — exit inner retry loop

                finally:
                    try:
                        if page is not None:
                            page.close()
                    except Exception:
                        pass

            except PlaywrightTimeout as e:
                logger.error(
                    "PLAYWRIGHT TIMEOUT for %s (attempt %d/%d): %s",
                    url, attempt + 1, max_retries + 1, e,
                )
                if attempt < max_retries:
                    continue
                # All timeout retries exhausted — result_markdown stays None

            except PlaywrightError as e:
                error_msg = str(e).lower()
                if "target page, context or browser has been closed" in error_msg:
                    logger.error(
                        "PLAYWRIGHT: Browser closed unexpectedly, resetting: %s", e,
                    )
                    _reset_playwright()
                elif "net::err_name_not_resolved" in error_msg:
                    logger.error("PLAYWRIGHT: DNS error for %s: %s", url, e)
                elif "net::err_connection_refused" in error_msg:
                    logger.error("PLAYWRIGHT: Connection refused for %s: %s", url, e)
                elif "executable doesn't exist" in error_msg:
                    logger.error(
                        "PLAYWRIGHT: Browser not installed. Run: playwright install chromium",
                    )
                else:
                    logger.error("PLAYWRIGHT error for %s: %s", url, e)
                return (None, None, None)

            except Exception as e:
                logger.error("PLAYWRIGHT unexpected error for %s: %s", url, e)
                return (None, None, None)

        # -- After inner retry loop ------------------------------------------
        if result_markdown is None:
            return (None, None, None)

        # -- QUALITY GATE ----------------------------------------------------
        quality = _assess_content_quality(result_markdown, last_html_length, active_pw_cfg)
        logger.debug(
            "Quality assessment for %s: %s (%d chars md, %d chars html)",
            url, quality, len(result_markdown), last_html_length,
        )

        if quality == "suspect" and not quality_retried:
            quality_retried = True
            active_pw_cfg = _escalate_pw_cfg(pw_cfg)
            logger.warning(
                "Quality gate: suspect content for %s (%d chars markdown from %d chars HTML), "
                "retrying with escalated strategy",
                url, len(result_markdown), last_html_length,
            )
            continue  # Re-enter outer loop with escalated config

        break  # Quality is good (or already retried) — no need for second iteration

    return (result_markdown, result_title, result_metadata)


def fetch_playwright_html(url: str) -> Optional[str]:
    """Fetch *url* via Playwright and return raw HTML content.

    Returns the full page HTML from ``page.content()`` without any markdown
    conversion or DOM filtering (no ``remove_selectors`` / ``target_selectors``).
    This preserves all ``<a>`` tags for downstream HTML sitemap link extraction.

    This function NEVER raises exceptions.  All errors are caught internally
    and logged; on failure the return value is ``None``.

    Retries up to 2 times on ``PlaywrightTimeout`` only.  Other Playwright
    errors are not retried.

    Args:
        url: The URL to fetch.

    Returns:
        Raw HTML string on success, or ``None`` on any failure.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        logger.error(
            "Playwright is not installed. Cannot fetch HTML. "
            "Install with: pip install playwright && playwright install chromium"
        )
        return None

    # Reject non-HTTP(S) URLs to prevent SSRF (e.g. file://, javascript:)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        logger.error(
            "PLAYWRIGHT HTML: Refusing to fetch URL with scheme '%s' — "
            "only http and https are allowed: %s",
            parsed.scheme, url,
        )
        return None

    cfg = get_config()
    pw_cfg = cfg.PLAYWRIGHT_CONFIG
    max_retries = 2

    for attempt in range(max_retries + 1):
        if attempt > 0:
            backoff = 2 ** (attempt - 1)
            logger.info(
                "PLAYWRIGHT HTML: Retry %d/%d for %s (waiting %ds)",
                attempt, max_retries, url, backoff,
            )
            time.sleep(backoff)

        page: Optional["Page"] = None
        try:
            context = _get_context()
            global _context_page_count
            _context_page_count += 1
            page = context.new_page()

            try:
                nav_timeout = pw_cfg.get("navigation_timeout_ms", 30000)
                page.set_default_navigation_timeout(nav_timeout)

                logger.info(
                    "PLAYWRIGHT HTML: Navigating to %s (wait_until=%s, timeout=%dms)",
                    url,
                    pw_cfg.get("wait_until", "domcontentloaded"),
                    nav_timeout,
                )

                html = _prepare_page(page, url, pw_cfg, include_content_actions=False)
                if html is None:
                    continue  # retry

                logger.info(
                    "PLAYWRIGHT HTML SUCCESS: %s (%d chars)",
                    url, len(html),
                )
                return html

            finally:
                try:
                    if page is not None:
                        page.close()
                except Exception:
                    pass

        except PlaywrightTimeout as e:
            logger.error(
                "PLAYWRIGHT HTML TIMEOUT for %s (attempt %d/%d): %s",
                url, attempt + 1, max_retries + 1, e,
            )
            if attempt < max_retries:
                continue
            return None

        except PlaywrightError as e:
            error_msg = str(e).lower()
            if "target page, context or browser has been closed" in error_msg:
                logger.error(
                    "PLAYWRIGHT HTML: Browser closed unexpectedly, resetting: %s", e,
                )
                _reset_playwright()
            elif "net::err_name_not_resolved" in error_msg:
                logger.error("PLAYWRIGHT HTML: DNS error for %s: %s", url, e)
            elif "net::err_connection_refused" in error_msg:
                logger.error("PLAYWRIGHT HTML: Connection refused for %s: %s", url, e)
            elif "executable doesn't exist" in error_msg:
                logger.error(
                    "PLAYWRIGHT HTML: Browser not installed. Run: playwright install chromium",
                )
            else:
                logger.error("PLAYWRIGHT HTML error for %s: %s", url, e)
            return None

        except Exception as e:
            logger.error("PLAYWRIGHT HTML unexpected error for %s: %s", url, e)
            return None

    return None
