"""Playwright-based content provider for GPT Scraper V3.

Provides headless browser automation for fetching and converting web pages
to markdown.  Designed as a drop-in provider alongside Jina AI and Firecrawl,
integrated via the ``provider_sequence`` mechanism.

Optional dependency: if ``playwright`` is not installed, ``is_available()``
returns ``False`` and the provider is silently skipped.

.. note:: Resources follow a **per-thread** model.  The Playwright sync API
   binds each ``Playwright``/``Browser``/``BrowserContext`` to the OS thread
   that created it (greenlet ownership), so a single shared browser cannot be
   driven cross-thread.  Each worker thread therefore owns its own
   ``Playwright`` + ``Browser`` + ``BrowserContext`` via ``threading.local()``
   (``_PWThreadState``).  Every live state object is also recorded in a
   lock-guarded module registry (``_pw_registry``) so an atexit backstop can
   make a best-effort sweep of any thread that failed to clean up.

   The PRIMARY teardown is ``close_current_thread_playwright()``, which each
   worker MUST call from its own ``finally`` block (same-thread, greenlet-safe).
   The atexit sweep is only a last resort and may hit "greenlet" ownership
   errors when closing objects owned by other threads — those are logged, never
   raised.
"""
from __future__ import annotations

import atexit
import copy
import logging
import os
import re
import threading
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
# Per-thread browser lifecycle
# ---------------------------------------------------------------------------
#
# The Playwright sync API binds each Playwright/Browser/BrowserContext to the
# OS thread that created it.  We therefore keep one state object per thread in
# ``threading.local()`` and record every live state in a module-level,
# lock-guarded registry so an atexit backstop can sweep leftovers.


class _PWThreadState:
    """Per-thread Playwright resources (owned by the creating thread only)."""

    __slots__ = ("playwright", "browser", "context", "context_page_count")

    def __init__(self) -> None:
        self.playwright: Optional["Playwright"] = None
        self.browser: Optional["Browser"] = None
        self.context: Optional["BrowserContext"] = None
        self.context_page_count: int = 0


_pw_tls = threading.local()
_pw_registry: List["_PWThreadState"] = []
_pw_registry_lock = threading.Lock()
_atexit_registered: bool = False
_atexit_registered_lock = threading.Lock()


def _get_tls_state() -> "_PWThreadState":
    """Return the calling thread's ``_PWThreadState``, creating it lazily.

    A freshly created state is appended to the lock-guarded module registry so
    the atexit backstop can find threads that did not clean up after themselves.
    """
    state: Optional["_PWThreadState"] = getattr(_pw_tls, "state", None)
    if state is None:
        state = _PWThreadState()
        _pw_tls.state = state
        with _pw_registry_lock:
            _pw_registry.append(state)
    return state


def _ensure_atexit_registered() -> None:
    """Register the atexit backstop sweep exactly once (thread-safe)."""
    global _atexit_registered
    if _atexit_registered:
        return
    with _atexit_registered_lock:
        if not _atexit_registered:
            atexit.register(_atexit_sweep)
            _atexit_registered = True


def _close_state(state: "_PWThreadState") -> None:
    """Close a single state's context -> browser -> playwright (each guarded).

    Every close is wrapped in its own try/except.  This is shared by the
    same-thread teardown (``close_current_thread_playwright``) and the atexit
    backstop (which may legitimately hit greenlet ownership errors on foreign
    threads — those are logged, never raised).
    """
    if state.context is not None:
        try:
            state.context.close()
        except Exception as exc:
            logger.warning("Playwright cleanup: failed to close context: %s", exc)
        state.context = None

    if state.browser is not None:
        try:
            state.browser.close()
        except Exception as exc:
            logger.warning("Playwright cleanup: failed to close browser: %s", exc)
        state.browser = None

    if state.playwright is not None:
        try:
            state.playwright.stop()
        except Exception as exc:
            logger.warning("Playwright cleanup: failed to stop playwright: %s", exc)
        state.playwright = None

    state.context_page_count = 0


def close_current_thread_playwright() -> None:
    """Close the CALLING thread's Playwright resources and deregister them.

    This is the PRIMARY teardown path: each worker thread MUST call it from its
    own ``finally`` block.  Because it runs on the owning thread, all closes are
    greenlet-safe.  Each close is individually guarded; the state is zeroed and
    removed from the module registry afterwards so the atexit backstop has
    nothing left to sweep for this thread.
    """
    state: Optional["_PWThreadState"] = getattr(_pw_tls, "state", None)
    if state is None:
        return

    logger.info("Playwright cleanup: tearing down resources for thread '%s'",
                threading.current_thread().name)

    _close_state(state)

    with _pw_registry_lock:
        try:
            _pw_registry.remove(state)
        except ValueError:
            pass
    _pw_tls.state = None


