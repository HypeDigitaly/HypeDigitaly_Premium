# V3 Logging System Fixes -- 2026-02-15

Seven logging defects fixed across four modules. The primary user-reported symptom was that log files accumulated output across multiple runs instead of starting fresh, but the investigation uncovered six additional issues introduced during the V2-to-V3 rewrite.

---

## Background: How V2 Logging Worked

V2's `setup_logging()` (line 1530 of `scrape_sitemap_GPT_v2.py`) did the following:

```python
def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if os.path.exists(LOG_FILE):
        open(LOG_FILE, 'w').close()           # Truncate log file

    logging.basicConfig(level=logging.INFO)    # Adds root StreamHandler
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    # ... add file_handler and console_handler ...
```

This worked in V2 because V2 was a single-file script. The `logging.basicConfig()` call added a `StreamHandler` to the root logger, but since V2 only used one logger (`__name__` = `__main__`), the duplicate output was not noticeable in most cases.

---

## Fixes

### 1. Log File Not Truncated Between Runs

**Files:** `gpt_scraper_v3/logging_setup.py`

**Symptom:** Log files grew indefinitely across runs. A user running the scraper daily would find weeks of accumulated log entries in a single file.

**Root cause:** The V3 rewrite intentionally removed V2's `open(LOG_FILE, 'w').close()` truncation, with a comment stating that `RotatingFileHandler` manages the file lifecycle. This was incorrect. `RotatingFileHandler` opens in append mode (`mode='a'`) and only rotates when a write would exceed `maxBytes` (10 MB). It never truncates.

**Fix:** Restore truncation before handler setup, using a context manager:

```python
# Truncate log file for a fresh start each run (V2 parity)
with open(cfg.LOG_FILE, "w"):
    pass
```

The `open("w")` call creates-or-truncates atomically, so no `os.path.exists()` guard is needed (see fix 6 below).

### 2. Duplicate Console Output

**Files:** `gpt_scraper_v3/logging_setup.py`

**Symptom:** Every log message appeared twice on the console.

**Root cause:** Two issues combined:
1. `logging.basicConfig(level=logging.INFO)` added a `StreamHandler` to the **root** logger.
2. The `gpt_scraper_v3` package logger had `propagate=True` (the default), so messages flowed up to the root logger's handler as well as the package logger's own `StreamHandler`.

In V2 this was masked because the script ran as `__main__` and `handlers.clear()` on the named logger removed the root handler's effect on most messages. In V3's multi-module package, every child logger (e.g., `gpt_scraper_v3.config`, `gpt_scraper_v3.cli`) propagated to the root, producing duplicates.

**Fix:** Two changes:
- Removed `logging.basicConfig(level=logging.INFO)` entirely.
- Set `logger.propagate = False` on the package logger.

```python
logger.handlers.clear()
logger.propagate = False  # Prevent duplicate console output via root logger
```

### 3. Pre-Logging Logger Calls

**Files:** `gpt_scraper_v3/config.py`

**Symptom:** Configuration status messages were either lost or routed through the root logger with default formatting before `setup_logging()` was called.

**Root cause:** `config.py` used `logger.info()` and `logger.warning()` during `load_configuration()`, which runs before `setup_logging()`. At that point, no handlers are configured on the package logger.

**Fix:** Converted all pre-logging output in `load_configuration()` to `print()` statements. These messages execute before the logging system is initialized, so `print()` is the correct mechanism. This matches V2's behavior where configuration status was printed before `setup_logging()`.

The `print()` calls appear at lines 271, 447, 451, 424-425, and throughout the status output block (lines 544-564 of `config.py`).

### 4. Debug Mode PII Exposure in Log Files

**Files:** `gpt_scraper_v3/cli.py`

**Symptom:** When `--debug` was used, scraped content previews and full API responses were written to the log file on disk, not just to the console.

**Root cause:** The V3 debug mode handler set all handlers (including `file_handler`) to `DEBUG` level. This persisted potentially sensitive scraped content to disk.

**Fix:** Only the console `StreamHandler` is set to DEBUG; the `RotatingFileHandler` stays at INFO:

```python
if args.debug:
    logger.setLevel(logging.DEBUG)
    for h in logger.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(logging.DEBUG)
    logger.info("Debug mode enabled (console=DEBUG, file=INFO)")
```

This uses the fact that `RotatingFileHandler` inherits from `FileHandler`, which inherits from `StreamHandler`. The `isinstance` check distinguishes between the two.

### 5. Inconsistent Logger Naming

**Files:** `gpt_scraper_v3/token_tracker.py`

**Symptom:** Token usage messages appeared to come from the parent `gpt_scraper_v3` logger rather than the `gpt_scraper_v3.token_tracker` module.

