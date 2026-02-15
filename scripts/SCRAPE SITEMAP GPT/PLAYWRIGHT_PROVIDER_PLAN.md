# Implementation Plan: Playwright Content Provider for GPT Scraper V3

## Approach

Add Playwright (sync API) as a third content provider alongside Jina AI and Firecrawl. Create a new `playwright_provider.py` module with singleton browser lifecycle, stealth anti-detection, configurable CSS selectors, network resource blocking, and HTML-to-markdown conversion via `markdownify`. Integrate through the existing `provider_sequence` mechanism with minimal changes to existing code. The provider is fully optional -- if Playwright is not installed, the scraper falls back to Jina/Firecrawl automatically.

## Plan Reviewed By

- `architect-reviewer` -- Validated module boundaries, dependency graph, config evolution, optional dependency pattern. Key feedback incorporated: conditional API key validation, merge overlapping file tasks, context memory management, provider sequence validation.
- `python-pro` -- Validated singleton lifecycle, atexit cleanup ordering, error containment, type safety. Key feedback incorporated: immediate atexit registration after `.start()`, full `_reset_playwright()` teardown, internal retry logic, periodic context recreation, defensive page.close() pattern.

## File Ownership Map

| Subagent Role | Owned Files |
|---|---|
| `python-pro` (Task 1) | `gpt_scraper_v3/utilities.py` (add `html_to_plain_text()`), `gpt_scraper_v3/content_fetching.py` (extract BS4 code + `requires_api_key` flag) |
| `python-pro` (Task 2) | `gpt_scraper_v3/config.py` (Playwright config loading + conditional API key validation) |
| `python-pro` (Task 3) | `gpt_scraper_v3/playwright_provider.py` (CREATE -- the main new module) |
| `python-pro` (Task 4) | `gpt_scraper_v3/content_fetching.py` (elif dispatch for Playwright) |
| `python-pro` (Task 5) | JSON config template + documentation |

---

## Task List

### Task 1: Extract html_to_plain_text() + Add requires_api_key flag

- **Subagent:** `python-pro`
- **Phase:** 2A-Preparatory
- **Files:** `gpt_scraper_v3/utilities.py`, `gpt_scraper_v3/content_fetching.py`
- **Dependencies:** None
- **Description:**

  **Part A: Extract shared utility.**
  Extract the duplicated BeautifulSoup HTML-to-text conversion from `content_fetching.py` strategies 4 and 5 into a shared utility function in `utilities.py`:

  ```python
  def html_to_plain_text(html: str) -> str:
      """Convert HTML to plain text by removing scripts/styles and extracting text.

      Strips ``script``, ``style``, ``meta``, ``link``, and ``noscript`` tags,
      then extracts visible text with double-newline paragraph separation.
      """
      from bs4 import BeautifulSoup
      soup = BeautifulSoup(html, "html.parser")
      for tag in soup(["script", "style", "meta", "link", "noscript"]):
          tag.decompose()
      text_content = soup.get_text(separator="\n", strip=True)
      lines = [ln.strip() for ln in text_content.split("\n") if ln.strip()]
      return "\n\n".join(lines)
  ```

  Replace the inline code in `_fetch_jina_markdown()` Strategy 4 (around lines 306-312) and Strategy 5 (around lines 341-346) with:
  ```python
  from gpt_scraper_v3.utilities import html_to_plain_text
  cleaned = html_to_plain_text(html_content)
  ```

  **Part B: Add requires_api_key flag.**
  Modify the API key check in `get_markdown_content()` (around line 120-121) from:
  ```python
  if not api_key:
  ```
  To:
  ```python
  if prov_cfg.get("requires_api_key", True) and not api_key:
  ```
  This allows providers like Playwright (which set `requires_api_key: False` in their config) to bypass the API key check. Defaults to `True` for backward compatibility.

- **Acceptance criteria:**
  - [ ] `html_to_plain_text()` exists in `utilities.py`, output identical to the original inline code
  - [ ] Strategies 4 and 5 call the shared function
  - [ ] `requires_api_key` flag check works -- providers with `requires_api_key: False` are not skipped
  - [ ] Existing jina/firecrawl providers still require API keys (default `True`)
  - [ ] All 15+ existing JSON configs work without modification
- **Do NOT touch:** `config.py`, `playwright_provider.py`, CLI arguments

---

### Task 2: Add Playwright config loading + conditional API key validation