def _atexit_sweep() -> None:
    """Backstop ONLY: best-effort close of any state left in the registry.

    The PRIMARY teardown is ``close_current_thread_playwright()`` invoked on
    each owning worker thread.  This sweep exists solely to mop up states whose
    owning thread exited without cleaning up (a bug or an abrupt shutdown).  It
    runs on the interpreter-shutdown thread, so closing a foreign thread's
    objects can raise greenlet ownership errors — every close is wrapped in
    try/except and such errors are logged at debug/warning level, NEVER raised.
    """
    with _pw_registry_lock:
        leftovers = list(_pw_registry)
        _pw_registry.clear()

    if not leftovers:
        return

    logger.warning(
        "Playwright atexit backstop: sweeping %d leftover thread state(s) "
        "(primary teardown should have closed these on their owning threads)",
        len(leftovers),
    )
    for state in leftovers:
        try:
            _close_state(state)
        except Exception as exc:  # defensive: _close_state already guards each step
            logger.debug("Playwright atexit backstop: close failed: %s", exc)


def get_browser() -> "Browser":
    """Return the calling thread's Browser instance, launching it lazily."""
    state = _get_tls_state()

    if state.browser is not None:
        return state.browser

    if not _PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "Playwright is not installed. Install with: pip install playwright && playwright install chromium"
        )

    cfg = get_config()
    pw_cfg = cfg.PLAYWRIGHT_CONFIG

    state.playwright = sync_playwright().start()

    # Register atexit backstop exactly once (primary teardown is in-worker).
    _ensure_atexit_registered()

    # Select browser engine (chromium, firefox, webkit)
    browser_type_name = pw_cfg.get("browser", "chromium")
    browser_type = getattr(state.playwright, browser_type_name)

    # Build launch kwargs
    launch_kwargs: Dict[str, Any] = {
        "headless": pw_cfg.get("headless", True),
    }
    executable_path = pw_cfg.get("executable_path", "")
    if executable_path:
        launch_kwargs["executable_path"] = executable_path

    try:
        state.browser = browser_type.launch(**launch_kwargs)
    except PlaywrightError as exc:
        if "executable doesn't exist" in str(exc).lower():
            logger.error(
                "PLAYWRIGHT: Browser executable not found. "
                "Run: playwright install %s",
                browser_type_name,
            )
        raise

    logger.info(
        "Playwright %s browser launched (headless=%s) on thread '%s'",
        browser_type_name,
        launch_kwargs["headless"],
        threading.current_thread().name,
    )
    return state.browser


def _get_context() -> "BrowserContext":
    """Return the calling thread's BrowserContext, recycling it at the page limit."""
    state = _get_tls_state()

    cfg = get_config()
    pw_cfg = cfg.PLAYWRIGHT_CONFIG
    context_pages_limit = pw_cfg.get("context_pages_limit", 500)

    # Recycle context if page count exceeds limit (per-thread)
    if state.context is not None and state.context_page_count >= context_pages_limit:
        try:
            state.context.close()
        except Exception:
            pass
        state.context = None
        state.context_page_count = 0
        logger.info(
            "Playwright context recreated after %d pages on thread '%s'",
            context_pages_limit, threading.current_thread().name,
        )

    if state.context is not None:
        return state.context

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

    context = browser.new_context(**context_kwargs)
    state.context = context

    # Route-based resource blocking
    block_resources: List[str] = pw_cfg.get("block_resources", [])
    if block_resources:
        resource_set = set(block_resources)

        def _block_handler(route, request):  # type: ignore[no-untyped-def]
            if request.resource_type in resource_set:
                route.abort()
            else:
                route.continue_()

        context.route("**/*", _block_handler)

    # URL pattern blocking
    block_urls: List[str] = pw_cfg.get("block_urls", [])
    for pattern in block_urls:
        context.route(pattern, lambda route, _request: route.abort())

    # Inject cookies
    cookies: List[Dict[str, Any]] = pw_cfg.get("cookies", [])
    if cookies:
        context.add_cookies(cookies)

    # Apply stealth anti-detection
    if pw_cfg.get("stealth", True):
        _apply_stealth_scripts(context)

    return context


