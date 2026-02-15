# Implementation Plan: Playwright Markdown Scraping Quality Improvements

## Approach
Improve Playwright-based markdown scraping quality by adding six configurable features to the fetch pipeline: smart DOM wait strategy, cookie banner dismissal, auto-scroll for lazy content, collapsible section expansion, improved markdownify configuration, and a content quality gate with escalating retries. All features are config-driven with backward-compatible defaults. Page interaction logic is extracted into a new `playwright_page_actions.py` module for clean separation and testability.

## Plan Reviewed By
- `python-pro` — Detailed feature specs, Playwright API correctness, error handling, import strategy
- `architect-reviewer` — Module boundaries, pipeline ordering, retry architecture, backward compatibility

### Review Issues Resolved (Revision 2)
- **[HIGH] `_prepare_page()` return type**: Changed from `bool` to `Optional[str]` — returns HTML directly, avoids double `page.content()` call
- **[HIGH] Cookie dismiss placement**: Moved OUTSIDE `include_content_actions` guard — banners affect raw HTML fetching too; acceptance criteria corrected
- **[HIGH] Quality gate retry**: Separated from timeout retry — uses a post-loop escalated attempt with `active_pw_cfg` variable, not shared retry counter
- **[MEDIUM] Task 3 too large**: Split into Task 3A (refactor), 3B (markdownify), 3C (quality gate) — 3B runs in parallel with Task 2
- **[MEDIUM] wait_for_selector/timeout branching**: Explicit `elif` guard documented — smart_wait handles them when enabled, `_prepare_page` handles them when disabled
- **[MEDIUM] Scroll JS must be async**: Explicit `async () => { ... }` with `await` in description + bounded `page.evaluate()` timeout
- **[MEDIUM] pw_cfg fallback**: Simplified to `pw_cfg or {}` — no coupling to `get_config()`
- **[MEDIUM] Image regex fragile**: Improved to handle title text in markdown image links
- **[MEDIUM] Expand may trigger navigation**: Added `event.preventDefault()` for click-based expand
- **[MEDIUM] block_resources interaction**: Documented in risk register

## Root Cause Analysis
The primary issue is that `wait_until: "domcontentloaded"` captures HTML before JavaScript frameworks (Liferay, React, Vue) render content. For the StredoceskyKraj site specifically:
- 80% of scraped files contain "Skryta" (Hidden) indicating JS-rendered content was missed
- The `target_selectors: "#gov-page"` doesn't exist on the site (silently falls back to whole document)
- No `wait_for_timeout_ms` or `wait_for_selector` configured as safety nets
- 36 URLs produced under 1000 chars of markdown from pages with rich content

## File Ownership Map

| Subagent Role | Owned Files |
|---------------|-------------|
| `python-pro` (Task 1) | `gpt_scraper_v3/config.py` |
| `python-pro` (Task 2) | `gpt_scraper_v3/playwright_page_actions.py` (NEW) |
| `python-pro` (Task 3A) | `gpt_scraper_v3/playwright_provider.py` — `_prepare_page()` extraction + pipeline wiring |
| `python-pro` (Task 3B) | `gpt_scraper_v3/playwright_provider.py` — `_html_to_markdown()` improvements |
| `python-pro` (Task 3C) | `gpt_scraper_v3/playwright_provider.py` — quality gate + retry integration |

Note: Tasks 3A, 3B, 3C all modify `playwright_provider.py` and MUST run sequentially (3A first, then 3B, then 3C).

## New Config Fields Summary (16 fields)

All fields added to the existing `content_providers.playwright` JSON block with `.get(key, default)` pattern. Existing config files need zero changes.

| Field | Type | Default | Feature |
|---|---|---|---|
| `smart_wait_enabled` | bool | `False` | A - Smart wait |
| `smart_wait_stability_rounds` | int | 3 | A - Smart wait |
| `smart_wait_stability_interval_ms` | int | 500 | A - Smart wait |
| `auto_scroll_enabled` | bool | `False` | B - Auto-scroll |
| `auto_scroll_max_scrolls` | int | 20 | B - Auto-scroll |
| `auto_scroll_step_delay_ms` | int | 200 | B - Auto-scroll |
| `quality_gate_enabled` | bool | `False` | C - Quality gate |
| `quality_gate_min_markdown_length` | int | 100 | C - Quality gate |
| `quality_gate_min_md_html_ratio` | float | 0.02 | C - Quality gate |
| `expand_collapsibles_enabled` | bool | `False` | D - Expand sections |
| `expand_collapsibles_selectors` | str | (built-in list) | D - Expand sections |
| `expand_collapsibles_wait_ms` | int | 500 | D - Expand sections |
| `convert_images_to_alt_text` | bool | `True` | E - Markdownify |
| `table_infer_header` | bool | `True` | E - Markdownify |
| `dismiss_cookie_banners` | bool | `False` | F - Cookie banners |
| `cookie_banner_selectors` | str | (built-in list) | F - Cookie banners |