- **Subagent:** `python-pro`
- **Phase:** 2A-Preparatory
- **Files:** `gpt_scraper_v3/config.py`
- **Dependencies:** None (can run in parallel with Task 1)
- **Description:**

  **Part A: Add PLAYWRIGHT_CONFIG to ScraperConfig.**
  Add this field to the `ScraperConfig` dataclass:
  ```python
  PLAYWRIGHT_CONFIG: Dict[str, Any] = field(default_factory=dict)
  ```

  **Part B: Load Playwright config in `load_configuration()`.**
  After the existing `MARKDOWN_PROVIDERS` construction (around line 449), add code to:
  1. Read `raw["content_providers"].get("playwright", {})` (safe `.get()`, no KeyError)
  2. Build `cfg.PLAYWRIGHT_CONFIG` dict with these keys and defaults:

  | Key | Type | Default | Description |
  |-----|------|---------|-------------|
  | `browser` | str | `"chromium"` | Browser engine: chromium, firefox, webkit |
  | `headless` | bool | `True` | Headless mode |
  | `stealth` | bool | `True` | Enable anti-detection stealth patches |
  | `viewport` | dict | `{"width": 1920, "height": 1080}` | Browser viewport size |
  | `user_agent` | str | `""` | Custom user agent (empty = browser default) |
  | `locale` | str | `"cs-CZ"` | Browser locale |
  | `timezone_id` | str | `"Europe/Prague"` | Timezone |
  | `extra_http_headers` | dict | `{}` | Additional HTTP headers |
  | `remove_selectors` | str | `""` | CSS selectors to remove (comma-separated) |
  | `target_selectors` | str | `""` | CSS selectors to target (comma-separated) |
  | `wait_until` | str | `"domcontentloaded"` | Page load wait: load, domcontentloaded, networkidle, commit |
  | `wait_for_selector` | str | `""` | Wait for this CSS selector after page load |
  | `wait_for_timeout_ms` | int | `0` | Additional wait in ms after load (0 = none) |
  | `navigation_timeout_ms` | int | `30000` | Navigation timeout in ms |
  | `javascript_to_execute` | str | `""` | Custom JS to run after page load |
  | `block_resources` | list | `["image", "font", "media"]` | Resource types to block for performance |
  | `block_urls` | list | `[]` | URL patterns to block (e.g., analytics) |
  | `cookies` | list | `[]` | Cookies to inject into browser context |
  | `storage_state` | str | `""` | Path to saved browser storage state file |
  | `ignore_https_errors` | bool | `False` | Ignore SSL certificate errors |
  | `executable_path` | str | `""` | Custom browser binary path (empty = bundled) |
  | `context_pages_limit` | int | `500` | Recreate browser context every N pages to prevent memory creep |

  3. **Conditionally** register Playwright in `MARKDOWN_PROVIDERS` only if the `"playwright"` key exists in the JSON config:
  ```python
  if "playwright" in raw.get("content_providers", {}):
      cfg.MARKDOWN_PROVIDERS["playwright"] = {
          "name": "playwright",
          "requires_api_key": False,
      }
  ```

  **Part C: Make API key validation conditional on provider_sequence.**
  Modify `validate_config()` (around lines 165-168) to NOT require `api_keys.jina_ai` and `api_keys.firecrawl` as unconditionally mandatory. Instead:
  - `api_keys.jina_ai` is required only if `"jina"` is in `provider_sequence`
  - `api_keys.firecrawl` is required only if `"firecrawl"` is in `provider_sequence`
  - `api_keys.openai` remains always required (used for vector store, not content fetching)

  Also update lines 277-278 (`cfg.JINA_AI_API_KEY = raw["api_keys"]["jina_ai"]`) to use `.get()` with empty string default instead of direct key access, to prevent KeyError when these keys are absent.

  **Part D: Provider sequence validation.**
  After loading, verify every provider ID in `MARKDOWN_PROVIDER_SEQUENCE` has a corresponding entry in `MARKDOWN_PROVIDERS`. Log a clear WARNING for any missing provider with actionable guidance (e.g., "Provider 'playwright' is in provider_sequence but not configured in content_providers. It will be skipped.").

- **Acceptance criteria:**
  - [ ] `PLAYWRIGHT_CONFIG` dict populated with all 22 options and safe defaults
  - [ ] Playwright registered in `MARKDOWN_PROVIDERS` only when configured in JSON
  - [ ] API key validation is conditional on `provider_sequence` (Playwright-only config works)
  - [ ] `api_keys.jina_ai` and `api_keys.firecrawl` loaded with `.get()`, no KeyError
  - [ ] Provider sequence validation logs clear warnings for misconfigured providers
  - [ ] All 15+ existing JSON configs (without playwright section) work unchanged
