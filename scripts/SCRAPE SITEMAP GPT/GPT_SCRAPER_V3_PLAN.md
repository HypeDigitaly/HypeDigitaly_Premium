# GPT Scraper V3 — Implementation Plan

**Source:** `scrape_sitemap_GPT_v2.py` (9,304 lines, 80+ functions)
**Target:** `gpt_scraper_v3/` (21 modules, ~7,135 lines, 500-line limit per file)
**Goal:** Modular architecture with all review fixes applied. Zero functionality loss.

---

## Table of Contents

1. [Review Findings to Fix](#1-review-findings-to-fix)
2. [Architecture Overview](#2-architecture-overview)
3. [Shared State Solution](#3-shared-state-solution)
4. [Module Breakdown](#4-module-breakdown)
5. [Dependency Graph](#5-dependency-graph)
6. [Cross-Cutting Refactors](#6-cross-cutting-refactors)
7. [Migration Checklist](#7-migration-checklist)

---

## 1. Review Findings to Fix

Every finding below must be resolved during the V3 migration. Fixes are applied inside the target module, not patched in V2.

### CRITICAL (3) — Block deployment

| # | Lines (V2) | Issue | Fix | Target Module |
|---|------------|-------|-----|---------------|
| C1 | 5148, 6415, 6548 | **Mutable default arguments** — `url_last_modified_map={}` and `path=[]` as function defaults. `extract_links()` is recursive with `path=[]`, so breadcrumbs accumulate across branches after first call. | Replace with `None` sentinel: `path=None` then `path = path if path is not None else []`. Same for all `dict` defaults. | `sitemap_parsing.py`, `content_fetching.py` |
| C2 | 332-600, 8643-8646 | **40+ mutable global variables** — `load_configuration()` sets 40+ module-level names via `global`. Every function coupled to module namespace. Untestable, not concurrency-safe. | Extract `ScraperConfig` dataclass (see [Section 3](#3-shared-state-solution)). All modules call `get_config()` instead of reading globals. | `config.py` (defines), all modules (consume) |
| C3 | 3704, 5347, 6216+ | **Full API responses logged at INFO level** — 6+ call sites dump entire HTTP response bodies (potentially GB). Information disclosure risk + disk exhaustion. | Move to `logger.debug()`. Truncate to 500 chars: `logger.debug(f"Response: {response.text[:500]}")`. Remove banner lines. | `content_fetching.py`, `openrouter_client.py`, `pagination.py` |

### HIGH (7) — Block merge

| # | Lines (V2) | Issue | Fix | Target Module |
|---|------------|-------|-----|---------------|
| H1 | 6304 | **`**globals()` passed as kwargs** — Injects entire Python namespace into function call. | Define typed context dict with only needed fields. Audit `process_and_save_chunk_immediately()` to accept explicit params. | `chunk_processing.py` |
| H2 | 8271-8319 | **Duplicate event metadata block** — Character-for-character identical block in `create_metadata_header()`. Event metadata written twice per file, wasting token budget. | Delete second block (copy-paste bug). | `file_saving.py` |
| H3 | 1017-3189 | **~900 lines of duplicated OpenRouter call logic** — 10+ functions repeat identical HTTP request, response parsing, reasoning-mode fallback, truncation. | Extract `_call_openrouter()` shared helper (see [Section 6.1](#61-unified-openrouter-client)). Each caller becomes ~15-25 lines. | `openrouter_client.py` |
| H4 | 8641-9305 | **`main()` is 660+ lines** — Handles argparse, caching, mode selection, URL dedup, processing loop, reporting. Cyclomatic complexity >30. | Decompose into `apply_cli_overrides()`, `build_caches()`, `resolve_urls()`, `process_urls()`, `report_summary()`. | `cli.py` |
| H5 | 1633-1652 | **New `requests.Session()` per call** — 27 call sites create fresh session. Defeats connection reuse, leaks sockets. | Create single module-level session via `get_session()`. Close in `atexit` handler. | `utilities.py` |
| H6 | 1659-1668 | **Path traversal in `sanitize_filename()`** — Does not strip `..` sequences. URL-derived filenames could write outside OUTPUT_DIR. | Add `filename.replace('..', '_')`. After `os.path.join()`, verify `os.path.realpath(result).startswith(os.path.realpath(OUTPUT_DIR))`. | `utilities.py` |
| H7 | 6108-6117 | **Token counting uses naive `len(text)//4`** — Wrong for Czech text (diacritics, longer words). Used for critical budget decisions. | Use ratio ~3.2 for Czech. Add `try: import tiktoken` with fallback. Document calibration. | `utilities.py` |

### MEDIUM (8) — Should fix

| # | Lines (V2) | Issue | Fix | Target Module |
|---|------------|-------|-----|---------------|
| M1 | 7012, 7025+ | **5 bare `except:` clauses** — Catches `KeyboardInterrupt`, `SystemExit`, `MemoryError`. | Replace with `except (ValueError, TypeError):` or narrowest type. | `xml_sitemap.py`, `file_saving.py` |
| M2 | 1539-1540 | **Log file truncated on every startup** — `open(LOG_FILE, 'w').close()` destroys previous run's logs. Defeats `RotatingFileHandler`. | Remove truncation line. RotatingFileHandler handles lifecycle. | `logging_setup.py` |
| M3 | 26-27, 1571+ | **13 redundant re-imports inside functions** — `os`, `re`, `json`, `urlparse` already imported at top level. | Remove all function-local re-imports of already-imported modules. | All affected modules |
| M4 | 610-700 | **90-line prompt embedded as f-string** — Hard to iterate, version-control, A/B test. | Keep as functions in `llm_prompts.py` (extracting to template files is optional future work). Ensure single source of truth. | `llm_prompts.py` |
| M5 | 1-9305 | **Zero type hints in 9,304 lines** — No static analysis, no IDE support. | Add type annotations to all public function signatures and the `ScraperConfig` dataclass during migration. | All modules |
| M6 | 1637-1639 | **`getattr(globals(), 'KEY', default)` is wrong** — `globals()` returns a dict; `getattr` on a dict looks for dict methods, always returns default. | Eliminated entirely by `ScraperConfig` migration. No more globals access. | `utilities.py` |
| M7 | 3360-3382 | **Emergency metadata truncation by character count** — Can break mid-UTF8 or mid-markdown tag. | Truncate by removing optional metadata sections in priority order (overlap > current_file > page_summary). | `chunk_processing.py` |
| M8 | 5291-5293 | **Content preview printed to stdout unconditionally** — 500 chars of scraped content dumped per URL. | Guard with `if logger.isEnabledFor(logging.DEBUG)`. | `content_fetching.py` |

### LOW (4) — May defer

| # | Lines (V2) | Issue | Fix | Target Module |
|---|------------|-------|-----|---------------|
| L1 | 1594-1600 | `.aspx`, `.jsp`, `.php` wrongly classified as file extensions to skip. | Remove from primary `FILE_EXTENSIONS_TO_SKIP`. Keep only in secondary handler-pattern check. | `utilities.py` |
| L2 | project root | No `requirements.txt` or dependency management. | Create `requirements.txt` in `gpt_scraper_v3/`. | `requirements.txt` |
| L3 | 8700-8712 | API keys accepted via CLI args — visible in `ps aux`, shell history. | Add env var fallback: `os.environ.get('JINA_API_KEY')`. Document risk in help text. | `cli.py` |
| L4 | 477+ | Mixed `print()` + `logger.info()` with emojis — no consistent boundary. | Use `print()` only before logger is initialized. After `setup_logging()`, use logger exclusively. | All modules |

---

## 2. Architecture Overview

```
gpt_scraper_v3/
    __init__.py              (~30 lines)   Package definition + exports
    __main__.py              (~15 lines)   python -m entry point
    config.py                (~470 lines)  ScraperConfig dataclass + loading
    logging_setup.py         (~50 lines)   Logger configuration
    token_tracker.py         (~60 lines)   OpenRouter token usage tracking
    utilities.py             (~350 lines)  URL, filename, session, token helpers
    openrouter_client.py     (~490 lines)  Unified OpenRouter API layer
    llm_prompts.py           (~500 lines)  All LLM prompt templates + question gen
    chunking.py              (~500 lines)  Adaptive content chunking
    metadata_budget.py       (~490 lines)  Budget-constrained metadata generation
    chunk_processing.py      (~420 lines)  Chunk pipeline (process + save)
    content_fetching.py      (~490 lines)  Jina AI, Firecrawl, markdown fetching
    pagination.py            (~500 lines)  AI + legacy pagination detection
    pagination_processing.py (~490 lines)  Paginated URL orchestration
    sitemap_parsing.py       (~130 lines)  HTML sitemap link extraction
    vector_store.py          (~495 lines)  OpenAI Vector Store operations
    rss_feeds.py             (~500 lines)  RSS/Atom/Events feed parsing
    xml_sitemap.py           (~490 lines)  XML sitemap + timestamps + cache
    url_processing.py        (~300 lines)  URL filtering, resume, eligibility
    file_saving.py           (~370 lines)  Metadata headers + file output
    cli.py                   (~500 lines)  Argparse + main orchestration
    requirements.txt                       Pinned dependencies

run_gpt_scraper_v3.py        (~10 lines)  Drop-in CLI replacement (outside folder)
```

**Total: ~7,135 lines** (23% reduction from 9,304 via deduplication, zero functionality removed)

---

## 3. Shared State Solution

### Problem

V2 uses 40+ module-level global variables set by `load_configuration()` via `global` statements. Every function reads these directly, creating hidden coupling.

### Solution: `ScraperConfig` dataclass

```python
# config.py
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple
from datetime import datetime
from urllib.parse import urlparse

@dataclass
class ScraperConfig:
    """All configuration state for a scraper run."""

    # Script identification
    SCRIPT_NAME: str = "scrape_sitemap_universal"
    LOG_DIR: Optional[str] = None
    LOG_FILE: Optional[str] = None
    OUTPUT_DIR: Optional[str] = None

    # API keys
    JINA_AI_API_KEY: str = ""
    FIRECRAWL_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    OPENROUTER_API_KEY: Optional[str] = None

    # Content provider settings
    JINA_REMOVE_SELECTORS: str = ""
    JINA_TARGET_SELECTORS: str = ""
    MARKDOWN_PROVIDER_SEQUENCE: str = ""
    MARKDOWN_PROVIDERS: dict = field(default_factory=dict)

    # OpenRouter settings
    OPENROUTER_MAX_TOKENS: int = 4000
    OPENROUTER_MODELS: list = field(default_factory=lambda: ["anthropic/claude-3.5-sonnet"])
    OPENROUTER_TEMPERATURE: float = 0.1
    OPENROUTER_TOP_P: float = 0.9
    OPENROUTER_TARGET_LANGUAGE: str = "Czech"
    OPENROUTER_PROVIDER_CONFIG: dict = field(default_factory=dict)
    OPENROUTER_CACHE_ENABLED: bool = False
    OPENROUTER_CACHE_TYPE: str = "ephemeral"

    # Vector store settings
    OPENAI_VECTOR_STORE_ID: str = ""
    ENABLE_DEDUPLICATION: bool = True
    DEFAULT_CHUNKING_STRATEGY: str = "auto"
    DEFAULT_MAX_CHUNK_SIZE: int = 800
    DEFAULT_CHUNK_OVERLAP: int = 400
    DEFAULT_CONTENT_TOKEN_OFFSET: int = 0
    DEFAULT_CONTENT_RATIO: float = 0.5

    # URL configuration
    BASE_URL: str = ""
    PARSED_BASE_URL: Optional[object] = None
    BASE_NETLOC: str = ""
    NON_WWW_BASE_NETLOC: str = ""
    BASE_SCHEME: str = ""
    CANONICAL_BASE_URLS: Set[str] = field(default_factory=set)
    SITEMAP_URL: str = ""
    XML_SITEMAP_URL: str = ""

    # URL lists
    BLACKLISTED_URLS: list = field(default_factory=list)
    BLACKLISTED_RELATIVE_PATHS: list = field(default_factory=list)
    RSS_FEEDS: list = field(default_factory=list)
    RECURSIVE_URLS: list = field(default_factory=list)
    PAGINATED_URLS: list = field(default_factory=list)
    TEST_URLS: list = field(default_factory=list)

    # HTTP settings
    REQUEST_TIMEOUT: int = 30
    REQUEST_RETRY_CODES: tuple = (500, 502, 503, 504, 524)
    REQUEST_RETRY_COUNT: int = 3
    REQUEST_BACKOFF_FACTOR: float = 0.3

    # Processing flags
    CHECK_LAST_MODIFIED: bool = True
    MAX_FILENAME_LENGTH: int = 200
    VERBOSE_URL_MATCHING: bool = False
    RSS_DATE_THRESHOLD: Optional[datetime] = None

    # Mutable run state
    PROCESS_BASE_URL_ONCE: bool = False
    BASE_URL_PROCESSED_IN_RUN: bool = False

    # File paths
    LAST_RUN_FILE: str = ""
    RSS_LAST_RUN_FILE: str = ""
    SITEMAP_LAST_RUN_FILE: str = ""

    # Constants
    OPENAI_API_BASE_URL: str = "https://api.openai.com/v1"
```

### Singleton access pattern

```python
# config.py (bottom)
_cfg: Optional[ScraperConfig] = None

def get_config() -> ScraperConfig:
    """Get the loaded configuration singleton."""
    if _cfg is None:
        raise RuntimeError("Configuration not loaded. Call load_configuration() first.")
    return _cfg

def load_configuration(config_file: str = "config.json") -> ScraperConfig:
    """Load config from JSON, validate, populate ScraperConfig, store as singleton."""
    global _cfg
    raw = load_config(config_file)
    validate_config(raw)
    _cfg = ScraperConfig(...)  # populate all fields from raw dict
    return _cfg
```

### Consumer pattern (all other modules)

```python
# In any module:
from gpt_scraper_v3.config import get_config

def some_function(url: str) -> str:
    cfg = get_config()
    if not cfg.OPENROUTER_API_KEY:
        return fallback()
    # ... use cfg.OPENROUTER_MODELS, cfg.REQUEST_TIMEOUT, etc.
```

---

## 4. Module Breakdown

### 4.1 `config.py` (~470 lines)

**Functions from V2:**
- `ScraperConfig` dataclass (~100 lines, NEW)
- `extract_config_identifier()` (V2 lines 24-55)
- `generate_unique_paths()` (V2 lines 57-65)
- `load_config()` (V2 lines 67-75)
- `validate_config()` (V2 lines 77-161)
- `parse_rss_date_threshold()` (V2 lines 163-174)
- `load_configuration()` (V2 lines 332-600, refactored to populate dataclass)
- `sanitize_json_response()` (V2 lines 248-285, generic utility)
- `get_config()` singleton accessor (NEW)

**Fixes applied:** C2 (global state elimination)

**Dependencies:** None (leaf module, stdlib only)

---

### 4.2 `logging_setup.py` (~50 lines)

**Functions from V2:**
- `setup_logging(cfg: ScraperConfig)` (V2 lines 1530-1563, refactored to accept config)
- `get_logger()` convenience accessor

**Fixes applied:** M2 (remove log file truncation on startup)

**Dependencies:** `config`

---

### 4.3 `token_tracker.py` (~60 lines)

**Functions from V2:**
- `global_token_usage` dict (V2 lines 192-197)
- `log_openrouter_token_usage()` (V2 lines 199-242)
- `get_token_usage_summary()` (NEW, for final reporting)
- `reset_token_usage()` (NEW, for test isolation)

**Dependencies:** `logging_setup`

---

### 4.4 `utilities.py` (~350 lines)

**Functions from V2:**
- `normalize_url_query_params()` (V2 lines 1569-1584)
- `is_file_url()` (V2 lines 1586-1618)
- `is_url_blacklisted_by_path()` (V2 lines 1621-1630)
- `requests_retry_session()` (V2 lines 1633-1652) -> refactored to `get_session()` singleton
- `remove_accents()` (V2 lines 1654-1657)
- `sanitize_filename()` (V2 lines 1659-1668)
- `create_filename_from_url()` (V2 lines 1670-1807)
- `construct_final_filename()` (V2 lines 1809-1850)
- `analyze_content_for_massive_data_warning()` (V2 lines 1852-1907)
- `count_tokens_approximate()` (V2 lines 6108-6117, moved here as fundamental utility)
- `get_chunk_postfix()` (extracted from nested function, NEW at module level)

**Fixes applied:**
- H5 (session singleton with `atexit` cleanup)
- H6 (path traversal protection in `sanitize_filename`)
- H7 (improved token counting ratio for Czech, optional tiktoken)
- M6 (eliminated `getattr(globals(), ...)` — no longer needed)
- L1 (remove `.aspx`/`.jsp`/`.php` from primary skip list)

**Dependencies:** `config`, `logging_setup`

---

### 4.5 `openrouter_client.py` (~490 lines)

**Functions from V2 (refactored):**
- `_call_openrouter()` (NEW ~60 lines — shared HTTP call helper)
- `_enforce_token_limit()` (NEW ~15 lines — shared truncation helper)
- `generate_page_summary_via_openrouter()` (V2 lines 1017-1141, ~40 lines after dedup)
- `generate_current_file_summary_via_openrouter()` (V2 lines 1197-1323, ~40 lines)
- `generate_overlap_summary_via_openrouter()` (V2 lines 1388-1525, ~45 lines)
- `_process_single_chunk_through_openrouter()` (V2 lines 6146-6280, ~40 lines)
- `summarize_content_via_openrouter()` (V2 lines 6254-6414, ~50 lines)
- `create_summarization_prompt()` (V2 lines 6119-6144)

**`_call_openrouter()` signature:**

```python
def _call_openrouter(
    messages: list[dict],
    max_tokens: int,
    call_name: str,
    url: str = "",
    temperature: Optional[float] = None,
    response_format: Optional[dict] = None,
) -> Optional[str]:
    """
    Unified OpenRouter API call. Handles:
    - Header construction with API key
    - Single model vs models array selection
    - Provider routing config
    - Prompt caching config
    - HTTP POST with retry session
    - Response parsing + reasoning-mode fallback
    - Token usage logging
    - Error handling (RequestException + general)
    Returns content string or None on failure.
    """
```

**Fixes applied:**
- H3 (eliminate ~600 lines of duplicated call logic)
- C3 (response logging moved to debug level)
- M5 (type hints on all signatures)

**Dependencies:** `config`, `logging_setup`, `token_tracker`, `utilities`

---

### 4.6 `llm_prompts.py` (~500 lines)

**Functions from V2:**
- `get_standard_summarization_instructions()` (V2 lines 610-700)
- `create_standard_fallback_content()` (V2 lines 702-728)
- `get_page_summary_instructions()` (V2 lines 730-766)
- `get_current_file_summary_instructions()` (V2 lines 1143-1195)
- `get_overlap_summary_instructions()` (V2 lines 1325-1386)
- `generate_question_section()` (V2 lines 768-879, uses `_call_openrouter`)
- `generate_events_question_section()` (V2 lines 882-1014, uses `_call_openrouter`)

**Fixes applied:** M4 (single source of truth for prompts), M5 (type hints)

**Dependencies:** `config`, `logging_setup`, `openrouter_client`, `utilities`

---

### 4.7 `chunking.py` (~500 lines)

**Functions from V2:**
- `chunk_massive_content_by_sections()` (V2 lines 1908-2145)
- `_chunk_by_natural_breaks()` (V2 lines 2147-2260)
- `_chunk_by_size_only()` (V2 lines 2262-2283)
- `chunk_content_with_metadata_budget()` (V2 lines 2284-2467)
- `chunk_large_content_simple()` (V2 lines 2470-2603, preserved with deprecation warning)
- `generate_section_based_filename()` (V2 lines 2605-2654)

**Notes:** `get_chunk_postfix()` inner function extracted to `utilities.py` module level.

**Dependencies:** `config`, `logging_setup`, `utilities`

---

### 4.8 `metadata_budget.py` (~490 lines)

**Functions from V2 (refactored to use `_call_openrouter`):**
- `calculate_metadata_token_allocation()` (V2 lines 2660-2717)
- `generate_budget_constrained_page_summary()` (V2 lines 2720-2825, ~55 lines after dedup)
- `generate_budget_constrained_current_file_summary()` (V2 lines 2828-2942, ~55 lines)
- `generate_budget_constrained_overlap_summary()` (V2 lines 2945-3056, ~55 lines)
- `generate_budget_constrained_question_section()` (V2 lines 3057-3189, ~70 lines)
- `create_budget_constrained_metadata_header()` (V2 lines 3192-3272)

**Notes:** Budget-constrained variants kept separate from non-budget versions (they use `system` messages + `temperature=0.0`). Deduplication via shared `_call_openrouter()`.

**Dependencies:** `config`, `logging_setup`, `openrouter_client`, `utilities`, `token_tracker`

---

### 4.9 `chunk_processing.py` (~420 lines)

**Functions from V2:**
- `process_and_save_chunks_with_metadata_budget()` (V2 lines 3274-3466)
- `process_and_save_chunk_immediately()` (V2 lines 3469-3596)
- `save_chunked_content_to_multiple_files()` (V2 lines 3599-3675)

**Fixes applied:**
- H1 (replace `**globals()` with explicit typed context)
- M7 (structured metadata truncation by section priority)

**Dependencies:** `config`, `logging_setup`, `utilities`, `openrouter_client`, `llm_prompts`, `metadata_budget`, `chunking`, `file_saving`, `vector_store`

---

### 4.10 `content_fetching.py` (~490 lines)

**Functions from V2:**
- `get_html_content_via_jina()` (V2 lines 3681-3723)
- `get_html_content_for_pagination_via_jina()` (V2 lines 3729-3771)
- `get_markdown_content()` (V2 lines 5238-5314)
- `_fetch_jina_markdown()` (V2 lines 5317-5471)
- `_fetch_firecrawl_markdown()` (V2 lines 5474-5506)
- `extract_links_from_jina_summary()` (V2 lines 6415-6512)

**Fixes applied:**
- C3 (response logging to debug)
- M3 (remove redundant imports)
- M8 (guard content preview with debug level)

**Dependencies:** `config`, `logging_setup`, `utilities`

---

### 4.11 `pagination.py` (~500 lines)

**Functions from V2:**
- `detect_pagination_in_html()` (V2 lines 3772-4120)
- `_detect_pagination_legacy_fallback()` (V2 lines 4122-4261)
- `is_url_recursive_enabled()` (V2 lines 4263-4298)
- `is_url_explicitly_paginated()` (V2 lines 4300-4315)
- `extract_suburls_from_ai_data()` (V2 lines 4317-4389)
- `_construct_pagination_url()` (V2 lines 4391-4446)

**Fixes applied:** C3 (response logging to debug)

**Dependencies:** `config`, `logging_setup`, `openrouter_client`, `utilities`, `token_tracker`

---

### 4.12 `pagination_processing.py` (~490 lines)

**Functions from V2:**
- `extract_pagination_urls()` (V2 lines 4447-4723)
- `process_paginated_url()` (V2 lines 4725-5010)
- `process_single_url_normally()` (V2 lines 5010-5147)

**Dependencies:** `config`, `logging_setup`, `utilities`, `content_fetching`, `pagination`, `chunking`, `chunk_processing`, `file_saving`, `metadata_budget`, `llm_prompts`, `openrouter_client`, `vector_store`

---

### 4.13 `sitemap_parsing.py` (~130 lines)

**Functions from V2:**
- `extract_links_from_html_sitemap()` (V2 lines 5148-5237)
- `parse_menu()` (V2 lines 6513-6546, DEPRECATED, preserved)
- `extract_links()` (V2 lines 6548-6622, DEPRECATED, preserved)

**Fixes applied:** C1 (mutable default args: `path=None`, `url_last_modified_map=None`)

**Dependencies:** `config`, `logging_setup`, `utilities`, `url_processing`, `xml_sitemap`

---

### 4.14 `vector_store.py` (~495 lines)

**Functions from V2:**
- `upload_file_to_openai()` (V2 lines 5512-5555)
- `add_file_to_vector_store()` (V2 lines 5558-5614)
- `list_vector_store_files()` (V2 lines 5617-5661)
- `get_vector_store_file_attributes()` (V2 lines 5664-5710)
- `find_existing_file_by_url()` (V2 lines 5713-5751)
- `delete_vector_store_file()` (V2 lines 5754-5785)
- `build_vector_store_cache()` (V2 lines 5788-5861)
- `find_existing_file_by_url_cached()` (V2 lines 5864-5876)
- `find_existing_files_by_url_cached()` (V2 lines 5879-5904)
- `create_chunking_strategy()` (V2 lines 5907-5974)
- `upload_and_add_to_vector_store()` (V2 lines 5975-6102)

**Dependencies:** `config`, `logging_setup`, `utilities`

---

### 4.15 `rss_feeds.py` (~500 lines)

**Functions from V2:**
- `parse_rss_feed()` (V2 lines 6628-6686)
- `parse_atom_feed()` (V2 lines 6688-6742)
- `parse_rss_2_0_feed()` (V2 lines 6743-6822)
- `parse_events_feed()` (V2 lines 6823-6979)
- `parse_custom_response_feed()` (V2 lines 6980-7088)
- `parse_generic_feed()` (V2 lines 7089-7149)
- `process_rss_feeds()` (V2 lines 7150-7205)

**Dependencies:** `config`, `logging_setup`, `utilities`, `xml_sitemap`

---

### 4.16 `xml_sitemap.py` (~490 lines)

**Functions from V2:**
- `parse_lastmod_date()` (V2 lines 7206-7224)
- `fetch_xml_sitemap()` (V2 lines 7226-7318)
- `find_url_last_modified()` (V2 lines 7320-7410)
- `_find_domain_variation_match()` (V2 lines 7413-7429)
- `_find_path_based_match()` (V2 lines 7432-7450)
- `_find_flexible_substring_match()` (V2 lines 7453-7499)
- `_find_legacy_substring_match()` (V2 lines 7502-7507)
- `should_process_rss_url()` (V2 lines 7508-7672)
- `extract_urls_from_xml_sitemap()` (V2 lines 7673-7712)
- `get_last_run_timestamp()` (V2 lines 7713-7735)
- `save_last_run_timestamp()` (V2 lines 7736-7758)
- `build_local_files_cache()` (V2 lines 7760-7867)

**Fixes applied:** M1 (replace bare `except:` with specific exception types)

**Dependencies:** `config`, `logging_setup`, `utilities`, `url_processing`

---

### 4.17 `url_processing.py` (~300 lines)

**Functions from V2:**
- `is_url_already_processed_locally()` (V2 lines 7868-7872)
- `is_url_from_test_urls()` (V2 lines 7874-7887)
- `is_url_suburl_of_test_recursive_url()` (V2 lines 7889-7934)
- `should_process_url_with_resume()` (V2 lines 7936-8132)
- `validate_test_urls()` (V2 lines 8494-8555)
- `process_test_urls()` (V2 lines 8556-8640)

**Dependencies:** `config`, `logging_setup`, `utilities`, `vector_store`

---

### 4.18 `file_saving.py` (~370 lines)

**Functions from V2:**
- `create_metadata_header()` (V2 lines 8133-8334)
- `save_markdown_to_file()` (V2 lines 8337-8488)

**Fixes applied:**
- H2 (delete duplicate event metadata block)
- M1 (replace bare `except:` with specific types)

**Dependencies:** `config`, `logging_setup`, `utilities`, `vector_store`

---

### 4.19 `cli.py` (~500 lines)

**Functions from V2 (refactored):**
- `build_argument_parser()` (NEW, extracted from V2 lines 9233-9301)
- `apply_cli_overrides(args, cfg: ScraperConfig)` (NEW, extracted from V2 lines 8660-8760)
- `build_caches(cfg, args)` (NEW, extracted from V2 lines 8788-8823)
- `resolve_urls(cfg, args, ...)` (NEW, extracted from V2 lines 8848-8970)
- `process_urls(extracted_urls, ...)` (NEW, extracted from V2 lines 9040-9180)
- `report_summary(stats)` (NEW, extracted from V2 lines 9180-9230)
- `main(args=None)` (V2 lines 8641-9232, orchestration shell ~80 lines)

**Fixes applied:**
- H4 (main decomposed from 660 lines into 6 focused functions)
- L3 (env var fallback for API keys)
- L4 (consistent print vs logger usage)

**Dependencies:** All modules (this is the orchestrator)

---

### 4.20 `__init__.py` (~30 lines)

```python
"""GPT Scraper V3 - Modular web scraping and RAG content processing toolkit."""

from gpt_scraper_v3.config import load_configuration, get_config, ScraperConfig
from gpt_scraper_v3.logging_setup import setup_logging, get_logger
from gpt_scraper_v3.cli import main

__version__ = "3.0.0"
__all__ = [
    "load_configuration", "get_config", "ScraperConfig",
    "setup_logging", "get_logger", "main",
]
```

---

### 4.21 `__main__.py` (~15 lines)

```python
"""Entry point: python -m gpt_scraper_v3"""
from gpt_scraper_v3.cli import build_argument_parser, main

if __name__ == "__main__":
    parser = build_argument_parser()
    args = parser.parse_args()
    main(args)
```

---

### 4.22 `run_gpt_scraper_v3.py` (~10 lines, outside folder)

Drop-in CLI replacement. Same arguments, same config files.

```python
"""Drop-in replacement for scrape_sitemap_GPT_v2.py"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gpt_scraper_v3.cli import build_argument_parser, main

if __name__ == "__main__":
    parser = build_argument_parser()
    args = parser.parse_args()
    main(args)
```

---

### 4.23 `requirements.txt`

```
requests>=2.28,<3.0
beautifulsoup4>=4.12
python-dateutil>=2.8
lxml>=4.9
```

---

## 5. Dependency Graph

```
Layer 0 (no deps):     config.py
                           |
Layer 1:              logging_setup.py
                           |
Layer 2:              token_tracker.py
                           |
Layer 3:               utilities.py
                        /    |    \
                       /     |     \
Layer 4:   openrouter_client |   vector_store.py
                |    \       |        |
                |     \      |        |
Layer 5:  llm_prompts  \    |        |
                |    metadata_budget  |
                |        |            |
Layer 6:     chunk_processing.py      |
                |                     |
Layer 7:  content_fetching.py         |
                |                     |
Layer 8:    pagination.py             |
                |                     |
Layer 9:  pagination_processing.py ---+
                |
Layer 10:   rss_feeds.py
                |
Layer 11:  xml_sitemap.py
                |
Layer 12:  url_processing.py
                |
Layer 13: sitemap_parsing.py
                |
Layer 14:  file_saving.py
                |
Layer 15:     cli.py  (imports all)
```

### Import table

| Module | Imports from |
|--------|-------------|
| `config.py` | _(none)_ |
| `logging_setup.py` | `config` |
| `token_tracker.py` | `logging_setup` |
| `utilities.py` | `config`, `logging_setup` |
| `openrouter_client.py` | `config`, `logging_setup`, `token_tracker`, `utilities` |
| `llm_prompts.py` | `config`, `logging_setup`, `openrouter_client`, `utilities` |
| `chunking.py` | `config`, `logging_setup`, `utilities` |
| `metadata_budget.py` | `config`, `logging_setup`, `openrouter_client`, `utilities`, `token_tracker` |
| `chunk_processing.py` | `config`, `logging_setup`, `utilities`, `openrouter_client`, `llm_prompts`, `metadata_budget`, `chunking`, `file_saving`, `vector_store` |
| `content_fetching.py` | `config`, `logging_setup`, `utilities` |
| `pagination.py` | `config`, `logging_setup`, `openrouter_client`, `utilities`, `token_tracker` |
| `pagination_processing.py` | `config`, `logging_setup`, `utilities`, `content_fetching`, `pagination`, `chunking`, `chunk_processing`, `file_saving`, `metadata_budget`, `llm_prompts`, `openrouter_client`, `vector_store` |
| `sitemap_parsing.py` | `config`, `logging_setup`, `utilities`, `url_processing`, `xml_sitemap` |
| `vector_store.py` | `config`, `logging_setup`, `utilities` |
| `rss_feeds.py` | `config`, `logging_setup`, `utilities`, `xml_sitemap` |
| `xml_sitemap.py` | `config`, `logging_setup`, `utilities`, `url_processing` |
| `url_processing.py` | `config`, `logging_setup`, `utilities`, `vector_store` |
| `file_saving.py` | `config`, `logging_setup`, `utilities`, `vector_store` |
| `cli.py` | ALL modules |

**No circular dependencies.** The graph is a strict DAG.

---

## 6. Cross-Cutting Refactors

### 6.1 Unified OpenRouter Client

**Problem:** 10+ functions duplicate ~40-80 lines of identical HTTP request boilerplate.

**Solution:** Single `_call_openrouter()` in `openrouter_client.py`.

```python
def _call_openrouter(
    messages: list[dict],
    max_tokens: int,
    call_name: str,
    url: str = "",
    temperature: Optional[float] = None,
    response_format: Optional[dict] = None,
) -> Optional[str]:
    """
    Single point of OpenRouter API interaction.

    Handles:
    1. Header construction (Authorization, Content-Type, HTTP-Referer, X-Title)
    2. Model selection (single "model" vs array "models" key)
    3. Provider routing + prompt caching config from ScraperConfig
    4. HTTP POST via shared session with retry
    5. Response parsing + reasoning-mode fallback
    6. Token usage logging via token_tracker
    7. Error handling (RequestException specifically, then Exception)
    8. Debug-level response logging (fix C3)

    Returns: extracted content string, or None on failure.
    """
```

**Before (V2, repeated 10+ times):**
```python
def generate_page_summary_via_openrouter(markdown_content, title="", url="", target_language="Czech"):
    if not OPENROUTER_API_KEY:                        # 3 lines: API key check
        return None
    prompt = ...                                       # 5 lines: prompt construction
    headers = {                                        # 5 lines: headers
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        ...
    }
    if isinstance(OPENROUTER_MODELS, list) and len(...):  # 15 lines: model branching
        payload = { "models": OPENROUTER_MODELS, ... }
    else:
        payload = { "model": primary_model, ... }
    try:                                               # 30 lines: request + parse
        response = requests_retry_session().post(...)
        response.raise_for_status()
        logger.info(f"Full response: {response.text}") # C3 bug
        data = response.json()
        log_openrouter_token_usage(data, ...)
        if "choices" in data and ...:
            message = data["choices"][0]["message"]
            content = message.get("content", "").strip()
            if not content and message.get("reasoning"):  # reasoning fallback
                content = message.get("reasoning", "").strip()
            if content:
                result_tokens = count_tokens_approximate(content)
                if result_tokens > MAX:                    # truncation
                    content = content[:MAX*4] + "..."
                return content
    except requests.exceptions.RequestException as e:  # 4 lines: error handling
        logger.error(...)
        return None
    except Exception as e:                             # 3 lines: fallback error
        logger.error(...)
        return None
```

**After (V3):**
```python
def generate_page_summary_via_openrouter(
    markdown_content: str, title: str = "", url: str = "", target_language: str = "Czech"
) -> Optional[str]:
    cfg = get_config()
    if not cfg.OPENROUTER_API_KEY:
        return None

    prompt = get_page_summary_instructions(target_language, url, title)
    prompt += f"\n\n## SOURCE CONTENT:\n{markdown_content}"

    result = _call_openrouter(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=280,
        call_name="PAGE_SUMMARY_GENERATION",
        url=url,
    )
    return _enforce_token_limit(result, max_tokens=190) if result else None
```

**Savings:** ~600 lines of duplicated code eliminated.

---

### 6.2 Token Limit Enforcement Helper

**Problem:** This pattern appears 6+ times identically:
```python
if result_tokens > MAX_TOKENS:
    estimated_chars = MAX_TOKENS * 4
    summary = summary[:estimated_chars] + "..."
```

**Solution:**
```python
def _enforce_token_limit(text: Optional[str], max_tokens: int) -> Optional[str]:
    """Truncate text to stay within token budget at word boundary."""
    if text is None:
        return None
    tokens = count_tokens_approximate(text)
    if tokens <= max_tokens:
        return text
    # Truncate at last whitespace before estimated char limit
    estimated_chars = int(max_tokens * 3.2)  # Czech-calibrated ratio
    truncated = text[:estimated_chars]
    last_space = truncated.rfind(' ')
    if last_space > estimated_chars // 2:
        truncated = truncated[:last_space]
    return truncated + "..."
```

---

### 6.3 Session Management

**Problem:** `requests_retry_session()` creates a new `Session()` on every call (27 call sites).

**Solution:**
```python
# utilities.py
import atexit

_session: Optional[requests.Session] = None

def get_session() -> requests.Session:
    """Get or create the shared requests session with retry configuration."""
    global _session
    if _session is None:
        cfg = get_config()
        _session = requests.Session()
        retry = Retry(
            total=cfg.REQUEST_RETRY_COUNT,
            backoff_factor=cfg.REQUEST_BACKOFF_FACTOR,
            status_forcelist=cfg.REQUEST_RETRY_CODES,
        )
        adapter = HTTPAdapter(max_retries=retry)
        _session.mount('http://', adapter)
        _session.mount('https://', adapter)
        atexit.register(_session.close)
    return _session
```

---

### 6.4 Path Traversal Protection

**Addition to `sanitize_filename()`:**
```python
def sanitize_filename(filename: str) -> str:
    """Sanitize filename for safe file system usage."""
    filename = remove_accents(filename)
    filename = filename.replace('..', '_')           # NEW: prevent traversal
    filename = re.sub(r"[<>:\"/\\|?*]", "_", filename)
    filename = re.sub(r"\s+", "_", filename)
    filename = re.sub(r"_+", "_", filename)
    filename = filename.strip("_.")                  # NEW: also strip dots
    cfg = get_config()
    return filename[:cfg.MAX_FILENAME_LENGTH]
```

**Addition to file save paths:**
```python
# In file_saving.py, after os.path.join():
resolved = os.path.realpath(filepath)
if not resolved.startswith(os.path.realpath(cfg.OUTPUT_DIR)):
    raise ValueError(f"Path traversal detected: {filepath}")
```

---

## 7. Migration Checklist

Execute in this order. Each step produces a working state.

### Phase 1: Scaffold (no behavior change)

- [ ] Create `gpt_scraper_v3/` directory
- [ ] Create `__init__.py` with version and exports
- [ ] Create `__main__.py` entry point
- [ ] Create `run_gpt_scraper_v3.py` outside folder
- [ ] Create `requirements.txt` with pinned dependencies

### Phase 2: Foundation modules (leaf dependencies first)

- [ ] Create `config.py` — `ScraperConfig` dataclass + all config functions
  - Apply fix C2 (global state -> dataclass)
  - Move `sanitize_json_response()` here
  - Add type hints (M5)
- [ ] Create `logging_setup.py` — logger setup
  - Apply fix M2 (remove log file truncation)
- [ ] Create `token_tracker.py` — token usage tracking
- [ ] Create `utilities.py` — all utility functions
  - Apply fixes H5 (session singleton), H6 (path traversal), H7 (token counting)
  - Apply fixes M6 (getattr fix), L1 (file extension fix)
  - Move `count_tokens_approximate()` here
  - Extract `get_chunk_postfix()` to module level
  - Remove redundant imports (M3)

### Phase 3: OpenRouter consolidation (biggest dedup win)

- [ ] Create `openrouter_client.py` — `_call_openrouter()` + `_enforce_token_limit()`
  - Apply fix H3 (eliminate ~600 lines duplication)
  - Apply fix C3 (debug-level response logging)
  - Migrate all 6 summary/generation functions to use shared helper
- [ ] Create `llm_prompts.py` — prompt templates + question generation
  - Migrate `generate_question_section` + `generate_events_question_section`
  - Apply fix M4 (single source of truth)

### Phase 4: Content processing pipeline

- [ ] Create `chunking.py` — all chunking algorithms
- [ ] Create `metadata_budget.py` — budget-constrained metadata generation
  - Refactor to use `_call_openrouter()` from openrouter_client
- [ ] Create `content_fetching.py` — Jina, Firecrawl, markdown fetching
  - Apply fix C3 (debug logging), M8 (guard content preview)
  - Move `extract_links_from_jina_summary()` here
- [ ] Create `vector_store.py` — all OpenAI Vector Store operations

### Phase 5: URL discovery pipeline

- [ ] Create `pagination.py` — AI + legacy pagination detection
- [ ] Create `pagination_processing.py` — paginated URL processing
- [ ] Create `rss_feeds.py` — all RSS/Atom/Events feed parsers
- [ ] Create `xml_sitemap.py` — XML sitemap + timestamps
  - Apply fix M1 (bare except clauses)
- [ ] Create `url_processing.py` — URL filtering and resume logic
- [ ] Create `sitemap_parsing.py` — HTML sitemap extraction
  - Apply fix C1 (mutable default args)

### Phase 6: Output + orchestration

- [ ] Create `file_saving.py` — metadata headers + file output
  - Apply fix H2 (delete duplicate event metadata block)
  - Apply fix M1 (bare except clauses)
- [ ] Create `chunk_processing.py` — chunk pipeline
  - Apply fix H1 (replace `**globals()` with explicit context)
  - Apply fix M7 (structured metadata truncation)
- [ ] Create `cli.py` — argparse + decomposed main()
  - Apply fix H4 (decompose 660-line main into focused functions)
  - Apply fix L3 (env var fallback for API keys)
  - Apply fix L4 (consistent print vs logger)

### Phase 7: Validation

- [ ] Verify all 80+ functions are present in V3 (no functionality lost)
- [ ] Run with each existing config file to confirm identical behavior
- [ ] Verify all `.bat` launcher scripts work with `run_gpt_scraper_v3.py`
- [ ] Verify every module is under 500 lines (600 max)
- [ ] Verify no circular imports (import each module individually)
- [ ] Spot-check that scraped output files are identical to V2 output

---

## Summary

| Metric | V2 | V3 |
|--------|----|----|
| Files | 1 | 21 |
| Total lines | 9,304 | ~7,135 |
| Max file length | 9,304 | 500 (hard limit) |
| Global variables | 40+ | 0 (ScraperConfig dataclass) |
| Duplicated OpenRouter code | ~900 lines | ~60 lines (shared helper) |
| Type hints | 0 | All public signatures |
| Mutable default arg bugs | 3 | 0 |
| Path traversal protection | No | Yes |
| Session reuse | No (27 new sessions) | Yes (singleton) |
| Testability | Impossible (globals) | Module-level isolation |
