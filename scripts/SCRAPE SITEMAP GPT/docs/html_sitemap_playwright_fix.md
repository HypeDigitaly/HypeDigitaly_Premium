# HTML Sitemap Playwright Fix & Security Hardening

**Date:** 2026-02-15
**Scope:** `gpt_scraper_v3/` modules: `cli.py`, `content_fetching.py`, `playwright_provider.py`, `sitemap_parsing.py`, `config.py`

---

## Summary

The HTML sitemap feature (`sitemap_url` config parameter) was broken when Playwright was the primary content provider. This fix restores correct provider-sequence routing for HTML sitemap fetching and adds security hardening across multiple modules.

---

## 1. What Was Broken

When the scraper is configured with an HTML sitemap URL (the `sitemap_url` field in config JSON), it needs to fetch that page as **raw HTML** to extract `<a>` anchor tags for URL discovery. The function `_resolve_html_sitemap()` in `cli.py` hardcoded a call to `get_html_content_via_jina()`, which meant:

- If `provider_sequence` was set to `"playwright"` (no Jina in the sequence), the HTML sitemap fetch would fail because it bypassed the configured provider entirely.
- Configurations that used Playwright as the sole or primary provider could not use the HTML sitemap feature at all.

The data flow before the fix:

```
config: provider_sequence = "playwright"
config: sitemap_url = "https://example.com/sitemap"

_resolve_html_sitemap()
  -> get_html_content_via_jina(sitemap_url)   # WRONG: ignores provider_sequence
  -> Jina API call (fails or returns nothing if no Jina key configured)
  -> Falls back to XML-only mode
```

A secondary problem existed in `playwright_provider.py`: there was no function to return raw HTML. The existing `fetch_playwright_markdown()` always converted HTML to markdown via `_html_to_markdown()`, which strips `<a>` tags -- making it useless for sitemap link extraction.

---

## 2. What Was Fixed

### 2.1 New function: `get_raw_html_content()` in `content_fetching.py`

**Location:** `gpt_scraper_v3/content_fetching.py`, lines 143-237

A new public function that iterates through `cfg.MARKDOWN_PROVIDER_SEQUENCE` and tries each provider in order until one returns raw HTML content. This mirrors the existing `get_markdown_content()` pattern but requests HTML instead of markdown.

Provider support:

| Provider     | Behaviour                                                   |
|-------------|-------------------------------------------------------------|
| `playwright` | Calls `fetch_playwright_html(url)` -- returns raw `page.content()` |
| `jina`       | Delegates to existing `get_html_content_via_jina(url)`       |
| `firecrawl`  | Skipped with a warning (does not support raw HTML mode)      |

### 2.2 New function: `fetch_playwright_html()` in `playwright_provider.py`

**Location:** `gpt_scraper_v3/playwright_provider.py`, lines 591-727

A Playwright fetch function that returns the raw HTML from `page.content()` **without** any DOM filtering or markdown conversion. This preserves all `<a>` tags for downstream link extraction by `extract_links_from_html_sitemap()`.

Key differences from `fetch_playwright_markdown()`:

- No `remove_selectors` or `target_selectors` processing.
- No `_html_to_markdown()` conversion.
- Returns `Optional[str]` (raw HTML) instead of a `(markdown, title, metadata)` tuple.
- Same retry/error handling pattern (2 retries on timeout, no retry on other errors).

### 2.3 Updated `_resolve_html_sitemap()` in `cli.py`

**Location:** `gpt_scraper_v3/cli.py`, lines 278-308

Changed from:

```python
html = get_html_content_via_jina(cfg.SITEMAP_URL)
```

To:

```python
html = get_raw_html_content(cfg.SITEMAP_URL)
```

The corrected data flow:

```
config: provider_sequence = "playwright"
config: sitemap_url = "https://example.com/sitemap"

_resolve_html_sitemap()
  -> get_raw_html_content(sitemap_url)
    -> reads cfg.MARKDOWN_PROVIDER_SEQUENCE
    -> tries "playwright" first
      -> fetch_playwright_html(sitemap_url)   # returns raw HTML with <a> tags
    -> falls back to "jina" if playwright fails
  -> extract_links_from_html_sitemap(html)    # parses <a> tags from raw HTML
```

### 2.4 Content quality warning in `sitemap_parsing.py`

**Location:** `gpt_scraper_v3/sitemap_parsing.py`, lines 78-86

Added an early diagnostic warning when the HTML content passed to `extract_links_from_html_sitemap()` is shorter than 200 characters. This helps identify cases where the page was not fully rendered or was blocked by anti-bot measures, before the parser silently returns zero links.

---

## 3. Security Hardening

### 3.1 SSRF Prevention

URL scheme validation (`http`/`https` only) was added to **all** content-fetching entry points. This prevents server-side request forgery via `file://`, `javascript:`, `data:`, or other dangerous URL schemes injected through config or sitemap data.

**Affected functions (6 total):**