- **Do NOT touch:** `content_fetching.py`, `playwright_provider.py`

---

### Task 3: Create playwright_provider.py module

- **Subagent:** `python-pro`
- **Phase:** 2B-Core Implementation
- **Files:** `gpt_scraper_v3/playwright_provider.py` (CREATE)
- **Dependencies:** Task 2 (needs PLAYWRIGHT_CONFIG in ScraperConfig)
- **Description:**

  Create the new Playwright provider module (~300-350 lines) with these components:

  #### 3a. Module-level optional dependency handling
  ```python
  _PLAYWRIGHT_AVAILABLE = False
  try:
      from playwright.sync_api import (
          sync_playwright, Playwright, Browser, BrowserContext, Page,
          TimeoutError as PlaywrightTimeout, Error as PlaywrightError,
      )
      _PLAYWRIGHT_AVAILABLE = True
  except ImportError:
      pass

  def is_available() -> bool:
      """Check if Playwright Python package is installed."""
      return _PLAYWRIGHT_AVAILABLE
  ```

  #### 3b. Singleton browser lifecycle (mirrors get_session() pattern)

  Three module-level singletons: `_playwright: Optional[Playwright]`, `_browser: Optional[Browser]`, `_context: Optional[BrowserContext]`.

  **`get_browser() -> Browser`:**
  - Lazy init: calls `sync_playwright().start()` to get Playwright instance
  - **CRITICAL**: Register `atexit.register(_cleanup_playwright)` IMMEDIATELY after `.start()` succeeds, BEFORE calling `browser_type.launch()`. This prevents Playwright subprocess leaks if launch() fails.
  - Select browser engine from `cfg.PLAYWRIGHT_CONFIG["browser"]` via `getattr(_playwright, browser_type_name)`
  - Launch with `headless=cfg.PLAYWRIGHT_CONFIG["headless"]` and optional `executable_path`
  - If launch fails with "Executable doesn't exist" error, log actionable message: "Browser not installed. Run: playwright install chromium"
  - Log at INFO: "Playwright chromium browser launched (headless=True)"

  **`_get_context() -> BrowserContext`:**
  - Lazy init from `get_browser().new_context(...)` with all configured options:
    - `viewport`, `user_agent`, `locale`, `timezone_id`, `extra_http_headers`, `storage_state`, `ignore_https_errors`
  - Set up route-based network blocking via `context.route("**/*", handler)` that checks `request.resource_type in block_resources` and aborts matching requests
  - Set up URL pattern blocking for each pattern in `block_urls`
  - Inject cookies from config via `context.add_cookies()`
  - Apply stealth patches via `_apply_stealth_scripts(context)` if `stealth=True`
  - Track page count: `_context_page_count = 0` module-level counter
  - On each call, increment counter. If `_context_page_count >= context_pages_limit` (default 500), recreate the context to prevent memory creep from accumulated cookies/localStorage/caches.

  **`_cleanup_playwright()`:**
  atexit handler. Cleanup order: context.close() -> browser.close() -> playwright.stop(). Each wrapped in try/except. Nullifies all singletons.

  **`_reset_playwright()`:**
  Full teardown for crash recovery. Closes context, browser, and calls playwright.stop(). Sets ALL three singletons to None. Next call to get_browser() does a completely fresh launch. Log at WARNING level.

  #### 3c. Stealth anti-detection

  **`_apply_stealth_scripts(context: BrowserContext)`:**
  - Try to import `playwright_stealth` package; if available, use it for comprehensive stealth
  - If not installed, apply manual stealth via `context.add_init_script()`:
    - Mask `navigator.webdriver` (return `undefined`)
    - Add `window.chrome.runtime` object
    - Mock `navigator.plugins` with realistic Plugin-like objects (NOT integers -- use objects with `name`, `description`, `filename` properties to pass anti-bot checks)
    - Set `navigator.languages` to match configured locale (e.g., `['cs-CZ', 'cs', 'en-US', 'en']`)
    - Override `Permissions.query` for notifications
  - Log WARNING if playwright-stealth not installed, INFO if stealth applied

  #### 3d. HTML-to-markdown conversion

  **`_html_to_markdown(html: str, remove_selectors: Optional[list], target_selectors: Optional[list]) -> Tuple[str, str]`:**
  Returns `(markdown_content, title)`.

  Steps:
  1. Parse with `BeautifulSoup(html, "html.parser")`
  2. Extract `<title>` text before any DOM manipulation
  3. Remove always-unwanted tags: `script`, `style`, `noscript`, `iframe`, `svg`
  4. **Selector order (important):** Apply `target_selectors` FIRST (extract matching subtrees), then `remove_selectors` within those subtrees. If `target_selectors` is empty, work on whole document.
  5. Convert to markdown using `markdownify` with these exact kwargs:
     ```python
     md(html_str,
        heading_style="ATX",          # # Heading style
        bullets="-",                   # - for unordered lists
        strip=["img", "svg"],         # strip images (blocked at network level)
     )
     ```
  6. Post-process: collapse excessive blank lines (max 2 consecutive)
  7. If `markdownify` is not installed, fall back to `html_to_plain_text()` from `utilities.py`

  **`_parse_selectors(selector_string: str) -> list`:**
  Helper to split comma-separated CSS selector string into a list. Handles empty strings.

  **Selector precedence:** The `remove_selectors` parameter passed from `get_markdown_content()` (which originates from `content_providers.jina.remove_selectors` or CLI `--jina-remove-selectors`) is used if non-empty. Otherwise, falls back to `PLAYWRIGHT_CONFIG["remove_selectors"]`. The Playwright-specific config selectors are always used for `target_selectors` (there is no per-call target_selectors parameter).

  #### 3e. Main fetch function

  ```python
  def fetch_playwright_markdown(
      url: str,
      remove_selectors: Optional[str] = None,
  ) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
  ```

  **CRITICAL: This function NEVER raises exceptions to callers.** All Playwright-specific exceptions are caught internally and the function returns `(None, None, None)` on any failure. This prevents the retry loop in `get_markdown_content()` from pointlessly retrying browser crashes.

  **Internal retry logic:** The function implements its OWN retry for transient failures (PlaywrightTimeout), separate from the HTTP-level retry in `get_markdown_content()`. Retry up to 2 times on timeout with doubling backoff. Do NOT retry on PlaywrightError (browser crashes, DNS failures, etc.) -- return (None, None, None) immediately for these.

  Steps per attempt:
  1. `page = _get_context().new_page()`
  2. IMMEDIATELY enter `try: ... finally: page.close()` -- the `page.close()` must be structurally guaranteed. Wrap `page.close()` itself in `try/except Exception` so a close failure does not mask the original error.
  3. `page.goto(url, wait_until=cfg.PLAYWRIGHT_CONFIG["wait_until"])`
  4. If `wait_for_selector` configured, `page.wait_for_selector(selector, timeout=10000)`
  5. If `wait_for_timeout_ms > 0`, `page.wait_for_timeout(ms)`
  6. If `javascript_to_execute` configured, `page.evaluate(js_code)`
  7. `html = page.content()`
  8. Check HTML is not empty/minimal (< 100 chars stripped)
  9. Determine remove_selectors: use per-call param if non-empty, else PLAYWRIGHT_CONFIG value
  10. `markdown, title = _html_to_markdown(html, remove_sels, target_sels)`
  11. Build metadata dict: `{"provider": "playwright", "browser": ..., "url": ..., "content_length": ...}`
  12. Return `(markdown, title, metadata)`

  **Error handling:**
  - `PlaywrightTimeout`: Log ERROR with timeout value. Retry up to 2 times.
  - `PlaywrightError` with "target page, context or browser has been closed": Call `_reset_playwright()` for self-healing. Return `(None, None, None)`.
  - `PlaywrightError` with "net::ERR_NAME_NOT_RESOLVED", "net::ERR_CONNECTION_REFUSED", "net::ERR_SSL_*": Log ERROR with specific message. Return `(None, None, None)`.
  - Any other Exception: Log ERROR. Return `(None, None, None)`.

  **Logging conventions (match existing modules):**
  - `logger = logging.getLogger(__name__)` -> `gpt_scraper_v3.playwright_provider`
  - INFO: "PLAYWRIGHT: Navigating to %s", "PLAYWRIGHT SUCCESS: %s (%d chars)", "PLAYWRIGHT: Context recreated (page limit reached)"
  - WARNING: "Playwright returned minimal HTML for %s", "playwright-stealth not installed; using manual stealth"
  - ERROR: "PLAYWRIGHT TIMEOUT for %s (limit: %dms)", "PLAYWRIGHT error for %s: %s"
  - DEBUG: Response preview (500 char truncation, behind `isEnabledFor` guard)

  **Module docstring:** Must note that the singleton pattern is NOT thread-safe (acceptable for the current single-threaded architecture).

