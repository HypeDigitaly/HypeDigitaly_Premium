# GPT SCRAPER PROJECT
- Parallelise when possible for max efficiency
- Dont put agents in the background wait for their output

- For subagents, you MUST always use Opus 4.6
- Always use Opus 4.6
# currentDate
Today's date is 2026-02-15.

## V3 Architecture
- V3 is a modular rewrite of V2 (single 9,304-line file -> 21 modules in gpt_scraper_v3/)
- Config: ScraperConfig dataclass in config.py, accessed via get_config() singleton
- All 55 V2 global variables mapped to ScraperConfig fields (verified 2026-02-14)
- New in V3: Playwright provider (PLAYWRIGHT_CONFIG), improved token counting, session singleton
- Review plans: GPT_SCRAPER_V3_PLAN.md (implementation), V3_CONFIG_REVIEW_PLAN.md (final review)

### New URL Detection (added 2026-02-15)
Detects URLs that appear in the sitemap for the first time between runs and processes them
regardless of their `lastmod` date. Previously, a URL added to a sitemap with an old `lastmod`
(or no `lastmod`) would be silently skipped by `should_process_url_with_resume()`.

**Snapshot file:** `{identifier}_known_urls.json` (naming follows the same `{identifier}_*` pattern
as `{identifier}_v3_last_run_time.txt`). Contains a sorted JSON array of normalized URLs
(via `normalize_url_query_params`).

**Lifecycle (orchestrated in `cli.py` main):**
1. Load previous snapshot via `load_known_urls_snapshot()` (returns empty set on first run)
2. Pass `previous_known_urls` through resolve_urls -> extraction functions -> `should_process_url_with_resume()`
3. After processing, build `current_known_urls` from `lm_map` keys (the XML sitemap data)
4. Diff against previous snapshot to find new/removed URLs
5. Save updated snapshot via `save_known_urls_snapshot()` (atomic write with `.tmp` + `os.replace`)

**Position in `should_process_url_with_resume()` decision chain:**
The new-URL check runs after RSS branch (RSS has its own date logic), after CLM==0 early return,
after no-last-run-timestamp early return, and before the `lastmod` vs `last_run_timestamp` comparison.
This means it only fires when CHECK_LAST_MODIFIED is 1 or 2 and a previous run exists.

**Exclusions:** RSS feeds and pagination processing do not participate in new-URL detection.
RSS has its own date-based logic; pagination handles recursively-discovered sub-pages that
are not in the sitemap snapshot.

**Only active when:** `CHECK_LAST_MODIFIED != 0` (mode 1 or 2).

### Removed URL Cleanup (added 2026-02-15)
When URLs disappear from the sitemap between runs, their corresponding files are automatically
deleted from the OpenAI vector store.

**Implementation (`vector_store.py` -> `cleanup_removed_urls()`):**
1. Builds an inverted index: `Dict[normalized_base_url, List[cache_key]]` from the vector store cache
   to handle chunk variants (e.g., `url#chunkA`, `url#chunkB`, `url#summarizedA`)
2. For each removed URL, looks up all matching cache keys via O(1) inverted index lookup
3. Deletes each matching vector store file via `delete_vector_store_file()`
4. Returns `Tuple[int, Set[str]]` -- (deleted_count, failed_urls)
5. Failed deletions are added back to `current_known_urls` so the snapshot preserves them for retry

**Safety guards (in `cli.py`):**
- Empty sitemap (0 URLs from lm_map): skip both cleanup and snapshot save (possible fetch failure)
- >50% of previous URLs removed: skip both cleanup AND snapshot save (possible partial sitemap fetch)
- Cleanup only runs when a vector store ID and vector store cache are available

**Only active when:** `CHECK_LAST_MODIFIED != 0` (mode 1 or 2).

### Files modified for URL detection and cleanup
- `config.py` -- Added `KNOWN_URLS_FILE: str` field to ScraperConfig dataclass
- `xml_sitemap.py` -- Added `load_known_urls_snapshot()` and `save_known_urls_snapshot()`
- `url_processing.py` -- Added `previous_known_urls` parameter to `should_process_url_with_resume()`
- `sitemap_parsing.py` -- Threaded `previous_known_urls` through HTML sitemap extraction functions
- `content_fetching.py` -- Threaded `previous_known_urls` through Jina-based extraction
- `vector_store.py` -- Added `cleanup_removed_urls()` with inverted index and failure tracking
- `cli.py` -- Orchestrates full snapshot lifecycle: load, pass, diff, safety guards, cleanup, save

### Playwright Page Actions (added 2026-02-15)
Six configurable features in `PLAYWRIGHT_CONFIG` to improve markdown scraping quality.
All default to OFF for backward compatibility.