def _reset_playwright() -> None:
    """Full teardown for crash recovery — reset ONLY the calling thread's state.

    Invoked from the crash-recovery branches of ``fetch_playwright_markdown`` /
    ``fetch_playwright_html``, which run INSIDE the worker thread that owns the
    state.  Closing on the owning thread is greenlet-safe.  The state is also
    removed from the registry; a subsequent call on this thread lazily creates a
    fresh one.  Other threads' states are untouched, so one worker's crash never
    disturbs a sibling worker's live browser.
    """
    state: Optional["_PWThreadState"] = getattr(_pw_tls, "state", None)
    if state is None:
        return

    logger.warning(
        "Playwright: performing full reset of browser resources on thread '%s'",
        threading.current_thread().name,
    )

    if state.context is not None:
        try:
            state.context.close()
        except Exception:
            pass
        state.context = None

    if state.browser is not None:
        try:
            state.browser.close()
        except Exception:
            pass
        state.browser = None

    if state.playwright is not None:
        try:
            state.playwright.stop()
        except Exception:
            pass
        state.playwright = None

    state.context_page_count = 0

    with _pw_registry_lock:
        try:
            _pw_registry.remove(state)
        except ValueError:
            pass
    _pw_tls.state = None


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


def _best_from_srcset(value: str) -> str:
    """Return the best candidate URL from a srcset/data-srcset value.

    Robust against commas inside URLs (Cloudinary `w_100,h_50`, query strings,
    data-URIs). Picks the highest `w` descriptor; else the highest `x` density;
    else the first candidate. A candidate with no descriptor is treated as 1x.
    """
    if not value:
        return ""
    # WHATWG-style tokenizer: a URL is a run of non-whitespace; if it ends with a
    # comma it is a no-descriptor candidate, otherwise the descriptor runs up to
    # the next comma. This correctly handles BOTH comma-no-space separators
    # (`/a.jpg 100w,/b.jpg 200w`) AND commas INSIDE URLs (Cloudinary
    # `w_100,h_50`, query strings), which a plain comma split cannot.
    candidates = []  # list of (url, descriptor)
    i, n = 0, len(value)
    while i < n:
        while i < n and (value[i].isspace() or value[i] == ","):
            i += 1
        if i >= n:
            break
        start = i
        while i < n and not value[i].isspace():
            i += 1
        url = value[start:i]
        desc = ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            while i < n and value[i].isspace():
                i += 1
            dstart = i
            while i < n and value[i] != ",":
                i += 1
            desc = value[dstart:i].strip()
            if i < n and value[i] == ",":
                i += 1
        if url:
            candidates.append((url, desc))

    best_url = ""
    best_w = -1.0
    best_x = -1.0
    for url, desc in candidates:
        # Reject a "URL" that is actually a bare descriptor (malformed srcset).
        if re.match(r"^[\d.]+[wx]$", url, re.IGNORECASE):
            continue
        desc = desc.lower()
        if desc.endswith("w"):
            try:
                w = float(desc[:-1])
            except ValueError:
                w = 0.0
            if w > best_w:
                best_w = w
                best_url = url
        elif desc.endswith("x"):
            try:
                x = float(desc[:-1])
            except ValueError:
                x = 1.0
            if best_w < 0 and x > best_x:  # only honour x when no w-candidate won
                best_x = x
                best_url = url
        else:
            # no descriptor -> treat as 1x
            if best_w < 0 and best_x < 1.0:
                best_x = 1.0
                if not best_url:
                    best_url = url
    return best_url


def _is_spacer_dimensions(img) -> bool:
    """True only when BOTH width and height attrs are exactly 1 (tolerant of 'px')."""
    def _is_one(v):
        if v is None:
            return False
        return bool(re.match(r'^\s*1\s*(px)?\s*$', str(v), re.IGNORECASE))
    return _is_one(img.get("width")) and _is_one(img.get("height"))