- **Acceptance criteria:**
  - [ ] Module loads without error when Playwright is NOT installed (`is_available()` returns `False`)
  - [ ] Module loads and works when Playwright IS installed
  - [ ] Singleton browser launches once, reuses for all URLs
  - [ ] Browser context recreated every `context_pages_limit` pages (default 500) to prevent memory creep
  - [ ] Pages are created and closed per URL with defensive `try/finally` pattern
  - [ ] `page.close()` wrapped in its own try/except so close failures don't mask original errors
  - [ ] atexit cleanup registered immediately after `sync_playwright().start()`, before `launch()`
  - [ ] atexit cleanup closes context -> browser -> playwright in correct order
  - [ ] `_reset_playwright()` does full teardown (all 3 singletons cleared)
  - [ ] Stealth init scripts applied with realistic navigator mocks
  - [ ] Network blocking works for configured resource types
  - [ ] HTML-to-markdown produces quality output (headings, tables, lists preserved)
  - [ ] Selector order: target_selectors applied before remove_selectors
  - [ ] Selector precedence: per-call remove_selectors overrides config value
  - [ ] Custom JS execution works via `page.evaluate()`
  - [ ] `fetch_playwright_markdown()` NEVER raises -- all exceptions caught internally
  - [ ] Internal retry for PlaywrightTimeout (up to 2 retries with backoff)
  - [ ] No retry for PlaywrightError (browser crashes) -- immediate return
  - [ ] Browser crash recovery via `_reset_playwright()` on "browser closed" error
  - [ ] Return type is `Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]`
  - [ ] Falls back to `html_to_plain_text()` if markdownify not installed
