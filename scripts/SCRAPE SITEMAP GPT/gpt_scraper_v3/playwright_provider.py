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
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

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
        context_kwargs["storage_state"] = storage_state

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
) -> Tuple[str, str]:
    """Convert raw HTML to markdown with optional DOM filtering.

    Args:
        html: Full HTML source of the page.
        remove_selectors: CSS selectors for elements to remove before conversion.
        target_selectors: CSS selectors for elements to extract.  If provided,
            only matching elements are converted.  Falls back to the whole
            document when nothing matches.

    Returns:
        A ``(markdown_content, title)`` tuple.
    """
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

        markdown = md(
            str(working_soup),
            heading_style="ATX",
            bullets="-",
            strip=["img", "svg"],
        )
    except ImportError:
        logger.warning(
            "markdownify not installed, falling back to plain text extraction. "
            "Install with: pip install markdownify"
        )
        from gpt_scraper_v3.utilities import html_to_plain_text

        markdown = html_to_plain_text(str(working_soup))

    # Post-process: collapse excessive blank lines (max 2 consecutive)
    markdown = re.sub(r"\n{4,}", "\n\n\n", markdown)

    return (markdown, title)


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

    Retries up to 2 times on ``PlaywrightTimeout`` only.  Other Playwright
    errors are not retried.

    Args:
        url: The URL to fetch.
        remove_selectors: Optional comma-separated CSS selectors for elements
            to remove from the page before markdown conversion.  Overrides the
            config-level ``remove_selectors`` when provided.

    Returns:
        A ``(markdown, title, metadata)`` tuple on success, or
        ``(None, None, None)`` on any failure.
    """
    cfg = get_config()
    pw_cfg = cfg.PLAYWRIGHT_CONFIG
    max_retries = 2

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
                nav_timeout = pw_cfg.get("navigation_timeout_ms", 30000)
                page.set_default_navigation_timeout(nav_timeout)

                wait_until = pw_cfg.get("wait_until", "domcontentloaded")
                logger.info(
                    "PLAYWRIGHT: Navigating to %s (wait_until=%s, timeout=%dms)",
                    url, wait_until, nav_timeout,
                )
                page.goto(url, wait_until=wait_until)

                # Optional: wait for specific selector
                wait_sel = pw_cfg.get("wait_for_selector", "")
                if wait_sel:
                    page.wait_for_selector(wait_sel, timeout=10000)

                # Optional: additional fixed wait
                wait_ms = pw_cfg.get("wait_for_timeout_ms", 0)
                if wait_ms > 0:
                    page.wait_for_timeout(wait_ms)

                # Optional: execute custom JavaScript
                js_code = pw_cfg.get("javascript_to_execute", "")
                if js_code:
                    page.evaluate(js_code)

                html = page.content()
                if not html or len(html.strip()) < 100:
                    logger.warning(
                        "PLAYWRIGHT: Minimal HTML returned for %s (%d chars)",
                        url, len(html) if html else 0,
                    )
                    return (None, None, None)

                # Determine selectors
                r_sel_str = remove_selectors if remove_selectors else pw_cfg.get("remove_selectors", "")
                t_sel_str = pw_cfg.get("target_selectors", "")
                r_sels = _parse_selectors(r_sel_str)
                t_sels = _parse_selectors(t_sel_str)

                markdown, title = _html_to_markdown(html, r_sels, t_sels)

                if not markdown or not markdown.strip():
                    logger.warning(
                        "PLAYWRIGHT: HTML-to-markdown produced empty result for %s",
                        url,
                    )
                    return (None, None, None)

                metadata: Dict[str, Any] = {
                    "provider": "playwright",
                    "browser": pw_cfg.get("browser", "chromium"),
                    "url": url,
                    "content_length": len(markdown),
                }

                logger.info(
                    "PLAYWRIGHT SUCCESS: %s (%d chars markdown)",
                    url, len(markdown),
                )
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("Content preview: %.500s", markdown[:500])
                return (markdown, title, metadata)

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
            return (None, None, None)

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

    return (None, None, None)
