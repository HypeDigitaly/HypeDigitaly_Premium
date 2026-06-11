"""Playwright page-level interaction functions for GPT Scraper V3.

Provides four composable page actions (DOM stability wait, cookie banner
dismissal, lazy-content scrolling, and collapsible section expansion) that
operate on an already-navigated Playwright ``Page``.

All functions are designed to **never raise** -- errors are logged internally
and execution continues gracefully.  Each function is gated by an enable flag
in the ``pw_cfg`` dict so callers can invoke them unconditionally.

.. note:: These functions use the Playwright **sync** API.  They do NOT
   import ``get_config()`` -- all configuration is passed via the ``pw_cfg``
   parameter to avoid circular imports and improve testability.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Function 1: Smart DOM stability detection
# ---------------------------------------------------------------------------


def wait_for_content_ready(page: "Any", pw_cfg: Dict[str, Any]) -> None:
    """Wait for DOM content to stabilise before extracting page content.

    Performs three sequential steps:

    1. Wait for ``networkidle`` load state.
    2. Poll ``document.body.innerText.length`` across multiple rounds until
       the value is stable for two consecutive checks.
    3. Honour ``wait_for_selector`` and ``wait_for_timeout_ms`` from
       *pw_cfg* (same keys already used by the provider).

    Args:
        page: A Playwright ``Page`` object that has already navigated to a URL.
        pw_cfg: Playwright configuration dict (subset of ``ScraperConfig``).
    """
    if not pw_cfg.get("smart_wait_enabled", False):
        return

    nav_timeout: int = pw_cfg.get("navigation_timeout_ms", 30000)

    # -- Step 1: networkidle -------------------------------------------------
    try:
        page.wait_for_load_state("networkidle", timeout=nav_timeout)
    except Exception:
        logger.warning("Smart wait: networkidle timeout for page, continuing")

    # -- Step 2: DOM text-length stability -----------------------------------
    stability_interval: int = pw_cfg.get("smart_wait_stability_interval_ms", 500)
    stability_rounds: int = pw_cfg.get("smart_wait_stability_rounds", 3)

    prev_length: int = -1
    stable_count: int = 0

    for _round in range(stability_rounds):
        try:
            current_length: int = page.evaluate("() => document.body.innerText.length")
        except Exception:
            logger.debug("Smart wait: failed to evaluate innerText length, skipping stability check")
            break

        if current_length == prev_length:
            stable_count += 1
            if stable_count >= 1:
                logger.debug(
                    "Smart wait: DOM stable after %d rounds (%d chars)",
                    _round + 1,
                    current_length,
                )
                break
        else:
            stable_count = 0

        prev_length = current_length

        try:
            page.wait_for_timeout(stability_interval)
        except Exception:
            break

    # -- Step 3: optional selector / fixed wait ------------------------------
    wait_sel: str = pw_cfg.get("wait_for_selector", "")
    if wait_sel:
        try:
            page.wait_for_selector(wait_sel, timeout=10000)
        except Exception:
            logger.debug("Smart wait: wait_for_selector '%s' timed out, continuing", wait_sel)

    wait_ms: int = pw_cfg.get("wait_for_timeout_ms", 0)
    if wait_ms > 0:
        try:
            page.wait_for_timeout(wait_ms)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Function 2: Cookie banner dismissal
# ---------------------------------------------------------------------------

_DEFAULT_COOKIE_SELECTORS: List[str] = [
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#CybotCookiebotDialogBodyButtonAccept",
    ".onetrust-accept-btn-handler",
    'button[data-cookiefirst-action="accept"]',
    "[data-cky-tag=\"accept-button\"]",
    ".cmplz-accept",
    ".cc-accept",
    ".cookie-accept",
]


def dismiss_cookie_banners(page: "Any", pw_cfg: Dict[str, Any]) -> None:
    """Attempt to dismiss a cookie consent banner on the current page.

    Tries a list of CSS selectors (configurable or built-in heuristics) and
    clicks the first matching element.  After a successful click the function
    waits briefly for the banner to animate away and returns immediately.

    Args:
        page: A Playwright ``Page`` object that has already navigated to a URL.
        pw_cfg: Playwright configuration dict.
    """
    if not pw_cfg.get("dismiss_cookie_banners", False):
        return

    # Build selector list
    custom_selectors: str = pw_cfg.get("cookie_banner_selectors", "")
    if custom_selectors:
        selectors = [s.strip() for s in custom_selectors.split(",") if s.strip()]
    else:
        selectors = list(_DEFAULT_COOKIE_SELECTORS)

    for selector in selectors:
        try:
            page.locator(selector).first.click(timeout=2000)
            # Click succeeded -- banner dismissed
            logger.info("Cookie banner dismissed via selector: %s", selector)
            try:
                page.wait_for_timeout(300)
            except Exception:
                pass
            return
        except Exception:
            continue

    logger.debug("No cookie banner found for any of %d selectors", len(selectors))


# ---------------------------------------------------------------------------
# Function 3: Scroll for lazy-loaded content
# ---------------------------------------------------------------------------


def scroll_for_lazy_content(page: "Any", pw_cfg: Dict[str, Any]) -> int:
    """Scroll the page to trigger lazy-loading of below-the-fold content.

    Executes a single ``page.evaluate()`` call with an explicit async JS
    function that scrolls one viewport height at a time, detects when the
    document height stabilises, and scrolls back to the top.

    Args:
        page: A Playwright ``Page`` object that has already navigated to a URL.
        pw_cfg: Playwright configuration dict.

    Returns:
        Number of scroll steps performed, or ``0`` if disabled / on error.
    """
    if not pw_cfg.get("auto_scroll_enabled", False):
        return 0

    max_scrolls: int = pw_cfg.get("auto_scroll_max_scrolls", 20)
    step_delay_ms: int = pw_cfg.get("auto_scroll_step_delay_ms", 200)

    js_code = """\