def _normalize_images(soup, base_url):
    """Rewrite <img> tags in-place so markdownify emits ![alt](absolute_url).

    Recovers lazy-loaded URLs, drops junk (data-URIs/spacers), resolves
    relative URLs to absolute. Never raises. Operates only when image markdown
    output is desired (caller gates on mode == 'markdown').
    """
    from urllib.parse import urljoin

    # Promote <picture><source srcset> into the inner <img> when src looks like a placeholder.
    for picture in soup.find_all("picture"):
        img = picture.find("img")
        if img is None:
            continue
        cur = (img.get("src") or "").strip()
        if (not cur) or cur.startswith("data:"):
            best = ""
            for source in picture.find_all("source"):
                cand = _best_from_srcset(source.get("srcset") or source.get("data-srcset") or "")
                if cand:
                    best = cand  # last/best source wins; sources are usually ordered
                    break
            if best:
                img["src"] = best

    for img in list(soup.find_all("img")):
        try:
            # 1. Resolve the real source in priority order.
            src = ""
            for attr in ("data-src", "data-original", "data-lazy-src", "data-lazy"):
                val = (img.get(attr) or "").strip()
                if val:
                    src = val
                    break
            if not src:
                src = _best_from_srcset(img.get("srcset") or img.get("data-srcset") or "")
            if not src:
                src = (img.get("src") or "").strip()

            # 2. Drop junk.
            if not src or src.startswith("data:"):
                img.decompose()
                continue
            if _is_spacer_dimensions(img):
                img.decompose()
                continue

            # 3. Resolve relative -> absolute (only when base_url is usable).
            if base_url:
                try:
                    src = urljoin(base_url, src)
                except Exception:
                    pass

            # 4. Write back the clean src; strip lazy/srcset attrs so nothing re-introduces a placeholder.
            img["src"] = src
            for attr in ("srcset", "data-srcset", "data-src", "data-original",
                         "data-lazy-src", "data-lazy"):
                if img.has_attr(attr):
                    del img[attr]
        except Exception as exc:
            logger.debug("Playwright: image normalization skipped one <img>: %s", exc)
            continue


# Tags whose presence inside an <a> marks it as an image- or heading-bearing
# "card" link that markdownify renders as a single `[...](href)` spanning block
# content, producing broken multi-line markdown such as
# `[![alt](img)\n### Heading](/href)`.  Deliberately NARROW: we only unwrap
# anchors that wrap images or headings (the cases that break image markdown).
# Anchors wrapping a bare <div>/<ul>/etc. with no image/heading are left intact
# so ordinary CTA / navigational link hrefs are preserved.
_BLOCK_LINK_DESCENDANTS = (
    "img", "figure", "picture",
    "h1", "h2", "h3", "h4", "h5", "h6",
)


def _unwrap_block_links(soup) -> None:
    """Unwrap <a> tags that wrap block-level content (images, headings, cards).

    Such anchors otherwise become a single markdown link spanning the block
    content, e.g. ``[![alt](img)\\n### Heading](/href)`` — leaving an orphan
    ``[`` and a broken closing ``](/href)``.  Unwrapping keeps the inner
    content (image, heading) as clean standalone markdown.  Purely-inline text
    links (email, phone, in-text links) contain none of these tags and are
    preserved.  Never raises.
    """
    try:
        for a in list(soup.find_all("a")):
            try:
                if a.find(_BLOCK_LINK_DESCENDANTS) is not None:
                    a.unwrap()
            except Exception:
                continue
    except Exception as exc:
        logger.debug("Playwright: block-link unwrap failed: %s", exc)


def _markdownify_safe_images(html: str, md_kwargs: Dict[str, Any]) -> str:
    """Run markdownify with a ``convert_img`` override that ALWAYS emits ``![alt](url)``.

    markdownify's stock ``convert_img`` returns the bare alt text (dropping the
    URL) whenever an image sits in an inline context (a heading or table cell)
    whose direct parent tag is not allow-listed — and the allow-list can never
    cover every wrapper tag (``span``/``strong``/``picture``/...).  Overriding
    ``convert_img`` to unconditionally emit a well-formed image guarantees the
    URL survives regardless of where the ``<img>`` sits.  We also sanitise the
    alt text and the URL so special characters can never break ``![alt](url)``
    syntax (``]`` in alt, spaces/parens in the URL, etc.).
    """
    from markdownify import MarkdownConverter  # type: ignore[import-untyped]

    class _ImgSafeConverter(MarkdownConverter):  # type: ignore[misc, valid-type]
        def convert_img(self, el, text, *args, **kwargs):  # type: ignore[no-untyped-def]
            alt = el.attrs.get("alt", None) or ""
            src = el.attrs.get("src", None) or ""
            title = el.attrs.get("title", None) or ""
            # Sanitise alt so it cannot break the ![...]() syntax.
            alt = alt.replace("\r", " ").replace("\n", " ")
            alt = alt.replace("![", "(").replace("[", "(").replace("]", ")")
            # Sanitise the URL: CommonMark angle-bracket form legally allows
            # spaces and parentheses; neutralise any literal angle brackets.
            if src and re.search(r"[\s()]", src):
                src = "<" + src.replace("<", "%3C").replace(">", "%3E") + ">"
            title_part = ' "%s"' % title.replace('"', r"\"") if title else ""
            return "![%s](%s%s)" % (alt, src, title_part)

    return _ImgSafeConverter(**md_kwargs).convert(html)


