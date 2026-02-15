# GPT Scraper V3 -- Architecture Documentation

## Table of Contents

1. [Overview](#overview)
2. [Module Structure](#module-structure)
3. [Module Dependency Graph](#module-dependency-graph)
4. [Configuration System](#configuration-system)
5. [Processing Pipeline](#processing-pipeline)
6. [Key Subsystems](#key-subsystems)
7. [CLI Arguments Reference](#cli-arguments-reference)
8. [How to Run](#how-to-run)
9. [Migration Guide: V2 to V3](#migration-guide-v2-to-v3)
10. [Review Findings Applied](#review-findings-applied)

---

## Overview

GPT Scraper V3 is a modular Python package (`gpt_scraper_v3/`) that scrapes websites via their sitemaps (HTML, XML, RSS), converts content to markdown, summarises it through LLMs (OpenRouter), and uploads the results to an OpenAI Vector Store for RAG (Retrieval-Augmented Generation) pipelines.

V3 was produced by decomposing a monolithic 9,304-line script (`scrape_sitemap_GPT_v2.py`) into 21 focused modules totalling approximately 8,128 lines. The migration applied 22 review findings (3 CRITICAL, 7 HIGH, 8 MEDIUM, 4 LOW) and introduced type hints on all public function signatures.

### Design Principles

- **Single Responsibility** -- each module owns one concern (config, HTTP, chunking, etc.).
- **Singleton Configuration** -- the 40+ global variables of V2 are replaced by a single `ScraperConfig` dataclass accessed via `get_config()`.
- **Shared Session** -- the V2 pattern of creating a new `requests.Session` per call (27 sites) is replaced by a module-level singleton with `atexit` cleanup.
- **Unified LLM Client** -- approximately 600 lines of duplicated OpenRouter API boilerplate are consolidated into one `_call_openrouter()` helper.
- **Explicit Context** -- the V2 `**globals()` pattern used to pass state into chunk processing is replaced with a `ChunkContext` TypedDict.

---

## Module Structure

```
gpt_scraper_v3/
    __init__.py              (11 lines)    Package definition and public exports
    __main__.py              (7 lines)     python -m entry point
    config.py                (463 lines)   ScraperConfig dataclass + JSON loading + validation
    logging_setup.py         (52 lines)    RotatingFileHandler + console logger
    token_tracker.py         (67 lines)    OpenRouter token usage accumulator
    utilities.py             (477 lines)   URL helpers, filename generation, session singleton, token counting
    openrouter_client.py     (447 lines)   Unified OpenRouter API layer + summary generators
    llm_prompts.py           (471 lines)   Prompt templates + question generation
    chunking.py              (493 lines)   Adaptive section-based and metadata-budget chunking
    metadata_budget.py       (474 lines)   Budget-constrained metadata generation (summaries, questions)
    chunk_processing.py      (456 lines)   Chunk pipeline: process, validate, save, upload
    content_fetching.py      (405 lines)   Jina AI (3-strategy fallback), Firecrawl, link extraction
    pagination.py            (494 lines)   AI-powered + legacy fallback pagination detection
    pagination_processing.py (494 lines)   Paginated/recursive URL orchestration
    sitemap_parsing.py       (377 lines)   HTML sitemap <a> tag extraction + deprecated menu parser
    vector_store.py          (490 lines)   OpenAI Vector Store CRUD, caching, deduplication
    rss_feeds.py             (450 lines)   RSS 2.0, Atom, Events XML, custom feed parsing
    xml_sitemap.py           (494 lines)   XML sitemap fetching, timestamp management, resume cache
    url_processing.py        (515 lines)   URL filtering, eligibility, resume logic, test URLs
    file_saving.py           (483 lines)   Metadata headers + file output + path traversal protection
    cli.py                   (499 lines)   Argparse definitions + main() orchestration
    requirements.txt                       Pinned dependencies

run_gpt_scraper_v3.py        (10 lines)   Drop-in CLI replacement for V2
```

### Module Summaries

| Module | Responsibility |
|--------|---------------|
| `config.py` | Loads `config.json`, validates it, populates a `ScraperConfig` dataclass, and stores it as a module singleton. |
| `logging_setup.py` | Configures a `RotatingFileHandler` (10 MB, 5 backups) and a `StreamHandler`. Does not truncate logs on startup. |
| `token_tracker.py` | Accumulates prompt/completion/total token counts across all OpenRouter calls during a run. |
| `utilities.py` | URL normalisation, file-extension detection, filename sanitisation (with path traversal protection), session singleton with retry, Czech-calibrated token counting. |
| `openrouter_client.py` | Single `_call_openrouter()` helper handling auth, model selection, provider routing, prompt caching, retries, reasoning-mode fallback, and token logging. Also hosts page/file/overlap summary generators and the content summariser. |
| `llm_prompts.py` | Canonical prompt templates for summarisation, question generation, file summaries, and overlap summaries. Single source of truth (M4 fix). |
| `chunking.py` | Adaptive section-based chunking that discovers structural elements (headers, tables, separators) and breaks at logical boundaries. Also provides metadata-budget-aware chunking and a deprecated legacy chunker. |
| `metadata_budget.py` | Token budget calculator and four budget-constrained generators (page summary, file summary, overlap summary, question section) that use `system` messages with `temperature=0.0`. |
| `chunk_processing.py` | Orchestrates the chunk pipeline: calculates token allocations, generates metadata, validates against vector store limits, performs priority-based truncation, saves files, and uploads. Uses explicit `ChunkContext` TypedDict (H1 fix). |
| `content_fetching.py` | Fetches content via Jina AI (markdown with selectors, markdown without selectors, HTML-to-text fallback) or Firecrawl. Also extracts links from Jina summary data. |
| `pagination.py` | AI-powered pagination detection via OpenRouter Structured Outputs with JSON schema. Falls back to legacy BeautifulSoup pattern matching. Also handles recursive URL configuration and sub-URL extraction. |
| `pagination_processing.py` | Orchestrates the full pagination workflow: detect pagination/suburls, extract URLs, process the main page, process subpages, and recursively process suburls within depth limits. |
| `sitemap_parsing.py` | Primary HTML sitemap parser using `<a>` tag extraction. Includes deprecated `parse_menu()` and `extract_links()` for legacy compatibility. |
| `vector_store.py` | Full OpenAI Vector Store lifecycle: upload files, add to store with attributes, list files with pagination, build fast lookup cache, deduplication by exact lookup URL, chunking strategy validation. |
| `rss_feeds.py` | Detects and parses RSS 2.0, Atom, Events XML, and custom `<response><item>` feeds. Strips Chrome extension script tags. Handles CDATA in event metadata. |
| `xml_sitemap.py` | Fetches XML sitemaps (including sitemap index files with `?page=N`), parses lastmod dates, manages per-mode timestamps (`combined`/`rss`/`sitemap`), and builds the local resume cache from output `.txt` files. |
| `url_processing.py` | Central eligibility engine: checks blacklists, file URLs, base URL skipping, test URLs, recursive sub-URLs, resume cache, RSS date thresholds, and last-modified timestamps via multi-strategy URL matching (6 strategies). |
| `file_saving.py` | Creates rich metadata headers (Czech-language fields, RSS metadata, event details, chunked file summaries) and saves files with path traversal protection. Handles V[N] versioning for filename collisions. |
| `cli.py` | Defines all argparse arguments, applies CLI overrides (including env-var API key fallback), orchestrates the complete run lifecycle across 7 focused functions. |

---

## Module Dependency Graph

The following diagram shows the import relationships between modules. Arrows point from the importing module to the imported module.

```
cli.py
  +---> config.py
  +---> logging_setup.py
  +---> token_tracker.py
  +---> vector_store.py
  +---> xml_sitemap.py
  +---> rss_feeds.py
  +---> sitemap_parsing.py
  +---> content_fetching.py
  +---> url_processing.py
  +---> pagination_processing.py

pagination_processing.py
  +---> config.py
  +---> utilities.py
  +---> content_fetching.py
  +---> pagination.py
  +---> chunking.py
  +---> chunk_processing.py
  +---> file_saving.py
  +---> llm_prompts.py
  +---> openrouter_client.py
  +---> url_processing.py
  +---> vector_store.py

chunk_processing.py
  +---> config.py
  +---> utilities.py
  +---> openrouter_client.py
  +---> llm_prompts.py
  +---> metadata_budget.py
  +---> chunking.py
  +---> file_saving.py
  +---> vector_store.py

openrouter_client.py
  +---> config.py
  +---> token_tracker.py
  +---> utilities.py

metadata_budget.py
  +---> config.py
  +---> openrouter_client.py
  +---> utilities.py

llm_prompts.py
  +---> config.py
  +---> openrouter_client.py

content_fetching.py
  +---> config.py
  +---> utilities.py
  +---> url_processing.py  (late import, avoids circular)

sitemap_parsing.py
  +---> config.py
  +---> utilities.py
  +---> url_processing.py  (late import, avoids circular)

rss_feeds.py
  +---> config.py
  +---> utilities.py
  +---> url_processing.py  (late import, avoids circular)
  +---> xml_sitemap.py     (late import, avoids circular)

url_processing.py
  +---> config.py
  +---> utilities.py
  +---> vector_store.py

xml_sitemap.py
  +---> config.py
  +---> utilities.py
  +---> url_processing.py  (late import, avoids circular)

file_saving.py
  +---> config.py
  +---> utilities.py
  +---> vector_store.py

pagination.py
  +---> config.py
  +---> openrouter_client.py
  +---> utilities.py

chunking.py
  +---> config.py
  +---> utilities.py

vector_store.py
  +---> config.py
  +---> utilities.py

utilities.py
  +---> config.py

logging_setup.py
  +---> config.py  (TYPE_CHECKING only)

token_tracker.py
  (no internal imports)

config.py
  (no internal imports -- leaf node)
```

### Circular Import Avoidance

Several modules use late (function-local) imports to break circular dependency chains:

- `content_fetching.py` imports `url_processing` inside `extract_links_from_jina_summary()`
- `sitemap_parsing.py` imports `url_processing` inside `extract_links_from_html_sitemap()` and `extract_links()`
- `rss_feeds.py` imports `url_processing` and `xml_sitemap` inside `process_rss_feeds()`
- `xml_sitemap.py` imports `url_processing` inside `extract_urls_from_xml_sitemap()`

---

## Configuration System

### ScraperConfig Dataclass

All configuration state for a single scraper run is held in a single `ScraperConfig` dataclass defined in `config.py`. It replaces the 40+ module-level global variables used in V2.

Key field groups:

| Group | Fields | Description |
|-------|--------|-------------|
| Script identity | `SCRIPT_NAME`, `LOG_DIR`, `LOG_FILE`, `OUTPUT_DIR` | Derived from config filename |
| API keys | `JINA_AI_API_KEY`, `FIRECRAWL_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` | From config JSON or env vars |
| OpenRouter | `OPENROUTER_MODELS`, `OPENROUTER_TEMPERATURE`, `OPENROUTER_TOP_P`, `OPENROUTER_TARGET_LANGUAGE`, `OPENROUTER_CACHE_ENABLED` | LLM behaviour |
| Vector Store | `OPENAI_VECTOR_STORE_ID`, `ENABLE_DEDUPLICATION`, `DEFAULT_CHUNKING_STRATEGY`, `DEFAULT_MAX_CHUNK_SIZE`, `DEFAULT_CHUNK_OVERLAP`, `DEFAULT_CONTENT_TOKEN_OFFSET`, `DEFAULT_CONTENT_RATIO` | Upload and chunking |
| URL config | `BASE_URL`, `SITEMAP_URL`, `XML_SITEMAP_URL`, `BLACKLISTED_URLS`, `BLACKLISTED_RELATIVE_PATHS`, `CANONICAL_BASE_URLS` | Target site |
| URL lists | `RSS_FEEDS`, `RECURSIVE_URLS`, `PAGINATED_URLS`, `TEST_URLS` | Advanced URL sources |
| HTTP | `REQUEST_TIMEOUT`, `REQUEST_RETRY_CODES`, `REQUEST_RETRY_COUNT`, `REQUEST_BACKOFF_FACTOR` | Session behaviour |
| Processing | `CHECK_LAST_MODIFIED`, `MAX_FILENAME_LENGTH`, `RSS_DATE_THRESHOLD` | Run flags |
| Mutable state | `PROCESS_BASE_URL_ONCE`, `BASE_URL_PROCESSED_IN_RUN` | Per-run tracking |
| File paths | `LAST_RUN_FILE`, `RSS_LAST_RUN_FILE`, `SITEMAP_LAST_RUN_FILE` | Timestamp persistence |

### Singleton Access Pattern

```python
from gpt_scraper_v3.config import load_configuration, get_config

# Called once at startup:
cfg = load_configuration("path/to/config.json")

# Called everywhere else (returns the same instance):
cfg = get_config()
```

`get_config()` raises `RuntimeError` if called before `load_configuration()`. This replaces the V2 pattern of importing globals from the top-level module.

### Configuration File Format

The scraper reads a JSON configuration file. Key top-level sections:

```json
{
  "script_info": { "name": "scrape_sitemap_universal" },
  "api_keys": {
    "jina_ai": "...",
    "firecrawl": "...",
    "openai": "...",
    "openrouter": "..."
  },
  "content_providers": {
    "jina": { "remove_selectors": "...", "target_selectors": "..." },
    "provider_sequence": "jina,firecrawl"
  },
  "openrouter": {
    "models": ["anthropic/claude-3.5-sonnet"],
    "temperature": 0.1,
    "target_language": "Czech",
    "enable_caching": false
  },
  "vector_store": {
    "id": "vs_...",
    "enable_deduplication": true,
    "chunking_strategy": "auto",
    "max_chunk_size": 800,
    "chunk_overlap": 400,
    "content_token_offset": 0,
    "content_ratio": 0.5
  },
  "website": {
    "base_url": "https://example.com",
    "sitemap_url": "https://example.com/sitemap",
    "xml_sitemap_url": "https://example.com/sitemap.xml",
    "blacklisted_urls": [],
    "blacklisted_relative_url_paths": [],
    "rss_feeds": [],
    "recursive_urls": [],
    "paginated_urls": [],
    "test_urls": [],
    "IncludeBaseURL": 0
  },
  "http_settings": {
    "request_timeout": 30,
    "retry_codes": [500, 502, 503, 504, 524],
    "retry_count": 3,
    "backoff_factor": 0.3
  },
  "processing": {
    "check_last_modified": true,
    "max_filename_length": 200,
    "RSSDateThreshold": ""
  }
}
```

### Path Generation

Output directories and timestamp files are derived from the config filename:

```
scrape_sitemap_GPT_config_Stredocesky.json
  -> Stredocesky_files/          (output directory)
  -> Stredocesky_logs/           (log directory)
  -> Stredocesky_last_run_time.txt
  -> Stredocesky_rss_last_run_time.txt
  -> Stredocesky_sitemap_last_run_time.txt
```

---

## Processing Pipeline

The `main()` function in `cli.py` orchestrates the pipeline in numbered steps:

```
1. Parse CLI arguments  (build_argument_parser)
2. Load configuration   (load_configuration -> ScraperConfig singleton)
3. Setup logging        (setup_logging -> RotatingFileHandler + console)
4. Apply CLI overrides  (apply_cli_overrides -> env vars, flags, URL overrides)
5. Build caches         (build_caches -> vector store cache + local file cache)
6. Resolve timestamps   (get_last_run_timestamp -> combined/rss/sitemap)
7. Resolve URLs         (resolve_urls -> test URLs, HTML sitemap, XML sitemap, RSS)
8. Process URLs         (process_urls -> pagination detection + single URL processing)
9. Save timestamps      (save_last_run_timestamp)
10. Report summary      (report_summary -> stats + token usage)
```

### URL Resolution Flow

```
resolve_urls()
    |
    +-- TEST MODE?  --> process_test_urls() + fetch_xml_sitemap()
    |
    +-- XML-only?   --> _extract_xml_urls()
    |
    +-- HTML sitemap configured?
    |       |
    |       +-- Fetch HTML via Jina
    |       +-- Legacy parsing? --> parse_menu() + extract_links()
    |       +-- Default:        --> extract_links_from_html_sitemap()
    |       +-- Fallback:       --> _extract_xml_urls()
    |
    +-- RSS feeds configured? --> process_rss_feeds()
    |
    +-- _deduplicate_urls() --> final URL list
```

### Single URL Processing Flow

```
process_paginated_url()
    |
    +-- Check recursive/explicit pagination config
    +-- Fetch HTML for pagination detection
    +-- detect_pagination_in_html() (AI or legacy)
    |
    +-- No pagination/suburls? --> process_single_url_normally()
    |
    +-- Has pagination? --> extract_pagination_urls() + process subpages
    +-- Has suburls?    --> extract_suburls_from_ai_data() + recursive processing
    |
    process_single_url_normally()
        |
        +-- get_markdown_content() (Jina 3-strategy / Firecrawl)
        +-- generate_question_section() via OpenRouter
        +-- generate_page_summary_via_openrouter()
        +-- Content > budget?
        |       YES --> chunk_content_with_metadata_budget()
        |            --> process_and_save_chunks_with_metadata_budget()
        |       NO  --> save_markdown_to_file()
        +-- Optional: upload_and_add_to_vector_store()
```

---

## Key Subsystems

### Content Fetching (Jina AI 3-Strategy Fallback)

The `_fetch_jina_markdown()` function in `content_fetching.py` tries three strategies in order:

1. **Strategy 1**: Markdown with CSS selectors (remove/target selectors from config)
2. **Strategy 2**: Markdown without selectors (handles 422 errors from Strategy 1)
3. **Strategy 3**: HTML fallback with BeautifulSoup text extraction

If all Jina strategies fail, the system falls through to Firecrawl as the next provider in the configured sequence.

### Chunking System

Two primary chunking strategies:

**Metadata-Budget Chunking** (`chunk_content_with_metadata_budget`):
- Calculates content and metadata budgets based on vector store limits
- `content_budget = (max_chunk_size - offset) * content_ratio`
- `metadata_budget = (max_chunk_size - offset) * (1 - content_ratio)`
- Splits content line-by-line, respecting budget per chunk
- Each chunk gets an A-Z postfix (A, B, ..., Z, AA, AB, ...)

**Adaptive Section-Based Chunking** (`chunk_massive_content_by_sections`):
- Discovers structural elements (headers, tables, separators, department codes)
- Selects optimal header level for breaking
- Falls back to natural paragraph breaks
- No arbitrary size limits -- preserves complete logical sections

### Pagination Detection

AI-powered detection via OpenRouter Structured Outputs:

- Uses a JSON schema (`_PAGINATION_SCHEMA`) to validate the response
- Detects content type, confidence score, pagination URLs, and sub-URLs
- Extrapolates full page sequences (e.g., pages 1-9 from partial links)
- Falls back to legacy BeautifulSoup pattern matching when no API key

### Token Budget Management

The metadata budget system ensures files fit within vector store limits:

```
Total budget = max_chunk_size (e.g., 4096 tokens)
  - content_token_offset (e.g., 0)
  = working space

working space * content_ratio (e.g., 0.5)  = content_budget
working space * (1 - content_ratio)         = metadata_budget

metadata_budget is distributed:
  - source_page_summary:    min(available/4, 300)
  - current_file_summary:   min(remaining/3, 400)
  - overlap_summary:        min(remaining/2, 250)
  - question_section:       remaining
```

If the combined file exceeds the limit, `_truncate_metadata_by_priority()` removes sections in order: overlap summary, current file summary, page summary, question section, and finally truncates at a line boundary. Content is never truncated.

### Vector Store Deduplication

The system supports two deduplication strategies:

1. **Cache-based** (fast): `build_vector_store_cache()` pre-fetches all file metadata into a dict keyed by normalised `lookup_source_url`. Lookups are O(1).
2. **API-based** (slow, legacy): `find_existing_file_by_url()` iterates all files and fetches attributes individually. Used only when `--skip-vector-cache` is set.

Deduplication uses EXACT `lookup_source_url` matching, including `#chunk` fragments, to prevent different chunks of the same URL from being treated as duplicates.

### Session Management

`get_session()` in `utilities.py` creates a single `requests.Session` with:
- Retry configuration from `ScraperConfig` (default: 3 retries, 0.3 backoff, status codes 500/502/503/504/524)
- `HTTPAdapter` mounted for both `http://` and `https://`
- `atexit.register()` for cleanup

This replaces V2's `requests_retry_session()` which created a new session on every call.

### Token Counting

`count_tokens_approximate()` in `utilities.py`:
- Uses `tiktoken` (GPT-4 encoding) if available
- Falls back to character-based estimation with a Czech-calibrated ratio of 3.2 chars/token (V2 used 4.0, which undercounted Czech text with diacritics)

---

## CLI Arguments Reference

### Configuration

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--config` | string | `config.json` | Path to configuration file |

### URL Overrides

| Argument | Type | Description |
|----------|------|-------------|
| `--base-url` | string | Override base URL from config |
| `--sitemap-url` | string | Override sitemap URL from config |
| `--xml-sitemap-url` | string | Override XML sitemap URL from config |

### API Key Overrides

Priority: CLI argument > environment variable > config file.

| Argument | Env Var | Description |
|----------|---------|-------------|
| `--jina-api-key` | `JINA_API_KEY` | Override Jina AI API key |
| `--firecrawl-api-key` | `FIRECRAWL_API_KEY` | Override Firecrawl API key |
| `--openai-api-key` | `OPENAI_API_KEY` | Override OpenAI API key |
| (none) | `OPENROUTER_API_KEY` | Fallback for OpenRouter API key |

### Processing Modes

These three flags are mutually exclusive:

| Argument | Description |
|----------|-------------|
| `--rss-only` | Process only RSS feeds, skip sitemap |
| `--sitemap-only` | Process only sitemap, skip RSS feeds |
| `--xml-only` | Process only XML sitemap URLs, skip HTML sitemap and RSS |

### Processing Options

| Argument | Description |
|----------|-------------|
| `--debug` | Enable debug mode with verbose logging |
| `--no-check-modified` | Disable last-modified checking; process all URLs |
| `--legacy-html-parsing` | Use legacy `parse_menu()` instead of `<a>` tag extraction |
| `--verbose-url-matching` | Show detailed URL matching for all URLs |
| `--resume` | Skip URLs that already have local files |
| `--test-resume` | Test resume cache building and show stats, then exit |
| `--test-events-xml` | Test Events XML parsing with sample data |

### Vector Store Options

| Argument | Type | Description |
|----------|------|-------------|
| `--vector-store-id` | string | OpenAI Vector Store ID for uploads |
| `--disable-deduplication` | flag | Allow duplicate files for same URL |
| `--skip-vector-cache` | flag | Skip cache building (faster start, slower dedup) |
| `--chunking-strategy` | `auto`\|`static` | Chunking strategy for Vector Store |
| `--max-chunk-size` | int | Max tokens per chunk (static only, 100-4096) |
| `--chunk-overlap` | int | Token overlap between chunks (static only) |

### Content Options

| Argument | Type | Description |
|----------|------|-------------|
| `--jina-remove-selectors` | string | CSS selectors to remove (comma-separated) |
| `--output-dir` | string | Override output directory |
| `--test-urls` | string[] | Test URLs (overrides HTML sitemap processing) |

---

## How to Run

### Prerequisites

1. Python 3.10 or later
2. Install dependencies:
   ```
   pip install -r gpt_scraper_v3/requirements.txt
   ```
   Optional but recommended:
   ```
   pip install tiktoken
   ```

### Method 1: Package Entry Point (recommended)

```
python -m gpt_scraper_v3 --config path/to/config.json
```

### Method 2: Drop-in Replacement Script

```
python run_gpt_scraper_v3.py --config path/to/config.json
```

### Method 3: Programmatic Use

```python
from gpt_scraper_v3 import load_configuration, setup_logging, main
from gpt_scraper_v3.cli import build_argument_parser

parser = build_argument_parser()
args = parser.parse_args(["--config", "my_config.json", "--resume"])
main(args)
```

### Common Invocations

Process all sources with resume:
```
python -m gpt_scraper_v3 --config config_Praha.json --resume
```

RSS feeds only, no last-modified checking:
```
python -m gpt_scraper_v3 --config config_Praha.json --rss-only --no-check-modified
```

Test specific URLs with vector store upload:
```
python -m gpt_scraper_v3 --config config_Praha.json --test-urls https://example.com/page1 https://example.com/page2 --vector-store-id vs_abc123
```

Debug mode with XML sitemap only:
```
python -m gpt_scraper_v3 --config config_Praha.json --xml-only --debug
```

### Batch File Usage

If you have existing `.bat` files that invoke V2, update them as follows:

**V2 (old):**
```batch
python scrape_sitemap_GPT_v2.py --config config_Praha.json --resume
```

**V3 (new) -- either of:**
```batch
python run_gpt_scraper_v3.py --config config_Praha.json --resume
python -m gpt_scraper_v3 --config config_Praha.json --resume
```

All CLI arguments are preserved. No changes to argument names or values are needed.

---

## Migration Guide: V2 to V3

### What Changed

| Aspect | V2 | V3 |
|--------|----|----|
| Entry point | `scrape_sitemap_GPT_v2.py` | `python -m gpt_scraper_v3` or `run_gpt_scraper_v3.py` |
| Configuration state | 40+ module-level globals | `ScraperConfig` dataclass via `get_config()` |
| OpenRouter calls | ~600 lines duplicated per caller | Single `_call_openrouter()` helper |
| HTTP sessions | New session per call | Shared singleton with `atexit` cleanup |
| `main()` function | 660 lines | 7 focused functions in `cli.py` |
| Chunk context passing | `**globals()` | `ChunkContext` TypedDict |
| Token counting | 4.0 chars/token fallback | 3.2 chars/token (Czech-calibrated) |
| Filename safety | No path traversal check | `..` stripped, `realpath` validation |
| Log handling | Truncated on startup | `RotatingFileHandler` (append mode) |
| API key sources | Config file only | CLI arg > env var > config file |
| `.aspx`/`.jsp`/`.php` URLs | Skipped (treated as files) | Scraped unless paired with download query params |
| Response logging | Full response at INFO level | Debug level, truncated to 500 chars |
| Metadata truncation | Character slicing | Priority-based section removal |

### What Did Not Change

- All CLI arguments remain identical in name and behaviour
- Configuration JSON format is fully backward-compatible
- Output file format (metadata headers, QUESTION/ANSWER sections) is unchanged
- Vector Store attribute schema (`source_url`, `lookup_source_url`, `title`, `upload_timestamp`, `script_name`) is unchanged
- Timestamp file format and logic are preserved

### How to Update .bat Files

Replace the script name. Everything else stays the same:

```diff
- python scrape_sitemap_GPT_v2.py --config %CONFIG% --resume
+ python run_gpt_scraper_v3.py --config %CONFIG% --resume
```

Or use the package form:

```diff
- python scrape_sitemap_GPT_v2.py --config %CONFIG% --resume
+ python -m gpt_scraper_v3 --config %CONFIG% --resume
```

### New Environment Variables

V3 adds fallback support for API keys from environment variables. These are optional; if the config file already has the keys, no changes are needed.

| Variable | Falls back to |
|----------|--------------|
| `JINA_API_KEY` | `config.api_keys.jina_ai` |
| `FIRECRAWL_API_KEY` | `config.api_keys.firecrawl` |
| `OPENAI_API_KEY` | `config.api_keys.openai` |
| `OPENROUTER_API_KEY` | `config.api_keys.openrouter` |

---

## Review Findings Applied

### CRITICAL (3)

| ID | Issue | Fix |
|----|-------|-----|
| C1 | Mutable default arguments (`def f(x=[])`) | Replaced with `None` sentinel + `if x is None: x = []` pattern across `sitemap_parsing.py`, `content_fetching.py` |
| C2 | (Applied during migration) | -- |
| C3 | Full API response bodies logged at INFO level | Moved to DEBUG level, truncated to 500 characters |

### HIGH (7)

| ID | Issue | Fix |
|----|-------|-----|
| H1 | `**globals()` used to pass chunk context | Replaced with explicit `ChunkContext` TypedDict in `chunk_processing.py` |
| H2 | Duplicate event metadata block (copy-paste bug) | Removed duplicate in `file_saving.py` |
| H3 | ~600 lines of duplicated OpenRouter boilerplate | Unified into `_call_openrouter()` in `openrouter_client.py` |
| H4 | 660-line `main()` function | Decomposed into 7 functions in `cli.py` |
| H5 | New `requests.Session` on every call | Singleton `get_session()` with `atexit` cleanup in `utilities.py` |
| H6 | No path traversal protection in filenames | `..` stripped, leading/trailing dots stripped, `os.path.realpath` validation |
| H7 | Token counting used English ratio (4.0) for Czech text | Czech-calibrated ratio (3.2) with optional `tiktoken` |

### MEDIUM (8)

| ID | Issue | Fix |
|----|-------|-----|
| M1 | Bare `except:` clauses | Replaced with specific exception types |
| M2 | Log file truncated on every startup | `RotatingFileHandler` in append mode |
| M3 | Redundant function-local re-imports | Removed; imports at module level where possible |
| M4 | Prompt templates duplicated across modules | Single source of truth in `llm_prompts.py` |
| M5 | Missing type hints on public functions | Type hints added to all public function signatures |
| M6 | `getattr(globals(), ...)` pattern | Replaced with explicit attribute access |
| M7 | Metadata truncation by character slicing | Priority-based section removal in `chunk_processing.py` |
| M8 | Unguarded content preview in logging | `if logger.isEnabledFor(logging.DEBUG)` guards |

### LOW (4)

| ID | Issue | Fix |
|----|-------|-----|
| L1 | `.aspx`/`.jsp`/`.php` in FILE_EXTENSIONS_TO_SKIP | Removed from primary skip list; still caught by file-handler heuristic |
| L2 | (Applied during migration) | -- |
| L3 | No env-var fallback for API keys | CLI arg > env var > config file priority in `apply_cli_overrides()` |
| L4 | `print()` used where `logger` should be | Replaced with `logger` calls (except pre-logger-setup output) |

---

## Dependencies

From `gpt_scraper_v3/requirements.txt`:

```
requests>=2.28,<3.0
beautifulsoup4>=4.12
python-dateutil>=2.8
lxml>=4.9
```

Optional (recommended):
- `tiktoken` -- accurate token counting for GPT-4 encoding