- **Do NOT touch:** Any other module

---

### Task 4: Wire Playwright into get_markdown_content() dispatch

- **Subagent:** `python-pro`
- **Phase:** 2C-Integration
- **Files:** `gpt_scraper_v3/content_fetching.py`
- **Dependencies:** Task 1 (requires_api_key flag), Task 3 (playwright_provider module)
- **Description:**

  Add the Playwright elif branch to the provider dispatch in `get_markdown_content()`, after the existing firecrawl branch (around line 140). The branch goes INSIDE the retry loop, at the same level as jina/firecrawl, so the existing `if content:` check and return flow works identically:

  ```python
  elif provider_name == "playwright":
      from gpt_scraper_v3.playwright_provider import is_available, fetch_playwright_markdown
      if not is_available():
          logger.warning(
              "Playwright is not installed. Skipping provider. "
              "Install with: pip install playwright && playwright install chromium"
          )
          break  # Skip to next provider (not retry)
      content, title, metadata = fetch_playwright_markdown(url, remove_selectors)
  ```

  **Key integration details:**
  - Lazy import inside elif: Playwright is never loaded unless it's in the provider sequence
  - `break` on `not is_available()`: exits the retry loop to try the next provider (not `continue` which would retry)
  - The `content, title, metadata` assignment is at the same level as lines 138-140, so the existing `if content:` check at line 141 and the `return content, title, metadata, provider_name` at line 145 work without modification
  - Since `fetch_playwright_markdown()` never raises (all exceptions caught internally), the existing `except requests.exceptions.Timeout` and `except Exception` blocks will not fire for Playwright -- this is correct and intentional. Playwright handles its own retries internally.

- **Acceptance criteria:**
  - [ ] Playwright dispatched correctly when `"playwright"` is in `provider_sequence`
  - [ ] Lazy import -- no import cost when Playwright not in sequence
  - [ ] Clear warning message when Playwright not installed
  - [ ] `is_available()` failure skips to next provider (break, not continue)
  - [ ] Return values flow correctly through existing `if content:` check
  - [ ] No modification to existing jina/firecrawl branches
- **Do NOT touch:** The jina or firecrawl branches, config.py, playwright_provider.py

---

### Task 5: Create JSON config template and documentation

