# Changelog

All notable changes to the GPT Scraper project are documented in this file.

---

## [3.0.2] - 2026-02-15

### Summary

7 logging system fixes across 4 modules, restoring V2 log-truncation behavior and fixing duplicate console output, debug-mode PII exposure, and pre-logging logger calls introduced during the V3 rewrite. Full details in [v3_logging_fixes_2026-02-15.md](v3_logging_fixes_2026-02-15.md).

### Fixed

- **Log file not truncated between runs** (logging_setup.py): Restored V2's per-run log truncation via `open("w")`. `RotatingFileHandler` appends by default and only rotates at `maxBytes`, it never truncates.
- **Duplicate console output** (logging_setup.py): Removed `logging.basicConfig()` and set `propagate=False` on the package logger to prevent messages from reaching the root logger's handler.
- **Pre-logging logger calls** (config.py): Converted `logger.info()`/`logger.warning()` in `load_configuration()` to `print()`, since these execute before `setup_logging()` is called.
- **Debug mode PII exposure** (cli.py): `--debug` now only sets the console `StreamHandler` to DEBUG level. The file handler stays at INFO, preventing scraped content previews from being persisted to disk.
- **Inconsistent logger naming** (token_tracker.py): Changed `getLogger("gpt_scraper_v3")` to `getLogger(__name__)` for correct source identification in log messages.
- **TOCTOU race condition** (logging_setup.py): Removed unnecessary `os.path.exists()` guard before `open("w")`, which creates-or-truncates atomically.
- **Path normalization inconsistency** (config.py): `LOG_DIR` and `OUTPUT_DIR` both use `os.path.realpath()` instead of mixed `os.path.abspath()`/raw paths. `os.makedirs(OUTPUT_DIR)` moved from `logging_setup.py` to `cli.py`.

---

## [3.0.1] - 2026-02-15

### Summary

10 bug fixes across 6 modules addressing critical URL blacklisting bypasses, CLI override inconsistencies, XML sitemap parsing gaps, and a configuration cleanup. Full details in [v3_bugfixes_2026-02-15.md](v3_bugfixes_2026-02-15.md).

### Fixed

- **Blacklist percent-encoding bypass** (utilities.py): `is_url_blacklisted_by_path()` now decodes both the URL path and blacklist entries with `unquote()` before comparison.
- **Blacklist trailing-slash mismatch** (utilities.py): Both sides are normalized with `rstrip("/")` so `/path/` matches `/path` and vice versa.
- **Blacklist false-positive prefix match** (utilities.py): `/aktuality` no longer matches `/aktualityXYZ`. A slash-boundary check (`startswith(prefix + "/")`) is now required.
- **Blacklist empty-entry guard** (utilities.py): Empty strings in the blacklist array are skipped instead of matching every URL.
- **Missing path blacklist in `extract_links()`** (sitemap_parsing.py): The deprecated legacy HTML parser now calls `is_url_blacklisted_by_path()` in addition to the exact-match check.
- **Missing path blacklist in `parse_generic_feed()`** (rss_feeds.py): The generic RSS parser now calls `is_url_blacklisted_by_path()` in addition to the exact-match check.
- **Missing path blacklist in `extract_links_from_jina_summary()`** (content_fetching.py): The primary HTML sitemap parser now calls `is_url_blacklisted_by_path()` in addition to the exact-match check.
- **`--base-url` derived fields not recomputed** (cli.py): When `--base-url` is overridden, `PARSED_BASE_URL`, `BASE_NETLOC`, `NON_WWW_BASE_NETLOC`, `BASE_SCHEME`, and `CANONICAL_BASE_URLS` are now recomputed to match.
- **Paged sitemap drops URLs without `<lastmod>`** (xml_sitemap.py): A second regex pass now captures `<url>` entries that lack a `<lastmod>` element, storing them with `None`. Regex changed from `\s*` to `.*?` with `re.DOTALL` for broader matching.
- **Duplicate `BLACKLISTED_RELATIVE_PATHS` assignment** (config.py): Removed the redundant second assignment in `load_configuration()`.

---

## [3.0.0] - 2026-02-13

### Summary

Complete modular rewrite of the monolithic `scrape_sitemap_GPT_v2.py` (9,304 lines) into the `gpt_scraper_v3/` package (21 modules, ~8,128 lines). Applied 22 code review findings (3 CRITICAL, 7 HIGH, 8 MEDIUM, 4 LOW).

### Added

- **Modular package**: `gpt_scraper_v3/` with 21 focused modules, each owning a single responsibility.
- **Drop-in replacement**: `run_gpt_scraper_v3.py` preserves all V2 CLI arguments for zero-friction migration.
- **`python -m gpt_scraper_v3`**: Package entry point via `__main__.py`.
- **`ScraperConfig` dataclass**: Single typed configuration object replacing 40+ module-level globals, accessed via `get_config()` singleton.
- **Unified OpenRouter client**: `_call_openrouter()` helper in `openrouter_client.py` consolidates ~600 lines of duplicated API boilerplate.
- **Session singleton**: `get_session()` in `utilities.py` shares one `requests.Session` across the entire run with `atexit` cleanup.
- **`ChunkContext` TypedDict**: Explicit context object for chunk processing, replacing the V2 `**globals()` pattern.
- **Path traversal protection**: `sanitize_filename()` strips `..` sequences; `save_markdown_to_file()` and `chunk_processing.py` validate resolved paths with `os.path.realpath`.
- **Czech-calibrated token counting**: Fallback ratio changed from 4.0 to 3.2 chars/token to better estimate Czech text with diacritics. Uses `tiktoken` when available.
- **Environment variable API key fallback**: Priority chain is CLI argument > environment variable > config file for `JINA_API_KEY`, `FIRECRAWL_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`.
- **Priority-based metadata truncation**: When a file exceeds vector store limits, metadata sections are removed in order of priority (overlap summary first, content never truncated).
- **Type hints**: All public function signatures across all 21 modules have type annotations.
- **Budget-constrained metadata generators**: `metadata_budget.py` provides four generators that use `system` messages with `temperature=0.0` and enforce strict token limits.
- **Reasoning-mode fallback**: `_call_openrouter()` checks `message.reasoning` when `message.content` is empty (supports models like `openai/gpt-5`).