**Note on `block_resources` interaction:** The default `block_resources: ["image", "font", "media"]` prevents image binary downloads but does NOT remove `<img>` tags from the DOM. Alt text in source HTML is preserved regardless of image loading. However, lazy-loaded images that inject `<img>` tags via JavaScript after image resources load will not be captured when `"image"` is in `block_resources`. If users want maximum lazy-image alt text extraction alongside `auto_scroll_enabled: true`, they should remove `"image"` from `block_resources`.

## Page Fetch Pipeline (New Order of Operations)

```
_prepare_page() encapsulates steps 1-8, returns Optional[str] (HTML) or None:

1. page.goto(url, wait_until=...)               -- navigate (existing)
2. WAIT STRATEGY BRANCH:
   2a. If smart_wait_enabled: call wait_for_content_ready() [handles networkidle + stability + wait_for_selector + wait_for_timeout_ms]
   2b. Else: existing wait_for_selector + wait_for_timeout_ms (unchanged)
3. COOKIE BANNER DISMISSAL (Feature F)          -- ALWAYS runs (not gated by include_content_actions)
4. If include_content_actions:
   4a. AUTO-SCROLL (Feature B)                  -- trigger lazy-loaded content
   4b. EXPAND COLLAPSIBLES (Feature D)          -- expand accordions/toggles
   4c. SECONDARY WAIT (300ms fixed)             -- let expanded/loaded content settle (only if 4a or 4b ran)
5. Custom JS execution                          -- existing javascript_to_execute config
6. html = page.content()                        -- capture final HTML
7. Validate HTML length > 200 chars             -- existing check
8. Return html (or None if validation failed)

Then in fetch_playwright_markdown():
9. _html_to_markdown(html, ..., pw_cfg) with improved config (E) -- convert
10. QUALITY GATE CHECK (Feature C)              -- evaluate output

And if quality gate says "suspect":
11. ONE escalated retry attempt (separate from timeout retries)
```

Rationale for ordering:
- Smart wait (2) before cookie banner (3): banner JS needs network to load first
- Cookie banner (3) before scroll (4a): overlays intercept scroll events
- Cookie banner (3) outside `include_content_actions` gate: banners affect raw HTML too
- Scroll (4a) before expand (4b): collapsibles may be lazy-loaded at bottom of page
- Quality gate (10) last: evaluates final output

---

## Task List

### Task 1: Add new config fields to `config.py`
- **Subagent:** `python-pro`
- **Phase:** 2A-Core
- **Files:** `gpt_scraper_v3/config.py`
- **Dependencies:** None — can parallelise
- **Description:**
  Add 16 new config fields to the `PLAYWRIGHT_CONFIG` dict construction in `config.py` (currently lines 506-531). Follow the existing pattern of `pw_raw.get("key", default)` for each field.

  Fields to add (grouped by feature):

  **Smart Wait (A):**
  ```python
  "smart_wait_enabled": pw_raw.get("smart_wait_enabled", False),
  "smart_wait_stability_rounds": pw_raw.get("smart_wait_stability_rounds", 3),
  "smart_wait_stability_interval_ms": pw_raw.get("smart_wait_stability_interval_ms", 500),
  ```

  **Auto-Scroll (B):**
  ```python
  "auto_scroll_enabled": pw_raw.get("auto_scroll_enabled", False),
  "auto_scroll_max_scrolls": pw_raw.get("auto_scroll_max_scrolls", 20),
  "auto_scroll_step_delay_ms": pw_raw.get("auto_scroll_step_delay_ms", 200),
  ```

  **Quality Gate (C):**
  ```python
  "quality_gate_enabled": pw_raw.get("quality_gate_enabled", False),
  "quality_gate_min_markdown_length": pw_raw.get("quality_gate_min_markdown_length", 100),
  "quality_gate_min_md_html_ratio": pw_raw.get("quality_gate_min_md_html_ratio", 0.02),
  ```

  **Expand Collapsibles (D):**
  ```python
  "expand_collapsibles_enabled": pw_raw.get("expand_collapsibles_enabled", False),
  "expand_collapsibles_selectors": pw_raw.get("expand_collapsibles_selectors", ""),
  "expand_collapsibles_wait_ms": pw_raw.get("expand_collapsibles_wait_ms", 500),
  ```

  **Markdownify (E):**
  ```python
  "convert_images_to_alt_text": pw_raw.get("convert_images_to_alt_text", True),
  "table_infer_header": pw_raw.get("table_infer_header", True),
  ```

  **Cookie Banners (F):**
  ```python
  "dismiss_cookie_banners": pw_raw.get("dismiss_cookie_banners", False),
  "cookie_banner_selectors": pw_raw.get("cookie_banner_selectors", ""),
  ```

  **Patterns to follow:** Lines 506-531 of `config.py` — exact same `.get()` with default pattern.
  **Do NOT touch:** Any code outside the `if pw_raw:` block.