**Root cause:** The module used `logging.getLogger("gpt_scraper_v3")` (hardcoded parent name) instead of `logging.getLogger(__name__)`.

**Fix:**

```python
# Before
logger: logging.Logger = logging.getLogger("gpt_scraper_v3")

# After
logger: logging.Logger = logging.getLogger(__name__)
```

This ensures log messages correctly identify their source module, which is important for debugging in a multi-module package.

### 6. TOCTOU Race Condition on Log File Truncation

**Files:** `gpt_scraper_v3/logging_setup.py`

**Context:** V2 had this pattern:

```python
if os.path.exists(LOG_FILE):
    open(LOG_FILE, 'w').close()
```

The `os.path.exists()` check is a time-of-check-to-time-of-use (TOCTOU) issue: the file could be created or deleted between the check and the open. Additionally, if the file does not exist, it would not be created, though `RotatingFileHandler` would create it later.

**Fix:** The guard is unnecessary because `open("w")` creates the file if it does not exist and truncates it if it does. The V3 fix uses a single atomic operation:

```python
with open(cfg.LOG_FILE, "w"):
    pass
```

### 7. Path Normalization Inconsistency

**Files:** `gpt_scraper_v3/config.py`

**Root cause:** `LOG_DIR` used `os.path.abspath()` (does not resolve symlinks) while `OUTPUT_DIR` was a raw relative path with no normalization at all. This inconsistency could cause path comparison failures and confusing log output.

**Fix:** Both now use `os.path.realpath()`, which resolves symlinks and produces absolute paths:

```python
cfg.LOG_DIR = os.path.realpath(unique_paths["log_directory"])
# ...
cfg.OUTPUT_DIR = os.path.realpath(unique_paths["files_directory"])
```

Additionally, the `os.makedirs(OUTPUT_DIR, exist_ok=True)` call was moved from `logging_setup.py` to `cli.py` (line 475), keeping output directory creation as an orchestration concern rather than a logging concern.

---

## Files Modified

| File | Changes |
|---|---|
| `gpt_scraper_v3/logging_setup.py` | Added log truncation via `open("w")`, removed `logging.basicConfig()`, set `propagate=False`, added `ValueError` guard for missing `LOG_DIR`/`LOG_FILE` |
| `gpt_scraper_v3/config.py` | `LOG_DIR` and `OUTPUT_DIR` now use `os.path.realpath()`, pre-logging `logger.info()`/`logger.warning()` converted to `print()` |
| `gpt_scraper_v3/cli.py` | Debug mode only sets console handler to DEBUG (file handler stays INFO), `os.makedirs(OUTPUT_DIR)` moved here from `logging_setup.py` |
| `gpt_scraper_v3/token_tracker.py` | Changed `getLogger("gpt_scraper_v3")` to `getLogger(__name__)` |

---

## How to Verify

**Log truncation (fix 1):**
Run the scraper twice against the same config. The log file should contain only the second run's output. Check the first timestamp in the log file -- it should match the second run's start time.

```bash
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_ICUK_v3.json --test-urls https://example.com
# Check log file size and first line
head -1 ICUK_v3_logs/scrape_sitemap_universal_detailed.log
# Run again
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_ICUK_v3.json --test-urls https://example.com
# First line should now show the second run's timestamp
head -1 ICUK_v3_logs/scrape_sitemap_universal_detailed.log
```

**No duplicate console output (fix 2):**
Run the scraper and observe the console. Each log line should appear exactly once. If duplicates are present, check that `propagate=False` is set and that `logging.basicConfig()` has not been re-added.

**Debug mode file safety (fix 4):**
Run with `--debug` and check the log file. It should contain only INFO-level messages and above. DEBUG-level content previews should appear on the console only.

```bash
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_ICUK_v3.json --debug --test-urls https://example.com
grep "DEBUG" ICUK_v3_logs/scrape_sitemap_universal_detailed.log
# Should return no results
```

**Logger naming (fix 5):**
In debug mode, check that token usage messages show `gpt_scraper_v3.token_tracker` as the source, not `gpt_scraper_v3`.

---

## Architecture Note

The V3 logging initialization sequence is:

```
1. cli.main()           -- parse CLI args (print() only)
2. load_configuration() -- load + validate config (print() only)
3. setup_logging(cfg)   -- truncate log, create handlers, set propagate=False
4. os.makedirs(OUTPUT_DIR) -- create output directory
5. apply_cli_overrides() -- apply --debug (console handler only)
6. All subsequent code uses logger
```

The key invariant is: **no `logger.*()` call may execute before step 3.** Any output needed before that point must use `print()`.