**Pipeline order:**
navigate -> smart wait -> cookie dismiss -> scroll -> expand -> settle -> custom JS -> capture HTML -> convert to markdown -> quality gate

**Features and config keys:**

1. **Smart Wait (DOM Stability Detection)** -- Waits for `networkidle`, then polls
   `document.body.innerText.length` until stable across consecutive rounds.
   - `smart_wait_enabled`: false
   - `smart_wait_stability_rounds`: 3
   - `smart_wait_stability_interval_ms`: 500

2. **Auto-Scroll** -- Scrolls viewport-by-viewport to trigger lazy-loaded content.
   Stops when `document.body.scrollHeight` stabilises or max scrolls reached.
   Scrolls back to top when finished.
   - `auto_scroll_enabled`: false
   - `auto_scroll_max_scrolls`: 20
   - `auto_scroll_step_delay_ms`: 200

3. **Quality Gate** -- After markdown extraction, compares markdown length to HTML length.
   If the ratio is "suspect", automatically retries with escalated settings (enables
   smart wait + scroll + expand + extra 3s wait, capped at 15s). Maximum 1 escalation retry.
   - `quality_gate_enabled`: false
   - `quality_gate_min_markdown_length`: 100
   - `quality_gate_min_md_html_ratio`: 0.02

4. **Expand Collapsibles** -- Opens `<details>` elements, clicks `aria-expanded="false"`
   toggles, `data-toggle="collapse"` elements, and accordion class patterns. Excludes
   `<a>` tags to prevent navigation. Supports custom selectors.
   - `expand_collapsibles_enabled`: false
   - `expand_collapsibles_selectors`: "" (uses built-in heuristics when empty)
   - `expand_collapsibles_wait_ms`: 500

5. **Markdownify Improvements** -- Dynamic `strip_tags` (conditionally strips `<img>`),
   `table_infer_header` support, and post-processing that converts `![alt](url)` to
   `[Image: alt text]` markers (alt text < 3 chars is dropped entirely).
   - `convert_images_to_alt_text`: true
   - `table_infer_header`: true

6. **Cookie Banner Dismissal** -- Tries 8 built-in CMP selectors (Cookiebot, OneTrust,
   CookieFirst, CookieYes, Complianz, CookieConsent, generic) or custom selectors from
   config. Clicks the first match with a 2s timeout per selector.
   - `dismiss_cookie_banners`: false
   - `cookie_banner_selectors`: "" (uses 8 built-in CMP selectors when empty)

**Architecture notes:**
- `_prepare_page()` in `playwright_provider.py` centralises navigate -> wait -> cookie ->
  scroll -> expand -> JS -> capture. Returns `Optional[str]` (HTML) or `None` for retry.
- `_escalate_pw_cfg()` uses `copy.deepcopy()` with wait cap of 15000ms.
- Quality gate: outer `for _quality_attempt in range(2)` loop, inner retry loop for timeouts.
- All page action functions never raise -- errors logged internally, execution continues.
- Lazy imports in `_prepare_page()` avoid circular dependencies with `playwright_page_actions`.

**Files created/modified:**
- `playwright_page_actions.py` (NEW, ~335 lines) -- Four page interaction functions:
  `wait_for_content_ready()`, `dismiss_cookie_banners()`, `scroll_for_lazy_content()`,
  `expand_collapsible_sections()`
- `playwright_provider.py` -- Refactored `_html_to_markdown()` with pw_cfg parameter,
  extracted `_prepare_page()` helper, added `_assess_content_quality()` and `_escalate_pw_cfg()`,
  restructured `fetch_playwright_markdown()` with quality gate outer loop
- `config.py` -- Added 16 new fields to `PLAYWRIGHT_CONFIG` dict

## Key Files
- `scrape_sitemap_GPT_v2.py` - Original V2 script (reference)
- `gpt_scraper_v3/config.py` - Config dataclass + loading (replaces 40+ globals)
- `gpt_scraper_v3/cli.py` - Main orchestration + CLI args
- `gpt_scraper_v3/xml_sitemap.py` - XML sitemap parsing, timestamps, known URLs snapshot
- `gpt_scraper_v3/url_processing.py` - URL eligibility logic (lastmod, resume, new-URL detection)
- `gpt_scraper_v3/vector_store.py` - OpenAI vector store operations + removed URL cleanup
- `gpt_scraper_v3/playwright_provider.py` - Playwright browser automation, markdown conversion, quality gate
- `gpt_scraper_v3/playwright_page_actions.py` - Page interaction functions (smart wait, cookie dismiss, scroll, expand)
- `run_gpt_scraper_v3.py` - Entry point
- `scrape_sitemap_GPT_config_*_v3.json` - V3 config files per site