| Module                    | Function                              |
|--------------------------|---------------------------------------|
| `content_fetching.py`    | `get_html_content_via_jina()`         |
| `content_fetching.py`    | `get_html_content_for_pagination_via_jina()` |
| `content_fetching.py`    | `get_raw_html_content()`              |
| `content_fetching.py`    | `get_markdown_content()`              |
| `playwright_provider.py` | `fetch_playwright_markdown()`         |
| `playwright_provider.py` | `fetch_playwright_html()`             |

Each function checks `urlparse(url).scheme` and returns `None` (or the failure tuple) if the scheme is not `http` or `https`.

### 3.2 Jina Host Validation

**Location:** `content_fetching.py`, lines 28-46

A new allowlist (`_JINA_ALLOWED_HOSTS`) ensures the Bearer API token is only sent to known Jina AI hosts (`r.jina.ai`, `s.jina.ai`). This prevents token leakage if the `api_url_template` in config is tampered with to point to an attacker-controlled server.

The validation function `_validate_jina_api_url()` is called before every Jina API request.

### 3.3 API Key Redaction in Config

**Location:** `config.py`, lines 23-41, 116-143

Previously, `ScraperConfig.__repr__()` only redacted top-level API key fields. API keys stored inside nested dicts (specifically `MARKDOWN_PROVIDERS`, which contains `{"jina": {"api_key": "sk-..."}}`) were leaked in full when the config was logged or printed.

Changes:

- New `_SENSITIVE_KEY_PATTERNS` set matches keys like `api_key`, `secret`, `token`, `password` in nested structures.
- New `_deep_redact()` function recursively walks dicts/lists and replaces matching string values with `***REDACTED***`.
- New `to_safe_dict()` method on `ScraperConfig` applies both top-level and deep redaction.
- `__str__()` now uses `to_safe_dict()` for full protection.

### 3.4 Playwright Hardening

Several smaller improvements in `playwright_provider.py`:

| Change | Detail |
|--------|--------|
| `_PLAYWRIGHT_AVAILABLE` guard | `fetch_playwright_html()` checks this flag before attempting any Playwright operations, preventing `NameError` if the package is not installed. |
| `ignore_https_errors` warning | When this option is enabled in config, a warning is logged stating TLS verification is disabled -- makes the security tradeoff visible. |
| `storage_state` validation | The path is checked with `os.path.exists()` before being passed to Playwright. Invalid paths are skipped with a warning instead of crashing. |
| Content threshold | Both `fetch_playwright_markdown()` and `fetch_playwright_html()` use a unified 200-character minimum threshold for content validation. |

---

## 4. How to Verify

### 4.1 Playwright-only HTML sitemap

Set up a config with Playwright as the sole provider and an HTML sitemap URL:

```json
{
  "content_providers": {
    "provider_sequence": "playwright",
    "playwright": {
      "browser": "chromium",
      "headless": true
    }
  },
  "website": {
    "sitemap_url": "https://example.com/sitemap",
    "xml_sitemap_url": ""
  }
}
```

Run the scraper and check logs for:

```
Fetching HTML sitemap from https://example.com/sitemap (provider-sequence-aware)
Using provider sequence: playwright
Attempting raw HTML fetch with provider: playwright
PLAYWRIGHT HTML: Navigating to https://example.com/sitemap ...
PLAYWRIGHT HTML SUCCESS: https://example.com/sitemap (XXXXX chars)
Successfully fetched raw HTML from https://example.com/sitemap using playwright
Extracting links from HTML sitemap by parsing <a> anchor tags
Found XX anchor tags with href attributes
```

If Playwright is not installed, the scraper should log:

```
Playwright is not installed. Skipping provider.
```

And then attempt the next provider in the sequence (or fall back to XML-only if no providers succeed).

### 4.2 SSRF prevention

Verify that non-HTTP URLs are rejected by checking logs:

```
Refusing to fetch non-HTTP URL scheme 'file': file:///etc/passwd
```

### 4.3 API key redaction

Print or log a `ScraperConfig` instance and confirm no API keys appear in the output:

```python
cfg = get_config()
print(cfg)  # Should show ***REDACTED*** for all sensitive fields
print(cfg.to_safe_dict()["MARKDOWN_PROVIDERS"])  # Nested api_key should be redacted
```

---

## 5. Files Changed

| File | Key Changes |
|------|-------------|
| `gpt_scraper_v3/cli.py` | `_resolve_html_sitemap()` now calls `get_raw_html_content()` instead of `get_html_content_via_jina()` |
| `gpt_scraper_v3/content_fetching.py` | Added `get_raw_html_content()`, `_validate_jina_api_url()`, `_JINA_ALLOWED_HOSTS`; SSRF checks in all 4 public functions |
| `gpt_scraper_v3/playwright_provider.py` | Added `fetch_playwright_html()`; SSRF checks; `_PLAYWRIGHT_AVAILABLE` guard; `ignore_https_errors` warning; `storage_state` path validation |
| `gpt_scraper_v3/sitemap_parsing.py` | Content quality warning for short HTML in `extract_links_from_html_sitemap()` |
| `gpt_scraper_v3/config.py` | Added `_SENSITIVE_KEY_PATTERNS`, `_deep_redact()`, `to_safe_dict()`; updated `__repr__`/`__str__` |
