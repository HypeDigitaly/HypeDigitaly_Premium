# V3 Config Parameter Review Plan

**Date:** 2026-02-14
**Objective:** Final comprehensive review to verify V3 properly loads ALL config parameters and uses them identically to V2.

---

## Review Strategy (Context-Window-Aware)

Due to the large codebase (V2: 9,304 lines, V3: ~7,135 lines across 21 modules), this review is split into parallelized sub-reviews:

### Phase 1: Config Loading Verification (COMPLETE)
- [x] Compare V2 `load_configuration()` (lines 332-600) global variable list vs V3 `ScraperConfig` fields
- [x] Verify V3 `load_configuration()` maps every JSON config key to the correct ScraperConfig field
- [x] Verify V3 config validation matches V2 validation logic
- [x] Check V2 vs V3 config JSON schema differences (new: playwright, openrouter section restructured)

### Phase 2: Module-Level Parameter Usage (COMPLETE)
Each V3 module checked for:
1. Does it call `get_config()` to access parameters?
2. Does it use the correct field names from ScraperConfig?
3. Does the parameter behavior match V2's usage of the equivalent global?

**Group A - Core Pipeline (Agent 1):**
- `openrouter_client.py` - OpenRouter params
- `llm_prompts.py` - target language, URL/title params
- `content_fetching.py` - Jina/Firecrawl/Playwright provider params
- `playwright_provider.py` - All PLAYWRIGHT_CONFIG fields

**Group B - URL Discovery & Processing (Agent 2):**
- `url_processing.py` - CHECK_LAST_MODIFIED, CANONICAL_BASE_URLS, TEST_URLS, RECURSIVE_URLS
- `sitemap_parsing.py` - BASE_URL, BLACKLISTED_URLS
- `xml_sitemap.py` - timestamp files, CHECK_LAST_MODIFIED
- `rss_feeds.py` - RSS_FEEDS, RSS_DATE_THRESHOLD
- `pagination.py` - RECURSIVE_URLS, PAGINATED_URLS
- `pagination_processing.py` - All processing params

**Group C - Output Pipeline (Agent 3):**
- `chunking.py` - MAX_CHUNK_SIZE, CHUNK_OVERLAP, CONTENT_TOKEN_OFFSET
- `metadata_budget.py` - token budget params
- `chunk_processing.py` - vector store params
- `file_saving.py` - OUTPUT_DIR, metadata params
- `vector_store.py` - OPENAI_API_KEY, VECTOR_STORE_ID, chunking params
- `cli.py` - All CLI arg overrides mapped to config

**Group D - Infrastructure (Agent 4):**
- `utilities.py` - session params, filename params
- `logging_setup.py` - LOG_DIR, LOG_FILE
- `token_tracker.py` - token tracking (no direct config usage expected)

### Phase 3: Config File Compatibility (COMPLETE)
- [x] Verify every V3 config JSON file has all required keys
- [x] Verify V2 config files work with V3 (backward compatibility via .get() defaults)
- [x] Verify V3-specific additions (playwright section) have proper defaults

### Phase 4: Consolidated Report (COMPLETE)
- [x] Merge all sub-review findings
- [x] Severity classification per review skill standard
- [x] Final verdict: **PASS WITH NOTES** (0 CRITICAL, 1 HIGH, 6 MEDIUM, 22 LOW, 82+ PRAISE)

---

## Consolidated Findings (2026-02-14)

### HIGH (1)
| # | File | Issue |
|---|------|-------|
| H1 | `cli.py` | `--base-url` CLI override doesn't recompute derived URL fields (PARSED_BASE_URL, BASE_NETLOC, etc.) |

### MEDIUM (6)
| # | File | Issue |
|---|------|-------|
| M1 | `cli.py` | `--test-events-xml` dead/no-op argument |
| M2 | `cli.py` | `--jina-remove-selectors` not written back to cfg |
| M3 | `metadata_budget.py` | Hardcoded fallback 2048 for max_chunk_size_tokens |
| M4 | `chunking.py` | Function defaults duplicate config defaults |
| M5 | `playwright_provider.py` | Hardcoded retry count (max_retries=2) |
| M6 | `utilities.py` | Hardcoded token warning thresholds |