### Changed

- **Main function decomposition**: V2's 660-line `main()` split into 7 focused functions in `cli.py`: `build_argument_parser()`, `apply_cli_overrides()`, `build_caches()`, `resolve_urls()`, `process_urls()`, `report_summary()`, `main()`.
- **Log handling**: Replaced startup log truncation with `RotatingFileHandler` (10 MB max, 5 backups, append mode). **Note:** This change was reverted in 3.0.2 -- per-run truncation was restored because `RotatingFileHandler` only rotates at `maxBytes`, it does not truncate between runs.
- **Response logging**: Full API response bodies moved from INFO to DEBUG level, truncated to 500 characters.
- **URL file detection**: `.aspx`, `.jsp`, `.php` removed from primary `FILE_EXTENSIONS_TO_SKIP` -- these are valid web application URLs. They are still caught by the file-handler heuristic when paired with download-style query parameters (`id_dokumenty`, `file`, `download`).
- **Print vs logger**: Console output before logger setup uses `print()`; all post-setup output uses the package logger.
- **Bare except clauses**: All bare `except:` replaced with specific exception types (`ValueError`, `OSError`, `TypeError`, `RequestException`, etc.).
- **Redundant imports**: Function-local re-imports removed. Module-level imports used where possible; late imports retained only to break circular dependencies.
- **Prompt template source of truth**: `llm_prompts.py` is the canonical location for all prompt templates (M4 fix). `openrouter_client.py` contains streamlined inline versions for its own summary generators.
- **`getattr(globals(), ...)` pattern**: Eliminated in favour of explicit attribute access on `ScraperConfig`.
- **Metadata truncation**: Changed from character-position slicing to structured section-priority removal.

### Fixed

- **Mutable default arguments** (C1): Functions like `extract_links_from_html_sitemap()`, `extract_links()`, and `extract_links_from_jina_summary()` no longer use mutable default arguments. All use the `None` sentinel pattern.
- **Duplicate event metadata block** (H2): Copy-paste duplication of the Events XML metadata block in file saving has been removed. The block now appears exactly once.
- **Content preview logging guard** (M8): `content[:500]` previews are now wrapped in `if logger.isEnabledFor(logging.DEBUG)` to avoid unnecessary string operations.

### Deprecated

- **`parse_menu()`**: Legacy HTML sitemap menu parser. Use `extract_links_from_html_sitemap()` instead. Emits `DeprecationWarning`.
- **`extract_links()`**: Legacy recursive link extractor. Use `extract_links_from_html_sitemap()` instead. Emits `DeprecationWarning`.
- **`chunk_large_content_simple()`**: Legacy simple chunker. Use `chunk_content_with_metadata_budget()` instead. Emits `DeprecationWarning`.

### Migration Notes

- **CLI arguments**: All V2 arguments are preserved. No changes needed.
- **Config JSON format**: Fully backward-compatible. V3 also supports a legacy `llm_config.openrouter` key path as a fallback for the top-level `openrouter` section.
- **Output format**: File metadata headers, QUESTION/ANSWER sections, and Vector Store attributes are unchanged.
- **Timestamp files**: Format and logic are preserved. Existing timestamp files work without modification.
- **Batch files**: Replace `python scrape_sitemap_GPT_v2.py` with `python run_gpt_scraper_v3.py` or `python -m gpt_scraper_v3`. All other arguments remain the same.

### New Files

```
gpt_scraper_v3/__init__.py
gpt_scraper_v3/__main__.py
gpt_scraper_v3/config.py
gpt_scraper_v3/logging_setup.py
gpt_scraper_v3/token_tracker.py
gpt_scraper_v3/utilities.py
gpt_scraper_v3/openrouter_client.py
gpt_scraper_v3/llm_prompts.py
gpt_scraper_v3/chunking.py
gpt_scraper_v3/metadata_budget.py
gpt_scraper_v3/chunk_processing.py
gpt_scraper_v3/content_fetching.py
gpt_scraper_v3/pagination.py
gpt_scraper_v3/pagination_processing.py
gpt_scraper_v3/sitemap_parsing.py
gpt_scraper_v3/vector_store.py
gpt_scraper_v3/rss_feeds.py
gpt_scraper_v3/xml_sitemap.py
gpt_scraper_v3/url_processing.py
gpt_scraper_v3/file_saving.py
gpt_scraper_v3/cli.py
gpt_scraper_v3/requirements.txt
run_gpt_scraper_v3.py
```