def _html_to_markdown(
    html: str,
    remove_selectors: Optional[List[str]] = None,
    target_selectors: Optional[List[str]] = None,
    pw_cfg: Optional[Dict[str, Any]] = None,
    base_url: Optional[str] = None,
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

            - ``image_handling`` (str, default ``"alt_text"``): 3-way image
              mode.  ``"markdown"`` keeps real images as ``![alt](absolute_url)``
              (lazy/srcset URLs recovered, relative URLs resolved against
              ``base_url``, data-URI/spacer images dropped); ``"alt_text"``
              rewrites images to ``[Image: alt text]`` markers (dropping
              images whose alt text is shorter than 3 chars); ``"strip"``
              removes ``<img>`` tags entirely.
            - ``table_infer_header`` (bool, default ``True``): when ``True``,
              markdownify receives ``table_infer_header=True`` so headerless
              HTML tables get an inferred header row.
        base_url: Page URL used to resolve relative image ``src`` values to
            absolute URLs when ``image_handling`` is ``"markdown"``.

    Returns:
        A ``(markdown_content, title)`` tuple.
    """
    if pw_cfg is None:
        pw_cfg = {}

    mode = str(pw_cfg.get("image_handling", "alt_text")).strip().lower()

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
            logger.warning(
                "Playwright: target_selectors %s matched nothing — falling back to whole document",
                target_selectors,
            )

    # Generic structural boilerplate pass (additive, safe-by-default).
    # Runs on BOTH the target-selector path and the whole-document fallback.
    # Strips navigation / language-widget / search / cookie containers that are
    # frequently siblings of <header> and thus missed by remove_selectors.
    if pw_cfg.get("strip_boilerplate_default", True):
        boilerplate_selectors = (
            "nav, header, footer, aside, form, "
            '[role="navigation"], [role="search"], '
            "#google_translate_element, .goog-te-combo, #languages, "
            "#main-nav, #fullscreen-search, "
            '[id*="translate" i], [id*="search" i], '
            '[class*="cookie" i], [class*="menu" i]'
        )
        # Snapshot pre-strip HTML so we can fall back if over-stripping empties the soup.
        pre_strip_html = str(working_soup)
        try:
            for el in working_soup.select(boilerplate_selectors):
                try:
                    el.decompose()
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("Playwright: boilerplate strip pass failed: %s", exc)
        # Never return empty because of over-stripping.
        if not working_soup.get_text(strip=True):
            logger.warning(
                "Playwright: boilerplate strip emptied the document — "
                "reverting to pre-strip content"
            )
            working_soup = BeautifulSoup(pre_strip_html, "html.parser")

    # Remove selector filtering
    if remove_selectors:
        for selector in remove_selectors:
            for el in working_soup.select(selector):
                el.decompose()

    if mode == "markdown":
        try:
            _normalize_images(working_soup, base_url)
        except Exception as exc:
            logger.debug("Playwright: image normalization failed: %s", exc)
        # Unwrap card/block links so images render as standalone ![alt](url)
        # instead of being wrapped in a multi-line [...](href) link.
        _unwrap_block_links(working_soup)

    # Convert to markdown
    try:
        from markdownify import markdownify as md  # type: ignore[import-untyped]

        strip_tags = ["svg"]  # Always strip SVG
        if mode == "strip":
            strip_tags.append("img")

        md_kwargs: Dict[str, Any] = {
            "heading_style": "ATX",
            "bullets": "-",
            "strip": strip_tags,
        }
        if pw_cfg.get("table_infer_header", True):
            md_kwargs["table_infer_header"] = True

        if mode == "markdown":
            # Custom converter guarantees ![alt](url) even for images nested in
            # headings / table cells, with alt/URL sanitisation.
            markdown = _markdownify_safe_images(str(working_soup), md_kwargs)
        else:
            markdown = md(str(working_soup), **md_kwargs)
    except ImportError:
        logger.warning(
            "markdownify not installed, falling back to plain text extraction. "
            "Install with: pip install markdownify"
        )
        from gpt_scraper_v3.utilities import html_to_plain_text

        markdown = html_to_plain_text(str(working_soup))

    # Post-process images according to mode.
    if mode == "alt_text":
        def _simplify_image(match: "re.Match") -> str:  # type: ignore[type-arg]
            alt = match.group(1).strip()
            if len(alt) < 3:
                return ""
            return f"[Image: {alt}]"

        markdown = re.sub(
            r'!\[([^\]]*)\]\([^)]*(?:\s+"[^"]*")?\)', _simplify_image, markdown
        )
    elif mode == "markdown":
        # Belt-and-suspenders: drop any residual data-URI image that slipped through.
        markdown = re.sub(r'!\[[^\]]*\]\(\s*data:[^)]*\)', "", markdown)
        # Drop any empty-URL image artifact (e.g. a src-less <img> that bypassed
        # normalization) so we never emit a malformed `![alt]()`.
        markdown = re.sub(r'!\[[^\]]*\]\(\s*\)', "", markdown)

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
    # The politeness-tier rate-limiter slot is held for the entire
    # network-active region (navigation + smart-wait + scroll/expand + capture)
    # because the page is actively driving the TARGET site throughout. It is
    # released as soon as this function returns -- BEFORE the caller performs
    # the CPU-bound HTML->markdown conversion -- so we never hold a politeness
    # slot during pure local work.
    from gpt_scraper_v3.rate_limiter import get_rate_limiter

    with get_rate_limiter().acquire(url):
        # -- Navigate -------------------------------------------------------
        wait_until = pw_cfg.get("wait_until", "domcontentloaded")
        page.goto(url, wait_until=wait_until)

        # -- Wait strategy --------------------------------------------------
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

        # -- Cookie banner dismissal (always, regardless of content actions) -
        from gpt_scraper_v3.playwright_page_actions import dismiss_cookie_banners

        dismiss_cookie_banners(page, pw_cfg)

        # -- Content actions (gated) ----------------------------------------
        if include_content_actions:
            from gpt_scraper_v3.playwright_page_actions import (
                scroll_for_lazy_content,
                expand_collapsible_sections,
            )

            did_scroll = scroll_for_lazy_content(page, pw_cfg)
            did_expand = expand_collapsible_sections(page, pw_cfg)
            if did_scroll or did_expand:
                page.wait_for_timeout(300)

        # -- Custom JS execution --------------------------------------------
        js_code = pw_cfg.get("javascript_to_execute", "")
        if js_code:
            logger.warning("Executing custom JavaScript from config (security: verify source)")
            page.evaluate(js_code)

        # -- Capture HTML (still on the target site) ------------------------
        html = page.content()

    # -- Validate HTML (slot released; markdown conversion happens in caller) -
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
# Retryable network errors (Chromium net:: errors that are transient)
# ---------------------------------------------------------------------------

_RETRYABLE_NET_ERRORS = (
    "net::err_connection_timed_out",
    "net::err_connection_reset",
    "net::err_timed_out",
    "net::err_connection_closed",
    "net::err_connection_aborted",
    "net::err_network_changed",
    "net::err_socket_not_connected",
    "net::err_http2_ping_failed",
)


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
                page = context.new_page()
                # Bug 11: count the page only AFTER new_page() succeeds.
                # context_page_count is thread-local (no lock needed).
                _get_tls_state().context_page_count += 1

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

                    markdown, title = _html_to_markdown(html, r_sels, t_sels, pw_cfg=active_pw_cfg, base_url=url)

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

                # Retryable transient network errors
                if any(net_err in error_msg for net_err in _RETRYABLE_NET_ERRORS):
                    logger.error(
                        "PLAYWRIGHT: Transient network error for %s (attempt %d/%d): %s",
                        url, attempt + 1, max_retries + 1, e,
                    )
                    if attempt < max_retries:
                        continue
                    # All retries exhausted
                    return (None, None, None)

                # Non-retryable errors
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
            page = context.new_page()
            # Bug 11: count the page only AFTER new_page() succeeds.
            # context_page_count is thread-local (no lock needed).
            _get_tls_state().context_page_count += 1

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

            # Retryable transient network errors
            if any(net_err in error_msg for net_err in _RETRYABLE_NET_ERRORS):
                logger.error(
                    "PLAYWRIGHT HTML: Transient network error for %s (attempt %d/%d): %s",
                    url, attempt + 1, max_retries + 1, e,
                )
                if attempt < max_retries:
                    continue
                return None

            # Non-retryable errors
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