- **Acceptance criteria:**
  - [ ] All 16 fields appear in `PLAYWRIGHT_CONFIG` dict
  - [ ] All defaults match the table above
  - [ ] Existing config files with no new fields produce identical `PLAYWRIGHT_CONFIG` to before (except new keys with defaults)
  - [ ] No imports added, no other code changed

---

### Task 2: Create `playwright_page_actions.py` with page interaction functions
- **Subagent:** `python-pro`
- **Phase:** 2A-Core
- **Files:** `gpt_scraper_v3/playwright_page_actions.py` (NEW)
- **Dependencies:** None — can parallelise with Task 1 and Task 3B
- **Description:**
  Create a new module containing four page-level interaction functions. Each function takes a Playwright `Page` object and the `pw_cfg` dict, performs its action, and returns. Functions never raise — they log errors and return gracefully (matching the existing pattern in `playwright_provider.py` where Playwright functions return None on failure).

  **Function 1: `wait_for_content_ready(page, pw_cfg)`**

  Smart DOM stability detection. Only runs when `pw_cfg.get("smart_wait_enabled", False)` is True.

  Implementation:
  1. Wait for `networkidle` using `page.wait_for_load_state("networkidle", timeout=pw_cfg.get("navigation_timeout_ms", 30000))`. Wrap in try/except — on timeout, log WARNING and continue (page may still have useful content).
  2. Poll for DOM stability: use `page.evaluate("() => document.body.innerText.length")` to snapshot text length, then `page.wait_for_timeout(pw_cfg.get("smart_wait_stability_interval_ms", 500))`, then re-snapshot. If length is stable (unchanged) across 2 consecutive checks, exit early. Loop up to `pw_cfg.get("smart_wait_stability_rounds", 3)` times.
  3. **IMPORTANT: This function also handles `wait_for_selector` and `wait_for_timeout_ms`** when smart_wait is enabled. After stability check, if `pw_cfg.get("wait_for_selector", "")` is set, call `page.wait_for_selector(sel, timeout=10000)` (same as existing code). If `pw_cfg.get("wait_for_timeout_ms", 0) > 0`, call `page.wait_for_timeout(ms)`. This centralises all wait logic when smart_wait is on, so the caller (`_prepare_page`) does NOT duplicate these waits.
  4. Wrap all `page.evaluate()` and `page.wait_for_load_state()` calls in try/except — treat any failure as "stable enough, proceed." Log at DEBUG level.

  **Function 2: `dismiss_cookie_banners(page, pw_cfg)`**

  Only runs when `pw_cfg.get("dismiss_cookie_banners", False)` is True.

  Implementation:
  1. If `pw_cfg.get("cookie_banner_selectors", "")` is non-empty, parse it (comma-separated CSS selectors) and try clicking each
  2. If empty, use built-in heuristic list of common CMP selectors:
     - `#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll`
     - `#CybotCookiebotDialogBodyButtonAccept`
     - `.onetrust-accept-btn-handler`
     - `button[data-cookiefirst-action="accept"]`
     - `[data-cky-tag="accept-button"]`
     - `.cmplz-accept`
     - `.cc-accept`
     - `.cookie-accept`
  3. For each selector: `page.locator(selector).first.click(timeout=2000)` wrapped in try/except (selector not found = skip silently)
  4. After first successful click, wait 300ms for animation, then break (don't click more buttons)
  5. Log at INFO when a banner is dismissed, DEBUG when no banner found

  **Function 3: `scroll_for_lazy_content(page, pw_cfg)`**

  Only runs when `pw_cfg.get("auto_scroll_enabled", False)` is True.

  Implementation — execute a single `page.evaluate()` with an **explicit async JS function**. The timeout for `page.evaluate()` must be bounded: compute `max_time = min(max_scrolls * step_delay_ms + 5000, 25000)` and pass as `timeout` kwarg.

  The JS function passed to `page.evaluate()`:
  ```javascript
  async ([maxScrolls, stepDelayMs]) => {
      const step = window.innerHeight;
      let prevHeight = 0;
      let stableCount = 0;
      let scrollCount = 0;

      for (let i = 0; i < maxScrolls; i++) {
          const currentHeight = document.body.scrollHeight;
          if (currentHeight === prevHeight) {
              stableCount++;
              if (stableCount >= 2) break;  // Height stabilised
          } else {
              stableCount = 0;
          }
          prevHeight = currentHeight;
          window.scrollBy(0, step);
          scrollCount++;
          await new Promise(r => setTimeout(r, stepDelayMs));
      }

      window.scrollTo(0, 0);  // Return to top
      return scrollCount;
  }
  ```

  Called as: `page.evaluate(js_code, [max_scrolls, step_delay_ms], timeout=max_time)`

  After the JS returns, call `page.wait_for_timeout(500)` for lazy content to render.
  Log at DEBUG: "Scrolled N steps for lazy content on {url}"

  **Function 4: `expand_collapsible_sections(page, pw_cfg)`**

  Only runs when `pw_cfg.get("expand_collapsibles_enabled", False)` is True.

  Implementation:
  1. If `pw_cfg.get("expand_collapsibles_selectors", "")` is non-empty, parse as comma-separated selectors
  2. If empty, use built-in defaults:
     - `details:not([open])` — HTML5 details (set `open` attribute via JS, no click)
     - `[aria-expanded="false"]:not(a)` — ARIA accordions, excluding `<a>` tags to prevent navigation
     - `[data-toggle="collapse"]:not(a)` — Bootstrap, excluding `<a>` tags
     - `.accordion-toggle:not(a), .collapse-toggle:not(a), .show-more:not(a)` — Generic, excluding `<a>` tags
  3. Execute a single `page.evaluate()` JS block that:
     - For `<details>` elements: set `.open = true` (no click needed)
     - For all other matching elements: call `el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}))` to avoid default link navigation. Use `.click()` only on elements that are not `<a>` tags (the `:not(a)` selector already filters these, but double-check in JS).
     - Return count of expanded elements
  4. Wait `pw_cfg.get("expand_collapsibles_wait_ms", 500)` for content to expand
  5. Log at DEBUG: "Expanded N collapsible sections on {url}"

  **Module-level patterns:**
  - Import `logging` and create `logger = logging.getLogger(__name__)`
  - Import types from `typing` as needed
  - Access config via function parameter `pw_cfg` (passed by caller), NOT via `get_config()` — this keeps the module decoupled from config singleton
  - All functions are synchronous (Playwright's sync API, which internally awaits async Playwright)
  - All functions handle their own exceptions internally — never propagate to caller
  - Do NOT import `re` at module level unless needed; prefer `page.evaluate()` for DOM operations

  **Do NOT touch:** `playwright_provider.py`, `config.py`, or any other existing file.

- **Acceptance criteria:**
  - [ ] Module contains exactly 4 public functions with signatures: `wait_for_content_ready(page, pw_cfg)`, `dismiss_cookie_banners(page, pw_cfg)`, `scroll_for_lazy_content(page, pw_cfg)`, `expand_collapsible_sections(page, pw_cfg)`
  - [ ] Each function checks its enable flag and returns immediately if disabled
  - [ ] No function raises exceptions — all wrapped in try/except with logging
  - [ ] Each function logs at appropriate levels (INFO for actions taken, DEBUG for details, WARNING for failures)
  - [ ] `wait_for_content_ready` handles `wait_for_selector` and `wait_for_timeout_ms` internally (caller must NOT duplicate)
  - [ ] `scroll_for_lazy_content` uses explicit `async () => { ... }` JS with `await` for delays, and bounded `page.evaluate()` timeout
  - [ ] `expand_collapsible_sections` excludes `<a>` tags from click targets (`:not(a)` selectors)
  - [ ] No circular imports — module only imports standard library + playwright types

---

### Task 3A: Refactor `playwright_provider.py` — extract `_prepare_page()` and wire pipeline
- **Subagent:** `python-pro`
- **Phase:** 2B-Integration
- **Files:** `gpt_scraper_v3/playwright_provider.py`
- **Dependencies:** Task 1 (config fields), Task 2 (page action functions)
- **Description:**
  Extract the duplicated page setup logic from `fetch_playwright_markdown()` (lines ~488-511) and `fetch_playwright_html()` (lines ~620-650) into a single `_prepare_page()` helper.

  **New function signature:**
  ```python
  def _prepare_page(
      page: "Page",
      url: str,
      pw_cfg: dict,
      include_content_actions: bool = True,
  ) -> Optional[str]:
      """Navigate to URL and prepare page for content extraction.

      Returns the HTML string on success, or None if retry is needed.
      """
  ```

  **Key design: returns `Optional[str]` (the HTML), NOT `bool`.** This avoids calling `page.content()` twice (once in `_prepare_page` for validation, once in caller). The caller pattern becomes:
  ```python
  html = _prepare_page(page, url, active_pw_cfg, include_content_actions=True)
  if html is None:
      continue  # retry
  # Use html directly — no second page.content() call
  ```

  **Implementation steps inside `_prepare_page()`:**

  1. **Navigate:** `page.goto(url, wait_until=pw_cfg.get("wait_until", "domcontentloaded"))` — existing logic

  2. **Wait strategy branch (EXPLICIT ELIF):**
     ```python
     if pw_cfg.get("smart_wait_enabled", False):
         # Smart wait handles ALL waiting: networkidle + stability + wait_for_selector + wait_for_timeout_ms
         from gpt_scraper_v3.playwright_page_actions import wait_for_content_ready
         wait_for_content_ready(page, pw_cfg)
     else:
         # Basic wait — existing behavior, unchanged
         wait_sel = pw_cfg.get("wait_for_selector", "")
         if wait_sel:
             page.wait_for_selector(wait_sel, timeout=10000)
         wait_ms = pw_cfg.get("wait_for_timeout_ms", 0)
         if wait_ms > 0:
             page.wait_for_timeout(wait_ms)
     ```
     **This explicit branching ensures no duplication of wait logic.** When `smart_wait_enabled=True`, `_prepare_page` does NOT call `wait_for_selector`/`wait_for_timeout_ms` itself — `wait_for_content_ready()` handles them. When `smart_wait_enabled=False`, the existing basic waits run directly.

  3. **Cookie banner dismissal (OUTSIDE content actions gate):**
     ```python
     from gpt_scraper_v3.playwright_page_actions import dismiss_cookie_banners
     dismiss_cookie_banners(page, pw_cfg)  # Safe no-op if dismiss_cookie_banners=False
     ```
     Cookie dismiss runs for BOTH `include_content_actions=True` AND `False` because banners affect raw HTML fetching too.

  4. **Content actions (GATED by `include_content_actions`):**
     ```python
     if include_content_actions:
         from gpt_scraper_v3.playwright_page_actions import (
             scroll_for_lazy_content,
             expand_collapsible_sections,
         )
         did_scroll = scroll_for_lazy_content(page, pw_cfg)
         did_expand = expand_collapsible_sections(page, pw_cfg)
         if did_scroll or did_expand:
             page.wait_for_timeout(300)  # Secondary settle wait
     ```
     Note: `scroll_for_lazy_content` and `expand_collapsible_sections` should return a truthy value (count of actions) when they do something, or 0/None when disabled or no-op. The 300ms settle wait only runs if at least one action was performed.

  5. **Custom JS execution** — existing `javascript_to_execute` logic, unchanged

  6. **Capture and validate HTML:**
     ```python
     html = page.content()
     if not html or len(html.strip()) < 200:
         logger.warning("PLAYWRIGHT: Minimal HTML for %s (%d chars)", url, len(html) if html else 0)
         return None  # Signals retry needed
     return html
     ```

  **Update `fetch_playwright_markdown()`:**
  Replace the inline navigation/wait/content-capture code with:
  ```python
  html = _prepare_page(page, url, active_pw_cfg, include_content_actions=True)
  if html is None:
      continue  # retry loop
  ```
  Where `active_pw_cfg` is initialised before the retry loop as `active_pw_cfg = pw_cfg` (see Task 3C for escalation).

  **Update `fetch_playwright_html()`:**
  Replace the inline navigation/wait/content-capture code with:
  ```python
  html = _prepare_page(page, url, pw_cfg, include_content_actions=False)
  if html is None:
      continue  # retry loop
  return html
  ```

  **Import strategy:** Use lazy imports inside `_prepare_page()` for all `playwright_page_actions` functions. This avoids import-time dependency and keeps `playwright_page_actions` optional (if the file is missing, the `import` will fail only when a feature is enabled, producing a clear error).

  **Patterns to follow:**
  - `re` is already imported at the top of the file (line 17) — do NOT re-import
  - All Playwright functions return None-tuples on failure (never raise)
  - Config access via `pw_cfg.get(key, default)` pattern
  - Logging: INFO for actions, DEBUG for content, WARNING for fallbacks, ERROR for failures
  - `_context_page_count` increment must remain in correct position (BEFORE `_prepare_page` call)
  - URL scheme validation (`urlparse`) must be preserved (BEFORE `_prepare_page` call)

  **Do NOT touch:** Browser lifecycle code (get_browser, _get_context, stealth), `_html_to_markdown()`, config.py, playwright_page_actions.py.

- **Acceptance criteria:**
  - [ ] `_prepare_page()` returns `Optional[str]` (HTML string or None)
  - [ ] `fetch_playwright_markdown()` and `fetch_playwright_html()` both use `_prepare_page()` — no duplicated navigation/wait code
  - [ ] `fetch_playwright_html()` does NOT run scroll, expand, or quality gate (only smart wait + cookie dismiss when enabled)
  - [ ] When `smart_wait_enabled=False`: exact same behavior as current code (basic wait_for_selector + wait_for_timeout_ms)
  - [ ] When `smart_wait_enabled=True`: `_prepare_page` does NOT call wait_for_selector/wait_for_timeout_ms directly — `wait_for_content_ready()` handles them
  - [ ] Cookie dismiss runs regardless of `include_content_actions` value
  - [ ] No new external dependencies required

---

### Task 3B: Improve `_html_to_markdown()` markdownify configuration
- **Subagent:** `python-pro`
- **Phase:** 2A-Core
- **Files:** `gpt_scraper_v3/playwright_provider.py` — only the `_html_to_markdown()` function
- **Dependencies:** Task 1 (config fields only — does NOT depend on Task 2 or 3A)
- **Description:**
  Modify the `_html_to_markdown()` function to support better image handling and table inference.

  **Step 1: Add `pw_cfg` parameter to function signature:**
  ```python
  def _html_to_markdown(
      html: str,
      remove_selectors: Optional[List[str]] = None,
      target_selectors: Optional[List[str]] = None,
      pw_cfg: Optional[Dict[str, Any]] = None,
  ) -> Tuple[str, str]:
      if pw_cfg is None:
          pw_cfg = {}  # All .get() calls resolve to safe defaults
  ```
  Note: Do NOT call `get_config()` here. The fallback `{}` means all feature flags resolve to their defaults via `.get()`.

  **Step 2: Update markdownify call** (currently lines ~406-411):

  Current:
  ```python
  markdown = md(str(working_soup), heading_style="ATX", bullets="-", strip=["img", "svg"])
  ```

  New:
  ```python
  strip_tags = ["svg"]  # Always strip SVG
  if not pw_cfg.get("convert_images_to_alt_text", True):
      strip_tags.append("img")

  md_kwargs = {
      "heading_style": "ATX",
      "bullets": "-",
      "strip": strip_tags,
  }
  if pw_cfg.get("table_infer_header", True):
      md_kwargs["table_infer_header"] = True

  markdown = md(str(working_soup), **md_kwargs)
  ```

  **Step 3: Add image post-processing** (after the markdownify call, before the blank-line collapse):

  ```python
  if pw_cfg.get("convert_images_to_alt_text", True):
      # Convert ![alt](url) or ![alt](url "title") to [Image: alt]
      # Drop images with empty or trivially short alt text
      def _simplify_image(match):
          alt = match.group(1).strip()
          if len(alt) < 3:
              return ""
          return f"[Image: {alt}]"
      markdown = re.sub(r'!\[([^\]]*)\]\([^)]*(?:\s+"[^"]*")?\)', _simplify_image, markdown)
  ```

  Note: `re` is already imported at the top of the file (line 17). Do NOT add a new import.

  **Step 4: Update the call site** in `fetch_playwright_markdown()` where `_html_to_markdown` is called — pass `pw_cfg`:
  ```python
  markdown, title = _html_to_markdown(html, r_sels, t_sels, pw_cfg=active_pw_cfg)
  ```
  (If Task 3A has not yet run and `active_pw_cfg` doesn't exist, use `pw_cfg` instead — the variable name may differ. The key is passing the config dict.)

  **Do NOT touch:** Anything outside `_html_to_markdown()` function body and its one call site. Do not modify browser lifecycle, _prepare_page, quality gate, or config.py.

- **Acceptance criteria:**
  - [ ] `_html_to_markdown` accepts optional `pw_cfg` parameter, defaults to `{}`
  - [ ] When `convert_images_to_alt_text=True` (default): `<img alt="Map of the city">` produces `[Image: Map of the city]`
  - [ ] When `convert_images_to_alt_text=True`: `<img alt="">` or `<img alt="x">` (alt < 3 chars) produces empty string (no `[Image: ]` noise)
  - [ ] When `convert_images_to_alt_text=False`: images are stripped entirely (current behavior)
  - [ ] When `table_infer_header=True` (default): tables get header inference via markdownify
  - [ ] Image regex handles `![alt](url "title")` format (title text in quotes)
  - [ ] No new imports added (re is already at top of file)
  - [ ] Blank-line collapse regex still runs after image post-processing

---

### Task 3C: Add quality gate and integrate into retry loop
- **Subagent:** `python-pro`
- **Phase:** 2C-Quality
- **Files:** `gpt_scraper_v3/playwright_provider.py` — quality gate function + `fetch_playwright_markdown()` retry logic
- **Dependencies:** Task 3A (needs `_prepare_page()` and `active_pw_cfg` variable), Task 3B (needs updated `_html_to_markdown()`)
- **Description:**
  Add content quality assessment and an escalated retry mechanism that is SEPARATE from the existing timeout-based retry loop.

  **New function `_assess_content_quality()`:**
  ```python
  def _assess_content_quality(markdown: str, html_length: int, pw_cfg: dict) -> str:
      """Assess whether markdown extraction produced sufficient content.

      Returns:
          'good' — content is acceptable
          'suspect' — content is suspiciously small relative to HTML
          'empty' — no content at all
      """
      if not pw_cfg.get("quality_gate_enabled", False):
          return "good"

      md_len = len(markdown.strip())
      if md_len == 0:
          return "empty"

      min_len = pw_cfg.get("quality_gate_min_markdown_length", 100)
      min_ratio = pw_cfg.get("quality_gate_min_md_html_ratio", 0.02)

      # If HTML itself is small (<2000 chars), page is genuinely sparse — accept
      if html_length < 2000:
          return "good"

      if md_len < min_len:
          return "suspect"

      ratio = md_len / html_length if html_length > 0 else 0
      if ratio < min_ratio:
          return "suspect"

      return "good"
  ```

  **New helper function `_escalate_pw_cfg()`:**
  ```python
  def _escalate_pw_cfg(pw_cfg: dict) -> dict:
      """Create an escalated copy of pw_cfg with more aggressive settings."""
      escalated = dict(pw_cfg)  # Shallow copy
      escalated["smart_wait_enabled"] = True
      escalated["auto_scroll_enabled"] = True
      escalated["expand_collapsibles_enabled"] = True
      # Add extra wait time
      current_wait = escalated.get("wait_for_timeout_ms", 0)
      escalated["wait_for_timeout_ms"] = current_wait + 3000
      return escalated
  ```

  **Integration into `fetch_playwright_markdown()` retry loop — ARCHITECTURE:**

  The quality gate retry is SEPARATE from timeout retries. The existing retry loop handles Playwright errors (timeout, connection). The quality gate runs AFTER the retry loop succeeds:

  ```python
  def fetch_playwright_markdown(url, remove_selectors=None):
      pw_cfg = get_config().PLAYWRIGHT_CONFIG
      active_pw_cfg = pw_cfg  # May be escalated after quality gate
      max_retries = 2
      quality_retried = False  # Separate flag for quality gate retry

      while True:  # Outer loop for quality gate retry (max 1 escalation)
          result_markdown = None
          result_title = None
          result_metadata = None

          for attempt in range(max_retries + 1):
              # ... existing retry loop with _prepare_page, _html_to_markdown ...
              # On success: set result_markdown, result_title, result_metadata, break
              # On PlaywrightTimeout: continue (existing)
              # On PlaywrightError: return (None, None, None) (existing)

          if result_markdown is None:
              return (None, None, None)  # All retries exhausted

          # QUALITY GATE — runs AFTER successful extraction
          html_length = len(html) if html else 0
          quality = _assess_content_quality(result_markdown, html_length, active_pw_cfg)

          if quality == "suspect" and not quality_retried:
              quality_retried = True
              active_pw_cfg = _escalate_pw_cfg(pw_cfg)
              logger.warning(
                  "Quality gate: suspect content for %s (%d chars markdown from %d chars HTML), "
                  "retrying with escalated strategy",
                  url, len(result_markdown), html_length
              )
              continue  # Re-enter outer while loop with escalated settings

          # Quality is "good", or we already retried once — return whatever we have
          # (partial content is better than no content)
          break

      return (result_markdown, result_title, result_metadata)
  ```

  **Key design decisions:**
  - Quality gate retry is at most ONE additional attempt (controlled by `quality_retried` flag)
  - The escalated `active_pw_cfg` is passed through the entire retry loop on the second pass
  - If the escalated attempt also produces "suspect" content, we return it anyway — partial content is better than None
  - The quality gate does NOT share the timeout retry counter — they are independent
  - `_assess_content_quality` returns "good" when `quality_gate_enabled=False`, so the outer while loop runs exactly once (no retry)
  - The `html` variable needed for `html_length` must be captured from the inner retry loop — ensure it's scoped correctly

  **Patterns to follow:**
  - Log quality gate triggers at WARNING level
  - Log quality assessment at DEBUG level for every URL
  - Return partial content (never return None just because quality is suspect)

  **Do NOT touch:** `_prepare_page()`, `_html_to_markdown()`, browser lifecycle, config.py, playwright_page_actions.py.

- **Acceptance criteria:**
  - [ ] `_assess_content_quality()` returns "good" when quality_gate disabled
  - [ ] `_assess_content_quality()` returns "good" for small HTML (<2000 chars) regardless of markdown size
  - [ ] `_assess_content_quality()` returns "suspect" when markdown/HTML ratio < 0.02 and HTML > 2000 chars
  - [ ] Quality gate retry is SEPARATE from timeout retry (at most 1 escalated attempt)
  - [ ] Escalated retry enables smart_wait, auto_scroll, expand_collapsibles, and adds 3000ms wait
  - [ ] "suspect" content after escalated retry is STILL RETURNED (not None)
  - [ ] WARNING log emitted when quality gate triggers escalation
  - [ ] When quality_gate_enabled=False: zero additional overhead, single pass through outer loop

---

## Execution Order

### Parallel Group 1 (no dependencies — all spawn in one call)
- Task 1: Config additions → `python-pro` (~30 LOC, `config.py`)
- Task 2: Page actions module → `python-pro` (~300-400 LOC, new `playwright_page_actions.py`)
- Task 3B: Markdownify improvements → `python-pro` (~50 LOC, `playwright_provider.py` `_html_to_markdown()` only)

### Sequential after Group 1
- Task 3A: Pipeline refactor → `python-pro` (~200 LOC, `playwright_provider.py` `_prepare_page()` + wiring)

### Sequential after Task 3A
- Task 3C: Quality gate → `python-pro` (~150 LOC, `playwright_provider.py` retry integration)

### Quality Gate (all spawn in one call)
- Task 4: Code review → `code-reviewer`
- Task 5: Architecture review → `architect-reviewer`

```
Group 1:  [Task 1]  ──┐
          [Task 2]  ──┤
          [Task 3B] ──┤
                       ├──→ [Task 3A] ──→ [Task 3C] ──→ [Task 4 + Task 5 parallel]
```

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Per-URL latency increase (10-15s when all features enabled) | High | All features default OFF; enable per-site. Smart wait alone adds ~2-3s max. |
| `networkidle` hangs on pages with persistent connections (WebSocket, long-polling) | Medium | Smart wait has bounded timeout via `navigation_timeout_ms`. Stability poll loop exits after max rounds. |
| Cookie banner heuristics click wrong button | Medium | Conservative selector list; per-site override via `cookie_banner_selectors`; log when banner dismissed. |
| Quality gate false positives on legitimately small pages | Medium | Skip quality check when HTML itself is small (<2000 chars); ratio-based check, not just absolute minimum. |
| `block_resources: ["image"]` with `auto_scroll_enabled` or `convert_images_to_alt_text` | Medium | Alt text in source HTML preserved regardless of image loading. Document that removing "image" from block_resources improves lazy-image capture. |
| `expand_collapsibles` clicking triggers navigation on `<a>` elements | Medium | All click selectors use `:not(a)` filter. `<details>` use `.open = true` (no click). |
| `page.evaluate()` timeout in scroll function | Medium | Timeout bounded to `min(max_scrolls * step_delay_ms + 5000, 25000)`. Early exit when scrollHeight stabilises. |
| Auto-scroll infinite loop on feed-style pages | Low | `auto_scroll_max_scrolls` cap (default 20); early exit when `scrollHeight` stabilises (2 consecutive unchanged). |
| markdownify table output rough with complex tables (colspan/rowspan) | Low | Known markdownify limitation; imperfect output still better than no data. Accept as-is. |
| Context recycling resets cookie banner dismissal state | Low | Cookie banner dismissal re-executes per page; context recycling at 500 pages is infrequent. |

## Estimated Scope
- Files created: 1 (`playwright_page_actions.py`)
- Files modified: 2 (`config.py`, `playwright_provider.py`)
- Total tasks: 7 (5 implementation + 2 review)
- Parallelisable groups: 3 (Group 1: T1+T2+T3B | Group 2: T3A | Group 3: T3C)
- Total new/changed LOC: ~750-900