### V3 Improvements Over V2
- OPENROUTER_PROVIDER_CONFIG and cache settings now actually applied to API calls (V2 loaded but ignored)
- Graceful API key loading with .get() defaults (V2 would KeyError)
- Czech-calibrated token counting (3.2 chars/token vs V2's 4)
- Session singleton with cleanup (V2 created new session per call)
- CHECK_LAST_MODIFIED tri-state (0/1/2) with proper bool→int conversion

---

## V2 Global Variables → V3 ScraperConfig Field Mapping (Verified)

| # | V2 Global | V3 ScraperConfig Field | JSON Config Path | Status |
|---|-----------|----------------------|------------------|--------|
| 1 | SCRIPT_NAME | SCRIPT_NAME | script_info.name | OK |
| 2 | LOG_DIR | LOG_DIR | (derived from config filename) | OK |
| 3 | LOG_FILE | LOG_FILE | (derived) | OK |
| 4 | OUTPUT_DIR | OUTPUT_DIR | (derived from config filename) | OK |
| 5 | JINA_AI_API_KEY | JINA_AI_API_KEY | api_keys.jina_ai | OK (.get vs direct) |
| 6 | FIRECRAWL_API_KEY | FIRECRAWL_API_KEY | api_keys.firecrawl | OK (.get vs direct) |
| 7 | OPENAI_API_KEY | OPENAI_API_KEY | api_keys.openai | OK |
| 8 | OPENROUTER_API_KEY | OPENROUTER_API_KEY | api_keys.openrouter | OK |
| 9 | JINA_REMOVE_SELECTORS | JINA_REMOVE_SELECTORS | content_providers.jina.remove_selectors | OK |
| 10 | JINA_TARGET_SELECTORS | JINA_TARGET_SELECTORS | content_providers.jina.target_selectors | OK |
| 11 | MARKDOWN_PROVIDER_SEQUENCE | MARKDOWN_PROVIDER_SEQUENCE | content_providers.provider_sequence | OK |
| 12 | MARKDOWN_PROVIDERS | MARKDOWN_PROVIDERS | (built in load_configuration) | OK |
| 13 | OPENROUTER_MAX_TOKENS | OPENROUTER_MAX_TOKENS | openrouter.max_tokens | OK |
| 14 | OPENROUTER_MODELS | OPENROUTER_MODELS | openrouter.models | OK |
| 15 | OPENROUTER_TEMPERATURE | OPENROUTER_TEMPERATURE | openrouter.temperature | OK |
| 16 | OPENROUTER_TOP_P | OPENROUTER_TOP_P | openrouter.top_p | OK |
| 17 | OPENROUTER_TARGET_LANGUAGE | OPENROUTER_TARGET_LANGUAGE | openrouter.target_language | OK |
| 18 | OPENROUTER_PROVIDER_CONFIG | OPENROUTER_PROVIDER_CONFIG | openrouter.provider | OK |
| 19 | OPENROUTER_CACHE_ENABLED | OPENROUTER_CACHE_ENABLED | openrouter.enable_caching | OK |
| 20 | OPENROUTER_CACHE_TYPE | OPENROUTER_CACHE_TYPE | openrouter.cache_type | OK |
| 21 | OPENAI_VECTOR_STORE_ID | OPENAI_VECTOR_STORE_ID | vector_store.id | OK |
| 22 | ENABLE_DEDUPLICATION | ENABLE_DEDUPLICATION | vector_store.enable_deduplication | OK |
| 23 | DEFAULT_CHUNKING_STRATEGY | DEFAULT_CHUNKING_STRATEGY | vector_store.chunking_strategy | OK |
| 24 | DEFAULT_MAX_CHUNK_SIZE | DEFAULT_MAX_CHUNK_SIZE | vector_store.max_chunk_size | OK |
| 25 | DEFAULT_CHUNK_OVERLAP | DEFAULT_CHUNK_OVERLAP | vector_store.chunk_overlap | OK |
| 26 | DEFAULT_CONTENT_TOKEN_OFFSET | DEFAULT_CONTENT_TOKEN_OFFSET | vector_store.content_token_offset | OK |
| 27 | DEFAULT_CONTENT_RATIO | DEFAULT_CONTENT_RATIO | vector_store.content_ratio | OK |
| 28 | BASE_URL | BASE_URL | website.base_url | OK |
| 29 | PARSED_BASE_URL | PARSED_BASE_URL | (derived) | OK |
| 30 | BASE_NETLOC | BASE_NETLOC | (derived) | OK |
| 31 | NON_WWW_BASE_NETLOC | NON_WWW_BASE_NETLOC | (derived) | OK |
| 32 | BASE_SCHEME | BASE_SCHEME | (derived) | OK |
| 33 | CANONICAL_BASE_URLS | CANONICAL_BASE_URLS | (derived) | OK |
| 34 | SITEMAP_URL | SITEMAP_URL | website.sitemap_url | OK |
| 35 | XML_SITEMAP_URL | XML_SITEMAP_URL | website.xml_sitemap_url | OK |
| 36 | BLACKLISTED_URLS | BLACKLISTED_URLS | website.blacklisted_urls | OK |
| 37 | BLACKLISTED_RELATIVE_PATHS | BLACKLISTED_RELATIVE_PATHS | website.blacklisted_relative_url_paths | OK |
| 38 | RSS_FEEDS | RSS_FEEDS | website.rss_feeds | OK |
| 39 | RECURSIVE_URLS | RECURSIVE_URLS | website.recursive_urls | OK |
| 40 | PAGINATED_URLS | PAGINATED_URLS | website.paginated_urls | OK |
| 41 | TEST_URLS | TEST_URLS | website.test_urls | OK |
| 42 | REQUEST_TIMEOUT | REQUEST_TIMEOUT | http_settings.request_timeout | OK |
| 43 | REQUEST_RETRY_CODES | REQUEST_RETRY_CODES | http_settings.retry_codes | OK |
| 44 | REQUEST_RETRY_COUNT | REQUEST_RETRY_COUNT | http_settings.retry_count | OK |
| 45 | REQUEST_BACKOFF_FACTOR | REQUEST_BACKOFF_FACTOR | http_settings.backoff_factor | OK |
| 46 | CHECK_LAST_MODIFIED | CHECK_LAST_MODIFIED | processing.check_last_modified | OK (bool->int) |
| 47 | MAX_FILENAME_LENGTH | MAX_FILENAME_LENGTH | processing.max_filename_length | OK |
| 48 | RSS_DATE_THRESHOLD | RSS_DATE_THRESHOLD | processing.RSSDateThreshold | OK |
| 49 | LAST_RUN_FILE | LAST_RUN_FILE | (derived from config filename) | OK |
| 50 | VERBOSE_URL_MATCHING | VERBOSE_URL_MATCHING | (init to False) | OK |
| 51 | RSS_LAST_RUN_FILE | RSS_LAST_RUN_FILE | (derived) | OK |
| 52 | SITEMAP_LAST_RUN_FILE | SITEMAP_LAST_RUN_FILE | (derived) | OK |
| 53 | PROCESS_BASE_URL_ONCE | PROCESS_BASE_URL_ONCE | website.IncludeBaseURL | OK |
| 54 | BASE_URL_PROCESSED_IN_RUN | BASE_URL_PROCESSED_IN_RUN | (mutable state, init False) | OK |
| 55 | OPENAI_API_BASE_URL | OPENAI_API_BASE_URL | (constant) | OK |
| 56 | N/A (new V3) | PLAYWRIGHT_CONFIG | content_providers.playwright | NEW |

**Result: 55/55 V2 globals mapped + 1 new V3 addition. FULL PARITY CONFIRMED.**

---

## Key Behavioral Differences (V2 vs V3)

| Area | V2 Behavior | V3 Behavior | Impact |
|------|-------------|-------------|--------|
| API key loading | `CONFIG["api_keys"]["jina_ai"]` (KeyError if missing) | `.get("jina_ai", "")` (graceful) | LOW - V3 validates separately |
| CHECK_LAST_MODIFIED | Stored as-is (bool or int) | Converted to int (0/1/2) | CORRECT - V3 properly handles both |
| Session creation | New session per call (27 sites) | Singleton with atexit cleanup | IMPROVEMENT |
| Token counting | `len(text)//4` | `len(text)/3.2` with tiktoken fallback | IMPROVEMENT for Czech |
| Provider sequence | jina,firecrawl only | jina,firecrawl,playwright | EXTENSION |
| IncludeBaseURL | Prints with emoji | Prints without emoji | COSMETIC |