async ([maxScrolls, stepDelayMs]) => {
    const step = window.innerHeight;
    let prevHeight = 0;
    let stableCount = 0;
    let scrollCount = 0;

    for (let i = 0; i < maxScrolls; i++) {
        const currentHeight = document.body.scrollHeight;
        if (currentHeight === prevHeight) {
            stableCount++;
            if (stableCount >= 2) break;
        } else {
            stableCount = 0;
        }
        prevHeight = currentHeight;
        window.scrollBy(0, step);
        scrollCount++;
        await new Promise(r => setTimeout(r, stepDelayMs));
    }

    window.scrollTo(0, 0);
    return scrollCount;
}"""

    try:
        scroll_count: int = page.evaluate(
            js_code,
            [max_scrolls, step_delay_ms],
        )
    except Exception as exc:
        logger.warning("Scroll for lazy content failed: %s", exc)
        return 0

    # Brief wait for any final lazy content to render
    try:
        page.wait_for_timeout(500)
    except Exception:
        pass

    logger.debug("Scrolled %d steps for lazy content", scroll_count)
    return scroll_count


# ---------------------------------------------------------------------------
# Function 4: Expand collapsible sections
# ---------------------------------------------------------------------------


def expand_collapsible_sections(page: "Any", pw_cfg: Dict[str, Any]) -> int:
    """Expand collapsed ``<details>``, accordion, and toggle sections.

    Opens all ``<details>`` elements that are not already open and clicks
    common accordion / toggle patterns.  Custom selectors can be provided
    via *pw_cfg* to override the built-in heuristics.

    ``<a>`` tags are explicitly excluded from click targets to avoid
    unintended navigation.

    Args:
        page: A Playwright ``Page`` object that has already navigated to a URL.
        pw_cfg: Playwright configuration dict.

    Returns:
        Number of sections expanded, or ``0`` if disabled / on error.
    """
    if not pw_cfg.get("expand_collapsibles_enabled", False):
        return 0

    custom_selectors: str = pw_cfg.get("expand_collapsibles_selectors", "")

    if custom_selectors:
        # ----- Custom selectors path -----
        selector_list = [s.strip() for s in custom_selectors.split(",") if s.strip()]
        js_code = """\
(selectors) => {
    let count = 0;
    for (const sel of selectors) {
        const elements = document.querySelectorAll(sel);
        for (const el of elements) {
            if (el.tagName === 'DETAILS' && !el.open) {
                el.open = true;
                count++;
            } else if (el.tagName !== 'A') {
                el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                count++;
            }
        }
    }
    return count;
}"""
        try:
            expanded: int = page.evaluate(js_code, selector_list)
        except Exception as exc:
            logger.warning("Expand collapsible sections (custom) failed: %s", exc)
            return 0
    else:
        # ----- Built-in heuristics path -----
        js_code = """\
() => {
    let count = 0;

    // Open all closed <details> elements
    const details = document.querySelectorAll('details:not([open])');
    for (const el of details) {
        el.open = true;
        count++;
    }

    // Click aria-expanded="false" elements (excluding <a>)
    const ariaCollapsed = document.querySelectorAll('[aria-expanded="false"]:not(a)');
    for (const el of ariaCollapsed) {
        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
        count++;
    }

    // Click data-toggle="collapse" elements (excluding <a>)
    const dataToggle = document.querySelectorAll('[data-toggle="collapse"]:not(a)');
    for (const el of dataToggle) {
        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
        count++;
    }

    // Click common accordion / toggle class patterns (excluding <a>)
    const classToggles = document.querySelectorAll(
        '.accordion-toggle:not(a), .collapse-toggle:not(a), .show-more:not(a)'
    );
    for (const el of classToggles) {
        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
        count++;
    }

    return count;
}"""
        try:
            expanded = page.evaluate(js_code)
        except Exception as exc:
            logger.warning("Expand collapsible sections failed: %s", exc)
            return 0

    # Wait for expand animations / content to load
    wait_ms: int = pw_cfg.get("expand_collapsibles_wait_ms", 500)
    if wait_ms > 0:
        try:
            page.wait_for_timeout(wait_ms)
        except Exception:
            pass

    logger.debug("Expanded %d collapsible sections", expanded)
    return expanded
