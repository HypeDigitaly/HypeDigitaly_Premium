# V3 Config Migration

Documentation for the migration of all 15 existing v2 client configurations to v3-ready JSON config files and `.bat` launchers.

---

## Table of Contents

1. [Overview](#overview)
2. [V2 to V3 Transformation Rules](#v2-to-v3-transformation-rules)
3. [New V3 Config Fields](#new-v3-config-fields)
4. [Naming Conventions](#naming-conventions)
5. [How to Run](#how-to-run)
6. [How to Create a New V3 Config](#how-to-create-a-new-v3-config)
7. [Differences from V2](#differences-from-v2)
8. [Bug Fixes](#bug-fixes)
9. [Dead Fields Note](#dead-fields-note)

---

## Overview

V3 of the GPT Scraper introduced a modular Python package (`gpt_scraper_v3/`) to replace the monolithic `scrape_sitemap_GPT_v2.py`. This migration created **v3-ready configuration files and `.bat` launchers** for all 15 existing v2 client configurations, ensuring every client can run on the new v3 engine without manual setup.

### What was done

- Created 15 new JSON config files (one per client), each named `scrape_sitemap_GPT_config_{Name}_v3.json`.
- Created 15 new `.bat` launcher files, each named `exec_scrape_sitemap_GPT_v3_{Name}.bat`.
- Applied consistent naming conventions across all `.bat` files, fixing inconsistencies present in the v2 set.
- Added 5 new optional fields required by the v3 config loader while preserving all existing v2 values verbatim.
- Updated `script_info` to reflect v3 identity.

### Clients migrated

| # | Client Name |
|---|-------------|
| 1 | HealthyTwenty |
| 2 | ICUK |
| 3 | Karlovarsky |
| 4 | KHK |
| 5 | KrajVysocina |
| 6 | Litomerice |
| 7 | StredoceskyKraj |
| 8 | Teplice |
| 9 | UsteckyKraj |
| 10 | Usti_Magistrat |
| 11 | Usti_Obvod_Nestemice |
| 12 | Usti_Obvod_SeverniTerasa |
| 13 | Usti_Obvod_Strekov |
| 14 | Usti_Obvod_UstinadLabem |
| 15 | VysocinaPecuje |

---

## V2 to V3 Transformation Rules

Each v3 config was produced by applying a deterministic set of changes to its corresponding v2 config. No client-specific values (URLs, API keys, vector store IDs, selectors, etc.) were altered.

### Fields added (5 new fields)

These fields were inserted into their respective sections with the default values shown:

| Section | Field | Default Value |
|---------|-------|---------------|
| `website` | `blacklisted_relative_url_paths` | `[]` |
| `website` | `IncludeBaseURL` | `0` |
| `openrouter` | `enable_caching` | `false` |
| `openrouter` | `cache_type` | `"ephemeral"` |
| `openrouter` | `provider` | `{}` |

### Fields updated (2 fields in `script_info`)

| Field | V2 Value | V3 Value |
|-------|----------|----------|
| `script_info.name` | `"scrape_sitemap_GPT_v2"` | `"scrape_sitemap_GPT_v3"` |
| `script_info.version` | `"1.0.0"` | `"3.0.0"` |

### Fields preserved verbatim

All other fields were copied from the v2 config without modification. This includes:

- `website.base_url`, `website.sitemap_url`, `website.xml_sitemap_url`
- `website.blacklisted_urls`, `website.rss_feeds`, `website.test_urls`, `website.recursive_urls`, `website.paginated_urls`
- `api_keys` (all four keys: `jina_ai`, `firecrawl`, `openai`, `openrouter`)
- `openrouter.max_tokens`, `openrouter.target_language`, `openrouter.models`, `openrouter.temperature`, `openrouter.top_p`
- `openrouter.fallback_enabled`, `openrouter.timeout_seconds` (preserved but see [Dead Fields Note](#dead-fields-note))
- `content_providers` (full section including `jina.remove_selectors`, `jina.target_selectors`, `firecrawl`, `provider_sequence`)
- `vector_store` (full section including `id`, `enable_deduplication`, `chunking_strategy`, `max_chunk_size`, `chunk_overlap`, `content_token_offset`, `content_ratio`)
- `http_settings` (full section)
- `processing` (full section including `check_last_modified`, `max_filename_length`, `RSSDateThreshold`)

### Formatting normalization

V2 configs contained some whitespace-only array entries (e.g., `"rss_feeds": [\n     \n    ]`) and mixed tab/space indentation. The v3 configs normalize these to clean empty arrays (`[]`) and consistent indentation.

---

## New V3 Config Fields

The following table details each new field, its purpose, its default value, and where it is consumed in the v3 codebase.

| Field | Type | Default | Purpose | Consumed by |
|-------|------|---------|---------|-------------|
| `website.blacklisted_relative_url_paths` | `string[]` | `[]` | A list of relative URL path prefixes to exclude from processing. Any URL whose path starts with one of these strings is skipped. | `config.py` -> `ScraperConfig.BLACKLISTED_RELATIVE_PATHS`; checked by `utilities.is_url_blacklisted_by_path()` |
| `website.IncludeBaseURL` | `int` (0 or 1) | `0` | When set to `1`, the root base URL itself is processed as a page (normally it is skipped as a homepage). | `config.py` -> `ScraperConfig.PROCESS_BASE_URL_ONCE`; checked during URL eligibility filtering |
| `openrouter.enable_caching` | `bool` | `false` | Enables OpenRouter prompt caching on requests. When `true`, prompt caching headers are sent with API calls. | `config.py` -> `ScraperConfig.OPENROUTER_CACHE_ENABLED`; used in `openrouter_client._call_openrouter()` |
| `openrouter.cache_type` | `string` | `"ephemeral"` | The caching strategy to use when `enable_caching` is `true`. Currently only `"ephemeral"` is supported. | `config.py` -> `ScraperConfig.OPENROUTER_CACHE_TYPE`; used in `openrouter_client._call_openrouter()` |
| `openrouter.provider` | `object` | `{}` | Provider routing configuration passed directly to the OpenRouter API. Allows specifying provider preferences (e.g., `{"order": ["Anthropic"]}`) or constraints. | `config.py` -> `ScraperConfig.OPENROUTER_PROVIDER_CONFIG`; included in the `provider` field of the OpenRouter API request payload |

---

## Naming Conventions

### Config files

Pattern: `scrape_sitemap_GPT_config_{Name}_v3.json`

Examples:
```
scrape_sitemap_GPT_config_HealthyTwenty_v3.json
scrape_sitemap_GPT_config_Usti_Obvod_SeverniTerasa_v3.json
scrape_sitemap_GPT_config_StredoceskyKraj_v3.json
```

The `{Name}` identifier matches the v2 config exactly. The version suffix changes from `_v2` to `_v3`.

### Batch launcher files

Pattern: `exec_scrape_sitemap_GPT_v3_{Name}.bat`

Examples:
```
exec_scrape_sitemap_GPT_v3_HealthyTwenty.bat
exec_scrape_sitemap_GPT_v3_Usti_Obvod_SeverniTerasa.bat
exec_scrape_sitemap_GPT_v3_StredoceskyKraj.bat
```

All v3 `.bat` files follow a **consistent** pattern: `exec_scrape_sitemap_GPT_v3_{Name}.bat`. The version marker `v3` always appears immediately after `GPT_`, before the client name. This corrects the inconsistent naming in v2 (see [Bug Fixes](#bug-fixes)).

### Output directories

The v3 config loader in `gpt_scraper_v3/config.py` derives output paths from the config filename using `extract_config_identifier()`. For a v3 config file, this produces:

```
scrape_sitemap_GPT_config_HealthyTwenty_v3.json
  -> HealthyTwenty_v3_files/       (output directory)
  -> HealthyTwenty_v3_logs/        (log directory)
  -> HealthyTwenty_v3_last_run_time.txt
  -> HealthyTwenty_v3_rss_last_run_time.txt
  -> HealthyTwenty_v3_sitemap_last_run_time.txt
```

This means v3 output is stored in a **separate directory** from v2 output (`HealthyTwenty_v3_files/` vs. `HealthyTwenty_v2_files/`). The two versions do not interfere with each other.

---

## How to Run

### Prerequisites

1. Python 3.10 or later.
2. Install dependencies:
   ```
   pip install -r gpt_scraper_v3/requirements.txt
   ```

### Using a .bat launcher (simplest)

Double-click any v3 `.bat` file or run it from the command line:

```
exec_scrape_sitemap_GPT_v3_HealthyTwenty.bat
```

Each `.bat` file contains:

```batch
@echo off
echo Spoustim scraping pro {description}...
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_{Name}_v3.json
pause
```

The `pause` at the end keeps the console window open so you can review the output.

### Running directly

From the `SCRAPE SITEMAP GPT` directory:

```
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_HealthyTwenty_v3.json
```

Or using the package entry point:

```
python -m gpt_scraper_v3 --config scrape_sitemap_GPT_config_HealthyTwenty_v3.json
```

### Adding CLI flags

All v2 CLI arguments are supported. Append them after the config path:

```
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_HealthyTwenty_v3.json --resume
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_HealthyTwenty_v3.json --rss-only --debug
```

See `docs/V3_ARCHITECTURE.md` for the full CLI arguments reference.

---

## How to Create a New V3 Config

To add a new client to the v3 system, follow these steps:

### Step 1: Create the JSON config file

Copy an existing v3 config as a template:

```
copy scrape_sitemap_GPT_config_HealthyTwenty_v3.json scrape_sitemap_GPT_config_NewClient_v3.json
```

### Step 2: Update client-specific values

Edit the new config file and update these fields:

| Field | What to set |
|-------|-------------|
| `website.base_url` | The root URL of the target website |
| `website.sitemap_url` | The HTML sitemap URL (leave empty string if none) |
| `website.xml_sitemap_url` | The XML sitemap URL |
| `website.blacklisted_urls` | Any URLs to exclude |
| `website.blacklisted_relative_url_paths` | Any relative path prefixes to exclude |
| `website.rss_feeds` | RSS feed URLs (if any) |
| `website.IncludeBaseURL` | Set to `1` if the homepage should be scraped |
| `api_keys.openai` | The OpenAI API key for this client's vector store |
| `api_keys.openrouter` | The OpenRouter API key for this client |
| `vector_store.id` | The OpenAI Vector Store ID (create one first via the OpenAI API) |
| `content_providers.jina.remove_selectors` | CSS selectors for elements to strip (e.g., `"header, footer, aside"`) |
| `content_providers.jina.target_selectors` | CSS selectors for content area (leave empty for auto-detection) |

Leave `script_info`, `http_settings`, `processing`, and `openrouter` model/temperature settings at their defaults unless you have a specific reason to change them.

### Step 3: Create the .bat launcher

Create a file named `exec_scrape_sitemap_GPT_v3_NewClient.bat` with this content:

```batch
@echo off
echo Spoustim scraping pro NewClient...
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_NewClient_v3.json
pause
```

### Step 4: Test

Run the `.bat` file or execute the command directly. Check that:

- The config loads without validation errors.
- The output directory `NewClient_v3_files/` is created.
- The log directory `NewClient_v3_logs/` is created.
- URLs are discovered from the sitemap.

---

## Differences from V2

### Output directory naming

| Version | Output Directory Pattern | Example |
|---------|--------------------------|---------|
| V2 | `{Name}_v2_files/` | `HealthyTwenty_v2_files/` |
| V3 | `{Name}_v3_files/` | `HealthyTwenty_v3_files/` |

Because the version suffix is part of the config identifier extracted from the filename, v2 and v3 produce completely separate output directories. This means you can run both versions side-by-side without overwriting data.

### Entry point script

| Version | Script |
|---------|--------|
| V2 | `scrape_sitemap_GPT_v2.py` (monolithic, 9,304 lines) |
| V3 | `run_gpt_scraper_v3.py` (10-line wrapper that calls the `gpt_scraper_v3` package) |

### Timestamp files

Timestamp files follow the same pattern change:

| Version | Example |
|---------|---------|
| V2 | `HealthyTwenty_v2_last_run_time.txt` |
| V3 | `HealthyTwenty_v3_last_run_time.txt` |

This means the v3 scraper starts fresh with no prior run history. It does not inherit v2 timestamps.

### Config file format

The v3 config is a **strict superset** of v2. All v2 fields are present and read by the v3 loader. The 5 new fields are optional in the v3 loader -- if missing, defaults are used. However, they are explicitly included in the migrated configs for clarity.

### Behavioral improvements in v3

The v3 engine includes numerous improvements over v2 that apply regardless of config changes. These are documented in `docs/CHANGELOG.md` and `docs/V3_ARCHITECTURE.md`. Key highlights:

- Session reuse (single `requests.Session` instead of a new one per call)
- Path traversal protection in filenames
- Czech-calibrated token counting (3.2 chars/token instead of 4.0)
- Debug-level API response logging (instead of INFO)
- Priority-based metadata truncation (instead of character slicing)

---

## Bug Fixes

### SeverniTerasa echo text corrected

The v2 `.bat` file for Severni Terasa contained an incorrect echo message:

**V2 (`exec_scrape_sitemap_GPT_Usti_v2_Obvod_SeverniTerasa.bat`):**
```batch
echo Spoustim scraping pro mesto Usti nad Labem mestsky obvod Strekov...
```

This incorrectly said "Strekov" when the script was actually processing Severni Terasa.

**V3 (`exec_scrape_sitemap_GPT_v3_Usti_Obvod_SeverniTerasa.bat`):**
```batch
echo Spoustim scraping pro mesto Usti nad Labem mestsky obvod Severni Terasa...
```

The echo text now correctly identifies the target district.

### Batch file naming consistency fixed

V2 `.bat` files used inconsistent naming patterns. Some placed the version marker `v2` before the client name, while others placed it in the middle:

**Inconsistent v2 naming:**
```
exec_scrape_sitemap_GPT_v2_HealthyTwenty.bat       (v2 before name)
exec_scrape_sitemap_GPT_v2_ICUK.bat                 (v2 before name)
exec_scrape_sitemap_GPT_v2_Usti_Magistrat.bat       (v2 before name)
exec_scrape_sitemap_GPT_v2_Usti_Obvod_Nestemice.bat (v2 before name)
exec_scrape_sitemap_GPT_Usti_v2_Obvod_SeverniTerasa.bat  (v2 in middle!)
exec_scrape_sitemap_GPT_Usti_v2_Obvod_Strekov.bat        (v2 in middle!)
exec_scrape_sitemap_GPT_Usti_v2_Obvod_UstinadLabem.bat   (v2 in middle!)
```

Three files (`SeverniTerasa`, `Strekov`, `UstinadLabem`) had `v2` embedded between `Usti` and `Obvod` rather than in the standard position.

**Consistent v3 naming:**

All 15 v3 `.bat` files follow the same pattern: `exec_scrape_sitemap_GPT_v3_{Name}.bat`

```
exec_scrape_sitemap_GPT_v3_HealthyTwenty.bat
exec_scrape_sitemap_GPT_v3_ICUK.bat
exec_scrape_sitemap_GPT_v3_Usti_Magistrat.bat
exec_scrape_sitemap_GPT_v3_Usti_Obvod_Nestemice.bat
exec_scrape_sitemap_GPT_v3_Usti_Obvod_SeverniTerasa.bat
exec_scrape_sitemap_GPT_v3_Usti_Obvod_Strekov.bat
exec_scrape_sitemap_GPT_v3_Usti_Obvod_UstinadLabem.bat
```

---

## Dead Fields Note

The following two fields are present in both v2 and v3 config files but are **not consumed** by the v3 config loader (`gpt_scraper_v3/config.py`):

| Field | Value | Why it exists |
|-------|-------|---------------|
| `openrouter.fallback_enabled` | `true` | Was present in v2 configs. Preserved in v3 for backward compatibility. The v3 engine handles model fallback through the `models` array and OpenRouter's native multi-model routing, making this flag redundant. |
| `openrouter.timeout_seconds` | `120` | Was present in v2 configs. Preserved in v3 for backward compatibility. The v3 engine uses `http_settings.request_timeout` (default `150`) for HTTP request timeouts, which is configured at the session level via `requests.Session` retry/timeout settings. |

These fields are safe to leave in config files. They are silently ignored by the v3 config loader -- the `load_configuration()` function in `config.py` reads the `openrouter` section using `.get()` calls for the fields it needs and does not raise errors on unrecognized keys. Removing them would have no effect on behavior.

If you are creating new v3 configs from scratch (not migrating from v2), you may omit these fields entirely.