- **Subagent:** `python-pro`
- **Phase:** 2D-Configuration
- **Files:** Example JSON config (new file or update to docs)
- **Dependencies:** Tasks 2, 3, 4
- **Description:**

  Create a comprehensive example showing how to configure the Playwright provider. Include:

  **Full config example** with all Playwright options documented:
  ```json
  {
    "content_providers": {
      "playwright": {
        "name": "playwright",
        "browser": "chromium",
        "headless": true,
        "stealth": true,
        "viewport": {"width": 1920, "height": 1080},
        "user_agent": "",
        "locale": "cs-CZ",
        "timezone_id": "Europe/Prague",
        "extra_http_headers": {},
        "remove_selectors": "nav, footer, .cookie-bar, #cookie-consent, .breadcrumb",
        "target_selectors": "",
        "wait_until": "domcontentloaded",
        "wait_for_selector": "",
        "wait_for_timeout_ms": 0,
        "navigation_timeout_ms": 30000,
        "javascript_to_execute": "",
        "block_resources": ["image", "font", "media"],
        "block_urls": ["*google-analytics.com*", "*googletagmanager.com*"],
        "cookies": [],
        "storage_state": "",
        "ignore_https_errors": false,
        "executable_path": "",
        "context_pages_limit": 500
      },
      "provider_sequence": "playwright,jina,firecrawl"
    }
  }
  ```

  **Minimal config example** (just required fields, all defaults):
  ```json
  {
    "content_providers": {
      "playwright": {
        "name": "playwright"
      },
      "provider_sequence": "playwright"
    }
  }
  ```

  **Prerequisites documentation:**
  ```
  pip install playwright markdownify
  pip install playwright-stealth  # Optional: enhanced anti-detection
  playwright install chromium     # Downloads browser binary (~150MB)
  ```

  **Update one existing JSON config** (e.g., `scrape_sitemap_GPT_config_Litomerice_v3.json`) as a working example with Playwright as the primary provider: `"provider_sequence": "playwright,jina,firecrawl"`.

- **Acceptance criteria:**
  - [ ] Full config template with all 22 Playwright options documented
  - [ ] Minimal config showing just the required fields
  - [ ] Prerequisites documented (pip + browser install)
  - [ ] One existing JSON config updated as working example
  - [ ] Notes about `wait_until` options explained (load vs domcontentloaded vs networkidle)
  - [ ] Notes about `block_resources` performance impact
- **Do NOT touch:** Python source files

---

## Execution Order

```
Parallel Group 1 (no dependencies -- spawn in one call):
  - Task 1: Extract html_to_plain_text + requires_api_key  -> python-pro (utilities.py + content_fetching.py)
  - Task 2: Playwright config loading                       -> python-pro (config.py)

Sequential after Group 1:
  - Task 3: Create playwright_provider.py                   -> python-pro (depends: Task 2)

Sequential after Task 3:
  - Task 4: Wire dispatch in content_fetching.py            -> python-pro (depends: Tasks 1, 3)

Sequential after Task 4:
  - Task 5: Config template + documentation                 -> python-pro (depends: Tasks 2, 3, 4)
```

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Playwright not installed | Medium | Lazy import + `is_available()` check + clear warning. Scraper falls back to Jina/Firecrawl |
| Browser binary not installed | Medium | `get_browser()` catches "Executable doesn't exist" error and logs actionable message. Sets `_PLAYWRIGHT_AVAILABLE = False` dynamically |
| Browser memory leak over 6000+ URLs | High | Defensive `page.close()` in finally. Context recreation every 500 pages. `_reset_playwright()` for crash recovery |
| Markdownify not installed | Low | Falls back to `html_to_plain_text()` (plain text, not markdown, but functional) |
| Browser crash mid-run | Medium | `_reset_playwright()` full teardown: clears all singletons, next URL does fresh launch |
| Stealth patches insufficient | Low | Configurable; user can provide custom JS via `javascript_to_execute`. playwright-stealth package for enhanced stealth |
| Config backwards compatibility | None | Playwright section is optional. All 15+ existing configs work unchanged. API key validation conditional on provider_sequence |
| Windows atexit not guaranteed on force-close | Low | atexit registered immediately after .start(). Browser process cleanup is best-effort. Orphaned processes self-terminate |
| Thread safety | None (current) | Documented as NOT thread-safe. Acceptable for single-threaded architecture |

## Estimated Scope

- **Files created:** 1 (`playwright_provider.py`, ~300-350 lines)
- **Files modified:** 3 (`config.py` ~40 lines added, `content_fetching.py` ~15 lines changed, `utilities.py` ~15 lines added)
- **Total tasks:** 5
- **Parallel groups:** 1 (Tasks 1+2 in parallel, then 3->4->5 sequential)
- **New dependencies (all optional):**
  - `playwright>=1.49.0` -- browser automation
  - `markdownify>=0.14.1` -- HTML-to-markdown conversion
  - `playwright-stealth>=1.0.6` -- enhanced anti-detection (optional extra)
