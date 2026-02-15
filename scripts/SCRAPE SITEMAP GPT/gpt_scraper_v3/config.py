"""Configuration module for GPT Scraper V3.

Replaces V2 global configuration variables with a ScraperConfig dataclass,
loaded once via load_configuration() and accessed through get_config().
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

logger = logging.getLogger(__name__)


@dataclass
class ScraperConfig:
    """All configuration state for a single scraper run."""

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
    MARKDOWN_PROVIDERS: Dict[str, Any] = field(default_factory=dict)
    PLAYWRIGHT_CONFIG: Dict[str, Any] = field(default_factory=dict)
    # OpenRouter settings
    OPENROUTER_MAX_TOKENS: int = 4000
    OPENROUTER_MODELS: List[str] = field(default_factory=lambda: ["anthropic/claude-3.5-sonnet"])
    OPENROUTER_TEMPERATURE: float = 0.1
    OPENROUTER_TOP_P: float = 0.9
    OPENROUTER_TARGET_LANGUAGE: str = "Czech"
    OPENROUTER_PROVIDER_CONFIG: Dict[str, Any] = field(default_factory=dict)
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
    PARSED_BASE_URL: Optional[Any] = None  # urllib ParseResult
    BASE_NETLOC: str = ""
    NON_WWW_BASE_NETLOC: str = ""
    BASE_SCHEME: str = ""
    CANONICAL_BASE_URLS: Set[str] = field(default_factory=set)
    SITEMAP_URL: str = ""
    XML_SITEMAP_URL: str = ""
    # URL lists
    BLACKLISTED_URLS: List[str] = field(default_factory=list)
    BLACKLISTED_RELATIVE_PATHS: List[str] = field(default_factory=list)
    RSS_FEEDS: List[Any] = field(default_factory=list)
    RECURSIVE_URLS: List[Any] = field(default_factory=list)
    PAGINATED_URLS: List[Any] = field(default_factory=list)
    TEST_URLS: List[str] = field(default_factory=list)
    # HTTP settings
    REQUEST_TIMEOUT: int = 30
    REQUEST_RETRY_CODES: Tuple[int, ...] = (500, 502, 503, 504, 524)
    REQUEST_RETRY_COUNT: int = 3
    REQUEST_BACKOFF_FACTOR: float = 0.3
    # Processing flags
    CHECK_LAST_MODIFIED: int = 1  # 0=off, 1=on (skip no-lastmod if exists), 2=partial (always process no-lastmod)
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

    def __repr__(self) -> str:
        """Safe repr that masks API keys."""
        return (
            f"ScraperConfig(BASE_URL={self.BASE_URL!r}, "
            f"OUTPUT_DIR={self.OUTPUT_DIR!r}, "
            f"SCRIPT_NAME={self.SCRIPT_NAME!r}, ...)"
        )


# -- Singleton access ---------------------------------------------------------

_cfg: Optional[ScraperConfig] = None


def get_config() -> ScraperConfig:
    """Return the loaded configuration singleton."""
    if _cfg is None:
        raise RuntimeError("Configuration not loaded. Call load_configuration() first.")
    return _cfg


# -- Helper utilities (migrated from V2) --------------------------------------

def extract_config_identifier(config_file: str) -> str:
    """Extract a unique identifier from a config filename for path generation."""
    base_name = os.path.splitext(os.path.basename(config_file))[0]
    patterns = [
        r".*_config_(.+)$",   # scrape_sitemap_GPT_config_Stredocesky
        r"config_(.+)$",      # config_example
        r"(.+)_config$",      # example_config
        r"^(.+)$",            # fallback: whole filename
    ]
    for pattern in patterns:
        match = re.match(pattern, base_name, re.IGNORECASE)
        if match:
            identifier = match.group(1)
            identifier = re.sub(r"[^a-zA-Z0-9_]", "_", identifier)
            identifier = re.sub(r"_+", "_", identifier)
            identifier = identifier.strip("_")
            if identifier:
                return identifier
    return "default"


def generate_unique_paths(config_file: str) -> Dict[str, str]:
    """Generate unique directory/file paths based on the config filename."""
    identifier = extract_config_identifier(config_file)
    return {
        "files_directory": f"{identifier}_files",
        "log_directory": f"{identifier}_logs",
        "last_run_file": f"{identifier}_last_run_time.txt",
    }


def load_config(config_file: str = "config.json") -> Dict[str, Any]:
    """Load raw configuration from a JSON file."""
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"Config file not found: {config_file}")
    with open(config_file, "r", encoding="utf-8") as f:
        config: Dict[str, Any] = json.load(f)
    return config


def parse_rss_date_threshold(date_str: str) -> Optional[datetime]:
    """Parse RSSDateThreshold (DD-MM-YYYY) into a UTC datetime object."""
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%d-%m-%Y")
        return dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"Invalid RSSDateThreshold format: '{date_str}'. Expected DD-MM-YYYY.")


def validate_config(config: Dict[str, Any]) -> bool:
    """Validate required keys and vector-store settings per OpenAI constraints."""
    required_keys: List[str] = [
        "website.base_url",
        "api_keys.openai",
    ]

    # Only require provider API keys if that provider is in the sequence
    provider_sequence: str = config.get("content_providers", {}).get("provider_sequence", "")
    if "jina" in provider_sequence:
        required_keys.append("api_keys.jina_ai")
    if "firecrawl" in provider_sequence:
        required_keys.append("api_keys.firecrawl")

    def _get_nested(data: Dict[str, Any], key_path: str) -> Any:
        value: Any = data
        for key in key_path.split("."):
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        return value

    missing_keys = [k for k in required_keys if _get_nested(config, k) is None]
    if missing_keys:
        raise ValueError(f"Missing required configuration keys: {missing_keys}")

    # Vector store validation
    vs: Dict[str, Any] = config.get("vector_store", {})
    if vs:
        chunking_strategy: str = vs.get("chunking_strategy", "auto")
        max_chunk_size: int = vs.get("max_chunk_size", 800)
        chunk_overlap: int = vs.get("chunk_overlap", 400)
        content_token_offset: int = vs.get("content_token_offset", 0)

        if not isinstance(content_token_offset, int) or content_token_offset < 0:
            raise ValueError(
                f"Config validation failed: vector_store.content_token_offset "
                f"must be a non-negative integer, got: {content_token_offset}")
        if max_chunk_size - content_token_offset <= 0:
            raise ValueError(
                f"Config validation failed: max_chunk_size ({max_chunk_size}) minus "
                f"content_token_offset ({content_token_offset}) must result in a positive content limit.")

        if chunking_strategy.lower() == "static":
            if not isinstance(max_chunk_size, int) or max_chunk_size < 100 or max_chunk_size > 4096:
                raise ValueError(
                    f"Config validation failed: vector_store.max_chunk_size "
                    f"must be integer between 100-4096, got: {max_chunk_size}")
            if not isinstance(chunk_overlap, int) or chunk_overlap < 0:
                raise ValueError(
                    f"Config validation failed: vector_store.chunk_overlap "
                    f"must be non-negative integer, got: {chunk_overlap}")
            max_allowed_overlap = max_chunk_size // 2
            if chunk_overlap > max_allowed_overlap:
                raise ValueError(
                    f"Config validation failed: vector_store.chunk_overlap ({chunk_overlap}) "
                    f"must NOT exceed half of max_chunk_size. For chunk_size={max_chunk_size}, "
                    f"maximum allowed overlap is {max_allowed_overlap}")
            print(f"PASS: Vector Store static chunking validated: {max_chunk_size} tokens, {chunk_overlap} overlap")
            if chunk_overlap == 0:
                print("OPTIMIZATION: Zero overlap configured for maximum efficiency")
            if max_chunk_size == 4096:
                print("OPTIMIZATION: Maximum chunk size (4096) for largest context windows")
        elif chunking_strategy.lower() == "auto":
            print("PASS: Vector Store auto chunking validated: OpenAI default (800 tokens, 400 overlap)")
        else:
            raise ValueError(
                f"Config validation failed: vector_store.chunking_strategy "
                f"must be 'auto' or 'static', got: {chunking_strategy}")

    # RSS Date Threshold validation
    rss_thresh: Optional[str] = config.get("processing", {}).get("RSSDateThreshold")
    if rss_thresh:
        try:
            parsed_date = parse_rss_date_threshold(rss_thresh)
            print(f"PASS: RSSDateThreshold validated: {rss_thresh} -> {parsed_date.isoformat()}")  # type: ignore[union-attr]
        except ValueError as exc:
            raise ValueError(f"Config validation failed: {exc}") from exc
    return True


def sanitize_json_response(json_text: str) -> str:
    """Strip markdown code-block fences from a JSON string."""
    if not json_text or not isinstance(json_text, str):
        return json_text  # type: ignore[return-value]
    json_text = json_text.strip()
    if json_text.startswith("```"):
        lines = json_text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        json_text = "\n".join(lines).strip()
    return json_text


# -- Main entry point ---------------------------------------------------------

def load_configuration(config_file: str = "config.json") -> ScraperConfig:
    """Load, validate, and store configuration as a ScraperConfig.

    Replaces the V2 function that set 40+ module-level globals. Populates a
    single ScraperConfig dataclass and stores it as the module singleton.
    """
    global _cfg

    raw: Dict[str, Any] = load_config(config_file)
    validate_config(raw)
    cfg = ScraperConfig()

    # Unique paths from config filename
    unique_paths = generate_unique_paths(config_file)

    # Script identification
    cfg.SCRIPT_NAME = raw["script_info"]["name"]
    cfg.LOG_DIR = unique_paths["log_directory"]
    cfg.LOG_FILE = os.path.join(cfg.LOG_DIR, f"{cfg.SCRIPT_NAME}_detailed.log")
    cfg.OUTPUT_DIR = unique_paths["files_directory"]

    # API keys
    cfg.JINA_AI_API_KEY = raw.get("api_keys", {}).get("jina_ai", "")
    cfg.FIRECRAWL_API_KEY = raw.get("api_keys", {}).get("firecrawl", "")
    cfg.OPENAI_API_KEY = raw["api_keys"]["openai"]
    cfg.OPENROUTER_API_KEY = raw["api_keys"].get("openrouter")

    # Content provider configuration
    cfg.JINA_REMOVE_SELECTORS = raw["content_providers"]["jina"]["remove_selectors"]
    cfg.JINA_TARGET_SELECTORS = raw["content_providers"]["jina"].get("target_selectors", "")
    cfg.MARKDOWN_PROVIDER_SEQUENCE = raw["content_providers"]["provider_sequence"]

    # OpenRouter configuration (with legacy llm_config fallback)
    or_cfg: Dict[str, Any] = {}
    if "openrouter" in raw:
        or_cfg = raw["openrouter"]
    elif "llm_config" in raw and "openrouter" in raw["llm_config"]:
        or_cfg = raw["llm_config"]["openrouter"]
        llm_cfg = raw["llm_config"]
        if "RAG_QUESTION_LANGUAGES" in llm_cfg and len(llm_cfg["RAG_QUESTION_LANGUAGES"]) > 0:
            first_lang: str = llm_cfg["RAG_QUESTION_LANGUAGES"][0]
            if first_lang == "cs":
                or_cfg["target_language"] = "Czech"
            elif first_lang == "en":
                or_cfg["target_language"] = "English"

    cfg.OPENROUTER_MAX_TOKENS = or_cfg.get("max_tokens", 4000)
    cfg.OPENROUTER_MODELS = or_cfg.get("models", ["anthropic/claude-3.5-sonnet"])
    cfg.OPENROUTER_TEMPERATURE = or_cfg.get("temperature", 0.1)
    cfg.OPENROUTER_TOP_P = or_cfg.get("top_p", 0.9)
    cfg.OPENROUTER_TARGET_LANGUAGE = or_cfg.get("target_language", "Czech")
    cfg.OPENROUTER_PROVIDER_CONFIG = or_cfg.get("provider", {})
    cfg.OPENROUTER_CACHE_ENABLED = or_cfg.get("enable_caching", False)
    cfg.OPENROUTER_CACHE_TYPE = or_cfg.get("cache_type", "ephemeral")

    # Vector store configuration
    cfg.OPENAI_VECTOR_STORE_ID = raw["vector_store"]["id"]
    cfg.ENABLE_DEDUPLICATION = raw["vector_store"]["enable_deduplication"]
    cfg.DEFAULT_CHUNKING_STRATEGY = raw["vector_store"]["chunking_strategy"]
    cfg.DEFAULT_MAX_CHUNK_SIZE = raw["vector_store"]["max_chunk_size"]
    cfg.DEFAULT_CHUNK_OVERLAP = raw["vector_store"]["chunk_overlap"]
    cfg.DEFAULT_CONTENT_TOKEN_OFFSET = raw["vector_store"].get("content_token_offset", 0)
    cfg.DEFAULT_CONTENT_RATIO = raw["vector_store"].get("content_ratio", 0.5)

    # URL configuration
    cfg.BASE_URL = raw["website"]["base_url"]
    cfg.PARSED_BASE_URL = urlparse(cfg.BASE_URL)
    cfg.BASE_NETLOC = cfg.PARSED_BASE_URL.netloc
    cfg.NON_WWW_BASE_NETLOC = (
        cfg.BASE_NETLOC[4:] if cfg.BASE_NETLOC.startswith("www.") else cfg.BASE_NETLOC)
    cfg.BASE_SCHEME = cfg.PARSED_BASE_URL.scheme

    # Canonical base URLs for homepage detection
    canonical: Set[str] = set()
    canonical.add(cfg.BASE_URL)
    normalized_base = cfg.BASE_URL.rstrip("/")
    canonical.add(normalized_base)
    canonical.add(normalized_base + "/")

    if cfg.BASE_NETLOC.startswith("www."):
        non_www_url = cfg.BASE_URL.replace("www.", "", 1)
        canonical.add(non_www_url)
        canonical.add(non_www_url.rstrip("/"))
        canonical.add(non_www_url.rstrip("/") + "/")
    else:
        www_url = cfg.BASE_URL.replace(cfg.BASE_SCHEME + "://", cfg.BASE_SCHEME + "://www.", 1)
        canonical.add(www_url)
        canonical.add(www_url.rstrip("/"))
        canonical.add(www_url.rstrip("/") + "/")

    if cfg.PARSED_BASE_URL.path.strip("/") == "":
        root_no_path = urlunparse((cfg.BASE_SCHEME, cfg.BASE_NETLOC, "", "", "", ""))
        default_pages = ["index.html", "index.htm", "default.aspx", "home.html"]
        for page in default_pages:
            canonical.add(urljoin(root_no_path, page))
        if cfg.BASE_NETLOC.startswith("www."):
            non_www_netloc = cfg.BASE_NETLOC[4:]
            root_non_www = urlunparse((cfg.BASE_SCHEME, non_www_netloc, "", "", "", ""))
            for page in default_pages:
                canonical.add(urljoin(root_non_www, page))

    canonical.add(cfg.BASE_URL)
    cfg.CANONICAL_BASE_URLS = canonical

    # IncludeBaseURL flag
    include_base_raw: Any = raw["website"].get("IncludeBaseURL", 0)
    try:
        include_base_raw = int(include_base_raw)
    except (ValueError, TypeError):
        include_base_raw = 0
    cfg.PROCESS_BASE_URL_ONCE = (include_base_raw == 1)
    cfg.BASE_URL_PROCESSED_IN_RUN = False
    status_label = "aktivni" if cfg.PROCESS_BASE_URL_ONCE else "vypnuty"
    print(f"Calculated {len(cfg.CANONICAL_BASE_URLS)} canonical base URLs for skipping.")
    print(f"IncludeBaseURL rezim: {include_base_raw} ({status_label})")

    # Sitemap URLs
    cfg.SITEMAP_URL = raw["website"]["sitemap_url"]
    cfg.XML_SITEMAP_URL = raw["website"]["xml_sitemap_url"]

    # Blacklisted URLs
    cfg.BLACKLISTED_URLS = raw["website"]["blacklisted_urls"]
    cfg.BLACKLISTED_RELATIVE_PATHS = raw["website"].get("blacklisted_relative_url_paths", [])

    # RSS feeds
    cfg.RSS_FEEDS = raw["website"].get("rss_feeds", [])

    # Recursive URLs (support both string and object format)
    recursive_urls_raw: List[Any] = raw["website"].get("recursive_urls", [])
    recursive_urls: List[Dict[str, Any]] = []
    for item in recursive_urls_raw:
        if isinstance(item, str):
            recursive_urls.append({"url": item, "recursive_level": 1})
        elif isinstance(item, dict) and "url" in item:
            level: int = item.get("recursive_level", 1)
            if not isinstance(level, int) or level < 1:
                print(f"WARNING: Invalid recursive_level {level} for URL {item['url']}, defaulting to 1")
                level = 1
            recursive_urls.append({"url": item["url"], "recursive_level": level})
        else:
            print(f"WARNING: Invalid recursive_urls configuration item: {item}, skipping")
    cfg.RECURSIVE_URLS = recursive_urls

    # Paginated URLs (simple string list)
    paginated_raw: List[Any] = raw["website"].get("paginated_urls", [])
    paginated_urls: List[str] = []
    for item in paginated_raw:
        if isinstance(item, str):
            paginated_urls.append(item.rstrip("/"))
        else:
            print(f"WARNING: Invalid paginated_urls configuration item: {item}, skipping")
    cfg.PAGINATED_URLS = paginated_urls

    # Test URLs
    cfg.TEST_URLS = raw["website"].get("test_urls", [])

    # HTTP settings
    cfg.REQUEST_TIMEOUT = raw["http_settings"]["request_timeout"]
    cfg.REQUEST_RETRY_CODES = tuple(raw["http_settings"]["retry_codes"])
    cfg.REQUEST_RETRY_COUNT = raw["http_settings"]["retry_count"]
    cfg.REQUEST_BACKOFF_FACTOR = raw["http_settings"]["backoff_factor"]

    # Processing flags
    raw_clm = raw["processing"]["check_last_modified"]
    if isinstance(raw_clm, bool):
        cfg.CHECK_LAST_MODIFIED = 1 if raw_clm else 0
    else:
        cfg.CHECK_LAST_MODIFIED = int(raw_clm)
    cfg.MAX_FILENAME_LENGTH = raw["processing"]["max_filename_length"]
    rss_thresh_str: Optional[str] = raw["processing"].get("RSSDateThreshold")
    cfg.RSS_DATE_THRESHOLD = parse_rss_date_threshold(rss_thresh_str) if rss_thresh_str else None

    # File paths
    cfg.LAST_RUN_FILE = unique_paths["last_run_file"]
    cfg.VERBOSE_URL_MATCHING = False
    identifier = extract_config_identifier(config_file)
    cfg.RSS_LAST_RUN_FILE = f"{identifier}_rss_last_run_time.txt"
    cfg.SITEMAP_LAST_RUN_FILE = f"{identifier}_sitemap_last_run_time.txt"

    # Markdown providers
    cfg.MARKDOWN_PROVIDERS = {
        "jina": {
            "name": "jina",
            "api_key": cfg.JINA_AI_API_KEY,
            "api_url_template": "https://r.jina.ai/{url}",
        },
        "firecrawl": {
            "name": "firecrawl",
            "api_key": cfg.FIRECRAWL_API_KEY,
            "api_url": "https://api.firecrawl.dev/v1/scrape",
        },
    }

    # --- Playwright provider (optional) ---
    pw_raw: Dict[str, Any] = raw["content_providers"].get("playwright", {})
    if pw_raw:
        cfg.PLAYWRIGHT_CONFIG = {
            "browser": pw_raw.get("browser", "chromium"),
            "headless": pw_raw.get("headless", True),
            "stealth": pw_raw.get("stealth", True),
            "viewport": pw_raw.get("viewport", {"width": 1920, "height": 1080}),
            "user_agent": pw_raw.get("user_agent", ""),
            "locale": pw_raw.get("locale", "cs-CZ"),
            "timezone_id": pw_raw.get("timezone_id", "Europe/Prague"),
            "extra_http_headers": pw_raw.get("extra_http_headers", {}),
            "remove_selectors": pw_raw.get("remove_selectors", ""),
            "target_selectors": pw_raw.get("target_selectors", ""),
            "wait_until": pw_raw.get("wait_until", "domcontentloaded"),
            "wait_for_selector": pw_raw.get("wait_for_selector", ""),
            "wait_for_timeout_ms": pw_raw.get("wait_for_timeout_ms", 0),
            "navigation_timeout_ms": pw_raw.get("navigation_timeout_ms", 30000),
            "javascript_to_execute": pw_raw.get("javascript_to_execute", ""),
            "block_resources": pw_raw.get("block_resources", ["image", "font", "media"]),
            "block_urls": pw_raw.get("block_urls", []),
            "cookies": pw_raw.get("cookies", []),
            "storage_state": pw_raw.get("storage_state", ""),
            "ignore_https_errors": pw_raw.get("ignore_https_errors", False),
            "executable_path": pw_raw.get("executable_path", ""),
            "context_pages_limit": pw_raw.get("context_pages_limit", 500),
        }
        cfg.MARKDOWN_PROVIDERS["playwright"] = {
            "name": "playwright",
            "requires_api_key": False,
        }
        logger.info("Playwright provider configured (browser=%s, headless=%s, stealth=%s)",
                     cfg.PLAYWRIGHT_CONFIG["browser"],
                     cfg.PLAYWRIGHT_CONFIG["headless"],
                     cfg.PLAYWRIGHT_CONFIG["stealth"])

    # Validate provider_sequence references configured providers
    for pid in [p.strip() for p in cfg.MARKDOWN_PROVIDER_SEQUENCE.split(",") if p.strip()]:
        if pid not in cfg.MARKDOWN_PROVIDERS:
            logger.warning(
                "Provider '%s' is in provider_sequence but not configured in "
                "content_providers. It will be skipped.", pid)

    # Status output (mirrors V2 print statements)
    print(f"Configuration loaded from: {config_file}")
    print(f"Target website: {cfg.BASE_URL}")
    print(f"Output directory: {cfg.OUTPUT_DIR}")
    print(f"Log directory: {cfg.LOG_DIR}")
    print(f"Config identifier: {identifier}")
    print(f"Combined last run file: {cfg.LAST_RUN_FILE}")
    print(f"RSS last run file: {cfg.RSS_LAST_RUN_FILE}")
    print(f"Sitemap last run file: {cfg.SITEMAP_LAST_RUN_FILE}")
    print(f"RSS feeds: {len(cfg.RSS_FEEDS)} configured")
    print(f"Recursive URLs: {len(cfg.RECURSIVE_URLS)} configured")
    print(f"Paginated URLs (Explicit): {len(cfg.PAGINATED_URLS)} configured")
    print(f"Test URLs: {len(cfg.TEST_URLS)} configured")
    if cfg.RSS_DATE_THRESHOLD:
        print(f"RSS Date Threshold: {cfg.RSS_DATE_THRESHOLD.strftime('%d-%m-%Y')} (UTC)")
    if cfg.TEST_URLS:
        print("TEST MODE: Test URLs will override HTML sitemap processing")
    if cfg.RECURSIVE_URLS:
        print(f"SUBURLS MODE: Suburls detection enabled for {len(cfg.RECURSIVE_URLS)} URLs")
        for rc in cfg.RECURSIVE_URLS:
            print(f"   {rc.get('url', 'N/A')} -> max depth: {rc.get('recursive_level', 1)}")

    # Store singleton and return
    _cfg = cfg
    return cfg
