import requests
from bs4 import BeautifulSoup
import logging
import json
import time
import os
from urllib.parse import urljoin, urlparse, urlunparse
import argparse
from datetime import datetime, timedelta, timezone
import re
import unicodedata
from logging.handlers import RotatingFileHandler
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
try:
    from dateutil import parser as dateutil_parser
except ImportError:
    dateutil_parser = None

# ============================================================================
# CONFIG LOADING
# ============================================================================

def extract_config_identifier(config_file):
    """Extract unique identifier from config filename for generating unique paths."""
    import os
    import re
    
    # Get base filename without path and extension
    base_name = os.path.splitext(os.path.basename(config_file))[0]
    
    # Try to extract meaningful identifier from various naming patterns
    patterns = [
        r'.*_config_(.+)$',           # pattern: scrape_sitemap_GPT_config_Stredocesky
        r'config_(.+)$',              # pattern: config_example
        r'(.+)_config$',              # pattern: example_config
        r'^(.+)$'                     # fallback: use whole filename
    ]
    
    for pattern in patterns:
        match = re.match(pattern, base_name, re.IGNORECASE)
        if match:
            identifier = match.group(1)
            # Clean identifier - remove special characters, keep only alphanumeric and underscores
            identifier = re.sub(r'[^a-zA-Z0-9_]', '_', identifier)
            # Remove multiple underscores
            identifier = re.sub(r'_+', '_', identifier)
            # Remove leading/trailing underscores
            identifier = identifier.strip('_')
            
            if identifier:  # Make sure we have something
                return identifier
    
    # Ultimate fallback
    return "default"

def generate_unique_paths(config_file):
    """Generate unique directory and file paths based on config filename."""
    identifier = extract_config_identifier(config_file)
    
    return {
        'files_directory': f"{identifier}_files",
        'log_directory': f"{identifier}_logs", 
        'last_run_file': f"{identifier}_last_run_time.txt"
    }

def load_config(config_file="config.json"):
    """Load configuration from JSON file."""
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"Config file not found: {config_file}")
    
    with open(config_file, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    return config

def validate_config(config):
    """Validate that all required configuration keys are present and vector store settings comply with OpenAI constraints."""
    required_keys = [
        'website.base_url',
        'api_keys.jina_ai',
        'api_keys.firecrawl',
        'api_keys.openai'
        # Note: openrouter is optional for backwards compatibility
    ]
    
    def get_nested_value(data, key_path):
        keys = key_path.split('.')
        value = data
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        return value
    
    # Check required keys
    missing_keys = []
    for key in required_keys:
        if get_nested_value(config, key) is None:
            missing_keys.append(key)
    
    if missing_keys:
        raise ValueError(f"Missing required configuration keys: {missing_keys}")
    
    # VALIDATE VECTOR STORE CONFIGURATION per OpenAI documentation
    vector_store_config = config.get("vector_store", {})
    if vector_store_config:
        chunking_strategy = vector_store_config.get("chunking_strategy", "auto")
        max_chunk_size = vector_store_config.get("max_chunk_size", 800)
        chunk_overlap = vector_store_config.get("chunk_overlap", 400)
        content_token_offset = vector_store_config.get("content_token_offset", 0)

        # Validate content_token_offset
        if not isinstance(content_token_offset, int) or content_token_offset < 0:
            raise ValueError(f"Config validation failed: vector_store.content_token_offset must be a non-negative integer, got: {content_token_offset}")
        
        # The content limit must be positive
        content_limit = max_chunk_size - content_token_offset
        if content_limit <= 0:
            raise ValueError(f"Config validation failed: max_chunk_size ({max_chunk_size}) minus content_token_offset ({content_token_offset}) must result in a positive content limit.")

        # Only validate static strategy constraints (auto uses fixed OpenAI values)
        if chunking_strategy.lower() == "static":
            # Validate max_chunk_size_tokens range (100-4096)
            if not isinstance(max_chunk_size, int) or max_chunk_size < 100 or max_chunk_size > 4096:
                raise ValueError(f"Config validation failed: vector_store.max_chunk_size must be integer between 100-4096, got: {max_chunk_size}")
            
            # Validate chunk_overlap_tokens non-negative
            if not isinstance(chunk_overlap, int) or chunk_overlap < 0:
                raise ValueError(f"Config validation failed: vector_store.chunk_overlap must be non-negative integer, got: {chunk_overlap}")
            
            # CRITICAL: Validate OpenAI constraint - overlap must NOT exceed half of max_chunk_size
            max_allowed_overlap = max_chunk_size // 2
            if chunk_overlap > max_allowed_overlap:
                raise ValueError(f"Config validation failed: vector_store.chunk_overlap ({chunk_overlap}) must NOT exceed half of max_chunk_size. "
                               f"For chunk_size={max_chunk_size}, maximum allowed overlap is {max_allowed_overlap}")
            
            print(f"PASS: Vector Store static chunking validated: {max_chunk_size} tokens, {chunk_overlap} overlap")
            
            # Log optimization status
            if chunk_overlap == 0:
                print(f"OPTIMIZATION: Zero overlap configured for maximum efficiency")
            if max_chunk_size == 4096:
                print(f"OPTIMIZATION: Maximum chunk size (4096) for largest context windows")
                
        elif chunking_strategy.lower() == "auto":
            print(f"PASS: Vector Store auto chunking validated: OpenAI default (800 tokens, 400 overlap)")
        else:
            raise ValueError(f"Config validation failed: vector_store.chunking_strategy must be 'auto' or 'static', got: {chunking_strategy}")
    
    # Validate RSS Date Threshold if present
    rss_date_threshold_str = config.get("processing", {}).get("RSSDateThreshold")
    if rss_date_threshold_str:
        try:
            parsed_date = parse_rss_date_threshold(rss_date_threshold_str)
            print(f"PASS: RSSDateThreshold validated: {rss_date_threshold_str} -> {parsed_date.isoformat()}")
        except ValueError as e:
            raise ValueError(f"Config validation failed: {str(e)}")
    
    return True

def parse_rss_date_threshold(date_str):
    """Parse RSSDateThreshold (DD-MM-YYYY) into a UTC datetime object."""
    if not date_str:
        return None
    
    try:
        # Parse DD-MM-YYYY format
        dt = datetime.strptime(date_str, "%d-%m-%Y")
        # Set time to midnight UTC and return
        return dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"Invalid RSSDateThreshold format: '{date_str}'. Expected DD-MM-YYYY.")

# ============================================================================
# GLOBAL CONFIGURATION VARIABLES
# ============================================================================

# This will be populated by load_configuration() function
CONFIG = None

# ============================================================================
# GLOBAL TOKEN USAGE TRACKING
# ============================================================================

# Global token usage accumulator for all OpenRouter API calls
global_token_usage = {
    'total_prompt_tokens': 0,
    'total_completion_tokens': 0,
    'total_tokens': 0,
    'api_calls_count': 0
}

def log_openrouter_token_usage(response_data, api_call_name, url=""):
    """
    Extract and log token usage from OpenRouter API response.
    Updates global token usage accumulator.

    Args:
        response_data (dict): The JSON response from OpenRouter API
        api_call_name (str): Descriptive name of the API call for logging
        url (str, optional): Associated URL for context
    """
    global global_token_usage

    try:
        usage = response_data.get('usage', {})
        if usage:
            prompt_tokens = usage.get('prompt_tokens', 0)
            completion_tokens = usage.get('completion_tokens', 0)
            total_tokens = usage.get('total_tokens', 0)

            # Update global accumulator
            global_token_usage['total_prompt_tokens'] += prompt_tokens
            global_token_usage['total_completion_tokens'] += completion_tokens
            global_token_usage['total_tokens'] += total_tokens
            global_token_usage['api_calls_count'] += 1

            # Log individual call usage
            logger.info(f"🔢 OPENROUTER TOKEN USAGE - {api_call_name}:")
            logger.info(f"   📥 Input tokens (prompt): {prompt_tokens:,}")
            logger.info(f"   📤 Output tokens (completion): {completion_tokens:,}")
            logger.info(f"   🔄 Total tokens: {total_tokens:,}")
            if url:
                logger.info(f"   🌐 URL: {url}")

            # Log running totals
            logger.info(f"📊 RUNNING TOTALS - Total API calls: {global_token_usage['api_calls_count']}")
            logger.info(f"   📥 Total input tokens: {global_token_usage['total_prompt_tokens']:,}")
            logger.info(f"   📤 Total output tokens: {global_token_usage['total_completion_tokens']:,}")
            logger.info(f"   🔄 Total tokens: {global_token_usage['total_tokens']:,}")

        else:
            logger.warning(f"⚠️ No usage data found in OpenRouter response for {api_call_name}")

    except Exception as e:
        logger.error(f"❌ Error logging token usage for {api_call_name}: {str(e)}")

# ============================================================================
# SCRIPT IDENTIFICATION
# ============================================================================

# These will be set dynamically from config
SCRIPT_NAME = "scrape_sitemap_universal"
LOG_DIR = None
LOG_FILE = None
OUTPUT_DIR = None

# Global variables that will be set by load_configuration()
RECURSIVE_URLS = []

# USAGE EXAMPLES:
# Basic usage: python scrape_sitemap_GPT.py
# With debug mode: python scrape_sitemap_GPT.py --debug
# Skip last modified check: python scrape_sitemap_GPT.py --no-check-modified
# Test resume cache: python scrape_sitemap_GPT.py --test-resume
# Resume from crashed run: python scrape_sitemap_GPT.py --resume
# Resume + Vector Store: python scrape_sitemap_GPT.py --resume --vector-store-id vs_abc123
# Upload to Vector Store: python scrape_sitemap_GPT.py --vector-store-id vs_abc123
# Upload with deduplication disabled: python scrape_sitemap_GPT.py --vector-store-id vs_abc123 --disable-deduplication
# Custom chunking strategy: python scrape_sitemap_GPT.py --vector-store-id vs_abc123 --chunking-strategy static --max-chunk-size 1200 --chunk-overlap 200
# Legacy HTML parsing: python scrape_sitemap_GPT.py --legacy-html-parsing
# Generalized HTML parsing (default): python scrape_sitemap_GPT.py
# Test specific URLs: python scrape_sitemap_GPT.py --test-urls https://example.com/page1 https://example.com/page2
# Test URLs with vector store: python scrape_sitemap_GPT.py --test-urls https://example.com/test --vector-store-id vs_abc123
#
# NEW PAGINATION FEATURES:
# - AUTOMATIC PAGINATION DETECTION: Each URL is checked for pagination indicators
# - AUTOMATIC SUBPAGE PROCESSING: If pagination is detected, all subpages are processed automatically
# - ENHANCED NAMING: Subpages are named with _PAGE[N] postfix (e.g., "Contact_List_PAGE2.txt")
# - PAGINATION INDICATORS: Detects numbered links, next/prev buttons, page counts, CSS classes
# - MULTILINGUAL SUPPORT: Works with Czech, English and other language pagination patterns
#
# OUTPUT: Each .txt file now includes metadata header with:
# - Source URL, title, navigation path, last modified date
# - Processing timestamp, content provider used (Jina/Firecrawl)
# - Script version for audit trail and troubleshooting
# - Pagination information (if applicable)

# ============================================================================
# CONFIGURATION INITIALIZATION
# ============================================================================

def load_configuration(config_file="config.json"):
    """Load and initialize global configuration."""
    global CONFIG, SCRIPT_NAME, LOG_DIR, LOG_FILE, OUTPUT_DIR
    global JINA_AI_API_KEY, FIRECRAWL_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY
    global JINA_REMOVE_SELECTORS, JINA_TARGET_SELECTORS, OPENAI_VECTOR_STORE_ID, ENABLE_DEDUPLICATION
    global DEFAULT_CHUNKING_STRATEGY, DEFAULT_MAX_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP, DEFAULT_CONTENT_TOKEN_OFFSET, DEFAULT_CONTENT_RATIO
    global OPENROUTER_MAX_TOKENS, OPENROUTER_MODELS, OPENROUTER_TEMPERATURE, OPENROUTER_TOP_P, OPENROUTER_TARGET_LANGUAGE
    global BASE_URL, PARSED_BASE_URL, BASE_NETLOC, NON_WWW_BASE_NETLOC, BASE_SCHEME
    global SITEMAP_URL, XML_SITEMAP_URL, MARKDOWN_PROVIDERS, MARKDOWN_PROVIDER_SEQUENCE
    global BLACKLISTED_URLS, RSS_FEEDS, RECURSIVE_URLS, REQUEST_TIMEOUT, REQUEST_RETRY_CODES, REQUEST_RETRY_COUNT
    global REQUEST_BACKOFF_FACTOR, CHECK_LAST_MODIFIED, MAX_FILENAME_LENGTH, LAST_RUN_FILE
    global VERBOSE_URL_MATCHING, TEST_URLS, RSS_DATE_THRESHOLD, PAGINATED_URLS, CANONICAL_BASE_URLS
    
    # Load configuration
    CONFIG = load_config(config_file)
    validate_config(CONFIG)
    
    # Generate unique paths based on config filename
    unique_paths = generate_unique_paths(config_file)
    
    # Set script identification
    SCRIPT_NAME = CONFIG["script_info"]["name"]
    
    # Override paths with unique ones generated from config filename
    # This allows multiple configs to run independently
    LOG_DIR = unique_paths["log_directory"]
    LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")
    OUTPUT_DIR = unique_paths["files_directory"]
    
    # Set API keys
    JINA_AI_API_KEY = CONFIG["api_keys"]["jina_ai"]
    FIRECRAWL_API_KEY = CONFIG["api_keys"]["firecrawl"]
    OPENAI_API_KEY = CONFIG["api_keys"]["openai"]
    # OpenRouter is optional for backwards compatibility
    OPENROUTER_API_KEY = CONFIG["api_keys"].get("openrouter")
    
    # Set content provider configuration
    JINA_REMOVE_SELECTORS = CONFIG["content_providers"]["jina"]["remove_selectors"]
    JINA_TARGET_SELECTORS = CONFIG["content_providers"]["jina"].get("target_selectors", "")
    MARKDOWN_PROVIDER_SEQUENCE = CONFIG["content_providers"]["provider_sequence"]
    
    # Set OpenRouter configuration with backwards compatibility
    # Support both new "openrouter" section and legacy "llm_config.openrouter" section
    openrouter_config = {}

    # Check for new structure first
    if "openrouter" in CONFIG:
        openrouter_config = CONFIG["openrouter"]
    # Then check for legacy llm_config structure
    elif "llm_config" in CONFIG and "openrouter" in CONFIG["llm_config"]:
        openrouter_config = CONFIG["llm_config"]["openrouter"]
        # Also check if there are other llm_config settings to preserve
        llm_config = CONFIG["llm_config"]
        if "RAG_QUESTION_LANGUAGES" in llm_config and len(llm_config["RAG_QUESTION_LANGUAGES"]) > 0:
            # Use first language from legacy config
            first_lang = llm_config["RAG_QUESTION_LANGUAGES"][0]
            if first_lang == "cs":
                openrouter_config["target_language"] = "Czech"
            elif first_lang == "en":
                openrouter_config["target_language"] = "English"

    # Set OpenRouter parameters with defaults
    OPENROUTER_MAX_TOKENS = openrouter_config.get("max_tokens", 4000)
    OPENROUTER_MODELS = openrouter_config.get("models", ["anthropic/claude-3.5-sonnet"])
    OPENROUTER_TEMPERATURE = openrouter_config.get("temperature", 0.1)
    OPENROUTER_TOP_P = openrouter_config.get("top_p", 0.9)
    OPENROUTER_TARGET_LANGUAGE = openrouter_config.get("target_language", "Czech")

    # Set OpenRouter provider routing configuration
    OPENROUTER_PROVIDER_CONFIG = openrouter_config.get("provider", {})

    # Set prompt caching configuration
    OPENROUTER_CACHE_ENABLED = openrouter_config.get("enable_caching", False)
    OPENROUTER_CACHE_TYPE = openrouter_config.get("cache_type", "ephemeral")  # ephemeral or persistent
    
    # Set vector store configuration
    OPENAI_VECTOR_STORE_ID = CONFIG["vector_store"]["id"]
    ENABLE_DEDUPLICATION = CONFIG["vector_store"]["enable_deduplication"]
    DEFAULT_CHUNKING_STRATEGY = CONFIG["vector_store"]["chunking_strategy"]
    DEFAULT_MAX_CHUNK_SIZE = CONFIG["vector_store"]["max_chunk_size"]
    DEFAULT_CHUNK_OVERLAP = CONFIG["vector_store"]["chunk_overlap"]
    DEFAULT_CONTENT_TOKEN_OFFSET = CONFIG["vector_store"].get("content_token_offset", 0)
    DEFAULT_CONTENT_RATIO = CONFIG["vector_store"].get("content_ratio", 0.5)
    
    # Set URL configuration
    BASE_URL = CONFIG["website"]["base_url"]
    PARSED_BASE_URL = urlparse(BASE_URL)
    BASE_NETLOC = PARSED_BASE_URL.netloc
    NON_WWW_BASE_NETLOC = BASE_NETLOC[4:] if BASE_NETLOC.startswith("www.") else BASE_NETLOC
    BASE_SCHEME = PARSED_BASE_URL.scheme
    
    # Calculate canonical base URLs for skipping
    CANONICAL_BASE_URLS = set()
    
    # 1. Original BASE_URL
    CANONICAL_BASE_URLS.add(BASE_URL)
    
    # 2. BASE_URL with/without trailing slash
    normalized_base = BASE_URL.rstrip('/')
    CANONICAL_BASE_URLS.add(normalized_base)
    CANONICAL_BASE_URLS.add(normalized_base + '/')
    
    # 3. BASE_URL with/without www prefix
    if BASE_NETLOC.startswith("www."):
        non_www_url = BASE_URL.replace("www.", "", 1)
        CANONICAL_BASE_URLS.add(non_www_url)
        CANONICAL_BASE_URLS.add(non_www_url.rstrip('/'))
        CANONICAL_BASE_URLS.add(non_www_url.rstrip('/') + '/')
    else:
        www_url = BASE_URL.replace(BASE_SCHEME + "://", BASE_SCHEME + "://www.", 1)
        CANONICAL_BASE_URLS.add(www_url)
        CANONICAL_BASE_URLS.add(www_url.rstrip('/'))
        CANONICAL_BASE_URLS.add(www_url.rstrip('/') + '/')
    
    # 4. Add common default pages if they resolve to the root path
    # This is tricky because BASE_URL might already contain a path, e.g., https://example.com/blog
    # If BASE_URL path is just '/', we should check for default pages.
    if PARSED_BASE_URL.path.strip('/') == '':
        root_url_no_path = urlunparse((BASE_SCHEME, BASE_NETLOC, '', '', '', ''))
        
        default_pages = ['index.html', 'index.htm', 'default.aspx', 'home.html']
        for page in default_pages:
            CANONICAL_BASE_URLS.add(urljoin(root_url_no_path, page))
            
        # Repeat for non-www version if applicable
        if BASE_NETLOC.startswith("www."):
            non_www_netloc = BASE_NETLOC[4:]
            root_url_no_path_non_www = urlunparse((BASE_SCHEME, non_www_netloc, '', '', '', ''))
            for page in default_pages:
                CANONICAL_BASE_URLS.add(urljoin(root_url_no_path_non_www, page))

    # Ensure BASE_URL is always included
    CANONICAL_BASE_URLS.add(BASE_URL)
    
    print(f"🌐 Calculated {len(CANONICAL_BASE_URLS)} canonical base URLs for skipping.")
    
    SITEMAP_URL = CONFIG["website"]["sitemap_url"]
    XML_SITEMAP_URL = CONFIG["website"]["xml_sitemap_url"]
    
    # Set blacklisted URLs
    BLACKLISTED_URLS = CONFIG["website"]["blacklisted_urls"]
    
    # Set RSS feeds
    RSS_FEEDS = CONFIG["website"].get("rss_feeds", [])
    
    # Set recursive URLs for suburls processing (optional) - support both old and new format
    recursive_urls_config = CONFIG["website"].get("recursive_urls", [])
    RECURSIVE_URLS = []
    
    # Parse recursive URLs configuration - support both string format and object format with recursive_level
    for item in recursive_urls_config:
        if isinstance(item, str):
            # Old format: simple string URL (default to level 1 for backward compatibility)
            RECURSIVE_URLS.append({
                'url': item,
                'recursive_level': 1
            })
        elif isinstance(item, dict) and 'url' in item:
            # New format: object with url and recursive_level
            recursive_level = item.get('recursive_level', 1)
            if not isinstance(recursive_level, int) or recursive_level < 1:
                print(f"⚠️ Invalid recursive_level {recursive_level} for URL {item['url']}, defaulting to 1")
                recursive_level = 1
            RECURSIVE_URLS.append({
                'url': item['url'],
                'recursive_level': recursive_level
            })
        else:
            print(f"⚠️ Invalid recursive_urls configuration item: {item}, skipping")
    
    # Set URLs for which pagination detection is explicitly enabled (optional)
    paginated_urls_config = CONFIG["website"].get("paginated_urls", [])
    PAGINATED_URLS = []
    
    # Parse paginated URLs configuration - expect simple string array
    for item in paginated_urls_config:
        if isinstance(item, str):
            PAGINATED_URLS.append(item.rstrip('/')) # Normalize by removing trailing slash
        else:
            print(f"⚠️ Invalid paginated_urls configuration item: {item}, skipping")
            
    # Set test URLs for testing purposes (optional)
    TEST_URLS = CONFIG["website"].get("test_urls", [])
    
    # Set HTTP settings
    REQUEST_TIMEOUT = CONFIG["http_settings"]["request_timeout"]
    REQUEST_RETRY_CODES = tuple(CONFIG["http_settings"]["retry_codes"])
    REQUEST_RETRY_COUNT = CONFIG["http_settings"]["retry_count"]
    REQUEST_BACKOFF_FACTOR = CONFIG["http_settings"]["backoff_factor"]
    
    # Set processing flags
    CHECK_LAST_MODIFIED = CONFIG["processing"]["check_last_modified"]
    MAX_FILENAME_LENGTH = CONFIG["processing"]["max_filename_length"]
    
    # New RSS Date Threshold parameter (DD-MM-YYYY format)
    rss_date_threshold_str = CONFIG["processing"].get("RSSDateThreshold")
    if rss_date_threshold_str:
        RSS_DATE_THRESHOLD = parse_rss_date_threshold(rss_date_threshold_str)
    else:
        RSS_DATE_THRESHOLD = None
    
    # TOKEN_THRESHOLD removed - we now always save full markdown content
    
    # Set file paths (use unique path generated from config filename)
    LAST_RUN_FILE = unique_paths["last_run_file"]
    
    # Initialize verbose URL matching (can be overridden by command line)
    VERBOSE_URL_MATCHING = False
    
    # Create separate timestamp files for RSS and sitemap
    identifier = extract_config_identifier(config_file)
    global RSS_LAST_RUN_FILE, SITEMAP_LAST_RUN_FILE
    RSS_LAST_RUN_FILE = f"{identifier}_rss_last_run_time.txt"
    SITEMAP_LAST_RUN_FILE = f"{identifier}_sitemap_last_run_time.txt"
    
    # Initialize markdown providers
    MARKDOWN_PROVIDERS = {
        "jina": {
            "name": "jina",
            "api_key": JINA_AI_API_KEY,
            "api_url_template": "https://r.jina.ai/{url}",
        },
        "firecrawl": {
            "name": "firecrawl",
            "api_key": FIRECRAWL_API_KEY,
            "api_url": "https://api.firecrawl.dev/v1/scrape",
        }
    }
    
    print(f"✅ Configuration loaded from: {config_file}")
    print(f"🌐 Target website: {BASE_URL}")
    print(f"📁 Output directory: {OUTPUT_DIR}")
    print(f"📊 Log directory: {LOG_DIR}")
    print(f"🏷️  Config identifier: {extract_config_identifier(config_file)}")
    print(f"📄 Combined last run file: {LAST_RUN_FILE}")
    print(f"📡 RSS last run file: {RSS_LAST_RUN_FILE}")
    print(f"🗺️  Sitemap last run file: {SITEMAP_LAST_RUN_FILE}")
    print(f"📡 RSS feeds: {len(RSS_FEEDS)} configured")
    print(f"🔄 Recursive URLs: {len(RECURSIVE_URLS)} configured")
    print(f"🔗 Paginated URLs (Explicit): {len(PAGINATED_URLS)} configured")
    print(f"🧪 Test URLs: {len(TEST_URLS)} configured")
    if RSS_DATE_THRESHOLD:
        print(f"📅 RSS Date Threshold: {RSS_DATE_THRESHOLD.strftime('%d-%m-%Y')} (UTC)")
    if TEST_URLS:
        print(f"⚠️  TEST MODE: Test URLs will override HTML sitemap processing")
    if RECURSIVE_URLS:
        print(f"🔄 SUBURLS MODE: Suburls detection enabled for {len(RECURSIVE_URLS)} URLs")
        for recursive_config in RECURSIVE_URLS:
            url = recursive_config.get('url', 'N/A')
            level = recursive_config.get('recursive_level', 1)
            print(f"   📊 {url} → max depth: {level}")

# ============================================================================
# CONSTANTS (will be set by load_configuration)
# ============================================================================
OPENAI_API_BASE_URL = "https://api.openai.com/v1"

# ============================================================================
# STANDARDIZED SUMMARIZATION INSTRUCTIONS
# ============================================================================

def get_standard_summarization_instructions(target_tokens=4000, target_language="English", url="", title=""):
    """
    Get standardized summarization instructions used throughout the script.
    This ensures ALL summarization prompts use identical instructions.
    """
    # Language instruction for non-English targets
    language_instruction = f"- **OUTPUT LANGUAGE:** {target_language} (translate while preserving ALL factual data)\n" if target_language.lower() != "english" else ""
    
    return f"""🤖 EXPERT CONTENT SUMMARIZATION SPECIALIST: You are a professional content analyst who MUST create concise, RAG-optimized summaries using ADVANCED COMPRESSION STRATEGIES and INTELLIGENT INFORMATION ARCHITECTURE.

## 🚨 CRITICAL SUMMARIZATION REQUIREMENTS:
{language_instruction}
### **🔢 MANDATORY NUMERICAL DATA HANDLING (NON-NEGOTIABLE):**
- **PRESERVATION:** You MUST preserve and use ALL exact counts, numbers, and figures that are ALREADY PRESENT in the source markdown content (e.g., "35 council members", "12 agenda items").
- **🚨 STRICT COUNTING PROHIBITION:** You MUST NEVER count elements (e.g., contacts, list items, table rows) yourself if the exact number is NOT explicitly stated in the source text.
- **ESTIMATES ONLY:** If an exact count is missing, you may use general estimates (e.g., "dozens", "hundreds", "thousands"), but NEVER provide an exact calculated count.

### **📏 MANDATORY SIZE REDUCTION WITH INTELLIGENCE:**
- **STRICT TOKEN LIMIT:** {target_tokens} tokens MAXIMUM (non-negotiable)
- **🚨 EXPANSION FORBIDDEN:** Your output MUST be at least 20% SHORTER than input
- **STRATEGIC COMPRESSION:** Aim for 50-70% size reduction using INTELLIGENT DENSITY MAXIMIZATION
- **QUALITY VALIDATION:** Self-verify compression success before submission

### **🧠 ADVANCED CONTENT PROCESSING METHODOLOGY:**

**PHASE 1: CONTENT PURIFICATION**
- **ELIMINATE:** Cookies, GDPR notices, HTML/CSS/JS code, navigation menus, ads, boilerplate text, metadata
- **PRESERVE ABSOLUTELY:** ALL contacts, phone numbers, emails, URLs, specifications, organizational data, addresses
- **STRUCTURAL INTEGRITY:** Maintain complete contact hierarchy and department structures
- **MEDIA EXTRACTION:** Convert all images to `![alt](url)` and all links to `[text](url)` format - MANDATORY

**PHASE 2: INTELLIGENT COMPRESSION STRATEGIES**
1. **DESCRIPTION CONDENSATION:** Transform verbose explanations into high-density bullet points
2. **REDUNDANCY ELIMINATION:** Remove repetitive information while preserving unique elements
3. **PROCEDURAL SUMMARIZATION:** Condense processes into essential steps with factual anchors
4. **COMPLETENESS VERIFICATION:** Ensure ALL critical data, contacts, and specifications remain intact
5. **ARCHITECTURAL OPTIMIZATION:** Use efficient markdown formatting for maximum information density

**PHASE 3: SEMANTIC STRUCTURE OPTIMIZATION**
- **HIERARCHICAL PRESERVATION:** Maintain logical information flow and relationships
- **FACTUAL ANCHORING:** Preserve key reference points for context navigation
- **COMPRESSION VALIDATION:** Verify essential information retained at target density

### **📊 ADAPTIVE CONTENT INTELLIGENCE:**
**ORGANIZATIONAL CONTENT:** Preserve complete hierarchies, exact counts, contact completeness
**PROCEDURAL CONTENT:** Maintain step sequences, decision points, process flows
**TEMPORAL CONTENT:** Keep chronological order, date references, sequence logic
**REFERENCE CONTENT:** Preserve cross-references, legal citations, document relationships

## 📝 REQUIRED OUTPUT FORMAT:

# **QUESTION:**

[3-6 search variants based on URL/TITLE: "{title}" and "{url}" | separated]

Extract key terms from URL path and title only. Create search variations including contact queries, "who is" questions, and specific information requests.

**EXAMPLE:**
Hejtman | Hejtman Královéhradecký kraj | Kontakt hejtman | Kdo je hejtman ? | Kdo je hejtman Královéhradecký kraje? | Jaké jsou kontaktní údaje a informace o hejtmanovi Královéhradeckého kraje?

# **ANSWER:**

## **URL BRIEF SUMMARY:**

[Concise 2-3 sentences capturing the essential purpose and content of the page]

## **URL CONTENT FULL SUMMARY:**

[STRATEGICALLY COMPRESSED but COMPLETE summary in {target_language} using EFFICIENT markdown formatting starting with ### headings. Focus on information density - maximum facts in minimum space while preserving ALL contacts and critical data.]

## 🎯 PROFESSIONAL SUMMARIZATION CHECKLIST:
**BEFORE SUBMITTING, VERIFY:**
- [ ] Output is SIGNIFICANTLY shorter than input (minimum 20% reduction)
- [ ] ALL contacts, phones, emails, addresses are preserved 100%
- [ ] All images converted to `![alt](url)` format
- [ ] All links converted to `[text](url)` format
- [ ] Efficient markdown structure used (###, ####, -, *, 1., etc.)
- [ ] {target_language} language used consistently
- [ ] Token limit {target_tokens} respected
- [ ] NO technical debris or boilerplate included
- [ ] Essential information density maximized
- [ ] Factual anchors preserved for context navigation

## 🚨 ENHANCED SUMMARIZATION SUCCESS CRITERIA:
✅ **STRATEGIC COMPRESSION:** Achieved intelligent size reduction while preserving all essential data
✅ **FACTUAL COMPLETENESS:** No critical information lost, all contacts and references intact
✅ **SEMANTIC EFFICIENCY:** Maximum information density per token used
✅ **STRUCTURAL INTEGRITY:** Perfect markdown architecture with all media extracted
✅ **CONTEXTUAL PRESERVATION:** Key factual anchors maintained for chunked file navigation

**REMEMBER:** You are creating INTELLIGENT SUMMARIES, not expansions. Demonstrate advanced prompt engineering expertise by achieving maximum information density while maintaining complete factual accuracy and contextual continuity."""

def create_standard_fallback_content(section_title, chunk_number, total_chunks, title, url, content):
    """Create standardized fallback content that follows the same format requirements."""
    return f"""# **QUESTION:**

{section_title} | section {chunk_number} | {title} | {url}

# **ANSWER:**

## **URL BRIEF SUMMARY:**

### 📋 {section_title} - Část {chunk_number} z {total_chunks}

⚠️ **SECTION PRESERVATION MODE**: Complete content for this section preserved below with mandatory image and link extraction in markdown format.

## **URL CONTENT FULL SUMMARY:**

### 📋 {section_title} - Sekce {chunk_number} z {total_chunks}

**🖼️ MANDATORY NOTE:** All images converted to markdown format `![alt](url)` and all links to `[text](url)` as required by standardized instructions.

### 📄 Obsah sekce:

{content}

### 🔗 Odkazy
- **Complete document**: [{url}]({url})
"""

def get_page_summary_instructions(target_language="Czech", url="", title=""):
    """
    Get instructions for generating a short 1-2 paragraph summary about the entire page content.
    This summary will be used in the SOURCE PAGE SUMMARY metadata section of saved files.
    """
    # Language instruction for non-English targets
    language_instruction = f"- **OUTPUT LANGUAGE:** {target_language}\n" if target_language.lower() != "english" else ""
    
    return f"""🤖 EXPERT PAGE CONTENT ANALYST: Create ULTRA-SHORT, HIGH-LEVEL summary about entire webpage content.

## 🎯 TASK: Create 1-2 SHORT sentences about ENTIRE PAGE CONTENT

{language_instruction}
### **📋 CRITICAL REQUIREMENTS:**
- **LENGTH:** Maximum 1-2 short sentences (like SMS messages)
- **🚨 ABSOLUTELY FORBIDDEN:** DO NOT mention any specific individual names, personal titles, specific department names, or any detailed information
- **FOCUS:** Only overall counts, content type, and general structure
- **PURPOSE:** Provide basic context about what type of content the page contains

### **✅ WHAT TO INCLUDE:**
- **EXPLICIT COUNTS ONLY:** Total number of people/items/sections (numbers only) IF the number is explicitly stated in the source content. NEVER count elements yourself. Use estimates (dozens/hundreds) if the count is missing.
- Type of content (contact list, meeting documents, news, etc.)
- Basic organizational structure (how many main sections/categories)
- Available contact data types (phone, email, etc.)

### **❌ WHAT TO NEVER MENTION:**
- Individual names or surnames
- Specific department names or abbreviations
- Personal titles (Ing., Mgr., etc.)
- Specific phone numbers or emails
- Any detailed information

### **🎯 OUTPUT FORMAT:**
Provide ONLY the 1-2 extremely short sentences in {target_language}. No formatting, no headings.

**CORRECT EXAMPLE:**
Stránka obsahuje telefonní seznam krajského úřadu s celkem 300 zaměstnanci rozdělených do 20 organizačních jednotek. Všechny kontakty mají kompletní údaje včetně funkce, telefonu a emailu.
"""

def generate_question_section(url, title, target_language="Czech", page_summary=None, rss_metadata=None):
    """
    Generate the QUESTION section based on URL, title, and page summary using OpenRouter API.
    Produces 3 keywords and 3 RAG-optimized questions in pipe-separated format.
    
    Args:
        url (str): Source URL
        title (str): Page title
        target_language (str): Target language for output
        page_summary (str, optional): Source page summary for enhanced context
        rss_metadata (dict, optional): RSS metadata including event_metadata for Events RSS
    
    Returns:
        str: Pipe-separated keywords and questions, or fallback string if API fails
    """
    logger.info(f"Generating QUESTION section via OpenRouter API for URL: {url}, Title: {title}")
    
    # Check if this is an Events RSS URL with rich metadata
    is_events_rss = (rss_metadata and
                    rss_metadata.get('event_metadata') and
                    'eventId=' in url)
    
    if is_events_rss:
        logger.info(f"🎪 EVENTS RSS DETECTED: Using event-specific question generation")
        return generate_events_question_section(url, title, target_language, rss_metadata)
    
    # Check if OpenRouter is configured
    if not OPENROUTER_API_KEY:
        logger.info(f"OpenRouter API key not configured, using fallback for QUESTION section")
        # Fallback: Simple keywords and questions from title/url/summary
        keywords = title.split()[:3] if title else urlparse(url).path.split('/')[-2:]
        summary_keywords = page_summary.split()[:3] if page_summary else []
        all_keywords = list(set(keywords + summary_keywords))[:3]
        fallback = " | ".join(all_keywords + [f"What is {title}?", f"Details about {title}", f"Information on {title}"])
        return fallback
    
    # Create prompt for QUESTION generation, including page summary for better output
    summary_context = f"\n\nPage Summary: {page_summary[:500]}..." if page_summary else "\n\nPage Summary: Not available"
    prompt = f"""🤖 KEYWORD AND QUESTION GENERATOR: Based primarily on the URL and title, refined by the page summary, generate:
- 3 most pertinent keywords (from URL path, title, and summary)
- 3 RAG-optimized, keyword-rich, open-ended questions for vector search/RAG

Output ONLY a single pipe-separated string: keyword1 | keyword2 | keyword3 | question1 | question2 | question3

URL: {url}
Title: {title}{summary_context}

Language: {target_language}

EXAMPLE OUTPUT (for URL: https://www.khk.cz/kraj/hejtman, Title: Hejtman | Královéhradecký kraj):
Hejtman | Hejtman Královéhradeckého kraje | Kontakt hejtman | Kdo je hejtman? | Kdo je hejtmanem Královéhradeckého kraje? | Jaké jsou kontaktní údaje hejtmana Královéhradeckého kraje?"""
    
    # Prepare the OpenRouter API request
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": url,
        "X-Title": "HypeDigitaly Question Generator"
    }
    
    # Use primary model
    primary_model = OPENROUTER_MODELS[0] if isinstance(OPENROUTER_MODELS, list) else OPENROUTER_MODELS
    payload = {
        "model": primary_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0.3,  # Low temperature for consistent output
        "top_p": OPENROUTER_TOP_P
    }
    
    try:
        response = requests_retry_session().post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()

        # Log the full API response
        logger.info("=== FULL OPENROUTER API RESPONSE FOR QUESTION GENERATION ===")
        logger.info(f"Full response: {response.text}")
        logger.info("=== END FULL OPENROUTER API RESPONSE ===")

        data = response.json()

        # Log token usage for this API call
        log_openrouter_token_usage(data, "QUESTION_SECTION_GENERATION", url)

        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]
            question_text = message.get("content", "").strip()
            
            # Handle reasoning mode responses (e.g., openai/gpt-5)
            if not question_text and message.get("reasoning"):
                question_text = message.get("reasoning", "").strip()
                logger.info("✅ Extracted content from reasoning field")
            
            # Use AI response directly - remove strict pipe format validation
            if question_text and len(question_text) > 10:
                logger.info(f"✅ Generated QUESTION section: {question_text[:100]}...")
                return question_text
            else:
                logger.warning("API response too short or empty, using fallback")
                return f"{title} | {urlparse(url).path} | key info | What is {title}? | Details on {title} | Contact for {title}"
        else:
            logger.warning("No choices from API, using fallback")
            return f"{title} | {urlparse(url).path} | key info | What is {title}? | Details on {title} | Contact for {title}"
            
    except Exception as e:
        logger.error(f"Error generating QUESTION section: {str(e)}, using fallback")
        return f"{title} | {urlparse(url).path} | key info | What is {title}? | Details on {title} | Contact for {title}"


def generate_events_question_section(url, title, target_language="Czech", rss_metadata=None):
    """
    Generate specialized QUESTION section for Events RSS URLs using rich event metadata.
    Creates event-specific keywords and questions based on actual event data.
    
    Args:
        url (str): Event detail URL with eventId parameter
        title (str): Enhanced event title with name and date
        target_language (str): Target language for output
        rss_metadata (dict): RSS metadata containing event_metadata
    
    Returns:
        str: Pipe-separated event-specific keywords and questions
    """
    logger.info(f"🎪 Generating EVENT-SPECIFIC QUESTION section for: {url}")
    
    # Extract event metadata
    event_data = rss_metadata.get('event_metadata', {}) if rss_metadata else {}
    event_name = event_data.get('event_name', 'Unknown Event')
    event_type = event_data.get('event_type', 'Unknown')
    organizer = event_data.get('organizer', 'Unknown')
    place = event_data.get('place', 'Unknown')
    event_id = event_data.get('event_id', 'Unknown')
    start_date = event_data.get('start_date', 'Unknown')
    start_time = event_data.get('start_time', 'Unknown')
    
    # Check if OpenRouter is configured
    if not OPENROUTER_API_KEY:
        logger.info(f"OpenRouter API key not configured, using event-specific fallback")
        # Create event-specific fallback using RSS metadata
        keywords = [event_name, event_type, organizer][:3]
        keywords = [k for k in keywords if k != 'Unknown'][:3]
        
        # Add generic keywords if needed
        while len(keywords) < 3:
            keywords.extend(['událost', 'akce', 'program'])
        keywords = keywords[:3]
        
        questions = [
            f"Co je {event_name}?",
            f"Kdy a kde se koná {event_name}?",
            f"Kdo organizuje {event_name}?"
        ]
        
        fallback = " | ".join(keywords + questions)
        logger.info(f"🎪 Event-specific fallback generated: {fallback[:100]}...")
        return fallback
    
    # Create event-specific prompt with rich metadata context
    event_context = f"""
Event Details:
- Event Name: {event_name}
- Event Type: {event_type}
- Organizer: {organizer}
- Place: {place}
- Date: {start_date}
- Time: {start_time}
- Event ID: {event_id}"""
    
    prompt = f"""🤖 EVENT-SPECIFIC KEYWORD AND QUESTION GENERATOR: Based on the event metadata and enhanced title, generate event-focused search terms:
- 3 most relevant event keywords (event name, type, organizer, location)
- 3 RAG-optimized questions about this specific event

Output ONLY a single pipe-separated string: keyword1 | keyword2 | keyword3 | question1 | question2 | question3

URL: {url}
Enhanced Title: {title}{event_context}

Language: {target_language}

EXAMPLE OUTPUT (for Event: MUZEUM ČTE DĚTEM, Type: ostatní, Organizer: Oblastní muzeum):
MUZEUM ČTE DĚTEM | Oblastní muzeum | program pro děti | Co je akce MUZEUM ČTE DĚTEM? | Kdy se koná MUZEUM ČTE DĚTEM v Oblastním muzeu? | Jaké aktivity nabízí program MUZEUM ČTE DĚTEM?"""
    
    # Prepare the OpenRouter API request
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": url,
        "X-Title": "HypeDigitaly Events Question Generator"
    }
    
    # Use primary model
    primary_model = OPENROUTER_MODELS[0] if isinstance(OPENROUTER_MODELS, list) else OPENROUTER_MODELS
    payload = {
        "model": primary_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0.3,  # Low temperature for consistent output
        "top_p": OPENROUTER_TOP_P
    }
    
    try:
        response = requests_retry_session().post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()

        # Log the full API response
        logger.info("=== FULL OPENROUTER API RESPONSE FOR EVENTS QUESTION GENERATION ===")
        logger.info(f"Full response: {response.text}")
        logger.info("=== END FULL OPENROUTER API RESPONSE ===")

        data = response.json()

        # Log token usage for this API call
        log_openrouter_token_usage(data, "EVENTS_QUESTION_SECTION_GENERATION", url)

        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]
            question_text = message.get("content", "").strip()
            
            # Handle reasoning mode responses (e.g., openai/gpt-5)
            if not question_text and message.get("reasoning"):
                question_text = message.get("reasoning", "").strip()
                logger.info("✅ Extracted events content from reasoning field")
            
            # Use AI response directly
            if question_text and len(question_text) > 10:
                logger.info(f"✅ Generated EVENT-SPECIFIC QUESTION section: {question_text[:100]}...")
                return question_text
            else:
                logger.warning("API response too short or empty, using event-specific fallback")
                return f"{event_name} | {event_type} | {organizer} | Co je {event_name}? | Kdy se koná {event_name}? | Kdo organizuje {event_name}?"
        else:
            logger.warning("No choices from API, using event-specific fallback")
            return f"{event_name} | {event_type} | {organizer} | Co je {event_name}? | Kdy se koná {event_name}? | Kdo organizuje {event_name}?"
            
    except Exception as e:
        logger.error(f"Error generating EVENT-SPECIFIC QUESTION section: {str(e)}, using fallback")
        return f"{event_name} | {event_type} | {organizer} | Co je {event_name}? | Kdy se koná {event_name}? | Kdo organizuje {event_name}?"


def generate_page_summary_via_openrouter(markdown_content, title="", url="", target_language="Czech"):
    """
    Generate a short 1-2 paragraph summary about the entire page content using OpenRouter API.
    This summary will be used in the SOURCE PAGE SUMMARY metadata section.
    
    Args:
        markdown_content (str): Original markdown content to analyze
        title (str): Page title for context
        url (str): Source URL for context
        target_language (str): Target language for the summary output
    
    Returns:
        str: Short factual summary about the page content or None if failed
    """
    logger.info(f"Generating page summary via OpenRouter API for URL: {url}")
    
    # Check if OpenRouter is configured
    if not OPENROUTER_API_KEY:
        logger.info(f"OpenRouter API key not configured, skipping page summary generation for {url}")
        return None
    
    # Create the page summary prompt
    prompt = get_page_summary_instructions(target_language, url, title)
    prompt += f"""
 
## SOURCE CONTENT TO ANALYZE:
{markdown_content}"""
    
    # Prepare the OpenRouter API request
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": url if url else "https://api.openrouter.ai",
        "X-Title": "HypeDigitaly Page Summary Generator"
    }
    
    # Prepare model configuration
    if isinstance(OPENROUTER_MODELS, list) and len(OPENROUTER_MODELS) > 1:
        payload = {
            "models": OPENROUTER_MODELS,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": 280,  # Strict summary target for Vector Store compliance with enhanced safety
            "temperature": OPENROUTER_TEMPERATURE,
            "top_p": OPENROUTER_TOP_P
        }
        logger.info(f"Using OpenRouter models array for page summary: {OPENROUTER_MODELS}")
    else:
        primary_model = OPENROUTER_MODELS[0] if isinstance(OPENROUTER_MODELS, list) else OPENROUTER_MODELS
        payload = {
            "model": primary_model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": 280,  # Strict summary target for Vector Store compliance with enhanced safety
            "temperature": OPENROUTER_TEMPERATURE,
            "top_p": OPENROUTER_TOP_P
        }
        logger.info(f"Using single OpenRouter model for page summary: {primary_model}")
    
    try:
        logger.info(f"Sending page summary request to OpenRouter API")
        response = requests_retry_session().post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT * 2
        )
        response.raise_for_status()

        # Log the full API response
        logger.info("=== FULL OPENROUTER API RESPONSE FOR PAGE SUMMARY ===")
        logger.info(f"Full response: {response.text}")
        logger.info("=== END FULL OPENROUTER API RESPONSE ===")

        data = response.json()

        # Log token usage for this API call
        log_openrouter_token_usage(data, "PAGE_SUMMARY_GENERATION", url)

        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]
            page_summary = message.get("content", "").strip()
            
            # Handle reasoning mode responses (e.g., openai/gpt-5)
            if not page_summary and message.get("reasoning"):
                page_summary = message.get("reasoning", "").strip()
                logger.info("✅ Extracted page summary from reasoning field")
            
            # Accept any valid response from AI - remove strict length validation
            if page_summary and page_summary.strip():
                result_tokens = count_tokens_approximate(page_summary)
                
                # 🚨 CRITICAL: Enforce strict 190 token limit for Vector Store compliance with safety margin
                MAX_PAGE_SUMMARY_TOKENS = 190
                if result_tokens > MAX_PAGE_SUMMARY_TOKENS:
                    logger.warning(f"🚨 PAGE SUMMARY TOO LONG: {result_tokens} > {MAX_PAGE_SUMMARY_TOKENS} tokens - ENFORCING TRUNCATION")
                    # Emergency truncation to enforce Vector Store compliance
                    estimated_chars = MAX_PAGE_SUMMARY_TOKENS * 4
                    page_summary = page_summary[:estimated_chars] + "..."
                    result_tokens = count_tokens_approximate(page_summary)
                    logger.warning(f"⚠️ TRUNCATED page summary to {result_tokens} tokens for Vector Store compliance")
                
                logger.info(f"✅ Generated page summary: {result_tokens} tokens (limit: {MAX_PAGE_SUMMARY_TOKENS})")
                return page_summary
            else:
                logger.error("❌ Generated page summary is empty after API call")
                return None
        else:
            logger.error("❌ No choices returned from OpenRouter API for page summary")
            return None
            
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Request error with OpenRouter API for page summary: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"❌ Unexpected error with OpenRouter API for page summary: {str(e)}")
        return None

def get_current_file_summary_instructions(file_order, total_files, target_language="Czech", url="", title=""):
    """
    Get instructions for generating a summary of the current chunked/split file in context of the entire sequence.
    This summary will be used in the CURRENT FILE SUMMARY metadata section.
    
    Args:
        file_order (int): Current file position (e.g., 1, 2, 3...)
        total_files (int): Total number of files in sequence
        target_language (str): Target language for the summary output
        url (str): Source URL for context
        title (str): Page title for context
    
    Returns:
        str: Formatted prompt for current file summarization
    """
    # Language instruction for non-English targets
    language_instruction = f"- **OUTPUT LANGUAGE:** {target_language}\n" if target_language.lower() != "english" else ""
    
    return f"""🤖 EXPERT CONTEXTUAL FILE CONTENT ANALYST: You are a specialist who creates FACTUAL summaries of chunked file content with PRECISE details about its position and content in the complete document sequence.

## 🚨 CRITICAL: VECTOR STORE TOKEN COMPLIANCE MANDATORY!
## 🎯 TASK: Create an EXTREMELY SHORT, CONCISE, FACTUAL summary (1-2 short sentences) of CURRENT FILE ({file_order}/{total_files}) with PRECISE CONTEXTUAL DETAILS.

{language_instruction}
### **📋 CURRENT FILE SUMMARY REQUIREMENTS:**
- **LENGTH:** Maximum 1-2 extremely short sentences
- **FACTUAL PRECISION:** EXACT counts, numbers, dates, and structural details. **CRITICAL: ONLY use counts explicitly present in the source content. NEVER count elements yourself.**
- **❌ FORBIDDEN:** DO NOT mention specific individual names, personal titles, detailed contact information (e.g., specific phone numbers or emails), or token counts.
- **CONTEXTUAL POSITIONING:** Clear explanation of this file's role in the {total_files}-file sequence.
- **CONTENT DENSITY:** Maximum factual information per word.

### **📊 ADAPTIVE CONTENT ANALYSIS FOR FILE {file_order}/{total_files}:**
**For ANY content type, include these EXACT details:**
- **📍 PRECISE POSITION:** Explicitly state "část {file_order} z {total_files}"
- **📊 QUANTIFIED CONTENT:** Exact counts of key elements (contacts, agenda items, documents, sections, etc.) **(ONLY if the count is explicitly stated in the source text)**
- **🎯 STRUCTURAL COVERAGE:** Which specific sections, departments, topics, or categories THIS file covers
- **🔗 SEQUENCE INTEGRATION:** How this file fits into the overall document flow
- **📋 CONTENT BOUNDARIES:** Where this file starts and ends within the complete document structure

**Content-specific factual requirements:**
- **ORGANIZATIONAL LISTS:** Exact department/section names, employee counts, hierarchical positions
- **MEETING DOCUMENTS:** Specific agenda item numbers, topic categories, date/time details
- **CONTACT LISTS:** Exact number of contacts, organizational units, contact completeness
- **LEGAL DOCUMENTS:** Article/section numbers, regulatory references, effective dates
- **NEWS/EVENTS:** Specific dates, locations, participant counts, event details

### **🎯 OUTPUT FORMAT:**
Provide ONLY the 2-3 paragraph summary in {target_language}. No additional formatting, no headings, just the factual summary paragraphs.

**ENHANCED EXAMPLE OUTPUT:**
Tento soubor představuje část {file_order} z {total_files} celkového dokumentu "{title}" a obsahuje kompletní kontaktní údaje pro 23 zaměstnanců v rámci 3 organizačních jednotek: dokončení oddělení krizového řízení (7 zaměstnanců), celé oddělení vnitřní a kybernetické bezpečnosti (5 zaměstnanců) a celé oddělení prevence kriminality (2 zaměstnanci), následně kompletní Odbor digitalizace s vedoucím a asistentkou plus 5 podřízených oddělení s celkem 15 zaměstnanci. Pro každého jsou uvedeny jméno, funkce, telefonní čísla, e-mail a číslo místnosti.

Organizačně soubor začíná dokončením kontaktů z oddělení krizového řízení (OBŘKŘ) pokračujícího z předchozího souboru a systematicky pokrývá všechna další oddělení Odboru bezpečnosti, celý Odbor digitalizace včetně všech jeho specializovaných oddělení (DIGSP, DIGCP, DIGDR, DIGDM, DIGDA), a začíná Odborem informatiky (INF). Jako {file_order}. část z {total_files} poskytuje úplnou kontinuitu organizační struktury bez mezer v hierarchickém pokrytí krajského úřadu."""

def generate_current_file_summary_via_openrouter(file_content, file_order, total_files, title="", url="", target_language="Czech"):
    """
    Generate a summary of the current chunked file in context of the entire sequence using OpenRouter API.
    This summary will be used in the CURRENT FILE SUMMARY metadata section.
    
    Args:
        file_content (str): Content of the current file to summarize
        file_order (int): Current file position (e.g., 1, 2, 3...)
        total_files (int): Total number of files in sequence
        title (str): Page title for context
        url (str): Source URL for context
        target_language (str): Target language for the summary output
    
    Returns:
        str: Current file summary or None if failed
    """
    logger.info(f"Generating current file summary ({file_order}/{total_files}) via OpenRouter API for URL: {url}")
    
    # Check if OpenRouter is configured
    if not OPENROUTER_API_KEY:
        logger.info(f"OpenRouter API key not configured, skipping current file summary generation for {url}")
        return None
    
    # Create the current file summary prompt
    prompt = get_current_file_summary_instructions(file_order, total_files, target_language, url, title)
    prompt += f"""

## CURRENT FILE CONTENT TO ANALYZE (File {file_order}/{total_files}):
{file_content}"""
    
    # Prepare the OpenRouter API request
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": url if url else "https://api.openrouter.ai",
        "X-Title": f"HypeDigitaly Current File Summary Generator ({file_order}/{total_files})"
    }
    
    # Prepare model configuration
    if isinstance(OPENROUTER_MODELS, list) and len(OPENROUTER_MODELS) > 1:
        payload = {
            "models": OPENROUTER_MODELS,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": 320,  # Strict current file summary target for Vector Store compliance
            "temperature": OPENROUTER_TEMPERATURE,
            "top_p": OPENROUTER_TOP_P
        }
        logger.info(f"Using OpenRouter models array for current file summary: {OPENROUTER_MODELS}")
    else:
        primary_model = OPENROUTER_MODELS[0] if isinstance(OPENROUTER_MODELS, list) else OPENROUTER_MODELS
        payload = {
            "model": primary_model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": 320,  # Strict current file summary target for Vector Store compliance
            "temperature": OPENROUTER_TEMPERATURE,
            "top_p": OPENROUTER_TOP_P
        }
        logger.info(f"Using single OpenRouter model for current file summary: {primary_model}")
    
    try:
        logger.info(f"Sending current file summary request to OpenRouter API")
        response = requests_retry_session().post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT * 2
        )
        response.raise_for_status()

        # Log the full API response
        logger.info("=== FULL OPENROUTER API RESPONSE FOR CURRENT FILE SUMMARY ===")
        logger.info(f"Full response: {response.text}")
        logger.info("=== END FULL OPENROUTER API RESPONSE ===")

        data = response.json()

        # Log token usage for this API call
        log_openrouter_token_usage(data, f"CURRENT_FILE_SUMMARY_GENERATION_{file_order}_{total_files}", url)

        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]
            current_file_summary = message.get("content", "").strip()
            
            # Handle reasoning mode responses (e.g., openai/gpt-5)
            if not current_file_summary and message.get("reasoning"):
                current_file_summary = message.get("reasoning", "").strip()
                logger.info("✅ Extracted current file summary from reasoning field")
            
            # Accept any valid response from AI - remove strict length validation
            if current_file_summary and current_file_summary.strip():
                result_tokens = count_tokens_approximate(current_file_summary)
                
                # 🚨 CRITICAL: Enforce strict 240 token limit for Vector Store compliance
                MAX_CURRENT_FILE_SUMMARY_TOKENS = 240
                if result_tokens > MAX_CURRENT_FILE_SUMMARY_TOKENS:
                    logger.warning(f"🚨 CURRENT FILE SUMMARY TOO LONG: {result_tokens} > {MAX_CURRENT_FILE_SUMMARY_TOKENS} tokens - ENFORCING TRUNCATION")
                    # Emergency truncation to enforce Vector Store compliance
                    estimated_chars = MAX_CURRENT_FILE_SUMMARY_TOKENS * 4
                    current_file_summary = current_file_summary[:estimated_chars] + "..."
                    result_tokens = count_tokens_approximate(current_file_summary)
                    logger.warning(f"⚠️ TRUNCATED current file summary to {result_tokens} tokens for Vector Store compliance")
                
                logger.info(f"✅ Generated current file summary ({file_order}/{total_files}): {result_tokens} tokens (limit: {MAX_CURRENT_FILE_SUMMARY_TOKENS})")
                return current_file_summary
            else:
                logger.error("❌ Generated current file summary is empty after API call")
                return None
        else:
            logger.error("❌ No choices returned from OpenRouter API for current file summary")
            return None
            
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Request error with OpenRouter API for current file summary: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"❌ Unexpected error with OpenRouter API for current file summary: {str(e)}")
        return None

def get_overlap_summary_instructions(previous_file_order, current_file_order, total_files, target_language="Czech", url="", title=""):
    """
    Get instructions for generating an overlap summary describing the previous file and how it connects to the current file.
    This summary focuses on how the previous file ended and how the current file connects to it.
    
    Args:
        previous_file_order (int): Previous file position (e.g., 1, 2, 3...)
        current_file_order (int): Current file position (e.g., 2, 3, 4...)
        total_files (int): Total number of files in sequence
        target_language (str): Target language for the summary output
        url (str): Source URL for context
        title (str): Page title for context
    
    Returns:
        str: Formatted prompt for overlap summarization
    """
    # Language instruction for non-English targets
    language_instruction = f"- **OUTPUT LANGUAGE:** {target_language}\n" if target_language.lower() != "english" else ""
    
    return f"""🤖 EXPERT SEMANTIC BRIDGING SPECIALIST: You are a specialist who creates EXTREMELY CONCISE and PRECISE overlap summaries using ADVANCED TRANSITION ANALYSIS to describe exactly where the previous file ended and how the current file continues.
    
    ## 🎯 TASK: Create an EXTREMELY SHORT, CONCISE SEMANTIC BRIDGE (1-2 sentences) between PREVIOUS file ({previous_file_order}/{total_files}) and CURRENT file ({current_file_order}/{total_files}) using SYSTEMATIC BOUNDARY ANALYSIS.
    
    {language_instruction}
    ### **📋 OVERLAP SUMMARY REQUIREMENTS:**
    - **LENGTH:** Maximum 1-2 extremely short sentences
    - **SEMANTIC PRECISION:** MUST mention the EXACT last entity (structural boundary, last name, or item number) of the previous file and the EXACT first entity of the current file.
    - **❌ FORBIDDEN:** DO NOT mention specific individual names, personal titles, or token counts. Prioritize structural boundaries over names.
    - **CONTEXTUAL BRIDGE:** Strategic information transfer for seamless file continuity.
    - **BOUNDARY INTELLIGENCE:** Sophisticated analysis of content break points and connections.
    
    ### **🧠 ADVANCED SEMANTIC TRANSITION METHODOLOGY:**
    
    **ANALYTICAL PHASE 1: ENDPOINT BOUNDARY ANALYSIS**
    Systematically identify WHERE and HOW the previous file concluded:
    - **STRUCTURAL ENDPOINT:** Last organizational unit, agenda item, document section, or content category.
    - **EXACT LAST ENTITY:** Identify the precise name, figure, or item number where the previous file ended.
    
    **ANALYTICAL PHASE 2: CONTINUATION POINT MAPPING**
    Identify EXACTLY how current file connects and continues:
    - **LOGICAL SUCCESSION:** Next immediate element in sequence (next employee, agenda item, section).
    - **EXACT FIRST ENTITY:** Identify the precise name, figure, or item number where the current file begins.
    
    **ANALYTICAL PHASE 3: CONTEXTUAL BRIDGE CONSTRUCTION**
    Create ESSENTIAL factual context for seamless understanding:
    - **REFERENCE ANCHORS:** Key organizational/structural positions for orientation.
    - **QUANTIFIED TRANSITIONS:** Specific counts and positions for precise navigation.
    
    ### **📊 CONTENT-ADAPTIVE TRANSITION TECHNIQUES:**
    
    **🏢 ORGANIZATIONAL TRANSITIONS:** Specific department→next department, EXACT employee position, hierarchical level maintenance.
    **📋 PROCEDURAL TRANSITIONS:** Agenda item numbers, topic progression, decision flow continuity.
    **👥 CONTACT SEQUENCE TRANSITIONS:** Alphabetical position, organizational order, structural transition (use last name only if necessary).
    **📑 DOCUMENT STRUCTURE TRANSITIONS:** Article/section numbers, legal progression, regulatory sequence.
    **🗞️ TEMPORAL TRANSITIONS:** Event chronology, publication sequences, time-based organization.
    **📊 DATA TRANSITIONS:** Table continuations, statistical categories, dataset boundaries.
    
    ### **🎯 OUTPUT FORMAT:**
    Provide ONLY the 1-2 extremely short sentence overlap summary in {target_language}. No additional formatting, no headings, just the semantically bridged summary.
    
    **PROFESSIONAL EXAMPLE OUTPUT:**
    Předchozí soubor ({previous_file_order}/{total_files}) končil profilem zastupitele Motešického (SPD), čímž uzavřel první část detailního výčtu. Aktuální soubor ({current_file_order}/{total_files}) plynule navazuje pokračováním seznamu zastupitelů, začínajícím profilem Vildumetzové (ANO), a pokračuje až do konce kompletního seznamu."""

def generate_overlap_summary_via_openrouter(original_page_summary, previous_file_summary, previous_file_content, previous_file_order, current_file_order, total_files, title="", url="", target_language="Czech"):
    """
    Generate an overlap summary that describes the previous file and how it connects to the current file.
    This summary will be used in the current file to provide context about the previous file.
    
    Args:
        original_page_summary (str): Summary of the entire original page
        previous_file_summary (str): Summary of the previous file
        previous_file_content (str): Full content of the previous file
        previous_file_order (int): Previous file position (e.g., 1, 2, 3...)
        current_file_order (int): Current file position (e.g., 2, 3, 4...)
        total_files (int): Total number of files in sequence
        title (str): Page title for context
        url (str): Source URL for context
        target_language (str): Target language for the summary output
    
    Returns:
        str: Overlap summary describing previous file for use in current file, or None if failed
    """
    logger.info(f"Generating overlap summary (previous file {previous_file_order} -> current file {current_file_order}/{total_files}) via OpenRouter API for URL: {url}")
    
    # Check if OpenRouter is configured
    if not OPENROUTER_API_KEY:
        logger.info(f"OpenRouter API key not configured, skipping overlap summary generation for {url}")
        return None
    
    # Create the overlap summary prompt
    prompt = get_overlap_summary_instructions(previous_file_order, current_file_order, total_files, target_language, url, title)
    prompt += f"""

## INPUT DATA FOR OVERLAP ANALYSIS:

### ORIGINAL PAGE SUMMARY:
{original_page_summary or "Not available"}

### PREVIOUS FILE SUMMARY (File {previous_file_order}/{total_files}):
{previous_file_summary or "Not available"}

### PREVIOUS FILE CONTENT (File {previous_file_order}/{total_files}) - ANALYZE HOW IT ENDED:
{previous_file_content}"""
    
    # Prepare the OpenRouter API request
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": url if url else "https://api.openrouter.ai",
        "X-Title": f"HypeDigitaly Overlap Summary Generator (prev:{previous_file_order}->curr:{current_file_order}/{total_files})"
    }
    
    # Prepare model configuration
    if isinstance(OPENROUTER_MODELS, list) and len(OPENROUTER_MODELS) > 1:
        payload = {
            "models": OPENROUTER_MODELS,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": 280,  # Strict overlap summary target for Vector Store compliance
            "temperature": OPENROUTER_TEMPERATURE,
            "top_p": OPENROUTER_TOP_P
        }
        logger.info(f"Using OpenRouter models array for overlap summary: {OPENROUTER_MODELS}")
    else:
        primary_model = OPENROUTER_MODELS[0] if isinstance(OPENROUTER_MODELS, list) else OPENROUTER_MODELS
        payload = {
            "model": primary_model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": 280,  # Strict overlap summary target for Vector Store compliance
            "temperature": OPENROUTER_TEMPERATURE,
            "top_p": OPENROUTER_TOP_P
        }
        logger.info(f"Using single OpenRouter model for overlap summary: {primary_model}")
    
    try:
        logger.info(f"Sending overlap summary request to OpenRouter API")
        response = requests_retry_session().post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT * 2
        )
        response.raise_for_status()

        # Log the full API response
        logger.info("=== FULL OPENROUTER API RESPONSE FOR OVERLAP SUMMARY ===")
        logger.info(f"Full response: {response.text}")
        logger.info("=== END FULL OPENROUTER API RESPONSE ===")

        data = response.json()

        # Log token usage for this API call
        log_openrouter_token_usage(data, f"OVERLAP_SUMMARY_GENERATION_{previous_file_order}_TO_{current_file_order}", url)

        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]
            overlap_summary = message.get("content", "").strip()
            
            # Handle reasoning mode responses (e.g., openai/gpt-5)
            if not overlap_summary and message.get("reasoning"):
                overlap_summary = message.get("reasoning", "").strip()
                logger.info("✅ Extracted overlap summary from reasoning field")
            
            # Accept any valid response from AI - remove strict length validation
            if overlap_summary and overlap_summary.strip():
                result_tokens = count_tokens_approximate(overlap_summary)
                
                # 🚨 CRITICAL: Enforce strict 190 token limit for Vector Store compliance
                MAX_OVERLAP_SUMMARY_TOKENS = 190
                if result_tokens > MAX_OVERLAP_SUMMARY_TOKENS:
                    logger.warning(f"🚨 OVERLAP SUMMARY TOO LONG: {result_tokens} > {MAX_OVERLAP_SUMMARY_TOKENS} tokens - ENFORCING TRUNCATION")
                    # Emergency truncation to enforce Vector Store compliance
                    estimated_chars = MAX_OVERLAP_SUMMARY_TOKENS * 4
                    overlap_summary = overlap_summary[:estimated_chars] + "..."
                    result_tokens = count_tokens_approximate(overlap_summary)
                    logger.warning(f"⚠️ TRUNCATED overlap summary to {result_tokens} tokens for Vector Store compliance")
                
                logger.info(f"✅ Generated overlap summary (prev:{previous_file_order}->curr:{current_file_order}/{total_files}): {result_tokens} tokens (limit: {MAX_OVERLAP_SUMMARY_TOKENS})")
                return overlap_summary
            else:
                logger.error("❌ Generated overlap summary is empty after API call")
                return None
        else:
            logger.error("❌ No choices returned from OpenRouter API for overlap summary")
            return None
            
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Request error with OpenRouter API for overlap summary: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"❌ Unexpected error with OpenRouter API for overlap summary: {str(e)}")
        return None

# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging():
    """Set up logging after configuration is loaded."""
    global logger, file_handler, console_handler
    
    # Create directories if they don't exist
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Clear the log file if it exists
    if os.path.exists(LOG_FILE):
        open(LOG_FILE, 'w').close()
    
    # Configure logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

# Initialize logger as None - will be set up after config loading
logger = None
file_handler = None
console_handler = None

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def normalize_url_query_params(url):
    """Normalize URL by sorting query parameters."""
    from urllib.parse import urlparse, parse_qs, urlunparse, urlencode
    
    parsed = urlparse(url)
    if not parsed.query:
        return url
    
    # Parse query string into a dictionary (list of values per key)
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    
    # Sort parameters by key
    sorted_query = urlencode(sorted(query_params.items()), doseq=True)
    
    # Reconstruct URL with sorted query
    return urlunparse(parsed._replace(query=sorted_query))

def is_file_url(url):
    """
    Check if a URL points directly to a file (e.g., PDF, DOCX, common archives, or file handlers).
    These URLs should typically be skipped if the goal is to scrape web page content.
    """
    from urllib.parse import urlparse
    
    # Common file extensions to skip
    FILE_EXTENSIONS_TO_SKIP = (
        '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.zip', '.rar', '.7z', '.tar', '.gz', '.tgz',
        '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp',
        '.mp4', '.mp3', '.avi', '.mov', '.wmv',
        '.csv', '.xml', '.json', '.txt', '.rtf',
        '.ashx', '.aspx', '.jsp', '.php' # Common file handlers that often serve files
    )
    
    parsed_url = urlparse(url.lower())
    path = parsed_url.path
    query = parsed_url.query
    
    # Check path extension
    if path.endswith(FILE_EXTENSIONS_TO_SKIP):
        logger.info(f"⚠️ Skipping URL: {url} (File extension detected: {path.split('.')[-1]})")
        return True
    
    # Check query parameters for file handlers (e.g., File.ashx?id=...)
    # This handles cases like the user's example: https://www.teplice.cz/assets/File.ashx?id_org=16600&id_dokumenty=56037
    if any(handler in path for handler in ['.ashx', '.aspx', '.jsp', '.php']) and ('id_dokumenty' in query or 'file' in query or 'download' in query):
        logger.info(f"⚠️ Skipping URL: {url} (File handler pattern detected in path/query)")
        return True
        
    return False

def requests_retry_session(retries=None, backoff_factor=None):
    """Create a requests session with retry configuration."""
    # Use global config values if available, otherwise use defaults
    if retries is None:
        retries = getattr(globals(), 'REQUEST_RETRY_COUNT', 3)
    if backoff_factor is None:
        backoff_factor = getattr(globals(), 'REQUEST_BACKOFF_FACTOR', 0.3)
    
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=getattr(globals(), 'REQUEST_RETRY_CODES', (500, 502, 503, 504, 524))
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

def remove_accents(input_str):
    """Remove accents from string for filename generation."""
    nfkd_form = unicodedata.normalize('NFKD', input_str)
    return ''.join([c for c in nfkd_form if not unicodedata.combining(c)])

def sanitize_filename(filename):
    """Sanitize filename for safe file system usage."""
    filename = remove_accents(filename)
    filename = re.sub(r"[<>:\"/\\|?*]", "_", filename)
    filename = re.sub(r"\s+", "_", filename)
    filename = re.sub(r"_+", "_", filename)
    filename = filename.strip("_")
    # Use global config value if available, otherwise use default
    max_length = getattr(globals(), 'MAX_FILENAME_LENGTH', 200)
    return filename[:max_length]

def create_filename_from_url(url, title="", rss_metadata=None):
    """
    Create a filename from URL path segments with enhanced support for Events XML URLs.
    
    Examples:
    - "https://www.khk.cz/kraj/rada/seznam-clenu-rady" → "Rada_Seznam_Clenu_Rady"
    - "https://www.khk.cz/dotace-prehled/aktuality" → "Dotace_Prehled_Aktuality"
    - "https://teplice.cz/vismo/telefonni-seznam.asp" → "Vismo_Telefonni_Seznam"
    - "http://www.usti.cz/cz/volny-cas/kultura/detail-akce.html?eventId=75318" → "Event_75318_MUZEUM_CTE_DETEM"
    
    Args:
        url (str): Source URL to extract path from
        title (str): Title for enhanced filename generation (especially for Events XML)
        rss_metadata (dict, optional): RSS metadata for Events RSS enhanced naming
    
    Returns:
        str: Base filename without extension or postfixes
    """
    from urllib.parse import parse_qs, unquote
    try:
        parsed_url = urlparse(url)
        path_parts = [part for part in parsed_url.path.split('/') if part.strip()]
        
        # ENHANCED: Special handling for Events XML URLs with eventId parameters
        if parsed_url.query and 'eventId=' in parsed_url.query:
            # Extract eventId from query parameters
            query_params = parse_qs(parsed_url.query)
            event_id = query_params.get('eventId', [None])[0]
            
            if event_id:
                # ENHANCED: Use RSS metadata for better event filename if available
                if rss_metadata and rss_metadata.get('event_metadata'):
                    event_data = rss_metadata['event_metadata']
                    event_name = event_data.get('event_name', '').strip()
                    
                    if event_name:
                        # Use actual event name from RSS metadata
                        clean_event_name = event_name
                        # Clean and normalize for filename
                        clean_event_name = re.sub(r'[^\w\s\-–áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]', '_', clean_event_name)
                        clean_event_name = re.sub(r'\s+', '_', clean_event_name)
                        clean_event_name = re.sub(r'_+', '_', clean_event_name).strip('_')
                        
                        # Create unique event filename with RSS event name
                        event_filename = f"Event_{event_id}_{clean_event_name}"
                        
                        # Sanitize and return
                        event_filename = sanitize_filename(event_filename)
                        logger.debug(f"🎯 Created Events RSS filename: {url} → {event_filename}")
                        return event_filename
                
                # Fallback: Extract clean event name from title (remove date/time info in parentheses)
                if title and title.strip():
                    clean_title = title.strip()
                    # Remove date/time pattern like "(25.09.2025 16:00)" from the end
                    import re
                    clean_title = re.sub(r'\s*\([^)]*\d{2}\.\d{2}\.\d{4}[^)]*\)$', '', clean_title)
                    # Clean and normalize for filename
                    clean_title = re.sub(r'[^\w\s\-–áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]', '_', clean_title)
                    clean_title = re.sub(r'\s+', '_', clean_title)
                    clean_title = re.sub(r'_+', '_', clean_title).strip('_')
                    
                    # Create unique event filename
                    event_filename = f"Event_{event_id}_{clean_title}"
                    
                    # Sanitize and return
                    event_filename = sanitize_filename(event_filename)
                    logger.debug(f"🎯 Created Events XML filename: {url} → {event_filename}")
                    return event_filename
                else:
                    # Fallback to just eventId if no title
                    event_filename = f"Event_{event_id}"
                    logger.debug(f"🎯 Created Events XML filename (no title): {url} → {event_filename}")
                    return sanitize_filename(event_filename)
        
        if not path_parts:
            # Fallback to homepage or title
            if title and title.strip():
                return sanitize_filename(title)
            return "Homepage"
        
        # Process each path part
        processed_parts = []
        for part in path_parts:
            # CRITICAL FIX: URL decode the part before processing
            part = unquote(part)
            
            # Remove file extensions
            if '.' in part:
                part_without_ext = part.rsplit('.', 1)[0]
                if part_without_ext:  # Only use if something remains after removing extension
                    part = part_without_ext
            
            # Clean and normalize the part
            clean_part = part.replace('-', ' ').replace('_', ' ').replace('.', ' ')
            
            # Convert to title case and handle Czech characters
            title_case_part = clean_part.title()
            
            # Join words with underscores and clean up
            title_case_part = title_case_part.replace(' ', '_')
            
            if title_case_part:
                processed_parts.append(title_case_part)
        
        if processed_parts:
            # Join all parts with underscores
            base_filename = '_'.join(processed_parts)
            
            # ENHANCED: Append unique ID from query parameters if available and not an event URL
            # This handles generic URLs like vismo/dokumenty2.asp?id=48140
            if parsed_url.query:
                query_params = parse_qs(parsed_url.query)
                
                # Prioritize 'id' parameter for uniqueness in generic URLs
                unique_id = query_params.get('id', [None])[0]
                
                if unique_id:
                    base_filename = f"{base_filename}_{unique_id}"
                    logger.debug(f"🏗️ Appended unique ID from query: {unique_id}")

            # Final sanitization
            base_filename = sanitize_filename(base_filename)
            
            logger.debug(f"🏗️ Created filename from URL: {url} → {base_filename}")
            return base_filename
        else:
            # Fallback if no usable parts
            if title and title.strip():
                return sanitize_filename(title)
            return "Unknown_Page"
            
    except Exception as e:
        logger.error(f"❌ Error creating filename from URL {url}: {str(e)}")
        # Fallback to title-based naming
        if title and title.strip():
            return sanitize_filename(title)
        return "Error_Page"

def construct_final_filename(base_filename, is_paginated=False, page_number=None, chunk_postfix=None, dedup_counter=None):
    """
    Construct final filename with all postfixes in correct order: CHUNK + PAGE + VERSION.
    
    Args:
        base_filename (str): Base filename from URL (e.g., "Rada_Seznam_Clenu_Rady")
        is_paginated (bool): Whether this is a paginated file
        page_number (int, optional): Page number for pagination
        chunk_postfix (str, optional): Chunk postfix (A, B, C, etc.)
        dedup_counter (int, optional): Deduplication counter for duplicate filenames
    
    Returns:
        str: Complete filename without .txt extension
        
    Examples:
        - Normal: "Rada_Seznam_Clenu_Rady"
        - Paginated: "Rada_Seznam_Clenu_Rady_PAGE2"
        - Chunked: "Rada_Seznam_Clenu_Rady_A"
        - Paginated + Chunked: "Rada_Seznam_Clenu_Rady_A_PAGE2"
        - Duplicated: "Rada_Seznam_Clenu_Rady_V2"
        - Chunked + Duplicated: "Rada_Seznam_Clenu_Rady_A_V3"
        - Paginated + Duplicated: "Rada_Seznam_Clenu_Rady_PAGE2_V3"
        - All Combined: "Rada_Seznam_Clenu_Rady_A_PAGE2_V4"
    """
    filename_parts = [base_filename]
    
    # Add chunk postfix FIRST
    if chunk_postfix:
        filename_parts.append(chunk_postfix)
    
    # Add pagination postfix SECOND
    if is_paginated and page_number is not None:
        filename_parts.append(f"PAGE{page_number}")
    
    # Add deduplication postfix LAST
    if dedup_counter and dedup_counter > 0:
        filename_parts.append(f"V{dedup_counter}")
    
    final_filename = '_'.join(filename_parts)
    logger.debug(f"🏷️ Constructed filename: {final_filename}")
    
    return final_filename

def analyze_content_for_massive_data_warning(content, url="", title=""):
    """
    Analyze content and provide user-friendly warnings about massive data.
    This function helps users understand what they're dealing with.
    
    Args:
        content (str): Content to analyze
        url (str): Source URL for context
        title (str): Content title for context
    
    Returns:
        dict: Analysis results with user recommendations
    """
    if not content:
        return {'analysis': 'empty', 'recommendation': 'none'}
    
    estimated_tokens = count_tokens_approximate(content)
    
    # Universal structure detection
    structure_indicators = {
        'table_rows': len(re.findall(r'^\|.*\|.*\|', content, re.MULTILINE)),
        'list_items': len(re.findall(r'^[\s]*[-*+]\s+', content, re.MULTILINE)),
        'numbered_items': len(re.findall(r'^\s*\d+\.\s+', content, re.MULTILINE)),
        'headings': len(re.findall(r'^#+\s+', content, re.MULTILINE)),
        'contact_elements': len(re.findall(r'\b\d{9,15}\b|[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', content)),
        'lines': len(content.split('\n'))
    }
    
    total_structure = sum(structure_indicators.values())
    dominant_structure = max(structure_indicators.items(), key=lambda x: x[1])
    
    # Classification
    if estimated_tokens > 20000:
        risk_level = "CRITICAL"
        message = f"🚨 CRITICAL: {estimated_tokens:,} tokens - Guaranteed information loss without chunking"
    elif estimated_tokens > 15000:
        risk_level = "HIGH"
        message = f"⚠️ HIGH RISK: {estimated_tokens:,} tokens - Likely information loss"
    elif estimated_tokens > 8000:
        risk_level = "MODERATE"
        message = f"⚠️ MODERATE: {estimated_tokens:,} tokens - Monitor for completeness"
    else:
        risk_level = "LOW"
        message = f"✅ SAFE: {estimated_tokens:,} tokens - Normal processing"
    
    return {
        'estimated_tokens': estimated_tokens,
        'risk_level': risk_level,
        'message': message,
        'structure_indicators': structure_indicators,
        'dominant_structure': dominant_structure[0],
        'dominant_count': dominant_structure[1],
        'total_structural_elements': total_structure,
        'recommendation': 'chunking_required' if estimated_tokens > 15000 else 'monitor'
    }

def chunk_massive_content_by_sections(content, min_section_tokens=500):
    """
    ADAPTIVE UNIVERSAL CHUNKING - Automatically analyzes ANY content structure and preserves COMPLETE SECTIONS.
    Works with any markdown format: documentation, contact lists, articles, manuals, etc.
    
    Algorithm:
    1. Scans content for ALL structural elements (headers, lists, tables, separators)
    2. Builds hierarchy map and identifies natural section boundaries
    3. Adapts breaking strategy based on discovered content patterns
    4. Preserves COMPLETE logical sections regardless of their individual size
    
    IMPORTANT: This function breaks ONLY at logical boundaries (headers, departments, separators).
    Each section can be any size - from 500 to 50,000+ tokens - preserving complete logical units.
    
    Args:
        content (str): Original markdown content to chunk
        min_section_tokens (int): Minimum tokens before considering section breaks (default 500)
    
    Returns:
        list: List of complete content sections with metadata for multi-file output
    """
    logger.info("🧠 ADAPTIVE UNIVERSAL CHUNKING: Auto-analyzing content structure...")
    
    lines = content.split('\n')
    
    # STEP 1: DISCOVER ALL STRUCTURAL ELEMENTS
    structural_elements = []
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
            
        element = {'line_number': i, 'content': line, 'type': None, 'level': 0, 'title': '', 'priority': 999}
        
        # Detect markdown headers (# ## ### #### ##### ######)
        header_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)
        if header_match:
            level = len(header_match.group(1))
            title = header_match.group(2).strip()
            element.update({
                'type': 'header',
                'level': level,
                'title': title,
                'priority': level  # Lower number = higher priority for breaking
            })
            structural_elements.append(element)
            continue
        
        # Detect major separators
        if re.match(r'^[-=_*]{3,}$', stripped):
            element.update({
                'type': 'separator',
                'level': 99,
                'title': 'Separator',
                'priority': 50
            })
            structural_elements.append(element)
            continue
        
        # Detect table headers (usually indicate new sections)
        if re.match(r'^\|.*\|.*\|', stripped) and any(keyword in stripped.lower() for keyword in ['jméno', 'name', 'title', 'contact', 'příjmení', 'function']):
            element.update({
                'type': 'table_header',
                'level': 98,
                'title': 'Table Section',
                'priority': 30
            })
            structural_elements.append(element)
            continue
        
        # Detect obvious section indicators in text (language-agnostic)
        section_indicators = [
            (r'^[A-Z]{2,5}\s*[-–]\s*.*$', 'department_abbreviation'),  # "MSZ - odbor..."
            (r'^[A-Z][a-zA-ZÀ-ž\s]+:?\s*$', 'section_title'),         # "DEPARTMENT:" or "Section Title"
            (r'^\d+\.\s+[A-Z].*$', 'numbered_section'),                # "1. Major Section"
            (r'^[IVX]+\.\s+.*$', 'roman_section'),                     # "I. Introduction"
        ]
        
        for pattern, section_type in section_indicators:
            if re.match(pattern, stripped):
                element.update({
                    'type': section_type,
                    'level': 97,
                    'title': stripped,
                    'priority': 25
                })
                structural_elements.append(element)
                break
    
    logger.info(f"🔍 Discovered {len(structural_elements)} structural elements")
    
    # STEP 2: ANALYZE CONTENT HIERARCHY AND DETERMINE BREAKING STRATEGY
    if not structural_elements:
        logger.info("⚠️ No structural elements found, using natural paragraph-based chunking")
        return _chunk_by_natural_breaks(content, min_section_tokens)
    
    # Group elements by type and analyze distribution
    element_types = {}
    for elem in structural_elements:
        elem_type = elem['type']
        if elem_type not in element_types:
            element_types[elem_type] = []
        element_types[elem_type].append(elem)
    
    # Find the best breaking strategy
    breaking_elements = []
    
    # Priority 1: Use headers as primary breaking points
    if 'header' in element_types:
        headers = element_types['header']
        header_levels = [h['level'] for h in headers]
        
        # Use the most common high-level header as primary break point
        level_counts = {}
        for level in header_levels:
            level_counts[level] = level_counts.get(level, 0) + 1
        
        # Find optimal breaking level (prefer higher level headers, but ensure reasonable distribution)
        optimal_level = min(header_levels)  # Start with highest level
        
        # If we have too few high-level headers, consider lower levels
        high_level_count = level_counts.get(optimal_level, 0)
        if high_level_count < 3 and len(set(header_levels)) > 1:
            # Try next level down
            next_levels = sorted([l for l in header_levels if l > optimal_level])
            if next_levels and level_counts.get(next_levels[0], 0) >= 3:
                optimal_level = next_levels[0]
        
        # Add headers of optimal level as breaking points
        for header in headers:
            if header['level'] == optimal_level:
                breaking_elements.append(header)
        
        logger.info(f"🎯 Selected header level {optimal_level} as primary breaking point ({len([h for h in headers if h['level'] == optimal_level])} headers)")
    
    # Priority 2: Add other strong structural elements if we don't have enough headers
    if len(breaking_elements) < 3:
        for elem_type in ['department_abbreviation', 'section_title', 'numbered_section']:
            if elem_type in element_types:
                breaking_elements.extend(element_types[elem_type])
                logger.info(f"➕ Added {len(element_types[elem_type])} {elem_type} elements as breaking points")
    
    # Priority 3: Add separators and table headers as backup breaking points
    if len(breaking_elements) < 5:
        for elem_type in ['separator', 'table_header']:
            if elem_type in element_types:
                breaking_elements.extend(element_types[elem_type][:3])  # Limit to avoid too many breaks
    
    # Sort breaking elements by line number
    breaking_elements.sort(key=lambda x: x['line_number'])
    
    logger.info(f"📋 Using {len(breaking_elements)} breaking points for intelligent chunking")
    
    # STEP 3: CREATE CHUNKS BASED ON DISCOVERED STRUCTURE
    chunks = []
    current_chunk = ""
    current_tokens = 0
    current_section_title = "Content Section"
    
    breaking_line_numbers = set(elem['line_number'] for elem in breaking_elements)
    
    i = 0
    while i < len(lines):
        line = lines[i]
        line_tokens = count_tokens_approximate(line)
        
        # Check if this line is a breaking point
        is_breaking_point = i in breaking_line_numbers
        current_breaking_element = None
        
        if is_breaking_point:
            for elem in breaking_elements:
                if elem['line_number'] == i:
                    current_breaking_element = elem
                    break
        
        # Decide if we should start a new chunk
        should_break = False
        
        if is_breaking_point and current_breaking_element:
            # This is a structural breaking point - ALWAYS break at logical boundaries
            if current_tokens > min_section_tokens and current_chunk.strip():
                should_break = True
                logger.info(f"📋 Breaking at logical boundary: {current_breaking_element['title']} ({current_tokens:,} tokens)")
            elif not chunks:  # First section - set title but don't break yet
                current_section_title = current_breaking_element['title']
        
        # NO SIZE-BASED BREAKING! Preserve complete sections regardless of size
        # Sections can be 500 tokens or 50,000 tokens - we preserve them completely
        
        if should_break:
            # Save current chunk
            if current_chunk.strip():
                chunks.append({
                    'content': current_chunk.strip(),
                    'tokens': current_tokens,
                    'section_title': current_section_title,
                    'chunk_number': len(chunks) + 1,
                    'type': 'adaptive_intelligent_section',
                    'breaking_strategy': f"{len(breaking_elements)} structural elements"
                })
                current_chunk = ""
                current_tokens = 0
            
            # Update section title for new chunk
            if current_breaking_element:
                current_section_title = current_breaking_element['title']
        
        current_chunk += line + '\n'
        current_tokens += line_tokens
        i += 1
    
    # Add final chunk
    if current_chunk.strip():
        chunks.append({
            'content': current_chunk.strip(),
            'tokens': current_tokens,
            'section_title': current_section_title,
            'chunk_number': len(chunks) + 1,
            'type': 'adaptive_intelligent_section',
            'breaking_strategy': f"{len(breaking_elements)} structural elements"
        })
    
    logger.info(f"✅ Pure section-based chunking created {len(chunks)} complete sections using {len(breaking_elements)} structural breaking points")
    logger.info(f"📊 Section sizes range from {min([c.get('tokens', 0) for c in chunks]):,} to {max([c.get('tokens', 0) for c in chunks]):,} tokens")
    
    # Log the breaking strategy used
    strategy_summary = []
    for elem_type in ['header', 'department_abbreviation', 'section_title', 'separator']:
        if elem_type in element_types:
            count = len([e for e in breaking_elements if e['type'] == elem_type])
            if count > 0:
                strategy_summary.append(f"{count} {elem_type}s")
    
    logger.info(f"📊 Breaking strategy: {', '.join(strategy_summary)}")
    
    return chunks

def _chunk_by_natural_breaks(content, min_section_tokens):
    """
    Natural paragraph-based chunking that preserves complete paragraphs/sections.
    Used when no structural elements are detected.
    
    IMPORTANT: This function preserves complete paragraphs and natural breaks.
    No arbitrary size limits - sections can be any size.
    """
    logger.info("🔍 Using natural paragraph preservation chunking (no structural elements detected)")
    
    chunks = []
    lines = content.split('\n')
    current_chunk = ""
    current_tokens = 0
    
    i = 0
    while i < len(lines):
        line = lines[i]
        line_tokens = count_tokens_approximate(line)
        
        # Look ahead for natural breaking points
        should_break = False
        
        # Look for natural breaking points when we have substantial content
        if current_tokens > min_section_tokens and current_chunk.strip():
            # Look for natural break in next few lines
            natural_break_found = False
            
            for look_ahead in range(min(10, len(lines) - i)):
                future_line = lines[i + look_ahead].strip()
                
                # Good natural breaking points
                if (not future_line or  # Empty line
                    future_line.startswith(('---', '===', '***')) or  # Separators
                    len(future_line) < 80 and future_line.isupper() or  # Uppercase section titles
                    re.match(r'^\d+\.\s+[A-Z]', future_line) or  # Numbered sections
                    re.match(r'^[A-Z][a-zA-ZÀ-ž\s]+:?\s*$', future_line)):  # Section headers
                    
                    # Include lines up to the natural break
                    for j in range(look_ahead + 1):
                        if i + j < len(lines):
                            current_chunk += lines[i + j] + '\n'
                            current_tokens += count_tokens_approximate(lines[i + j])
                    
                    should_break = True
                    natural_break_found = True
                    logger.info(f"📋 Breaking at natural boundary after {current_tokens:,} tokens")
                    i += look_ahead  # Skip the lines we just added
                    break
            
            # NO FORCED BREAKING! If no natural break found, continue building the section
            if not natural_break_found:
                should_break = False  # Keep building this natural section
        
        if should_break:
            # Save current chunk
            if current_chunk.strip():
                # Try to extract meaningful title from chunk start
                chunk_lines = current_chunk.strip().split('\n')
                title_candidates = []
                
                for chunk_line in chunk_lines[:10]:  # Check first 10 lines
                    stripped = chunk_line.strip()
                    if stripped and len(stripped) < 100:
                        # Look for title-like patterns
                        if (stripped.isupper() or
                            stripped.startswith(('##', '#')) or
                            ':' in stripped or
                            any(word in stripped.lower() for word in ['section', 'chapter', 'part', 'oddělení', 'odbor'])):
                            title_candidates.append(stripped)
                
                section_title = title_candidates[0] if title_candidates else f"Natural Section {len(chunks) + 1}"
                
                chunks.append({
                    'content': current_chunk.strip(),
                    'tokens': current_tokens,
                    'section_title': section_title,
                    'chunk_number': len(chunks) + 1,
                    'type': 'natural_break_section'
                })
                current_chunk = ""
                current_tokens = 0
        else:
            current_chunk += line + '\n'
            current_tokens += line_tokens
        
        i += 1
    
    # Add final chunk
    if current_chunk.strip():
        # Extract title for final chunk
        chunk_lines = current_chunk.strip().split('\n')
        title_candidates = []
        
        for chunk_line in chunk_lines[:10]:
            stripped = chunk_line.strip()
            if stripped and len(stripped) < 100:
                if (stripped.isupper() or
                    stripped.startswith(('##', '#')) or
                    ':' in stripped):
                    title_candidates.append(stripped)
        
        section_title = title_candidates[0] if title_candidates else f"Natural Section {len(chunks) + 1}"
        
        chunks.append({
            'content': current_chunk.strip(),
            'tokens': current_tokens,
            'section_title': section_title,
            'chunk_number': len(chunks) + 1,
            'type': 'natural_break_section'
        })
    
    logger.info(f"✅ Natural breaks chunking created {len(chunks)} sections")
    return chunks

def _chunk_by_size_only(content, min_section_tokens):
    """Simple fallback chunking method - preserves complete content blocks."""
    logger.info("⚠️ Using simple content preservation (fallback method)")
    logger.info("📋 No arbitrary size limits - preserving complete content as single section")
    
    chunks = []
    
    # For unstructured content, treat as single complete section
    # This preserves the entire content without artificial breaks
    total_tokens = count_tokens_approximate(content)
    
    chunks.append({
        'content': content.strip(),
        'tokens': total_tokens,
        'section_title': "Complete Content Section",
        'chunk_number': 1,
        'type': 'complete_content_section'
    })
    
    logger.info(f"📋 Preserved complete content as single section: {total_tokens:,} tokens")
    
    return chunks

def chunk_content_with_metadata_budget(content, vector_store_max_chunk_size, base_title="", url="", target_language="Czech"):
    """
    NEW CONTENT SPLITTING SYSTEM: Split content into A-Z files with exact 50/50 token budget allocation.
    Each file uses exactly half vector store limit for content and half for metadata.
    
    TOKEN BUDGET CALCULATION:
    - Content Budget: vector_store_max_chunk_size / 2 tokens
    - Metadata Budget: vector_store_max_chunk_size / 2 tokens
    - Required Chunks: MARKDOWN CONTENT TOKENS / (VECTOR STORE TOKEN LIMIT / 2)
    
    Args:
        content (str): Original markdown content to split
        vector_store_max_chunk_size (int): Maximum tokens per file from vector store config
        base_title (str): Base title for filename generation
        url (str): Source URL for metadata
        target_language (str): Target language for metadata generation
    
    Returns:
        list: List of content chunks with [A-Z] naming, ready for immediate metadata generation and file saving
    """
    logger.info(f"🔀 NEW CONTENT SPLITTING SYSTEM: Processing content with 50/50 token budget allocation")
    logger.info(f"📐 Vector Store max chunk size: {vector_store_max_chunk_size} tokens")
    
    # Calculate exact token budgets based on configured offset
    # Content Limit = vector_store_max_chunk_size - DEFAULT_CONTENT_TOKEN_OFFSET
    # Metadata Budget = DEFAULT_CONTENT_TOKEN_OFFSET
    
    offset = DEFAULT_CONTENT_TOKEN_OFFSET
    
    working_space = vector_store_max_chunk_size - offset
    
    # Calculate content and metadata budget based on content_ratio
    content_ratio = DEFAULT_CONTENT_RATIO
    
    content_budget = int(working_space * content_ratio)
    metadata_budget = working_space - content_budget
    
    logger.info(f"💰 Using configured token offset: {offset} tokens")
    
    # Safety check: ensure content budget is positive (already validated in validate_config, but safe to check here)
    if content_budget <= 0:
        logger.error(f"🚨 CRITICAL ERROR: Calculated content budget is {content_budget}. Defaulting to 100 tokens.")
        content_budget = 100
        metadata_budget = vector_store_max_chunk_size - 100
    
    logger.info(f"💰 TOKEN BUDGET ALLOCATION:")
    logger.info(f"   📄 Content budget per file: {content_budget} tokens ({content_ratio*100:.0f}% of working space)")
    logger.info(f"   📋 Dynamic Metadata budget per file: {metadata_budget} tokens ({(1-content_ratio)*100:.0f}% of working space)")
    logger.info(f"   🗂️ Fixed Offset/Buffer: {offset} tokens (Reserved space at end)")
    logger.info(f"   🎯 Total per file: {content_budget + metadata_budget + offset} tokens (= {vector_store_max_chunk_size})")
    
    # Calculate total content size and required chunks
    total_content_tokens = count_tokens_approximate(content)
    required_chunks = max(1, (total_content_tokens + content_budget - 1) // content_budget)  # Ceiling division
    
    logger.info(f"📊 CONTENT ANALYSIS:")
    logger.info(f"   📄 Total content: {total_content_tokens:,} tokens")
    logger.info(f"   📊 Required chunks: {required_chunks} (calculated as ⌈{total_content_tokens:,} / {content_budget}⌉)")
    
    # Generate alphabet postfixes (A, B, C, ..., Z, AA, AB, etc.)
    def get_chunk_postfix(chunk_num):
        if chunk_num < 26:
            return chr(ord('A') + chunk_num)
        else:
            # For more than 26 chunks: AA, AB, AC, etc.
            first_letter = chr(ord('A') + (chunk_num // 26) - 1)
            second_letter = chr(ord('A') + (chunk_num % 26))
            return first_letter + second_letter
    
    chunks = []
    lines = content.split('\n')
    current_chunk = ""
    current_tokens = 0
    chunk_number = 0
    
    logger.info(f"🔄 CONTENT SPLITTING: Creating {required_chunks} chunks with {content_budget} token budget each")
    
    for line in lines:
        line_tokens = count_tokens_approximate(line)

        # Handle extremely long lines that exceed content budget
        if line_tokens > content_budget:
            logger.warning(f"⚠️ Single line has {line_tokens:,} tokens > content_budget {content_budget:,}. Forcing word-level split.")
            
            # 1. Finalize any preceding content as a chunk
            if current_chunk.strip():
                postfix = get_chunk_postfix(chunk_number)
                chunks.append({
                    'content': current_chunk.strip(),
                    'tokens': current_tokens,
                    'chunk_postfix': postfix,
                    'chunk_number': chunk_number + 1,
                    'type': 'metadata_budget_chunk',
                    'filename_postfix': postfix,
                    'content_budget': content_budget,
                    'metadata_budget': metadata_budget
                })
                logger.info(f"📄 Created chunk {postfix}: {current_tokens:,} tokens (preceding content)")
                current_chunk = ""
                current_tokens = 0
                chunk_number += 1
            
            # 2. Split the long line into word-level segments
            words = line.split(' ')
            segment_buffer = ""
            
            for word in words:
                test_content = segment_buffer + " " + word if segment_buffer else word
                if count_tokens_approximate(test_content) > content_budget:
                    # Save current segment as new chunk
                    if segment_buffer.strip():
                        postfix = get_chunk_postfix(chunk_number)
                        chunks.append({
                            'content': segment_buffer.strip(),
                            'tokens': count_tokens_approximate(segment_buffer),
                            'chunk_postfix': postfix,
                            'chunk_number': chunk_number + 1,
                            'type': 'metadata_budget_chunk',
                            'filename_postfix': postfix,
                            'content_budget': content_budget,
                            'metadata_budget': metadata_budget
                        })
                        logger.info(f"📄 Created chunk {postfix}: {count_tokens_approximate(segment_buffer):,} tokens (long line segment)")
                        chunk_number += 1
                    
                    # Start new segment with current word
                    segment_buffer = word
                else:
                    # Continue accumulating
                    segment_buffer = test_content
            
            # 3. Add final segment to current chunk buffer
            if segment_buffer.strip():
                current_chunk = segment_buffer.strip() + '\n'
                current_tokens = count_tokens_approximate(current_chunk)
            
            continue
            
        # Normal line processing - check if adding this line would exceed content budget
        elif current_tokens + line_tokens > content_budget and current_chunk.strip():
            # Create new chunk with current content
            postfix = get_chunk_postfix(chunk_number)
            chunks.append({
                'content': current_chunk.strip(),
                'tokens': current_tokens,
                'chunk_postfix': postfix,
                'chunk_number': chunk_number + 1,
                'type': 'metadata_budget_chunk',
                'filename_postfix': postfix,
                'content_budget': content_budget,
                'metadata_budget': metadata_budget
            })
            logger.info(f"📄 Created chunk {postfix}: {current_tokens:,} tokens (budget: {content_budget:,})")
            
            # Start new chunk with current line
            current_chunk = line + '\n'
            current_tokens = line_tokens
            chunk_number += 1
        else:
            # Add line to current chunk
            current_chunk += line + '\n'
            current_tokens = count_tokens_approximate(current_chunk)
    
    # Add final chunk if there's content left
    if current_chunk.strip():
        postfix = get_chunk_postfix(chunk_number)
        chunks.append({
            'content': current_chunk.strip(),
            'tokens': current_tokens,
            'chunk_postfix': postfix,
            'chunk_number': chunk_number + 1,
            'type': 'metadata_budget_chunk',
            'filename_postfix': postfix,
            'content_budget': content_budget,
            'metadata_budget': metadata_budget
        })
        logger.info(f"📄 Created final chunk {postfix}: {current_tokens:,} tokens (budget: {content_budget:,})")
    
    logger.info(f"✅ METADATA BUDGET CHUNKING COMPLETE: {len(chunks)} chunks created")
    logger.info(f"📊 Content budget per chunk: {content_budget:,} tokens")
    logger.info(f"📋 Metadata budget per chunk: {metadata_budget:,} tokens")
    logger.info(f"🎯 Total file size per chunk: {vector_store_max_chunk_size:,} tokens")
    
    return chunks


def chunk_large_content_simple(content, max_chunk_size, base_title="", url=""):
    """
    DEPRECATED: Legacy simple chunking function - kept for backward compatibility.
    
    ⚠️ WARNING: This function is deprecated and should not be used for new implementations.
    Use chunk_content_with_metadata_budget() instead for proper token budget management.
    
    Args:
        content (str): Original markdown content to chunk
        max_chunk_size (int): Maximum tokens per chunk (from vector_store.max_chunk_size)
        base_title (str): Base title for filename generation
        url (str): Source URL for metadata
    
    Returns:
        list: List of simple content chunks with [A-Z] naming
    """
    logger.warning(f"🚨 USING DEPRECATED FUNCTION: chunk_large_content_simple() is deprecated")
    logger.warning(f"💡 RECOMMENDATION: Use chunk_content_with_metadata_budget() for proper token budget management")
    logger.info(f"🔢 LEGACY SIMPLE CHUNKING: Splitting large content for {url}")
    logger.info(f"📐 Max chunk size: {max_chunk_size} tokens")
    
    chunks = []
    lines = content.split('\n')
    current_chunk = ""
    current_tokens = 0
    chunk_number = 0
    
    # Generate alphabet postfixes (A, B, C, ..., Z, AA, AB, etc.)
    def get_chunk_postfix(chunk_num):
        if chunk_num < 26:
            return chr(ord('A') + chunk_num)
        else:
            # For more than 26 chunks: AA, AB, AC, etc.
            first_letter = chr(ord('A') + (chunk_num // 26) - 1)
            second_letter = chr(ord('A') + (chunk_num % 26))
            return first_letter + second_letter
    
    for line in lines:
        line_tokens = count_tokens_approximate(line)

        if line_tokens > max_chunk_size:
            logger.warning(f"⚠️ Single line has {line_tokens:,} tokens > max_chunk_size {max_chunk_size:,}. Forcing split into segments.")
            
            # 1. Finalize any preceding content as a chunk (explains why Chunk A is short)
            if current_chunk.strip():
                postfix = get_chunk_postfix(chunk_number)
                chunks.append({
                    'content': current_chunk.strip(),
                    'tokens': current_tokens,
                    'chunk_postfix': postfix,
                    'chunk_number': chunk_number + 1,
                    'type': 'simple_size_based_chunk',
                    'filename_postfix': postfix
                })
                logger.info(f"📄 Created chunk {postfix}: {current_tokens:,} tokens (preceding content)")
                current_chunk = ""
                current_tokens = 0
                chunk_number += 1
            
            # 2. Segment the long line into pieces that fit max_chunk_size
            words = line.split(' ')
            segment_buffer = ""
            
            for word in words:
                # Check if adding the next word exceeds the max chunk size
                # We use a conservative check here to ensure the segment fits
                if count_tokens_approximate(segment_buffer + " " + word) > max_chunk_size:
                    # Save the current segment buffer as a new chunk
                    postfix = get_chunk_postfix(chunk_number)
                    chunks.append({
                        'content': segment_buffer.strip(),
                        'tokens': count_tokens_approximate(segment_buffer),
                        'chunk_postfix': postfix,
                        'chunk_number': chunk_number + 1,
                        'type': 'simple_size_based_chunk',
                        'filename_postfix': postfix
                    })
                    logger.info(f"📄 Created chunk {postfix}: {count_tokens_approximate(segment_buffer):,} tokens (long line segment)")
                    chunk_number += 1
                    
                    # Start a new segment buffer with the current word
                    segment_buffer = word + " "
                else:
                    # Continue accumulating the segment
                    segment_buffer += word + " "
            
            # 3. Add the final segment of the long line to the current_chunk buffer
            if segment_buffer.strip():
                current_chunk = segment_buffer.strip() + '\n'
                current_tokens = count_tokens_approximate(current_chunk)
            
            # Continue to next line in main loop (Line 2174)
            continue
            
        elif current_tokens + line_tokens > max_chunk_size and current_chunk.strip():
            postfix = get_chunk_postfix(chunk_number)
            chunks.append({
                'content': current_chunk.strip(),
                'tokens': current_tokens,
                'chunk_postfix': postfix,
                'chunk_number': chunk_number + 1,
                'type': 'simple_size_based_chunk',
                'filename_postfix': postfix
            })
            logger.info(f"📄 Created chunk {postfix}: {current_tokens:,} tokens")
            
            current_chunk = line + '\n'
            current_tokens = line_tokens
            chunk_number += 1
        else:
            current_chunk += line + '\n'
            current_tokens = count_tokens_approximate(current_chunk)
    
    # Add final chunk if there's content left
    if current_chunk.strip():
        # NOTE: We rely on the calling function (process_single_url_normally) to ensure that
        # max_chunk_size is set low enough to accommodate the metadata header.
        # We explicitly remove the emergency truncation here to prevent data loss,
        # as the goal is for NO information to be lost.
        
        postfix = get_chunk_postfix(chunk_number)
        chunks.append({
            'content': current_chunk.strip(),
            'tokens': current_tokens,
            'chunk_postfix': postfix,
            'chunk_number': chunk_number + 1,
            'type': 'simple_size_based_chunk',
            'filename_postfix': postfix  # For [A-Z] naming
        })
        
        logger.info(f"📄 Created final chunk {postfix}: {current_tokens:,} tokens (limit: {max_chunk_size:,})")
    
    logger.info(f"✅ Legacy chunking complete: {len(chunks)} chunks created")
    return chunks

def generate_section_based_filename(section_title, chunk_number, base_title=""):
    """
    Generate a meaningful filename based on section content instead of just sequential numbers.
    
    Args:
        section_title (str): The section title from chunking
        chunk_number (int): Chunk number for fallback
        base_title (str): Base title for context
    
    Returns:
        str: Sanitized filename without extension
    """
    # Clean and format section title for filename
    if section_title and section_title.strip() and section_title != "Content Section":
        # Remove common prefixes and clean up
        clean_title = section_title.strip()
        
        # Remove markdown formatting
        clean_title = re.sub(r'^#+\s*', '', clean_title)
        clean_title = re.sub(r'\*\*(.+?)\*\*', r'\1', clean_title)
        clean_title = re.sub(r'\*(.+?)\*', r'\1', clean_title)
        
        # Remove special characters and normalize
        clean_title = re.sub(r'[^\w\s\-–áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]', '_', clean_title)
        clean_title = re.sub(r'\s+', '_', clean_title)
        clean_title = re.sub(r'_+', '_', clean_title)
        clean_title = clean_title.strip('_')
        
        # Limit length to reasonable size
        if len(clean_title) > 80:
            clean_title = clean_title[:80]
        
        # Use section name as filename
        if clean_title:
            section_filename = clean_title
        else:
            # Fallback to chunk number
            section_filename = f"sekce_{chunk_number:02d}"
    else:
        # Fallback when no meaningful section title
        section_filename = f"sekce_{chunk_number:02d}"
    
    # Sanitize the final filename
    section_filename = sanitize_filename(section_filename)
    
    # Don't add base title to keep filenames clean and short
    # The section title is already descriptive enough
    
    return section_filename


# ============================================================================
# NEW BUDGET-CONSTRAINED METADATA GENERATION FUNCTIONS
# ============================================================================

def calculate_metadata_token_allocation(metadata_budget, static_metadata_tokens=100):
    """
    Calculate optimal token allocation for dynamic metadata components within budget.
    
    Args:
        metadata_budget (int): Total available tokens for metadata (e.g., 2048)
        static_metadata_tokens (int): Estimated tokens for static metadata (URL, title, dates, etc.)
    
    Returns:
        dict: Token allocation for each dynamic metadata component
    """
    logger.info(f"📊 CALCULATING METADATA TOKEN ALLOCATION:")
    logger.info(f"   💰 Total metadata budget: {metadata_budget} tokens")
    logger.info(f"   📋 Static metadata (URL, title, dates): ~{static_metadata_tokens} tokens")
    
    # Reserve tokens for static metadata
    available_for_dynamic = metadata_budget - static_metadata_tokens
    
    if available_for_dynamic <= 0:
        logger.warning(f"⚠️ No tokens available for dynamic metadata after static allocation!")
        return {
            'source_page_summary': 50,
            'current_file_summary': 50,
            'overlap_summary': 50,
            'question_section': 50
        }
    
    # Distribute remaining tokens among dynamic components
    # Priority: Source page summary > Current file summary > Overlap summary > Question section
    source_page_summary_tokens = min(available_for_dynamic // 4, 300)  # Max 300 tokens
    remaining = available_for_dynamic - source_page_summary_tokens
    
    current_file_summary_tokens = min(remaining // 3, 400)  # Max 400 tokens
    remaining -= current_file_summary_tokens
    
    overlap_summary_tokens = min(remaining // 2, 250)  # Max 250 tokens
    remaining -= overlap_summary_tokens
    
    question_section_tokens = remaining  # Rest goes to questions
    
    allocation = {
        'source_page_summary': source_page_summary_tokens,
        'current_file_summary': current_file_summary_tokens,
        'overlap_summary': overlap_summary_tokens,
        'question_section': question_section_tokens,
        'static_metadata': static_metadata_tokens,
        'total_allocated': static_metadata_tokens + source_page_summary_tokens + current_file_summary_tokens + overlap_summary_tokens + question_section_tokens
    }
    
    logger.info(f"📊 METADATA TOKEN ALLOCATION COMPLETE:")
    logger.info(f"   📄 Source page summary: {allocation['source_page_summary']} tokens")
    logger.info(f"   📋 Current file summary: {allocation['current_file_summary']} tokens")
    logger.info(f"   🔄 Overlap summary: {allocation['overlap_summary']} tokens")
    logger.info(f"   ❓ Question section: {allocation['question_section']} tokens")
    logger.info(f"   📊 Static metadata: {allocation['static_metadata']} tokens")
    logger.info(f"   💯 Total allocated: {allocation['total_allocated']} tokens (budget: {metadata_budget})")
    
    return allocation


def generate_budget_constrained_page_summary(markdown_content, title="", url="", target_language="Czech", max_tokens=300):
    """
    Generate page summary with strict token budget constraints.
    
    Args:
        markdown_content (str): Original markdown content to analyze
        title (str): Page title for context
        url (str): Source URL for context
        target_language (str): Target language for the summary output
        max_tokens (int): Maximum tokens allowed for this summary
    
    Returns:
        str: Budget-constrained page summary or None if failed
    """
    logger.info(f"Generating budget-constrained page summary (max: {max_tokens} tokens) for URL: {url}")
    
    # Check if OpenRouter is configured
    if not OPENROUTER_API_KEY:
        logger.info(f"OpenRouter API key not configured, skipping page summary generation")
        return None
    
    # Create budget-constrained prompt
    prompt = f"""🤖 EXPERT PAGE CONTENT ANALYST: Create SHORT, FACTUAL summary about entire webpage content for metadata with STRICT TOKEN BUDGET COMPLIANCE.

## 🚨 CRITICAL: STRICT TOKEN BUDGET COMPLIANCE MANDATORY!
## 🎯 TASK: Create 1-2 paragraph summary about the ENTIRE PAGE CONTENT

- **OUTPUT LANGUAGE:** {target_language}
- **🚨 CRITICAL TOKEN LIMIT:** {max_tokens} tokens ABSOLUTE MAXIMUM (NON-NEGOTIABLE!)
- **LENGTH:** Maximum 1-2 paragraphs
- **FACTUAL DENSITY:** Maximum essential information per token
- **PURPOSE:** Provide context for understanding across chunked files

### **🔢 MANDATORY NUMERICAL DATA HANDLING:**
- **EXPLICIT COUNTS ONLY:** Only use counts explicitly stated in the source content. NEVER count elements yourself. Use estimates (dozens/hundreds) if the count is missing.

## SOURCE CONTENT TO ANALYZE:
{markdown_content}"""
    
    try:
        # Use existing OpenRouter infrastructure with budget constraints
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": url if url else "https://api.openrouter.ai",
            "X-Title": "HypeDigitaly Budget Page Summary Generator"
        }
        
        primary_model = OPENROUTER_MODELS[0] if isinstance(OPENROUTER_MODELS, list) else OPENROUTER_MODELS
        payload = {
            "model": primary_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a professional content analyst specializing in ultra-concise, high-level summaries. You NEVER mention specific individual names, personal titles, or detailed information. You focus ONLY on structural information, counts, and content types. Your responses are extremely brief, factual, and SMS-like in length."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": max_tokens + 50,  # Small buffer for model
            "temperature": 0.0,  # Deterministic output for consistent summaries
            "top_p": OPENROUTER_TOP_P
        }
        
        response = requests_retry_session().post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT * 2
        )
        response.raise_for_status()
        
        data = response.json()
        log_openrouter_token_usage(data, f"BUDGET_PAGE_SUMMARY_{max_tokens}T", url)
        
        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]
            page_summary = message.get("content", "").strip()
            
            if not page_summary and message.get("reasoning"):
                page_summary = message.get("reasoning", "").strip()
            
            if page_summary and page_summary.strip():
                result_tokens = count_tokens_approximate(page_summary)
                
                # CRITICAL: Enforce budget limit
                if result_tokens > max_tokens:
                    logger.warning(f"🚨 PAGE SUMMARY EXCEEDS BUDGET: {result_tokens} > {max_tokens} tokens - TRUNCATING")
                    estimated_chars = max_tokens * 4
                    page_summary = page_summary[:estimated_chars] + "..."
                    result_tokens = count_tokens_approximate(page_summary)
                
                logger.info(f"✅ Generated budget page summary: {result_tokens} tokens (budget: {max_tokens})")
                return page_summary
            else:
                logger.error("❌ Generated page summary is empty")
                return None
        else:
            logger.error("❌ No choices returned from OpenRouter API")
            return None
            
    except Exception as e:
        logger.error(f"❌ Error generating budget page summary: {str(e)}")
        return None


def generate_budget_constrained_current_file_summary(file_content, file_order, total_files, title="", url="", target_language="Czech", max_tokens=400):
    """
    Generate current file summary with strict token budget constraints.
    
    Args:
        file_content (str): Content of the current file to summarize
        file_order (int): Current file position (e.g., 1, 2, 3...)
        total_files (int): Total number of files in sequence
        title (str): Page title for context
        url (str): Source URL for context
        target_language (str): Target language for the summary output
        max_tokens (int): Maximum tokens allowed for this summary
    
    Returns:
        str: Budget-constrained current file summary or None if failed
    """
    logger.info(f"Generating budget-constrained current file summary ({file_order}/{total_files}, max: {max_tokens} tokens)")
    
    if not OPENROUTER_API_KEY:
        logger.info(f"OpenRouter API key not configured, skipping current file summary")
        return None
    
    prompt = f"""🤖 EXPERT CONTEXTUAL FILE ANALYST: Create ULTRA-SHORT summary of current file in sequence.

## 🎯 TASK: Create 1-2 SHORT sentences about CURRENT FILE ({file_order}/{total_files})

- **OUTPUT LANGUAGE:** {target_language}
- **🚨 CRITICAL TOKEN LIMIT:** {max_tokens} tokens MAXIMUM
- **LENGTH:** Maximum 1-2 short sentences (like SMS messages)
- **🚨 ABSOLUTELY FORBIDDEN:** DO NOT mention any specific individual names, personal titles, or detailed information
- **FOCUS:** Only file position, overall counts, and general content type

### **✅ WHAT TO INCLUDE:**
- File position: "část {file_order} z {total_files}"
- Total number of items/people in THIS file (numbers only) **(ONLY if the number is explicitly stated in the source content. NEVER count elements yourself.)**
- Basic content type and structure

### **❌ WHAT TO NEVER MENTION:**
- Individual names or surnames
- Specific department names
- Personal titles (Ing., Mgr., etc.)
- Any detailed information

### **🎯 OUTPUT FORMAT:**
Provide ONLY the 1-2 extremely short sentences in {target_language}. No formatting, no headings.

## CURRENT FILE CONTENT TO ANALYZE (File {file_order}/{total_files}):
{file_content}"""
    
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": url if url else "https://api.openrouter.ai",
            "X-Title": f"HypeDigitaly Budget Current File Summary ({file_order}/{total_files})"
        }
        
        primary_model = OPENROUTER_MODELS[0] if isinstance(OPENROUTER_MODELS, list) else OPENROUTER_MODELS
        payload = {
            "model": primary_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a professional file content analyst specializing in ultra-concise file summaries within document sequences. You NEVER mention specific individual names, personal titles, or detailed information. You focus ONLY on file position, structural boundaries, and content counts. Your responses are extremely brief, factual, and SMS-like in length."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": max_tokens + 50,
            "temperature": 0.0,  # Deterministic output for consistent file summaries
            "top_p": OPENROUTER_TOP_P
        }
        
        response = requests_retry_session().post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT * 2
        )
        response.raise_for_status()
        
        data = response.json()
        log_openrouter_token_usage(data, f"BUDGET_CURRENT_FILE_SUMMARY_{file_order}_{total_files}_{max_tokens}T", url)
        
        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]
            current_file_summary = message.get("content", "").strip()
            
            if not current_file_summary and message.get("reasoning"):
                current_file_summary = message.get("reasoning", "").strip()
            
            if current_file_summary and current_file_summary.strip():
                result_tokens = count_tokens_approximate(current_file_summary)
                
                # CRITICAL: Enforce budget limit
                if result_tokens > max_tokens:
                    logger.warning(f"🚨 CURRENT FILE SUMMARY EXCEEDS BUDGET: {result_tokens} > {max_tokens} tokens - TRUNCATING")
                    estimated_chars = max_tokens * 4
                    current_file_summary = current_file_summary[:estimated_chars] + "..."
                    result_tokens = count_tokens_approximate(current_file_summary)
                
                logger.info(f"✅ Generated budget current file summary: {result_tokens} tokens (budget: {max_tokens})")
                return current_file_summary
            else:
                logger.error("❌ Generated current file summary is empty")
                return None
        else:
            logger.error("❌ No choices returned from OpenRouter API")
            return None
            
    except Exception as e:
        logger.error(f"❌ Error generating budget current file summary: {str(e)}")
        return None


def generate_budget_constrained_overlap_summary(original_page_summary, previous_file_summary, previous_file_content, previous_file_order, current_file_order, total_files, title="", url="", target_language="Czech", max_tokens=250):
    """
    Generate overlap summary with strict token budget constraints.
    
    Args:
        original_page_summary (str): Summary of the entire original page
        previous_file_summary (str): Summary of the previous file
        previous_file_content (str): Full content of the previous file
        previous_file_order (int): Previous file position
        current_file_order (int): Current file position
        total_files (int): Total number of files in sequence
        title (str): Page title for context
        url (str): Source URL for context
        target_language (str): Target language for the summary output
        max_tokens (int): Maximum tokens allowed for this summary
    
    Returns:
        str: Budget-constrained overlap summary or None if failed
    """
    logger.info(f"Generating budget-constrained overlap summary (prev:{previous_file_order}->curr:{current_file_order}, max: {max_tokens} tokens)")
    
    if not OPENROUTER_API_KEY:
        logger.info(f"OpenRouter API key not configured, skipping overlap summary")
        return None
    
    prompt = f"""🤖 EXPERT SEMANTIC BRIDGING SPECIALIST: Create PRECISE overlap summary with STRICT TOKEN BUDGET COMPLIANCE.

## 🚨 CRITICAL: STRICT TOKEN BUDGET COMPLIANCE MANDATORY!
## 🎯 TASK: Create SEMANTIC BRIDGE between PREVIOUS file ({previous_file_order}/{total_files}) and CURRENT file ({current_file_order}/{total_files})

- **OUTPUT LANGUAGE:** {target_language}
- **🚨 CRITICAL TOKEN LIMIT:** {max_tokens} tokens ABSOLUTE MAXIMUM (NON-NEGOTIABLE!)
- **LENGTH:** Maximum 1-2 paragraphs
- **SEMANTIC PRECISION:** EXACT transition details with logical flow preservation. **CRITICAL: Any numerical data used (e.g., agenda item numbers, department counts) MUST be explicitly present in the source content (PREVIOUS FILE CONTENT section). NEVER count elements yourself.**

## INPUT DATA FOR OVERLAP ANALYSIS:

### ORIGINAL PAGE SUMMARY:
{original_page_summary or "Not available"}

### PREVIOUS FILE SUMMARY (File {previous_file_order}/{total_files}):
{previous_file_summary or "Not available"}

### PREVIOUS FILE CONTENT (File {previous_file_order}/{total_files}) - ANALYZE HOW IT ENDED:
{previous_file_content}"""
    
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": url if url else "https://api.openrouter.ai",
            "X-Title": f"HypeDigitaly Budget Overlap Summary (prev:{previous_file_order}->curr:{current_file_order})"
        }
        
        primary_model = OPENROUTER_MODELS[0] if isinstance(OPENROUTER_MODELS, list) else OPENROUTER_MODELS
        payload = {
            "model": primary_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a professional semantic bridging specialist creating ultra-concise transition summaries between file sequences. You NEVER mention specific individual names, personal titles, or detailed information. You focus ONLY on structural boundaries, file positions, and content flow connections. Your responses are extremely brief, factual, and SMS-like in length."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": max_tokens + 50,
            "temperature": 0.0,  # Deterministic output for consistent overlap summaries
            "top_p": OPENROUTER_TOP_P
        }
        
        response = requests_retry_session().post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT * 2
        )
        response.raise_for_status()
        
        data = response.json()
        log_openrouter_token_usage(data, f"BUDGET_OVERLAP_SUMMARY_{previous_file_order}_TO_{current_file_order}_{max_tokens}T", url)
        
        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]
            overlap_summary = message.get("content", "").strip()
            
            if not overlap_summary and message.get("reasoning"):
                overlap_summary = message.get("reasoning", "").strip()
            
            if overlap_summary and overlap_summary.strip():
                result_tokens = count_tokens_approximate(overlap_summary)
                
                # CRITICAL: Enforce budget limit
                if result_tokens > max_tokens:
                    logger.warning(f"🚨 OVERLAP SUMMARY EXCEEDS BUDGET: {result_tokens} > {max_tokens} tokens - TRUNCATING")
                    estimated_chars = max_tokens * 4
                    overlap_summary = overlap_summary[:estimated_chars] + "..."
                    result_tokens = count_tokens_approximate(overlap_summary)
                
                logger.info(f"✅ Generated budget overlap summary: {result_tokens} tokens (budget: {max_tokens})")
                return overlap_summary
            else:
                logger.error("❌ Generated overlap summary is empty")
                return None
        else:
            logger.error("❌ No choices returned from OpenRouter API")
            return None
            
    except Exception as e:
        logger.error(f"❌ Error generating budget overlap summary: {str(e)}")
        return None


def generate_budget_constrained_question_section(url, title, target_language="Czech", page_summary=None, rss_metadata=None, max_tokens=200):
    """
    Generate question section with strict token budget constraints.
    
    Args:
        url (str): Source URL
        title (str): Page title
        target_language (str): Target language for output
        page_summary (str, optional): Source page summary for enhanced context
        rss_metadata (dict, optional): RSS metadata for enhanced context
        max_tokens (int): Maximum tokens allowed for question section
    
    Returns:
        str: Budget-constrained question section or fallback string
    """
    logger.info(f"Generating budget-constrained question section (max: {max_tokens} tokens) for URL: {url}")
    
    # Check if this is an Events RSS URL with rich metadata
    is_events_rss = (rss_metadata and
                    rss_metadata.get('event_metadata') and
                    'eventId=' in url)
    
    if not OPENROUTER_API_KEY:
        logger.info(f"OpenRouter API key not configured, using fallback for question section")
        # Create fallback based on available context
        if is_events_rss:
            event_data = rss_metadata.get('event_metadata', {})
            event_name = event_data.get('event_name', title)
            fallback = f"{event_name} | akce | událost | Co je {event_name}? | Kdy se koná {event_name}? | Kdo organizuje {event_name}?"
        else:
            fallback = f"{title} | informace | kontakt | Co je {title}? | Informace o {title} | Kontakt na {title}"
        return fallback[:max_tokens * 4]  # Rough character limit
    
    # Create budget-constrained prompt
    summary_context = f"\nPage Summary: {page_summary[:300]}..." if page_summary else "\nPage Summary: Not available"
    
    if is_events_rss:
        event_data = rss_metadata.get('event_metadata', {})
        event_context = f"""
Event Details:
- Event Name: {event_data.get('event_name', 'Unknown')}
- Event Type: {event_data.get('event_type', 'Unknown')}
- Organizer: {event_data.get('organizer', 'Unknown')}"""
        
        prompt = f"""🤖 EVENT-SPECIFIC KEYWORD AND QUESTION GENERATOR: Generate event-focused search terms with STRICT TOKEN BUDGET.

## 🚨 CRITICAL TOKEN LIMIT: {max_tokens} tokens MAXIMUM
Generate: 3 event keywords | 3 RAG-optimized questions
Output ONLY pipe-separated string.

URL: {url}
Title: {title}{event_context}{summary_context}
Language: {target_language}"""
    else:
        prompt = f"""🤖 KEYWORD AND QUESTION GENERATOR: Generate search terms with STRICT TOKEN BUDGET.

## 🚨 CRITICAL TOKEN LIMIT: {max_tokens} tokens MAXIMUM
Generate: 3 keywords | 3 RAG-optimized questions
Output ONLY pipe-separated string.

URL: {url}
Title: {title}{summary_context}
Language: {target_language}"""
    
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": url,
            "X-Title": "HypeDigitaly Budget Question Generator"
        }
        
        primary_model = OPENROUTER_MODELS[0] if isinstance(OPENROUTER_MODELS, list) else OPENROUTER_MODELS
        payload = {
            "model": primary_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a professional keyword and question generator specializing in ultra-concise, RAG-optimized search terms. You create pipe-separated keywords and questions without any additional formatting or explanations. Your output is extremely brief and focused."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": max_tokens + 30,
            "temperature": 0.0,  # Deterministic output for consistent question generation
            "top_p": OPENROUTER_TOP_P
        }
        
        response = requests_retry_session().post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        
        data = response.json()
        log_openrouter_token_usage(data, f"BUDGET_QUESTION_SECTION_{max_tokens}T", url)
        
        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]
            question_text = message.get("content", "").strip()
            
            if not question_text and message.get("reasoning"):
                question_text = message.get("reasoning", "").strip()
            
            if question_text and len(question_text) > 10:
                result_tokens = count_tokens_approximate(question_text)
                
                # CRITICAL: Enforce budget limit
                if result_tokens > max_tokens:
                    logger.warning(f"🚨 QUESTION SECTION EXCEEDS BUDGET: {result_tokens} > {max_tokens} tokens - TRUNCATING")
                    estimated_chars = max_tokens * 4
                    question_text = question_text[:estimated_chars]
                    result_tokens = count_tokens_approximate(question_text)
                
                logger.info(f"✅ Generated budget question section: {result_tokens} tokens (budget: {max_tokens})")
                return question_text
            else:
                logger.warning("API response too short, using fallback")
                fallback = f"{title} | informace | kontakt | Co je {title}? | Informace o {title} | Kontakt na {title}"
                return fallback[:max_tokens * 4]
        else:
            logger.warning("No choices from API, using fallback")
            fallback = f"{title} | informace | kontakt | Co je {title}? | Informace o {title} | Kontakt na {title}"
            return fallback[:max_tokens * 4]
            
    except Exception as e:
        logger.error(f"Error generating budget question section: {str(e)}, using fallback")
        fallback = f"{title} | informace | kontakt | Co je {title}? | Informace o {title} | Kontakt na {title}"
        return fallback[:max_tokens * 4]


def create_budget_constrained_metadata_header(url, title, last_modified=None, path=None, rss_metadata=None, source_page_summary=None, file_order=None, total_files=None, current_file_summary=None, overlap_summary=None):
    """
    Create metadata header with ONLY essential static information (no provider info, no script version).
    This creates the minimal static metadata as requested by user.
    
    Args:
        url (str): Source URL of the content
        title (str): Page title
        last_modified (datetime, optional): Last modification date from sitemap
        path (str, optional): Navigation path from sitemap
        rss_metadata (dict, optional): RSS-specific metadata
        source_page_summary (str, optional): Source page summary
        file_order (int, optional): Current file position in sequence
        total_files (int, optional): Total number of files in sequence
        current_file_summary (str, optional): Summary of current file
        overlap_summary (str, optional): Overlap summary
    
    Returns:
        str: Minimal static metadata header
    """
    # Format last modified date
    last_mod_str = "Unknown"
    if last_modified:
        if hasattr(last_modified, 'strftime'):
            last_mod_str = last_modified.strftime("%Y-%m-%d %H:%M:%S")
        else:
            last_mod_str = str(last_modified)
    
    # Create minimal static metadata (no provider info, no script version as requested)
    metadata_lines = [
        f"## 🔗 **ZDROJOVÁ URL:**",
        f"### **{url}**",
        "",
        f"## 📝 **TITULEK:**",
        f"### **{title or 'N/A'}**",
        "",
        f"## 📅 **DATUM POSLEDNÍ MODIFIKACE:**",
        f"### **{last_mod_str}**",
        ""
    ]
    
    # Add file order information if this is a chunked file
    if file_order is not None and total_files is not None:
        metadata_lines.extend([
            f"## 🗂️ **FILE ORDER:**",
            f"### **File {file_order} of {total_files}**",
            ""
        ])
    
    # Add SOURCE PAGE SUMMARY if available
    if source_page_summary and source_page_summary.strip():
        metadata_lines.extend([
            f"## 📊 **SOURCE PAGE SUMMARY:**",
            f"### **{source_page_summary.strip()}**",
            ""
        ])
    
    # Add CURRENT FILE SUMMARY if available
    if current_file_summary and current_file_summary.strip():
        metadata_lines.extend([
            f"## 📋 **CURRENT FILE SUMMARY:**",
            f"### **{current_file_summary.strip()}**",
            ""
        ])
    
    # Add OVERLAP SUMMARY if available
    if overlap_summary and overlap_summary.strip():
        metadata_lines.extend([
            f"## 🔄 **PREVIOUS FILE OVERLAP SUMMARY:**",
            f"### **{overlap_summary.strip()}**",
            ""
        ])
    
    metadata_lines.extend([
        "---",
        "",
        ""  # Empty line before content
    ])
    
    return "\n".join(metadata_lines)


def process_and_save_chunks_with_metadata_budget(chunks, base_title, url, target_language="Czech", upload_to_vector_store=False, vector_store_id=None, enable_deduplication=True, chunking_strategy=None, vector_store_cache=None, last_modified=None, path=None, provider_used=None, rss_metadata=None):
    """
    Process chunks with metadata budget management and save complete files immediately.
    This function generates metadata within budget and creates complete files.
    
    Args:
        chunks (list): List of content chunks from chunk_content_with_metadata_budget()
        base_title (str): Base title for filename generation
        url (str): Source URL
        target_language (str): Target language for metadata generation
        ... (other parameters for save_markdown_to_file)
    
    Returns:
        list: List of saved file paths
    """
    logger.info(f"📋 PROCESSING CHUNKS WITH METADATA BUDGET: {len(chunks)} chunks to process")
    
    if not chunks:
        logger.error("❌ No chunks provided for processing")
        return []
    
    total_chunks = len(chunks)
    saved_files = []
    previous_file_content = None
    previous_file_summary = None
    
    # Generate page summary for the entire content (within budget)
    original_content = '\n'.join(chunk['content'] for chunk in chunks)
    metadata_budget = chunks[0].get('metadata_budget', 2048)  # Get from first chunk
    
    # Calculate metadata token allocation
    allocation = calculate_metadata_token_allocation(metadata_budget)
    
    # Generate page summary once for all chunks
    page_summary = generate_budget_constrained_page_summary(
        original_content, base_title, url, target_language,
        max_tokens=allocation['source_page_summary']
    )
    
    logger.info(f"🔄 PROCESSING {total_chunks} CHUNKS WITH BUDGET-CONSTRAINED METADATA...")
    
    for i, chunk in enumerate(chunks):
        chunk_order = chunk['chunk_number']
        is_last_chunk = (chunk_order == total_chunks)
        chunk_content = chunk['content']
        content_budget = chunk.get('content_budget', 2048)
        
        logger.info(f"📄 Processing chunk {chunk_order}/{total_chunks} (postfix: {chunk['chunk_postfix']})")
        
        # Generate current file summary
        current_file_summary = generate_budget_constrained_current_file_summary(
            chunk_content, chunk_order, total_chunks, base_title, url, target_language,
            max_tokens=allocation['current_file_summary']
        )
        
        # Generate overlap summary (for ALL chunks B-Z, INCLUDING the last one)
        overlap_summary = None
        if chunk_order > 1 and previous_file_content:  # All files except the first (A) should have overlap summary
            overlap_summary = generate_budget_constrained_overlap_summary(
                page_summary, previous_file_summary, previous_file_content,
                chunk_order - 1, chunk_order, total_chunks, base_title, url, target_language,
                max_tokens=allocation['overlap_summary']
            )
        
        # Generate question section
        question_text = generate_budget_constrained_question_section(
            url, base_title, target_language, page_summary, rss_metadata,
            max_tokens=allocation['question_section']
        )
        
        # Create budget-constrained metadata header
        metadata_header = create_budget_constrained_metadata_header(
            url, base_title, last_modified, path, rss_metadata,
            source_page_summary=page_summary,
            file_order=chunk_order,
            total_files=total_chunks,
            current_file_summary=current_file_summary,
            overlap_summary=overlap_summary
        )
        
        # Assemble complete file content: METADATA + QUESTION + CONTENT
        question_section = f"# **QUESTION:**\n\n{question_text}\n\n# **ANSWER:**\n\n" if question_text else ""
        complete_file_content = metadata_header + question_section + chunk_content
        
        # Validate total file size before saving
        total_file_tokens = count_tokens_approximate(complete_file_content)
        vector_store_limit = DEFAULT_MAX_CHUNK_SIZE # The absolute limit is always max_chunk_size
        
        if total_file_tokens > vector_store_limit:
            logger.error(f"🚨 CRITICAL: Complete file exceeds vector store limit!")
            logger.error(f"   📊 File tokens: {total_file_tokens:,}")
            logger.error(f"   🎯 Vector store limit: {vector_store_limit:,}")
            logger.error(f"   ⚠️ EMERGENCY: Truncating metadata as user specified")
            
            # EMERGENCY: User specified that ONLY metadata can be truncated, NEVER content
            metadata_tokens = count_tokens_approximate(metadata_header + question_section)
            content_tokens = count_tokens_approximate(chunk_content)
            overflow = total_file_tokens - vector_store_limit
            
            logger.error(f"   📋 Metadata tokens: {metadata_tokens:,}")
            logger.error(f"   📄 Content tokens: {content_tokens:,} (NEVER TRUNCATED)")
            logger.error(f"   ❌ Overflow: {overflow:,} tokens - REDUCING METADATA ONLY")
            
            # Calculate maximum allowed metadata size
            max_metadata_tokens = vector_store_limit - content_tokens
            if max_metadata_tokens > 0:
                # Truncate metadata to fit
                estimated_chars = max_metadata_tokens * 4
                truncated_metadata_and_question = (metadata_header + question_section)[:estimated_chars] + "..."
                complete_file_content = truncated_metadata_and_question + chunk_content
                
                final_tokens = count_tokens_approximate(complete_file_content)
                logger.warning(f"⚠️ EMERGENCY METADATA TRUNCATION: Reduced to {final_tokens:,} tokens")
            else:
                logger.error(f"❌ CRITICAL ERROR: Content alone exceeds vector store limit!")
                # This should not happen with proper budget allocation
                continue
        
        # Create filename from URL with chunk postfix
        base_filename = create_filename_from_url(url, base_title, rss_metadata)
        
        # Extract pagination info if applicable
        is_paginated = False
        page_number = None
        if "_PAGE" in base_title:
            is_paginated = True
            try:
                page_part = base_title.split("_PAGE")[-1]
                page_number = int(page_part) if page_part.isdigit() else None
            except (ValueError, IndexError):
                page_number = None
        
        # Construct complete filename with chunk postfix
        chunk_filename = construct_final_filename(
            base_filename=base_filename,
            is_paginated=is_paginated,
            page_number=page_number,
            chunk_postfix=chunk['chunk_postfix'],
            dedup_counter=None
        )
        
        # Create unique lookup URL for vector store deduplication
        if "_PAGE" in base_title:
            page_info = base_title.split("_PAGE")[-1]
            chunk_lookup_url = f"{url}#PAGE{page_info}chunk{chunk['chunk_postfix']}"
        else:
            chunk_lookup_url = f"{url}#chunk{chunk['chunk_postfix']}"
        
        logger.info(f"💾 SAVING COMPLETE FILE: {chunk_filename}")
        logger.info(f"   📊 Total file tokens: {count_tokens_approximate(complete_file_content):,}")
        logger.info(f"   📄 Content tokens: {count_tokens_approximate(chunk_content):,} (Target: {content_budget:,})")
        logger.info(f"   📋 Metadata tokens: {count_tokens_approximate(metadata_header + question_section):,} (Target: {metadata_budget + DEFAULT_CONTENT_TOKEN_OFFSET:,})")
        
        # Save complete file directly (bypassing normal save_markdown_to_file content assembly)
        try:
            filepath = os.path.join(OUTPUT_DIR, f"{chunk_filename}.txt")
            
            # Check for file collision and add versioning if needed
            version_counter = 1
            original_filepath = filepath
            while os.path.exists(filepath):
                name, ext = os.path.splitext(original_filepath)
                filepath = f"{name}_V{version_counter}{ext}"
                version_counter += 1
            
            # Save complete file content
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(complete_file_content)
            
            logger.info(f"✅ Saved complete chunk file: {filepath}")
            print(f"✅ Saved: {filepath}")
            
            # Upload to Vector Store if requested
            if upload_to_vector_store and vector_store_id:
                upload_success = upload_and_add_to_vector_store(
                    filepath, vector_store_id, chunk_lookup_url, base_title,
                    enable_deduplication, chunking_strategy, vector_store_cache
                )
                if not upload_success:
                    logger.warning(f"Failed to upload {filepath} to vector store, but file was saved locally")
            
            saved_files.append(filepath)
            
        except Exception as e:
            logger.error(f"❌ Error saving chunk file {chunk_filename}: {str(e)}")
            continue
        
        # Store data for next iteration
        previous_file_content = chunk_content
        previous_file_summary = current_file_summary
    
    logger.info(f"✅ BUDGET-CONSTRAINED PROCESSING COMPLETE: {len(saved_files)}/{total_chunks} chunks saved")
    return saved_files


def process_and_save_chunk_immediately(chunk, base_title, url, target_tokens, total_chunks, target_language, **globals_dict):
    """
    Process a single chunk through OpenRouter and save it immediately to prevent memory issues.
    
    Args:
        chunk (dict): Chunk to process
        base_title (str): Base title for filename generation
        url (str): Source URL
        target_tokens (int): Target token limit
        total_chunks (int): Total number of chunks for cross-references
        target_language (str): Target language for processing
        **globals_dict: All global variables passed from main function
    
    Returns:
        str: Saved file path or None if failed
    """
    try:
        # Use standardized instructions with special section enhancement
        section_instructions = get_standard_summarization_instructions(
            target_tokens,
            target_language,
            url,
            f"{base_title} - {chunk['section_title']}"
        )
        
        # Add section-specific enhancement to the standardized instructions with aggressive preservation
        enhanced_prompt = section_instructions.replace(
            "**🚨 CRITICAL LIST COMPLETENESS:**",
            "**🚨 ABSOLUTE CONTENT PRESERVATION - CHUNKED SECTION:**\n\n**📋 MANDATORY: PRESERVE EVERY SINGLE ITEM:** This is a section from a larger document. You MUST include EVERY contact, phone number, email, and table row from this section. ANY truncation or omission is STRICTLY FORBIDDEN. If you cannot fit everything, you MUST include ALL contact tables completely and truncate only non-essential text.\n\n**🚨 CRITICAL LIST COMPLETENESS:**"
        ).replace(
            "**📊 TOKEN LIMIT CONFLICT RESOLUTION:** If lists exceed token limit:",
            "**🚨 EMERGENCY PRESERVATION PROTOCOL:** If you CANNOT fit all content:\n**PRIORITY 1:** Preserve ALL contact tables (names, phones, emails) - NEVER truncate these\n**PRIORITY 2:** Preserve department structure and hierarchies\n**PRIORITY 3:** Truncate only descriptive text and navigation elements\n\n**📊 TOKEN LIMIT CONFLICT RESOLUTION:** If lists exceed token limit:"
        ) + f"""

## SOURCE:
{chunk['content']}"""
        
        # Process the chunk
        processed_content = None
        try:
            chunk_result = _process_single_chunk_through_openrouter(enhanced_prompt, target_tokens)
            if chunk_result:
                processed_content = chunk_result
                logger.info(f"✅ Successfully processed section {chunk['chunk_number']}")
            else:
                # Emergency fallback for failed chunk
                logger.warning(f"⚠️ Section {chunk['chunk_number']} processing failed, using preservation mode")
                # Use standardized fallback content creation
                processed_content = create_standard_fallback_content(
                    chunk['section_title'],
                    chunk['chunk_number'],
                    total_chunks,
                    base_title,
                    url,
                    chunk['content']
                )
        except Exception as e:
            logger.error(f"❌ Error processing section {chunk['chunk_number']}: {str(e)}")
            # Use fallback content for failed processing
            processed_content = create_standard_fallback_content(
                chunk['section_title'],
                chunk['chunk_number'],
                total_chunks,
                base_title,
                url,
                chunk['content']
            )
        
        # IMMEDIATELY save this processed chunk to file
        if processed_content:
            # Create section-based filename
            section_filename = generate_section_based_filename(
                chunk['section_title'],
                chunk['chunk_number'],
                base_title
            )
            
            # Create cross-reference metadata
            chunk_metadata = f"""
# 📋 Část {chunk['chunk_number']} z {total_chunks} - {base_title}

## 🔗 **ZDROJOVÁ URL:**
### **{url}**

## 📊 **INFORMACE O ČÁSTI:**
- **Sekce**: {chunk['section_title']}
- **Část**: {chunk['chunk_number']} z {total_chunks}
- **Velikost**: {chunk.get('tokens', count_tokens_approximate(chunk.get('content', ''))):,} tokenů
- **Typ obsahu**: Inteligentně rozdělená sekce

🔗 **KOMPLETNÍ PŮVODNÍ DOKUMENT**: {url}

---

"""
            
            # Combine metadata with chunk content
            full_chunk_content = chunk_metadata + processed_content
            
            # Create lookup URL for vector store deduplication
            section_lookup_url = f"{url}#section{chunk['chunk_number']}"
            
            # Save chunk as separate file immediately
            chunk_filepath = save_markdown_to_file(
                full_chunk_content,
                section_filename,  # Use section-based filename
                url,  # Clean original URL for display in metadata
                # Pass through all the arguments from globals
                upload_to_vector_store=globals_dict.get('_current_upload_to_vector_store', False),
                vector_store_id=globals_dict.get('_current_vector_store_id', None),
                enable_deduplication=globals_dict.get('_current_enable_deduplication', True),
                chunking_strategy=globals_dict.get('_current_chunking_strategy', None),
                vector_store_cache=globals_dict.get('_current_vector_store_cache', None),
                last_modified=globals_dict.get('_current_last_modified', None),
                path=globals_dict.get('_current_path', None),
                provider_used=globals_dict.get('_current_provider_used', None),
                rss_metadata=globals_dict.get('_current_rss_metadata', None),
                lookup_url=section_lookup_url  # Use section lookup URL for deduplication
            )
            
            return chunk_filepath
        else:
            logger.error(f"❌ No processed content available for chunk {chunk['chunk_number']}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Error in process_and_save_chunk_immediately: {str(e)}")
        return None


def save_chunked_content_to_multiple_files(chunks, base_title, url, **save_kwargs):
    """
    Save chunked content to multiple files with intelligent naming and cross-references.
    Each file contains complete sections with all their contacts/items.
    
    Args:
        chunks (list): List of content chunks from chunk_massive_content_by_sections()
        base_title (str): Base title for file naming
        url (str): Source URL
        **save_kwargs: Additional arguments for save_markdown_to_file()
    
    Returns:
        list: List of saved file paths
    """
    saved_files = []
    
    for chunk in chunks:
        # Create chunk-specific title and filename
        section_title = chunk.get('section_title', f"Section {chunk['chunk_number']}")
        chunk_title = f"{base_title} - {section_title}"
        
        # Create cross-reference metadata
        chunk_metadata = f"""
# 📋 Část {chunk['chunk_number']} z {len(chunks)} - {base_title}

## 🔗 **ZDROJOVÁ URL:**
### **{url}**

## 📊 **INFORMACE O ČÁSTI:**
- **Sekce**: {section_title}
- **Část**: {chunk['chunk_number']} z {len(chunks)}
- **Velikost**: {chunk.get('processed_tokens', chunk.get('original_tokens', 0)):,} tokenů
- **Typ obsahu**: Inteligentně rozdělená sekce

### 🗂️ **OSTATNÍ ČÁSTI TOHOTO DOKUMENTU:**
"""
        
        # Add references to all other parts
        for other_chunk in chunks:
            if other_chunk['chunk_number'] != chunk['chunk_number']:
                other_filename = f"{sanitize_filename(base_title)}_{other_chunk['chunk_number']:02d}.txt"
                other_title = other_chunk.get('section_title', f"Section {other_chunk['chunk_number']}")
                chunk_metadata += f"- **Část {other_chunk['chunk_number']}**: {other_title} → {other_filename}\n"
        
        chunk_metadata += f"""
🔗 **KOMPLETNÍ PŮVODNÍ DOKUMENT**: {url}

---

"""
        
        # Combine metadata with chunk content
        full_chunk_content = chunk_metadata + chunk['content']
        
        # Create unique filename for this chunk
        chunk_filename = f"{sanitize_filename(base_title)}_{chunk['chunk_number']:02d}"
        
        # Create lookup URL for vector store deduplication
        chunk_lookup_url = f"{url}#legacychunk{chunk['chunk_number']:02d}"
        
        # Save chunk as separate file
        chunk_filepath = save_markdown_to_file(
            full_chunk_content,
            chunk_filename,  # This will get .txt extension added
            url,  # Clean original URL for display in metadata
            lookup_url=chunk_lookup_url,  # Use chunk lookup URL for deduplication
            **save_kwargs
        )
        
        if chunk_filepath:
            saved_files.append(chunk_filepath)
            logger.info(f"✅ Saved chunk {chunk['chunk_number']}/{len(chunks)}: {chunk_filepath}")
        else:
            logger.error(f"❌ Failed to save chunk {chunk['chunk_number']}")
    
    logger.info(f"📁 Chunked content saved to {len(saved_files)} files")
    return saved_files

# ============================================================================
# JINA AI API FUNCTIONS
# ============================================================================

def get_html_content_via_jina(url, remove_selectors=None):
    """Fetch HTML content using Jina AI API for sitemap processing."""
    api_url = MARKDOWN_PROVIDERS["jina"]["api_url_template"].format(url=url)
    headers = {
        "Authorization": f"Bearer {JINA_AI_API_KEY}",
        "X-Return-Format": "html",
        "X-Engine": "browser",
        "X-Proxy": "auto"
    }
    
    # Přidání CSS selektorů pro odstranění nežádoucích částí stránky
    selectors = remove_selectors or JINA_REMOVE_SELECTORS
    if selectors and selectors.strip():
        headers["X-Remove-Selector"] = selectors
        logger.debug(f"Using remove selectors for HTML: {selectors}")
    
    logger.info(f"Fetching HTML content with links summary from: {url}")
    
    try:
        response = requests_retry_session().get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        
        # NOVÉ: Logování celé HTML response pro kontrolu sitemap API odpovědi
        logger.info("=== CELÁ HTML RESPONSE Z JINA AI API PRO SITEMAP ===")
        logger.info(f"Full HTML Response (complete): {response.text}")
        logger.info("=== KONEC HTML RESPONSE ===")
        
        if response.status_code == 200 and response.text:
            html_content = response.text
            logger.info(f"Successfully fetched HTML content from {url}")
            logger.info(f"HTML content length: {len(html_content)} characters")
            
            return html_content
        else:
            logger.error(f"Jina API error for {url}: HTTP Status {response.status_code}")
            return None
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error when fetching HTML from {url}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching HTML from {url}: {str(e)}")
        return None

# ============================================================================
# PAGINATION DETECTION AND PROCESSING FUNCTIONS
# ============================================================================

def get_html_content_for_pagination_via_jina(url, remove_selectors=None):
    """Fetch HTML content using Jina AI API specifically for pagination detection."""
    api_url = MARKDOWN_PROVIDERS["jina"]["api_url_template"].format(url=url)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {JINA_AI_API_KEY}",
        "X-Engine": "browser",
        "X-Return-Format": "html"
    }
    
    # Add CSS selectors for removing unwanted page parts
    selectors = remove_selectors or JINA_REMOVE_SELECTORS
    if selectors and selectors.strip():
        headers["X-Remove-Selector"] = selectors
        logger.debug(f"Using remove selectors for pagination HTML: {selectors}")
    
    logger.info(f"🔍 PAGINATION CHECK: Fetching HTML content for pagination detection from: {url}")
    
    try:
        response = requests_retry_session().get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        
        # NOVÉ: Logování celé HTML response pro kontrolu pagination API odpovědi
        logger.info("=== CELÁ HTML RESPONSE Z JINA AI API PRO PAGINATION DETECTION ===")
        logger.info(f"Full HTML Response (complete): {response.text}")
        logger.info("=== KONEC HTML RESPONSE ===")
        
        if response.status_code == 200 and response.text:
            html_content = response.text
            logger.info(f"✅ Successfully fetched HTML content for pagination check from {url}")
            logger.info(f"📄 HTML content length: {len(html_content)} characters")
            
            return html_content
        else:
            logger.error(f"❌ Jina API error for pagination check {url}: HTTP Status {response.status_code}")
            return None
        
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Request error when fetching HTML for pagination from {url}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"❌ Unexpected error fetching HTML for pagination from {url}: {str(e)}")
        return None

def detect_pagination_in_html(html_content, url=""):
    """
    AI-POWERED PAGINATION DETECTION: Use OpenRouter API with Structured Outputs to intelligently
    analyze HTML content and return validated JSON with pagination patterns.
    
    Uses OpenRouter's JSON Schema validation to ensure consistent, type-safe responses
    compatible with existing script functionality (urljoin with relative URLs).
    
    Args:
        html_content (str): HTML content to analyze for pagination
        url (str): Source URL for logging context
    
    Returns:
        dict: Pagination detection results with indicators and metadata
    """
    logger.info(f"🤖 AI-POWERED PAGINATION DETECTION: Analyzing HTML content via OpenRouter Structured Outputs")
    
    if not html_content or not html_content.strip():
        logger.warning(f"⚠️ No HTML content provided for pagination detection")
        return {
            'has_pagination': False,
            'indicators': [],
            'confidence': 0,
            'reason': 'No HTML content provided'
        }
    
    # Check if OpenRouter is configured
    if not OPENROUTER_API_KEY:
        logger.warning(f"🤖 OpenRouter API key not configured, falling back to legacy pagination detection")
        return _detect_pagination_legacy_fallback(html_content, url)
    
    try:
        # Define JSON Schema for structured output validation
        pagination_schema = {
            "name": "pagination_and_suburls_detection",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "has_pagination": {
                        "type": "boolean",
                        "description": "True if legitimate content pagination is detected (for MORE of the SAME content type)"
                    },
                    "confidence_score": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Confidence level of pagination detection (0-100)"
                    },
                    "content_type_detected": {
                        "type": "string",
                        "description": "Type of content being paginated (e.g., 'news articles', 'contact list', 'documents', 'events')"
                    },
                    "pagination_urls": {
                        "type": "array",
                        "description": "Array of pagination URLs found that are relevant to the current page content",
                        "items": {
                            "type": "object",
                            "properties": {
                                "relative_url": {
                                    "type": "string",
                                    "description": "RELATIVE URL compatible with urljoin() - examples: '?page=1', '?page=2', '/path?stranka=3'"
                                },
                                "page_number": {
                                    "type": ["integer", "null"],
                                    "description": "Page number if determinable from link, null for navigation links"
                                },
                                "link_text": {
                                    "type": "string",
                                    "description": "Visible text content of the pagination link"
                                },
                                "link_type": {
                                    "type": "string",
                                    "enum": ["numbered", "navigation", "first", "last", "next", "previous"],
                                    "description": "Type of pagination link identified"
                                }
                            },
                            "required": ["relative_url", "page_number", "link_text", "link_type"],
                            "additionalProperties": False
                        }
                    },
                    "pagination_indicators": {
                        "type": "array",
                        "description": "List of pagination indicators found during analysis",
                        "items": {
                            "type": "string"
                        }
                    },
                    "suburls": {
                        "type": "array",
                        "description": "Array of direct descendant/child page URLs found (e.g., individual member profiles, detail pages, document pages)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "relative_url": {
                                    "type": "string",
                                    "description": "RELATIVE URL compatible with urljoin() - examples: '/person-name', '/detail/123', '../category/item'"
                                },
                                "link_text": {
                                    "type": "string",
                                    "description": "Visible text content of the suburl link"
                                },
                                "url_type": {
                                    "type": "string",
                                    "description": "Type of suburl (e.g., 'profile', 'detail', 'document', 'member', 'article')"
                                },
                                "is_direct_descendant": {
                                    "type": "boolean",
                                    "description": "Whether this is a direct child page of the current URL"
                                }
                            },
                            "required": ["relative_url", "link_text", "url_type", "is_direct_descendant"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["has_pagination", "confidence_score", "content_type_detected", "pagination_urls", "pagination_indicators", "suburls"],
                "additionalProperties": False
            }
        }
        
        # SYSTEM PROMPT: Instructions, methodology, and examples
        system_prompt = """🤖 EXPERT CONTENT-AWARE PAGINATION ANALYST: You are a specialist who analyzes HTML content to detect pagination patterns and extrapolate the full sequence of numbered pages with CONTENT RELEVANCE VALIDATION using STRUCTURED OUTPUTS.

## 🎯 TASK:
Analyze HTML content and return VALIDATED JSON identifying pagination elements ONLY when they paginate the SAME content type as the current page.

## 🚨 CRITICAL PAGINATION REQUIREMENTS:

**✅ DETECT PAGINATION ONLY FOR:**
- Lists/collections that match the current page content type
- MORE of the SAME content (more news articles, more contacts, more documents, more events, etc.)
- Pagination within the SAME section/category as the current page

**❌ DO NOT DETECT PAGINATION FOR:**
- General site navigation (different website sections)
- Unrelated content pagination (e.g., if on news page but find pagination for products)
- Base domain navigation/menu
- Cross-section pagination (different content types)

## 🧠 ENHANCED ANALYSIS METHODOLOGY:

**STEP 1: IDENTIFY CURRENT PAGE CONTENT TYPE**
First determine what type of content this page contains:
- News/Articles (aktuality, novinky, články, news)
- Contacts/Directory (kontakty, telefonní seznam, seznam, directory)
- Documents/Files (dokumenty, soubory, files, downloads)
- Events/Calendar (události, kalendář, events, calendar)
- Products/Services (produkty, služby, products, services)
- Grants/Funding (dotace, granty, funding, grants)
- Meeting/Council pages (zastupitelstvo, usnesení, rada)
- Other list-based content

**STEP 2: VALIDATE PAGINATION RELEVANCE**
Only detect pagination if it's for MORE of the SAME content type:
- If page shows news articles → pagination should be for more news articles
- If page shows contact list → pagination should be for more contacts
- If page shows documents → pagination should be for more documents
- If page shows grants/funding → pagination should be for more grants

**STEP 3: EXTRACT AND EXTRAPOLATE RELEVANT PAGINATION URLS**
For each validated pagination element:
- Extract href attribute AS-IS (relative format like "?page=1")
- Determine page number from link text or URL parameter
- Classify link type (numbered, navigation, etc.)
- **CRITICAL EXTRAPOLATION:** If a numbered sequence is detected (e.g., 1, 2, 3, ..., 9), you MUST extrapolate and include ALL missing numbered pages (e.g., 6, 7, 8) using the detected URL pattern.

## 🚨 URL FORMAT REQUIREMENTS:
- **ALWAYS return RELATIVE URLs** compatible with urljoin()
- Examples for pagination: "?page=1", "?stranka=2", "/aktuality?page=3"
- **NEVER** include domain/protocol

## 🔍 EXAMPLE ANALYSIS:

**SCENARIO 1: Contact List Page with Pagination (1, 2, 3, 4, 5, ..., 9)**
```html
<div class="strlistovani"><strong>-1-</strong> <a href="?stranka=2">2</a> <a href="?stranka=3">3</a> ... <a href="?stranka=9">9</a></div>
```
**RESULT: ✅ EXTRAPOLATE ALL 9 PAGES** - Return relative URLs for pages 1 through 9, using the detected pattern (e.g., `?stranka=N`).

## 📋 CONFIDENCE SCORING:
- High (80-100): Clear content match + strong pagination indicators
- Medium (60-79): Probable content match + pagination indicators
- Low (40-59): Weak content match or unclear pagination
- None (0-39): No content match or no legitimate pagination"""

        # USER PROMPT: Current context and HTML content to analyze
        user_prompt = f"""**CRITICAL: Return ONLY valid JSON matching the schema. NO markdown, NO explanations, NO code blocks.**

Analyze this HTML for pagination AND suburls, then return PURE JSON.

Current URL: {url}

## PAGINATION DETECTION:
1. Identify content type (e.g., 'contact list', 'news articles')
2. Find pagination for MORE of SAME content type
3. Extrapolate full numbered sequence if detected (e.g., 1,2,3...9)
4. Extract RELATIVE URLs (e.g., "?page=2")

## SUBURLS DETECTION (CRITICAL):
1. Find links to DETAIL/PROFILE pages of items shown on current page
2. Example: If page lists 55 council members, extract all 55 member profile links
3. Look for patterns: `/person-name`, `/member/123`, `/detail/abc`
4. Include ONLY direct children (detail pages), NOT navigation links
5. Extract RELATIVE URLs (e.g., "/mgr-richard-brabec")

## IMPORTANT DISTINCTIONS:
- **PAGINATION** = More items of same type (page 2, page 3)
- **SUBURLS** = Detail pages for current items (member profiles, document details)

Return ONLY valid JSON. Example for page with 11 council members and no pagination:
{{
  "has_pagination": false,
  "confidence_score": 0,
  "content_type_detected": "council_members_directory",
  "pagination_urls": [],
  "pagination_indicators": [],
  "suburls": [
    {{"relative_url": "/mgr-richard-brabec", "link_text": "Mgr. Richard Brabec", "url_type": "profile", "is_direct_descendant": true}},
    {{"relative_url": "/ing-jindra-zalabakova", "link_text": "Ing. Jindra Zalabáková", "url_type": "profile", "is_direct_descendant": true}}
  ]
}}

HTML content to analyze:
{html_content}"""

        # Prepare the OpenRouter API request with structured outputs
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": url if url else "https://api.openrouter.ai",
            "X-Title": "HypeDigitaly AI Content-Aware Pagination Detector"
        }
        
        # Use primary model for pagination detection
        primary_model = OPENROUTER_MODELS[0] if isinstance(OPENROUTER_MODELS, list) else OPENROUTER_MODELS
        payload = {
            "model": primary_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "max_tokens": 20000,  # Increased significantly to prevent JSON truncation due to large suburls list
            "temperature": 0.1,  # Low temperature for consistent analysis
            "top_p": 0.9,
            "response_format": {
                "type": "json_schema",
                "json_schema": pagination_schema
            }
        }
        
        logger.info(f"🤖 Sending HTML content to OpenRouter for content-aware pagination analysis...")
        response = requests_retry_session().post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT * 2
        )
        response.raise_for_status()

        # Log the full API response
        logger.info("=== FULL OPENROUTER API RESPONSE FOR AI PAGINATION DETECTION ===")
        logger.info(f"Full response: {response.text}")
        logger.info("=== END FULL OPENROUTER API RESPONSE ===")

        data = response.json()

        # Log token usage for this API call
        log_openrouter_token_usage(data, "AI_PAGINATION_DETECTION", url)

        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]
            ai_response = message.get("content", "").strip()
            
            # Handle reasoning mode responses
            if not ai_response and message.get("reasoning"):
                ai_response = message.get("reasoning", "").strip()
                logger.info("✅ Extracted AI pagination analysis from reasoning field")
            
            if ai_response:
                try:
                    # Parse structured JSON response (guaranteed valid by OpenRouter schema validation)
                    import json
                    pagination_data = json.loads(ai_response)
                    
                    has_pagination = pagination_data.get('has_pagination', False)
                    confidence = pagination_data.get('confidence_score', 0)
                    content_type = pagination_data.get('content_type_detected', 'unknown')
                    pagination_urls = pagination_data.get('pagination_urls', [])
                    suburls = pagination_data.get('suburls', [])
                    indicators = pagination_data.get('pagination_indicators', [])
                    
                    logger.info(f"🤖 AI PAGINATION & SUBURLS ANALYSIS COMPLETE:")
                    logger.info(f"   📊 Has pagination: {has_pagination}")
                    logger.info(f"   📈 Confidence: {confidence}%")
                    logger.info(f"   📄 Content type: {content_type}")
                    logger.info(f"   🔗 Found {len(pagination_urls)} pagination URLs")
                    logger.info(f"   🔄 Found {len(suburls)} suburls")
                    logger.info(f"   📋 Indicators: {indicators}")
                    
                    if has_pagination:
                        logger.info(f"✅ AI DETECTED CONTENT-RELEVANT PAGINATION with {confidence}% confidence")
                        logger.info(f"📄 Content type being paginated: {content_type}")
                        for i, pag_url in enumerate(pagination_urls, 1):
                            logger.info(f"   {i}. {pag_url.get('link_type', 'unknown')} link: '{pag_url.get('link_text', '')}' -> {pag_url.get('relative_url', '')}")
                    else:
                        logger.info(f"❌ AI: NO CONTENT-RELEVANT PAGINATION DETECTED (confidence: {confidence}%)")
                        logger.info(f"📄 Content type analyzed: {content_type}")
                    
                    if suburls:
                        logger.info(f"✅ AI DETECTED {len(suburls)} SUBURLS:")
                        for i, suburl in enumerate(suburls, 1):
                            logger.info(f"   {i}. {suburl.get('url_type', 'unknown')} suburl: '{suburl.get('link_text', '')}' -> {suburl.get('relative_url', '')}")
                    else:
                        logger.info(f"❌ AI: NO SUBURLS DETECTED")
                    
                    return {
                        'has_pagination': has_pagination,
                        'indicators': indicators,
                        'confidence': confidence,
                        'content_type_detected': content_type,
                        'pagination_urls': pagination_urls,
                        'suburls': suburls,
                        'ai_analysis': True
                    }
                    
                except json.JSONDecodeError as e:
                    logger.error(f"❌ Failed to parse structured AI response as JSON: {str(e)}")
                    logger.error(f"AI structured response was: {ai_response[:500]}...")
                    # This should rarely happen with structured outputs, but fallback just in case
                    logger.info("🔄 Falling back to legacy pagination detection...")
                    return _detect_pagination_legacy_fallback(html_content, url)
            else:
                logger.error("❌ Empty AI response for pagination detection")
                return _detect_pagination_legacy_fallback(html_content, url)
        else:
            logger.error("❌ No choices returned from OpenRouter for pagination detection")
            return _detect_pagination_legacy_fallback(html_content, url)
            
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Request error with OpenRouter API for pagination detection: {str(e)}")
        return _detect_pagination_legacy_fallback(html_content, url)
    except Exception as e:
        logger.error(f"❌ Unexpected error in AI pagination detection: {str(e)}")
        return _detect_pagination_legacy_fallback(html_content, url)

def _detect_pagination_legacy_fallback(html_content, url=""):
    """
    Legacy fallback pagination detection using pattern matching.
    Used when OpenRouter API is not available or fails.
    """
    logger.info(f"🔍 LEGACY PAGINATION DETECTION: Using pattern matching fallback")
    
    try:
        # Parse HTML content
        soup = BeautifulSoup(html_content, 'html.parser')
        pagination_indicators = []
        confidence_score = 0
        
        # ENHANCED PATTERN 1: Look for numbered pagination links with cleaned href attributes
        all_links = soup.find_all('a')  # Get ALL <a> tags, with or without href
        page_number_count = 0
        href_page_count = 0
        no_href_page_count = 0
        
        logger.info(f"🔍 ANALYZING {len(all_links)} <a> tags for numbered pages")
        
        for link in all_links:
            href = link.get('href', '')
            text = link.get_text(strip=True)
            class_names = ' '.join(link.get('class', []))
            
            # CRITICAL FIX: Clean malformed href attributes
            original_href = href
            if href:
                href = href.strip('\'"').replace('\\"', '').replace("\\'", '').replace('\\', '')
                href = href.replace('"', '').replace("'", '').strip()
                logger.debug(f"🧹 Cleaned href in legacy detection: {original_href} -> {href}")
            
            # Enhanced debugging - log potential pagination links
            if (any(css_class in class_names.lower() for css_class in ['pagination', 'pager', 'gov-pagination']) or
                any(param in href.lower() for param in ['page=', 'stranka=']) or
                text.isdigit() or
                'page' in text.lower()):
                logger.info(f"🔍 POTENTIAL PAGINATION LINK: text='{text}' href='{href}' classes='{class_names}'")
            
            # Check if text contains a number (potential page number)
            page_num = None
            clean_text = text
            
            if text.isdigit():
                page_num = int(text)
                clean_text = text
                logger.info(f"🔍 Pure number detected: {page_num}")
            elif 'page' in text.lower():
                page_match = re.search(r'page\s*(\d+)', text.lower())
                if page_match:
                    page_num = int(page_match.group(1))
                    clean_text = page_match.group(1)
                    logger.info(f"🔍 Extracted page number {page_num} from text '{text}'")
            elif re.search(r'\d+$', text):
                number_match = re.search(r'(\d+)$', text)
                if number_match:
                    page_num = int(number_match.group(1))
                    clean_text = number_match.group(1)
                    logger.info(f"🔍 Extracted page number {page_num} from text '{text}'")
            
            # If we found a page number, check pagination context
            if page_num is not None:
                logger.info(f"🎯 FOUND PAGE NUMBER {page_num} - checking context...")
                
                # Case 1: Link with href and pagination parameters
                if href and any(param in href.lower() for param in ['page=', 'stranka=', 'p=', 'pg=', 'pagenum=']):
                    page_number_count += 1
                    href_page_count += 1
                    logger.info(f"📄 COUNTED: numbered page link with href: {clean_text} -> {href}")
                
                # Case 2: Link without href but with pagination CSS classes
                elif not href and any(css_class in class_names.lower()
                                     for css_class in ['pagination', 'pager', 'gov-pagination', 'strlistovani']):
                    page_number_count += 1
                    no_href_page_count += 1
                    logger.info(f"📄 COUNTED: numbered page element without href: {clean_text} (class: {class_names})")
                
                # Case 3: Check if element has pagination-related CSS classes even with href
                elif any(css_class in class_names.lower()
                        for css_class in ['pagination', 'pager', 'gov-pagination', 'strlistovani']):
                    page_number_count += 1
                    if href:
                        href_page_count += 1
                    else:
                        no_href_page_count += 1
                    logger.info(f"📄 COUNTED: pagination CSS element: {clean_text} (class: {class_names}) href: {bool(href)}")
        
        logger.info(f"🔍 NUMBERED PAGE DETECTION COMPLETE: Found {page_number_count} numbered page elements")
        logger.info(f"📊 Breakdown: {href_page_count} with href, {no_href_page_count} without href")
        
        # Look for next/previous navigation
        next_prev_keywords = ['next', 'previous', 'prev', 'last', 'first', 'další', 'předchozí', 'následující', 'poslední', 'první']
        next_prev_count = 0
        
        for element in soup.find_all(['a', 'button']):
            text = element.get_text(strip=True).lower()
            title = element.get('title', '').lower()
            
            if any(keyword in text or keyword in title for keyword in next_prev_keywords):
                next_prev_count += 1
        
        # Calculate confidence
        if page_number_count >= 3:
            pagination_indicators.append(f"numbered_page_elements: {page_number_count}")
            confidence_score += 40
        elif page_number_count >= 1:
            pagination_indicators.append(f"partial_numbered_page_elements: {page_number_count}")
            confidence_score += 20
            
        if next_prev_count >= 1:
            pagination_indicators.append(f"next_prev_navigation: {next_prev_count} elements")
            confidence_score += 25
        
        # Determine if pagination is detected
        has_pagination = confidence_score >= 60  # Threshold for legacy detection
        
        result = {
            'has_pagination': has_pagination,
            'indicators': pagination_indicators,
            'confidence': confidence_score,
            'ai_analysis': False,  # Mark as legacy analysis
            'legacy_fallback': True
        }
        
        if has_pagination:
            logger.info(f"🎯 LEGACY PAGINATION DETECTED: Confidence {confidence_score}%")
        else:
            logger.info(f"❌ LEGACY: NO PAGINATION (confidence: {confidence_score}%)")
        
        return result
        
    except Exception as e:
        logger.error(f"❌ Error in legacy pagination detection: {str(e)}")
        return {
            'has_pagination': False,
            'indicators': [],
            'confidence': 0,
            'reason': f'Legacy detection error: {str(e)}'
        }

def is_url_recursive_enabled(url):
    """
    Check if a URL is configured for recursive suburls processing and return recursion level.
    
    Args:
        url (str): URL to check against the recursive_urls configuration
    
    Returns:
        tuple: (is_enabled, max_recursive_level) - (bool, int)
               - (False, 0) if URL is not configured for recursive processing
               - (True, level) if URL is enabled, where level is the max recursion depth
    """
    if not RECURSIVE_URLS:
        return False, 0
    
    # Normalize URL for comparison
    normalized_url = url.rstrip('/')
    
    for recursive_config in RECURSIVE_URLS:
        # Handle both old format (string) and new format (dict)
        if isinstance(recursive_config, str):
            recursive_url = recursive_config
            recursive_level = 1  # Default for backward compatibility
        else:
            recursive_url = recursive_config.get('url', '')
            recursive_level = recursive_config.get('recursive_level', 1)
        
        recursive_url_normalized = recursive_url.rstrip('/')
        
        # Check if the URL matches exactly or is a descendant of the recursive URL
        if normalized_url == recursive_url_normalized or normalized_url.startswith(recursive_url_normalized + '/'):
            logger.info(f"🔄 URL {url} is enabled for recursive suburls processing (matches: {recursive_url}, max level: {recursive_level})")
            return True, recursive_level
    
    logger.debug(f"URL {url} is not configured for recursive suburls processing")
    return False, 0

def is_url_explicitly_paginated(url):
    """Check if a URL is configured for explicit pagination processing."""
    global PAGINATED_URLS
    if not PAGINATED_URLS:
        return False
    
    # Normalize URL for comparison
    normalized_url = url.rstrip('/')
    
    # Check for exact match in the list of normalized URLs
    if normalized_url in PAGINATED_URLS:
        logger.info(f"🔗 URL {url} is explicitly enabled for pagination processing.")
        return True
        
    logger.debug(f"URL {url} is not explicitly configured for pagination processing.")
    return False

def extract_suburls_from_ai_data(suburls_data, base_url, current_url=""):
    """
    Extract and process suburls from AI-detected data.
    
    Args:
        suburls_data (list): List of suburl dictionaries from AI response
        base_url (str): Base URL for constructing absolute URLs
        current_url (str): Current URL for context and logging
    
    Returns:
        list: List of dictionaries containing suburl URLs and metadata
    """
    logger.info(f"🔄 SUBURLS EXTRACTION: Processing {len(suburls_data)} AI-detected suburls")
    
    suburl_list = []
    seen_urls = set()
    
    for i, suburl_info in enumerate(suburls_data, 1):
        try:
            relative_url = suburl_info.get('relative_url', '')
            link_text = suburl_info.get('link_text', '')
            url_type = suburl_info.get('url_type', 'other')
            is_direct_descendant = suburl_info.get('is_direct_descendant', False)
            
            if not relative_url:
                logger.warning(f"⚠️ Skipping AI suburl with empty relative_url")
                continue
            
            # Convert AI-provided relative URL to absolute URL using urljoin
            try:
                absolute_url = urljoin(base_url, relative_url)
                logger.debug(f"🔧 AI suburl construction: base='{base_url}' + relative='{relative_url}' = '{absolute_url}'")
                
                # Validate the resulting URL
                parsed_result = urlparse(absolute_url)
                if not parsed_result.scheme or not parsed_result.netloc:
                    logger.warning(f"⚠️ Invalid absolute URL generated from AI suburl data: {absolute_url}, skipping")
                    continue
                    
            except Exception as e:
                logger.error(f"❌ Error constructing absolute URL from AI suburl data: base='{base_url}' + relative='{relative_url}': {str(e)}")
                continue
            
            # Skip duplicates
            if absolute_url in seen_urls:
                logger.debug(f"⏭️ Skipping duplicate AI suburl: {absolute_url}")
                continue
            seen_urls.add(absolute_url)
            
            # Skip if it's the same as current URL (self-reference)
            if absolute_url == current_url:
                logger.debug(f"⏭️ Skipping self-reference AI suburl: {absolute_url}")
                continue
            
            # Create suburl info
            url_info = {
                'url': absolute_url,
                'link_text': link_text,
                'url_type': url_type,
                'is_direct_descendant': is_direct_descendant,
                'original_relative_url': relative_url,
                'ai_source': True
            }
            
            suburl_list.append(url_info)
            logger.info(f"✅ Added AI-detected suburl: {absolute_url} (type: {url_type}, text: '{link_text}')")
            
        except Exception as e:
            logger.error(f"❌ Error processing AI suburl {i}: {str(e)}")
            continue
    
    logger.info(f"🔄 AI SUBURLS EXTRACTION COMPLETE: Found {len(suburl_list)} valid suburls")
    return suburl_list

def _construct_pagination_url(current_url, page_number):
    """
    Construct a pagination URL from current URL and page number.
    This handles cases where pagination links don't have href attributes.
    
    Args:
        current_url (str): Current page URL
        page_number (int): Target page number
    
    Returns:
        str: Constructed pagination URL or None if construction fails
    """
    try:
        if not current_url or not page_number:
            return None
        
        # Parse current URL
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parsed = urlparse(current_url)
        query_params = parse_qs(parsed.query)
        
        # Common pagination parameter names to try
        pagination_params = ['stranka', 'page', 'p', 'pg', 'pagenum']
        
        # Check if any pagination parameter already exists
        existing_param = None
        for param in pagination_params:
            if param in query_params:
                existing_param = param
                break
        
        # Use existing parameter or default to 'stranka' (common in Czech sites)
        if existing_param:
            param_name = existing_param
        else:
            # Try to detect which parameter to use based on current URL
            if 'stranka=' in current_url.lower():
                param_name = 'stranka'
            elif 'page=' in current_url.lower():
                param_name = 'page'
            else:
                param_name = 'stranka'  # Default for Czech sites
        
        # Set the page parameter
        query_params[param_name] = [str(page_number)]
        
        # Reconstruct URL
        new_query = urlencode(query_params, doseq=True)
        new_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
        
        logger.debug(f"🔧 Constructed pagination URL: {current_url} + page {page_number} -> {new_url}")
        return new_url
        
    except Exception as e:
        logger.error(f"❌ Error constructing pagination URL: {str(e)}")
        return None

def extract_pagination_urls(html_content, base_url, url="", ai_pagination_data=None):
    """
    Extract all pagination URLs from HTML content with AI-POWERED or legacy detection.
    
    This function can work with AI-detected pagination data (preferred) or fall back to
    manual HTML parsing for various pagination patterns.
    
    Args:
        html_content (str): HTML content containing pagination elements
        base_url (str): Base URL for constructing absolute URLs
        url (str): Current URL for context and logging
        ai_pagination_data (dict, optional): AI-detected pagination data with relative URLs
    
    Returns:
        list: List of dictionaries containing pagination URLs and metadata
    """
    logger.info(f"🔗 PAGINATION EXTRACTION: Processing pagination URLs")
    
    pagination_urls = []
    
    # PREFERRED METHOD: Use AI-detected pagination data if available
    if ai_pagination_data and ai_pagination_data.get('ai_analysis') and ai_pagination_data.get('pagination_urls'):
        logger.info(f"🤖 Using AI-detected pagination data with {len(ai_pagination_data['pagination_urls'])} URLs")
        
        seen_urls = set()
        for ai_pag_url in ai_pagination_data['pagination_urls']:
            try:
                relative_url = ai_pag_url.get('relative_url', '')
                page_number = ai_pag_url.get('page_number')
                link_text = ai_pag_url.get('link_text', '')
                link_type = ai_pag_url.get('link_type', 'unknown')
                
                if not relative_url:
                    logger.warning(f"⚠️ Skipping AI pagination URL with empty relative_url")
                    continue
                
                # Convert AI-provided relative URL to absolute URL using urljoin
                try:
                    absolute_url = urljoin(base_url, relative_url)
                    logger.debug(f"🔧 AI URL construction: base='{base_url}' + relative='{relative_url}' = '{absolute_url}'")
                    
                    # Validate the resulting URL
                    parsed_result = urlparse(absolute_url)
                    if not parsed_result.scheme or not parsed_result.netloc:
                        logger.warning(f"⚠️ Invalid absolute URL generated from AI data: {absolute_url}, skipping")
                        continue
                        
                except Exception as e:
                    logger.error(f"❌ Error constructing absolute URL from AI data: base='{base_url}' + relative='{relative_url}': {str(e)}")
                    continue
                
                # Skip duplicates
                if absolute_url in seen_urls:
                    logger.debug(f"⏭️ Skipping duplicate AI URL: {absolute_url}")
                    continue
                seen_urls.add(absolute_url)
                
                # Skip if it's the same as current URL (self-reference)
                if absolute_url == url:
                    logger.debug(f"⏭️ Skipping self-reference AI URL: {absolute_url}")
                    continue
                
                # Create pagination URL info from AI data
                url_info = {
                    'url': absolute_url,
                    'page_number': page_number,
                    'link_text': link_text,
                    'link_title': '',  # Not provided by AI
                    'link_classes': '',  # Not provided by AI
                    'original_href': relative_url,  # Store original AI relative URL
                    'cleaned_href': relative_url,  # AI URLs are already clean
                    'constructed_url': relative_url,
                    'link_type': link_type,
                    'element_type': 'ai_detected',
                    'ai_source': True  # Mark as AI-detected
                }
                
                pagination_urls.append(url_info)
                logger.info(f"✅ Added AI-detected pagination URL: {absolute_url} (page: {page_number}, text: '{link_text}', type: {link_type})")
                
            except Exception as e:
                logger.error(f"❌ Error processing AI pagination URL: {str(e)}")
                continue
        
        logger.info(f"🤖 AI PAGINATION EXTRACTION COMPLETE: Found {len(pagination_urls)} content-relevant pagination URLs")
        return pagination_urls
    
    # FALLBACK METHOD: Legacy HTML parsing when AI data is not available
    logger.info(f"🔗 Using legacy HTML parsing for pagination URL extraction")
    
    if not html_content or not html_content.strip():
        logger.warning(f"⚠️ No HTML content provided for pagination URL extraction")
        return pagination_urls
    
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        seen_urls = set()
        
        # ENHANCED APPROACH: Find ALL <a> tags (with and without href) and other clickable elements
        
        # Method 1: Traditional links with href attributes
        links_with_href = soup.find_all('a', href=True)
        logger.info(f"🔗 Found {len(links_with_href)} links with href attributes")
        
        # Method 2: Links without href (like Středočeský pagination) but with click handlers or specific classes
        links_without_href = soup.find_all('a')
        links_without_href = [link for link in links_without_href if not link.get('href')]
        logger.info(f"🔗 Found {len(links_without_href)} links without href attributes")
        
        # Method 3: Button elements that might be pagination
        buttons = soup.find_all('button')
        logger.info(f"🔘 Found {len(buttons)} button elements")
        
        # Combine all potential pagination elements
        all_pagination_elements = links_with_href + links_without_href + buttons
        logger.info(f"🎯 Analyzing {len(all_pagination_elements)} total elements for pagination patterns")
        
        for element in all_pagination_elements:
            try:
                href = element.get('href', '').strip()
                text = element.get_text(strip=True)
                title = element.get('title', '').strip()
                class_names = ' '.join(element.get('class', []))
                onclick = element.get('onclick', '').strip()
                
                # CRITICAL FIX: Clean malformed href attributes that contain quotes, backslashes, or other artifacts
                if href:
                    # Remove surrounding quotes (single or double)
                    href = href.strip('\'"')
                    # Remove escaped quotes and backslashes
                    href = href.replace('\\"', '').replace("\\'", '').replace('\\', '')
                    # Remove any remaining quote artifacts
                    href = href.replace('"', '').replace("'", '')
                    # Re-strip after cleaning
                    href = href.strip()
                    logger.debug(f"🧹 Cleaned href: {element.get('href', '')} -> {href}")
                
                # Check if this element looks like a pagination element
                is_pagination_element = False
                page_number = None
                constructed_url = None
                
                # ENHANCED Method 1: Text is a number (page number) - works with or without href
                if text.isdigit():
                    page_number = int(text)
                    
                    if href:
                        # Has href - use it directly (now properly cleaned)
                        is_pagination_element = True
                        constructed_url = href
                        logger.debug(f"📄 Found numbered page link with href: {text} -> {href}")
                    else:
                        # No href - try to construct URL from current page URL and page number
                        if url:
                            # Try to construct pagination URL by replacing/adding page parameter
                            constructed_url = _construct_pagination_url(url, page_number)
                            if constructed_url:
                                is_pagination_element = True
                                logger.debug(f"📄 Constructed numbered page URL: {text} -> {constructed_url}")
                
                # ENHANCED Method 2: Link contains pagination URL parameters (only for href links)
                elif href and any(param in href.lower() for param in ['page=', 'stranka=', 'p=', 'pg=', 'pagenum=']):
                    is_pagination_element = True
                    constructed_url = href
                    
                    # Try to extract page number from URL
                    import re
                    page_match = re.search(r'(?:page|stranka|p|pg|pagenum)=(\d+)', href, re.IGNORECASE)
                    if page_match:
                        page_number = int(page_match.group(1))
                        logger.debug(f"🔗 Found pagination URL parameter: page {page_number} -> {href}")
                
                # ENHANCED Method 3: Next/Previous navigation links
                elif any(keyword in text.lower() or keyword in title.lower()
                        for keyword in ['next', 'previous', 'další', 'předchozí', 'vpred', 'zpet', 'následující']):
                    if href:
                        is_pagination_element = True
                        constructed_url = href
                        logger.debug(f"🔄 Found navigation link: {text} -> {href}")
                    elif onclick:
                        # Try to extract URL from onclick if available
                        url_match = re.search(r'["\']([^"\']*(?:page|stranka)[^"\']*)["\']', onclick)
                        if url_match:
                            is_pagination_element = True
                            constructed_url = url_match.group(1)
                            logger.debug(f"🔄 Found navigation link with onclick: {text} -> {constructed_url}")
                
                # ENHANCED Method 4: CSS classes suggest pagination
                elif any(css_class in class_names.lower()
                        for css_class in ['page', 'pagination', 'pager', 'stranka', 'aktivni', 'gov-pagination']):
                    if href:
                        is_pagination_element = True
                        constructed_url = href
                        logger.debug(f"🎯 Found CSS pagination link: {text} (class: {class_names}) -> {href}")
                    elif text.isdigit() and url:
                        # CSS pagination without href but with page number - construct URL
                        page_number = int(text)
                        constructed_url = _construct_pagination_url(url, page_number)
                        if constructed_url:
                            is_pagination_element = True
                            logger.debug(f"🎯 Constructed CSS pagination URL: {text} (class: {class_names}) -> {constructed_url}")
                
                if is_pagination_element and constructed_url:
                    # Additional validation for constructed_url before urljoin
                    if not constructed_url or constructed_url.isspace():
                        logger.warning(f"⚠️ Empty or whitespace-only constructed_url, skipping element")
                        continue
                        
                    # Convert to absolute URL with enhanced error handling
                    try:
                        absolute_url = urljoin(base_url, constructed_url)
                        logger.debug(f"🔧 URL construction: base='{base_url}' + relative='{constructed_url}' = '{absolute_url}'")
                        
                        # Validate the resulting URL
                        parsed_result = urlparse(absolute_url)
                        if not parsed_result.scheme or not parsed_result.netloc:
                            logger.warning(f"⚠️ Invalid absolute URL generated: {absolute_url}, skipping")
                            continue
                            
                    except Exception as e:
                        logger.error(f"❌ Error constructing absolute URL from base='{base_url}' + relative='{constructed_url}': {str(e)}")
                        continue
                    
                    # Skip duplicates
                    if absolute_url in seen_urls:
                        logger.debug(f"⏭️ Skipping duplicate URL: {absolute_url}")
                        continue
                    seen_urls.add(absolute_url)
                    
                    # Skip if it's the same as current URL (self-reference)
                    if absolute_url == url:
                        logger.debug(f"⏭️ Skipping self-reference URL: {absolute_url}")
                        continue
                    
                    # Create pagination URL info
                    url_info = {
                        'url': absolute_url,
                        'page_number': page_number,
                        'link_text': text,
                        'link_title': title,
                        'link_classes': class_names,
                        'original_href': element.get('href', ''),  # Keep original for debugging
                        'cleaned_href': href,  # Store cleaned version
                        'constructed_url': constructed_url,
                        'link_type': 'numbered' if text.isdigit() else 'navigation',
                        'element_type': element.name
                    }
                    
                    pagination_urls.append(url_info)
                    logger.debug(f"✅ Added pagination URL: {absolute_url} (page: {page_number}, text: '{text}')")
                    
            except Exception as e:
                logger.error(f"❌ Error processing pagination element: {str(e)}")
                continue
        
        # Sort pagination URLs by page number for better organization
        numbered_pages = [url_info for url_info in pagination_urls if url_info['page_number'] is not None]
        navigation_pages = [url_info for url_info in pagination_urls if url_info['page_number'] is None]
        
        # Sort numbered pages by page number
        numbered_pages.sort(key=lambda x: x['page_number'])
        
        # Combine: numbered pages first, then navigation pages
        sorted_pagination_urls = numbered_pages + navigation_pages
        
        logger.info(f"🔗 LEGACY PAGINATION EXTRACTION COMPLETE: Found {len(sorted_pagination_urls)} pagination URLs")
        logger.info(f"📊 Breakdown: {len(numbered_pages)} numbered pages, {len(navigation_pages)} navigation links")
        
        if numbered_pages:
            page_numbers = [url_info['page_number'] for url_info in numbered_pages]
            logger.info(f"📄 Page numbers found: {page_numbers}")
        
        return sorted_pagination_urls
        
    except Exception as e:
        logger.error(f"❌ Error extracting pagination URLs from HTML: {str(e)}")
        return pagination_urls

def process_paginated_url(url, title, path, url_last_modified_map, last_run_timestamp,
                         local_files_cache, enable_resume, vector_store_id, deduplication_enabled,
                         chunking_strategy, vector_store_cache, remove_selectors, rss_metadata=None, current_depth=0):
    """
    Process a URL that may have pagination by first checking for pagination,
    then processing all subpages if pagination is found.
    
    Args:
        url (str): URL to check for pagination and process
        title (str): Original page title
        path (str): Navigation path
        url_last_modified_map (dict): URL to last modified mapping
        last_run_timestamp (datetime): Last run timestamp
        local_files_cache (dict): Local files cache for resume
        enable_resume (bool): Whether resume is enabled
        vector_store_id (str): Vector store ID for uploads
        deduplication_enabled (bool): Whether deduplication is enabled
        chunking_strategy (dict): Chunking strategy for vector store
        vector_store_cache (dict): Vector store cache
        remove_selectors (str): CSS selectors to remove
        rss_metadata (dict, optional): RSS metadata if applicable
        current_depth (int, optional): Current recursion depth (0 = root level)
    
    Returns:
        tuple: (success_count, total_processed) indicating processing results
    """
    logger.info(f"🔍 PAGINATION PROCESSING: Starting pagination check and processing for {url}")
    
    success_count = 0
    total_processed = 0
    
    try:
        # Check if current URL is enabled for recursive suburls processing and get max recursion level
        url_recursive_enabled, max_recursive_level = is_url_recursive_enabled(url)
        
        # Check if URL is explicitly configured for pagination
        url_explicitly_paginated = is_url_explicitly_paginated(url)
        
        # Decide if we need to scrape HTML and run AI detection
        should_run_enhanced_detection = url_recursive_enabled or url_explicitly_paginated
        
        html_content = None
        pagination_result = {'has_pagination': False, 'suburls': [], 'confidence': 0, 'indicators': []}
        has_suburls = False
        
        if should_run_enhanced_detection:
            # Step 1: Get HTML content specifically for pagination detection
            logger.info(f"📄 Step 1: Fetching HTML content for pagination detection (Explicit/Recursive mode)")
            html_content = get_html_content_for_pagination_via_jina(url, remove_selectors)
            
            if not html_content:
                logger.warning(f"⚠️ Failed to get HTML content for pagination check, processing URL normally")
                # Fallback: Process the URL normally without pagination
                return process_single_url_normally(url, title, path, url_last_modified_map, last_run_timestamp,
                                                 local_files_cache, enable_resume, vector_store_id,
                                                 deduplication_enabled, chunking_strategy, vector_store_cache,
                                                 remove_selectors, rss_metadata)
            
            # Step 2: Detect pagination and suburls in the HTML content
            logger.info(f"🔍 Step 2: Analyzing HTML content for pagination indicators and suburls")
            pagination_result = detect_pagination_in_html(html_content, url)
            
            has_suburls = pagination_result.get('suburls', []) and url_recursive_enabled
        else:
            logger.info(f"⏭️ Skipping HTML scrape/AI detection: URL not configured for explicit pagination or recursion.")
            
        
        if not pagination_result['has_pagination'] and not has_suburls:
            logger.info(f"❌ NO PAGINATION OR SUBURLS DETECTED: Processing URL normally")
            logger.info(f"📋 Pagination confidence: {pagination_result['confidence']}%, Indicators: {pagination_result['indicators']}")
            logger.info(f"🔄 Suburls found: {len(pagination_result.get('suburls', []))}, Recursive enabled: {url_recursive_enabled}, Max level: {max_recursive_level}, Current depth: {current_depth}")
            # Process the URL normally without pagination or suburls
            return process_single_url_normally(url, title, path, url_last_modified_map, last_run_timestamp,
                                             local_files_cache, enable_resume, vector_store_id,
                                             deduplication_enabled, chunking_strategy, vector_store_cache,
                                             remove_selectors, rss_metadata)
        
        # Step 3: Extract pagination URLs and suburls
        pagination_urls = []
        suburls = []
        
        if pagination_result['has_pagination']:
            logger.info(f"✅ PAGINATION DETECTED: Confidence {pagination_result['confidence']}% - Extracting pagination URLs")
            logger.info(f"📋 Pagination indicators: {pagination_result['indicators']}")
            print(f"🔍 PAGINATION FOUND: {url}")
            print(f"📊 Confidence: {pagination_result['confidence']}% with {len(pagination_result['indicators'])} indicators")
            
            # SIMPLIFIED APPROACH: ALWAYS use current URL as base for pagination
            # This works for both query parameter (?page=1) and path-based (/page/2) pagination
            logger.info(f"🔗 PAGINATION URL CONSTRUCTION: Always using current URL as base")
            logger.info(f"🏗️ Base URL for pagination: {url}")
            
            # Pass AI pagination data to extract_pagination_urls for intelligent processing
            pagination_urls = extract_pagination_urls(html_content, url, url, ai_pagination_data=pagination_result)
            
            if pagination_urls:
                logger.info(f"🔗 Found {len(pagination_urls)} pagination URLs to process")
                print(f"📄 Found {len(pagination_urls)} pages to process")
            else:
                logger.warning(f"⚠️ Pagination detected but no pagination URLs extracted")
        
        if has_suburls:
            logger.info(f"✅ SUBURLS DETECTED: Found {len(pagination_result['suburls'])} suburls for recursive URL")
            print(f"🔄 SUBURLS FOUND: {len(pagination_result['suburls'])} direct descendants for {url}")
            
            # Extract suburls using AI data
            suburls = extract_suburls_from_ai_data(pagination_result['suburls'], url, url)
            
            if suburls:
                logger.info(f"🔄 Found {len(suburls)} valid suburls to process")
                print(f"🔄 Found {len(suburls)} suburls to process")
            else:
                logger.warning(f"⚠️ Suburls detected but no valid suburls extracted")
        
        # Check if we have anything to process beyond the main URL
        total_additional_urls = len(pagination_urls) + len(suburls)
        if total_additional_urls == 0:
            logger.warning(f"⚠️ Pagination or suburls detected but no additional URLs extracted, processing normally")
            return process_single_url_normally(url, title, path, url_last_modified_map, last_run_timestamp,
                                             local_files_cache, enable_resume, vector_store_id,
                                             deduplication_enabled, chunking_strategy, vector_store_cache,
                                             remove_selectors, rss_metadata)
        
        logger.info(f"🔗 Total additional URLs to process: {total_additional_urls} ({len(pagination_urls)} pagination + {len(suburls)} suburls)")
        print(f"📄 Total additional URLs: {total_additional_urls} ({len(pagination_urls)} pagination + {len(suburls)} suburls)")
        
        # Step 4: Process the original URL first (page 1 or main page)
        
        # CRITICAL CHECK: Re-check the main URL against last modified date and resume cache
        main_url_last_modified = find_url_last_modified(url, url_last_modified_map)
        
        if should_process_url_with_resume(url, main_url_last_modified, last_run_timestamp, local_files_cache, enable_resume, rss_published_date=None, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache):
            logger.info(f"📄 Step 4: Processing original URL (main page): {url}")
            main_result = process_single_url_normally(url, title, path, url_last_modified_map, last_run_timestamp,
                                                    local_files_cache, enable_resume, vector_store_id,
                                                    deduplication_enabled, chunking_strategy, vector_store_cache,
                                                    remove_selectors, rss_metadata)
            
            success_count += main_result[0]
            total_processed += main_result[1]
        else:
            logger.info(f"⏭️ Skipping original URL {url} (timestamp check or already processed locally). Continuing to check for pagination/suburls.")
            total_processed += 1 # Count the main URL as processed/checked
        
        # Step 5: Process all pagination URLs (subpages)
        if pagination_urls:
            logger.info(f"📄 Step 5a: Processing {len(pagination_urls)} pagination subpages")
            
            for i, page_info in enumerate(pagination_urls, 1):
                page_url = page_info['url']
                page_number = page_info.get('page_number')
                page_text = page_info.get('link_text', '')
                
                # Create subpage title with _PAGE[N] postfix
                if page_number is not None:  # Handle page_number=0 correctly (don't treat as falsy)
                    subpage_title = f"{title}_PAGE{page_number}"
                    subpage_path = f"{path} > Page {page_number}"
                else:
                    subpage_title = f"{title}_PAGE{i}"
                    subpage_path = f"{path} > {page_text or f'Page {i}'}"
                
                logger.info(f"📄 Processing pagination subpage {i}/{len(pagination_urls)}: {page_url}")
                logger.info(f"📝 Subpage title: {subpage_title}")
                
                # Process the subpage with all existing functionality
                subpage_result = process_single_url_normally(page_url, subpage_title, subpage_path,
                                                           url_last_modified_map, last_run_timestamp,
                                                           local_files_cache, enable_resume, vector_store_id,
                                                           deduplication_enabled, chunking_strategy,
                                                           vector_store_cache, remove_selectors, rss_metadata)
                
                success_count += subpage_result[0]
                total_processed += subpage_result[1]
                
                # Small delay between subpage requests
                time.sleep(1)
            
            logger.info(f"✅ PAGINATION PROCESSING COMPLETE: Processed {len(pagination_urls)} pagination URLs")
        
        # Step 6: Process all suburls (only if URL is configured for recursive processing AND within depth limit)
        if suburls and url_recursive_enabled:
            # Check if we're within the allowed recursion depth
            if current_depth >= max_recursive_level:
                logger.info(f"🚫 RECURSION DEPTH LIMIT REACHED: Current depth {current_depth} >= max level {max_recursive_level}")
                logger.info(f"⚠️ SUBURLS SKIPPED: Found {len(suburls)} suburls but recursion depth limit reached")
                print(f"🚫 Recursion depth limit reached: {current_depth}/{max_recursive_level} - skipping {len(suburls)} suburls")
            else:
                logger.info(f"🔄 Step 5b: Processing {len(suburls)} suburls (recursive mode enabled, depth {current_depth+1}/{max_recursive_level})")
                print(f"🔄 Processing suburls at depth {current_depth+1}/{max_recursive_level}")
                
                for i, suburl_info in enumerate(suburls, 1):
                    suburl_url = suburl_info['url']
                    suburl_text = suburl_info.get('link_text', '')
                    suburl_type = suburl_info.get('url_type', 'other')
                    
                    # Create suburl title with _SUB postfix to distinguish from pagination
                    suburl_title = f"{title}_SUB_{suburl_type.upper()}_{i}"
                    suburl_path = f"{path} > Suburl: {suburl_text or f'Link {i}'}"
                    
                    logger.info(f"🔄 Processing suburl {i}/{len(suburls)} at depth {current_depth+1}: {suburl_url}")
                    logger.info(f"📝 Suburl title: {suburl_title}")
                    logger.info(f"🏷️ Suburl type: {suburl_type}")
                    logger.info(f"📊 Recursion depth: {current_depth+1}/{max_recursive_level}")
                    
                    # CRITICAL: Check if the suburl should be processed based on last modified date and resume cache
                    suburl_last_modified = find_url_last_modified(suburl_url, url_last_modified_map)
                    
                    if not should_process_url_with_resume(suburl_url, suburl_last_modified, last_run_timestamp, local_files_cache, enable_resume, rss_published_date=None, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache):
                        logger.info(f"⏭️ Skipping recursive suburl {suburl_url} (timestamp check or already processed locally).")
                        total_processed += 1
                        continue
                        
                    # Process the suburl with all existing functionality (including potential recursive pagination)
                    # CRITICAL: Pass current_depth + 1 to track recursion depth
                    suburl_result = process_paginated_url(suburl_url, suburl_title, suburl_path,
                                                         url_last_modified_map, last_run_timestamp,
                                                         local_files_cache, enable_resume, vector_store_id,
                                                         deduplication_enabled, chunking_strategy,
                                                         vector_store_cache, remove_selectors, rss_metadata,
                                                         current_depth=current_depth + 1)
                    
                    success_count += suburl_result[0]
                    total_processed += suburl_result[1]
                    
                    # Small delay between suburl requests
                    time.sleep(1)
                
                logger.info(f"✅ SUBURLS PROCESSING COMPLETE: Processed {len(suburls)} suburls at depth {current_depth+1}")
        elif suburls and not url_recursive_enabled:
            logger.info(f"⚠️ SUBURLS SKIPPED: Found {len(suburls)} suburls but URL not configured for recursive processing")
            print(f"⚠️ Suburls found but skipped (not in recursive_urls config): {len(suburls)}")
        
        total_additional_processed = len(pagination_urls) + (len(suburls) if url_recursive_enabled and current_depth < max_recursive_level else 0)
        logger.info(f"✅ ENHANCED PROCESSING COMPLETE: {success_count}/{total_processed} total URLs processed")
        print(f"✅ Enhanced processing complete: {success_count}/{total_processed} total URLs processed")
        processed_suburls = len(suburls) if url_recursive_enabled and current_depth < max_recursive_level else 0
        print(f"📊 Breakdown: 1 main + {len(pagination_urls)} pagination + {processed_suburls} suburls (depth {current_depth}/{max_recursive_level})")
        
        return (success_count, total_processed)
        
    except Exception as e:
        logger.error(f"❌ Error in pagination processing for {url}: {str(e)}")
        # Fallback: Process URL normally if pagination processing fails
        return process_single_url_normally(url, title, path, url_last_modified_map, last_run_timestamp,
                                         local_files_cache, enable_resume, vector_store_id,
                                         deduplication_enabled, chunking_strategy, vector_store_cache,
                                         remove_selectors, rss_metadata)

def process_single_url_normally(url, title, path, url_last_modified_map, last_run_timestamp,
                               local_files_cache, enable_resume, vector_store_id, deduplication_enabled,
                               chunking_strategy, vector_store_cache, remove_selectors, rss_metadata=None):
    
    # CRITICAL FIX: Override chunking strategy for upload to force maximum chunk size (4096/0 overlap)
    # This prevents OpenAI's internal chunking from breaking logical units (like contact entries).
    # This ensures the entire pre-chunked file is treated as a single unit by the Vector Store indexer.
    upload_chunking_strategy = chunking_strategy
    if vector_store_id:
        try:
            # Use max size (4096) and zero overlap to force single chunk indexing by OpenAI
            upload_chunking_strategy = create_chunking_strategy(
                strategy_type="static",
                max_chunk_size=4096,
                chunk_overlap=0
            )
            logger.info("✅ Overriding upload chunking strategy to STATIC 4096/0 to prevent internal OpenAI chunking.")
        except Exception as e:
            logger.error(f"❌ Failed to create override chunking strategy: {str(e)}. Using default.")
            upload_chunking_strategy = chunking_strategy
    """
    Process a single URL with the existing normal functionality.
    This is extracted from the main function to be reusable for both regular and pagination processing.
    
    Returns:
        tuple: (success_count, total_processed) - (1,1) for success, (0,1) for failure
    """
    try:
        # Get markdown content using existing functionality
        content, api_title, metadata, provider_used = get_markdown_content(url, remove_selectors)
        
        if content:
            # For pagination URLs, preserve the pagination-aware title instead of using API title
            if "_PAGE" in title:
                # This is a paginated subpage - preserve the _PAGE[N] naming
                final_title = title
                logger.info(f"🔖 PAGINATION-AWARE NAMING: Preserving pagination title: {title}")
            else:
                # ENHANCED: Preserve RSS event titles over generic API titles for Events RSS
                if (rss_metadata and
                    rss_metadata.get('event_metadata') and
                    'eventId=' in url and
                    api_title == "Detail akce"):
                    # This is an Events RSS URL with generic "Detail akce" title from API
                    # Preserve the enhanced event title from RSS instead
                    final_title = title
                    logger.info(f"🎪 EVENTS RSS: Preserving RSS event title '{title}' over generic API title '{api_title}'")
                else:
                    # Use title from API response if available, otherwise use provided title
                    final_title = api_title if api_title and api_title.strip() else title
            
            # Generate QUESTION section with RSS metadata for Events RSS
            question_text = generate_question_section(url, final_title, OPENROUTER_TARGET_LANGUAGE, rss_metadata=rss_metadata)
            
            # Find last modified date
            last_modified = find_url_last_modified(url, url_last_modified_map)
            
            # Generate page summary for metadata
            page_summary = None
            if OPENROUTER_API_KEY:
                logger.info(f"📊 Generating page summary for metadata header...")
                page_summary = generate_page_summary_via_openrouter(content, final_title, url, OPENROUTER_TARGET_LANGUAGE)
            
            # Process content (chunking logic from existing code)
            content_tokens = count_tokens_approximate(content)
            logger.info(f"Content size: {content_tokens:,} tokens")
            
            # Check if content needs chunking based on configured content budget
            working_space = DEFAULT_MAX_CHUNK_SIZE - DEFAULT_CONTENT_TOKEN_OFFSET
            content_budget = int(working_space * DEFAULT_CONTENT_RATIO)
            dynamic_metadata_budget = working_space - content_budget
            
            if content_tokens > content_budget:
                # NEW CONTENT SPLITTING SYSTEM: Use dynamic ratio token budget allocation
                logger.info(f"🔀 TRIGGERING NEW CONTENT SPLITTING SYSTEM")
                logger.info(f"📊 Content tokens: {content_tokens:,} > Content budget: {content_budget:,}")
                logger.info(f"🎯 Using dynamic ratio: {content_budget} content + {dynamic_metadata_budget} dynamic metadata + {DEFAULT_CONTENT_TOKEN_OFFSET} offset = {DEFAULT_MAX_CHUNK_SIZE} total")
                
                # Split content using new system with exact 50/50 budget allocation
                chunks = chunk_content_with_metadata_budget(
                    content,
                    DEFAULT_MAX_CHUNK_SIZE,  # Vector store limit from config
                    final_title,
                    url,
                    OPENROUTER_TARGET_LANGUAGE
                )
                
                logger.info(f"📄 NEW SPLITTING SYSTEM: Created {len(chunks)} chunks with metadata budget management")
                
                # Process and save chunks with budget-constrained metadata
                saved_files = process_and_save_chunks_with_metadata_budget(
                    chunks,
                    final_title,
                    url,
                    target_language=OPENROUTER_TARGET_LANGUAGE,
                    upload_to_vector_store=bool(vector_store_id),
                    vector_store_id=vector_store_id,
                    enable_deduplication=deduplication_enabled,
                    chunking_strategy=upload_chunking_strategy,
                    vector_store_cache=vector_store_cache,
                    last_modified=last_modified,
                    path=path,
                    provider_used=provider_used,
                    rss_metadata=rss_metadata
                )
                
                if saved_files:
                    logger.info(f"✅ Successfully processed chunked URL with NEW SYSTEM: {url}")
                    logger.info(f"📁 Created {len(saved_files)} files with 50/50 token budget allocation")
                    return (len(saved_files), 1)  # Multiple files saved, 1 URL processed
                else:
                    logger.error(f"❌ Failed to save chunked files with NEW SYSTEM for: {url}")
                    return (0, 1)
            else:
                # Single file processing (ONLY for content that doesn't need chunking)
                final_content = content
                
                saved_file = save_markdown_to_file(
                    final_content, final_title, url, question_text=question_text,
                    upload_to_vector_store=bool(vector_store_id), vector_store_id=vector_store_id,
                    enable_deduplication=deduplication_enabled, chunking_strategy=upload_chunking_strategy,
                    vector_store_cache=vector_store_cache, last_modified=last_modified, path=path,
                    provider_used=provider_used, rss_metadata=rss_metadata, source_page_summary=page_summary
                )
                
                if saved_file:
                    logger.info(f"✅ Successfully processed single URL: {url}")
                    return (1, 1)
                else:
                    logger.error(f"❌ Failed to save file for: {url}")
                    return (0, 1)
        else:
            logger.error(f"❌ Failed to fetch content for: {url}")
            return (0, 1)
            
    except Exception as e:
        logger.error(f"❌ Error processing single URL {url}: {str(e)}")
        return (0, 1)

def extract_links_from_html_sitemap(html_content, url_last_modified_map={}, last_run_timestamp=None, local_files_cache=None, enable_resume=False, vector_store_id=None, vector_store_cache=None):
    """
    Extract links from HTML sitemap by parsing <a> anchor tags.
    
    Args:
        html_content (str): HTML content of the sitemap
        url_last_modified_map (dict): Mapping of URLs to their last modified dates
        last_run_timestamp (datetime): Timestamp of the last script run
        local_files_cache (dict): Cache of already processed URLs for resume functionality
        enable_resume (bool): Whether resume functionality is enabled
    
    Returns:
        list: List of extracted URL dictionaries with metadata
    """
    logger.info("Extracting links from HTML sitemap by parsing <a> anchor tags")
    
    extracted_urls = []
    
    if not html_content:
        logger.warning("No HTML content provided for link extraction")
        return extracted_urls
    
    try:
        # Parse HTML using BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Find all anchor tags with href attributes
        anchor_tags = soup.find_all('a', href=True)
        
        logger.info(f"Found {len(anchor_tags)} anchor tags with href attributes")
        
        for i, anchor in enumerate(anchor_tags, 1):
            try:
                # Extract URL and text from anchor tag
                url = anchor.get('href', '').strip()
                text = anchor.get_text(strip=True) or anchor.get('title', '').strip()
                
                # Skip if no URL
                if not url:
                    logger.debug(f"Skipping anchor {i}: No URL found")
                    continue
                
                # Make URL absolute if it's relative
                absolute_url = urljoin(BASE_URL, url)
                
                # Skip if URL doesn't belong to our domain
                parsed_url = urlparse(absolute_url)
                if parsed_url.netloc not in [BASE_NETLOC, NON_WWW_BASE_NETLOC]:
                    logger.debug(f"Skipping external URL: {absolute_url}")
                    continue
                
                # Check if URL is blacklisted
                if absolute_url in BLACKLISTED_URLS:
                    logger.info(f"URL {absolute_url} is blacklisted. Skipping.")
                    continue
                
                # Use text as title, fallback to URL path if no text
                title = text if text else parsed_url.path.split('/')[-1] or 'Homepage'
                
                # Create a simple path for sitemap context
                path = f"HTML Sitemap > {title}"
                
                logger.info(f"\n=== Processing URL {i}/{len(anchor_tags)}: {absolute_url} ===")
                logger.info(f"Title: {title}")
                logger.info(f"Path: {path}")
                
                # Find last modified date from XML sitemap
                last_modified = find_url_last_modified(absolute_url, url_last_modified_map)
                
                # Check if the URL should be processed
                if should_process_url_with_resume(absolute_url, last_modified, last_run_timestamp, local_files_cache, enable_resume, rss_published_date=None, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache):
                    extracted_urls.append({
                        'url': absolute_url,
                        'title': title,
                        'path': path,
                        'last_modified': last_modified
                    })
                else:
                    logger.info(f"Skipping URL {absolute_url} (timestamp check or already processed locally).")
                    
            except Exception as e:
                logger.error(f"Error processing anchor tag {i}: {str(e)}")
                continue
                
        logger.info(f"Successfully extracted {len(extracted_urls)} valid URLs from HTML sitemap")
        return extracted_urls
        
    except Exception as e:
        logger.error(f"Error parsing HTML sitemap: {str(e)}")
        return extracted_urls

def get_markdown_content(url, remove_selectors=None):
    """
    Fetches markdown content from a URL using configured providers.
    
    Args:
        url (str): The URL to fetch content from.
        remove_selectors (str): CSS selectors to remove from page
        
    Returns:
        tuple: (content, title, metadata, provider_used) or (None, None, None, None) if all providers fail.
    """
    logger.info(f"Fetching markdown content from: {url}")
    
    sequence_ids = [id.strip() for id in MARKDOWN_PROVIDER_SEQUENCE.split(',') if id.strip()]
    logger.info(f"Using provider sequence: {MARKDOWN_PROVIDER_SEQUENCE}")
    
    if not sequence_ids:
        logger.error("Provider sequence is empty or invalid.")
        return None, None, None, None
    
    for provider_id in sequence_ids:
        config = MARKDOWN_PROVIDERS.get(provider_id)
        if not config:
            logger.warning(f"Provider ID '{provider_id}' not found in configuration. Skipping.")
            continue
            
        provider_name = config["name"]
        api_key = config.get("api_key")
        
        logger.info(f"Attempting to fetch content with provider: {provider_name}")
        
        if not api_key:
            logger.error(f"Missing API key for provider '{provider_name}'. Skipping.")
            continue
            
        # Inner retry loop for the current provider
        for retry_attempt in range(REQUEST_RETRY_COUNT + 1):
            if retry_attempt > 0:
                logger.warning(f"Retry attempt {retry_attempt}/{REQUEST_RETRY_COUNT} for URL: {url} with {provider_name}")
                backoff_time = REQUEST_BACKOFF_FACTOR * (2 ** (retry_attempt - 1))
                logger.info(f"Waiting {backoff_time:.2f} seconds before retry...")
                time.sleep(backoff_time)
                
            try:
                content, title, metadata = None, None, None
                
                if provider_name == "jina":
                    content, title, metadata = _fetch_jina_markdown(url, api_key, remove_selectors)
                elif provider_name == "firecrawl":
                    content, title, metadata = _fetch_firecrawl_markdown(url, api_key)
                
                if content:
                    logger.info(f"Successfully fetched markdown content from {url} using {provider_name}")
                    print(f"\n=== {provider_name.upper()} MARKDOWN CONTENT PREVIEW FOR {url} ===")
                    print(f"Title: {title}")
                    print(f"{content[:500]}...\n")
                    return content, title, metadata, provider_name
                else:
                    logger.error(f"{provider_name} returned no content for {url}")
                    break  # Try next provider
                    
            except requests.exceptions.Timeout as e:
                logger.error(f"Timeout with {provider_name} for {url} (attempt {retry_attempt+1}): {str(e)}")
                if retry_attempt < REQUEST_RETRY_COUNT:
                    continue
                logger.error(f"Max retries reached for {provider_name}. Trying next provider.")
                break
                
            except Exception as e:
                logger.error(f"Error with {provider_name} for {url}: {str(e)}")
                if retry_attempt < REQUEST_RETRY_COUNT:
                    continue
                logger.error(f"Max retries reached for {provider_name}. Trying next provider.")
                break
    
    logger.error(f"Failed to fetch markdown content for {url} from all providers.")
    return None, None, None, None


def _fetch_jina_markdown(url, api_key, remove_selectors=None):
    """Fetch markdown content using Jina AI API with intelligent fallback for 422 errors."""
    api_url = MARKDOWN_PROVIDERS["jina"]["api_url_template"].format(url=url)
    
    # STRATEGY 1: Try markdown with selectors (standard approach)
    logger.info(f"🔄 STRATEGY 1: Attempting markdown fetch with selectors for {url}")
    headers_with_selectors = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Return-Format": "markdown",
        "X-Engine": "browser",
        "X-Proxy": "auto"
    }
    
    # Add CSS selectors for removing unwanted page parts
    selectors = remove_selectors or JINA_REMOVE_SELECTORS
    if selectors and selectors.strip():
        headers_with_selectors["X-Remove-Selector"] = selectors
        logger.debug(f"Using remove selectors: {selectors}")
    
    # Add CSS selectors for targeting specific page content
    if JINA_TARGET_SELECTORS and JINA_TARGET_SELECTORS.strip():
        headers_with_selectors["X-Target-Selector"] = JINA_TARGET_SELECTORS
        logger.debug(f"Using target selectors: {JINA_TARGET_SELECTORS}")
    
    try:
        response = requests_retry_session().get(api_url, headers=headers_with_selectors, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        
        # Log response for debugging
        logger.info("=== JINA AI MARKDOWN RESPONSE (WITH SELECTORS) ===")
        logger.info(f"Full Jina Response (complete): {response.text}")
        logger.info("=== END JINA AI RESPONSE ===")
        
        data = response.json()
        if response.status_code == 200 and data.get("data"):
            content = data["data"].get("content", "")
            title = data["data"].get("title", "")
            if content:
                logger.info(f"✅ STRATEGY 1 SUCCESS: Fetched markdown with selectors")
                return content, title, data["data"]
            else:
                logger.warning("⚠️ STRATEGY 1: Jina returned success but content is missing")
        
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 422:
            logger.warning(f"⚠️ STRATEGY 1 FAILED: 422 Unprocessable Entity with selectors - trying without selectors")
        else:
            logger.error(f"❌ STRATEGY 1 FAILED: HTTP {e.response.status_code} - {str(e)}")
            raise  # Re-raise non-422 errors
    except Exception as e:
        logger.error(f"❌ STRATEGY 1 FAILED: {str(e)}")
        raise  # Re-raise for outer handler
    
    # STRATEGY 2: Try markdown WITHOUT selectors (selectors may cause 422)
    logger.info(f"🔄 STRATEGY 2: Attempting markdown fetch WITHOUT selectors for {url}")
    headers_no_selectors = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Return-Format": "markdown",
        "X-Engine": "browser",
        "X-Proxy": "auto"
    }
    
    try:
        response = requests_retry_session().get(api_url, headers=headers_no_selectors, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        
        logger.info("=== JINA AI MARKDOWN RESPONSE (WITHOUT SELECTORS) ===")
        logger.info(f"Full Jina Response (complete): {response.text}")
        logger.info("=== END JINA AI RESPONSE ===")
        
        data = response.json()
        if response.status_code == 200 and data.get("data"):
            content = data["data"].get("content", "")
            title = data["data"].get("title", "")
            if content:
                logger.info(f"✅ STRATEGY 2 SUCCESS: Fetched markdown without selectors")
                return content, title, data["data"]
            else:
                logger.warning("⚠️ STRATEGY 2: Jina returned success but content is missing")
        
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 422:
            logger.warning(f"⚠️ STRATEGY 2 FAILED: 422 Unprocessable Entity without selectors - trying HTML fallback")
        else:
            logger.error(f"❌ STRATEGY 2 FAILED: HTTP {e.response.status_code} - {str(e)}")
            raise
    except Exception as e:
        logger.error(f"❌ STRATEGY 2 FAILED: {str(e)}")
        raise
    
    # STRATEGY 3: Fallback to HTML and extract text content
    logger.info(f"🔄 STRATEGY 3: Fetching HTML and extracting text content for {url}")
    headers_html = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Return-Format": "html",
        "X-Engine": "browser",
        "X-Proxy": "auto"
    }
    
    try:
        response = requests_retry_session().get(api_url, headers=headers_html, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        
        logger.info("=== JINA AI HTML FALLBACK RESPONSE ===")
        logger.info(f"Response status: {response.status_code}")
        logger.info(f"Response length: {len(response.text)} characters")
        logger.info("=== END JINA AI HTML FALLBACK ===")
        
        data = response.json()
        if response.status_code == 200 and data.get("data"):
            html_content = data["data"].get("html", "")
            title = data["data"].get("title", "")
            
            if html_content:
                # Convert HTML to basic markdown/text
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(html_content, 'html.parser')
                    
                    # Remove script, style, and other non-content tags
                    for tag in soup(['script', 'style', 'meta', 'link', 'noscript']):
                        tag.decompose()
                    
                    # Extract text content
                    text_content = soup.get_text(separator='\n', strip=True)
                    
                    # Basic cleanup
                    lines = [line.strip() for line in text_content.split('\n') if line.strip()]
                    cleaned_content = '\n\n'.join(lines)
                    
                    if cleaned_content:
                        logger.info(f"✅ STRATEGY 3 SUCCESS: Converted HTML to text ({len(cleaned_content)} chars)")
                        logger.warning(f"⚠️ NOTE: Using HTML-to-text conversion fallback (not true markdown)")
                        return cleaned_content, title, data["data"]
                    else:
                        logger.error("❌ STRATEGY 3: HTML conversion resulted in empty content")
                        return None, None, None
                        
                except Exception as e:
                    logger.error(f"❌ STRATEGY 3: HTML-to-text conversion failed: {str(e)}")
                    return None, None, None
            else:
                logger.error("❌ STRATEGY 3: Jina returned HTML response but content is missing")
                return None, None, None
        else:
            error_message = data.get("error", {}).get("message", f"HTTP Status {response.status_code}")
            logger.error(f"❌ STRATEGY 3 FAILED: Jina HTML error: {error_message}")
            return None, None, None
            
    except Exception as e:
        logger.error(f"❌ STRATEGY 3 FAILED: {str(e)}")
        return None, None, None


def _fetch_firecrawl_markdown(url, api_key):
    """Fetch markdown content using Firecrawl API."""
    api_url = "https://api.firecrawl.dev/v1/scrape"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}'
    }
    payload = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "parsePDF": True,
        "maxAge": 14400000
    }
    
    response = requests_retry_session().post(api_url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    
    data = response.json()
    if response.status_code == 200 and data.get("success") and data.get("data"):
        content = data["data"].get("markdown", "")
        fc_metadata = data["data"].get("metadata", {})
        title = fc_metadata.get("title", url.split('/')[-1])
        
        if content:
            return content, title, fc_metadata
        else:
            logger.error("Firecrawl API returned successful status but markdown content is missing")
            return None, None, None
    else:
        error_message = data.get("error", f"HTTP Status {response.status_code} or success=false")
        logger.error(f"Firecrawl API error: {error_message}")
        return None, None, None

# ============================================================================
# OPENAI API FUNCTIONS
# ============================================================================

def upload_file_to_openai(filepath):
    """Upload a file to OpenAI Files API and return the file ID."""
    logger.info(f"Uploading file to OpenAI: {filepath}")
    
    if not os.path.exists(filepath):
        logger.error(f"File does not exist: {filepath}")
        return None
    
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "assistants=v2"
    }
    
    try:
        with open(filepath, 'rb') as file:
            files = {
                'file': (os.path.basename(filepath), file, 'text/plain'),
                'purpose': (None, 'assistants')
            }
            
            response = requests_retry_session().post(
                f"{OPENAI_API_BASE_URL}/files",
                headers=headers,
                files=files,
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            
            data = response.json()
            file_id = data.get('id')
            
            if file_id:
                logger.info(f"Successfully uploaded file to OpenAI. File ID: {file_id}")
                return file_id
            else:
                logger.error(f"OpenAI file upload failed: No file ID returned")
                return None
                
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error uploading file to OpenAI: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error uploading file to OpenAI: {str(e)}")
        return None


def add_file_to_vector_store(file_id, vector_store_id, attributes=None, chunking_strategy=None):
    """Add a file to an OpenAI Vector Store."""
    logger.info(f"Adding file {file_id} to vector store {vector_store_id}")
    
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2"
    }
    
    payload = {
        "file_id": file_id
    }
    
    if attributes:
        payload["attributes"] = attributes
    
    if chunking_strategy:
        payload["chunking_strategy"] = chunking_strategy
        logger.info(f"Using chunking strategy: {chunking_strategy}")
    
    try:
        response = requests_retry_session().post(
            f"{OPENAI_API_BASE_URL}/vector_stores/{vector_store_id}/files",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        
        data = response.json()
        if data.get('status') in ['completed', 'in_progress']:
            logger.info(f"Successfully added file to vector store. Status: {data.get('status')}")
            return data
        else:
            logger.error(f"Failed to add file to vector store. Status: {data.get('status')}, Error: {data.get('last_error')}")
            return None
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error adding file to vector store: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error adding file to vector store: {str(e)}")
        return None


def list_vector_store_files(vector_store_id, limit=100):
    """List all files in a vector store."""
    logger.info(f"Listing files in vector store {vector_store_id}")
    
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2"
    }
    
    all_files = []
    after = None
    
    try:
        while True:
            params = {"limit": limit}
            if after:
                params["after"] = after
            
            response = requests_retry_session().get(
                f"{OPENAI_API_BASE_URL}/vector_stores/{vector_store_id}/files",
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            
            data = response.json()
            files = data.get('data', [])
            all_files.extend(files)
            
            if not data.get('has_more', False):
                break
                
            after = data.get('last_id')
            
        logger.info(f"Retrieved {len(all_files)} files from vector store")
        return all_files
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error listing vector store files: {str(e)}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error listing vector store files: {str(e)}")
        return []


def get_vector_store_file_attributes(vector_store_id, file_id):
    """Get attributes of a specific file in the vector store."""
    logger.debug(f"Getting attributes for file {file_id} in vector store {vector_store_id}")
    
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2"
    }
    
    try:
        response = requests_retry_session().get(
            f"{OPENAI_API_BASE_URL}/vector_stores/{vector_store_id}/files/{file_id}",
            headers=headers,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        
        data = response.json()
        return data.get('attributes', {})
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error getting file attributes: {str(e)}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error getting file attributes: {str(e)}")
        return {}


def find_existing_file_by_url(vector_store_id, lookup_url):
    """
    Find an existing file in vector store by lookup URL.
    
    ⚠️ WARNING: This is the SLOW legacy method!
    Use find_existing_file_by_url_cached() with pre-built cache instead.
    This method is O(n*m) complexity and makes many API calls.
    """
    logger.warning(f"Using SLOW legacy lookup for lookup URL: {lookup_url} (consider using cache)")
    
    # Extract base URL without fragment for broader matching
    base_url = lookup_url.split('#')[0] if '#' in lookup_url else lookup_url
    
    # Get all files in the vector store
    files = list_vector_store_files(vector_store_id)
    
    matching_files = []
    for file_info in files:
        file_id = file_info.get('id')
        if not file_id:
            continue
            
        # Get file attributes
        attributes = get_vector_store_file_attributes(vector_store_id, file_id)
        # Use lookup_source_url for deduplication, fallback to source_url for backward compatibility
        file_lookup_url = attributes.get('lookup_source_url') or attributes.get('source_url')
        
        if file_lookup_url:
            # Extract base URL from file's lookup URL
            file_base_url = file_lookup_url.split('#')[0] if '#' in file_lookup_url else file_lookup_url
            
            if file_base_url == base_url:
                logger.info(f"Found existing file {file_id} with matching base URL: {base_url}")
                matching_files.append(file_info)
    
    if not matching_files:
        logger.info(f"No existing files found with base URL: {base_url}")
        return None
    else:
        logger.info(f"Found {len(matching_files)} existing files with base URL: {base_url}")
        return matching_files[0]  # Return first match for backward compatibility


def delete_vector_store_file(vector_store_id, file_id):
    """Delete a file from the vector store (but not the actual file)."""
    logger.info(f"Deleting file {file_id} from vector store {vector_store_id}")
    
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2"
    }
    
    try:
        response = requests_retry_session().delete(
            f"{OPENAI_API_BASE_URL}/vector_stores/{vector_store_id}/files/{file_id}",
            headers=headers,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        
        data = response.json()
        if data.get('deleted'):
            logger.info(f"Successfully deleted file {file_id} from vector store")
            return True
        else:
            logger.error(f"Failed to delete file {file_id} from vector store")
            return False
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error deleting file from vector store: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error deleting file from vector store: {str(e)}")
        return False


def build_vector_store_cache(vector_store_id):
    """Build a fast lookup cache of all files in Vector Store with their metadata."""
    logger.info(f"Building Vector Store cache for {vector_store_id}")
    print(f"🔄 Building Vector Store cache...")
    start_time = time.time()
    
    # Get all files in vector store
    files = list_vector_store_files(vector_store_id)
    logger.info(f"Found {len(files)} files in Vector Store")
    print(f"📁 Found {len(files)} files in Vector Store")
    
    if len(files) == 0:
        logger.info("Vector Store is empty, skipping cache build")
        print("🗂️  Vector Store is empty, no cache needed")
        return {}
    
    # Build URL lookup cache
    url_to_file_cache = {}
    metadata_fetch_count = 0
    
    for i, file_info in enumerate(files, 1):
        file_id = file_info.get('id')
        if not file_id:
            continue
            
        # Progress indicator for large Vector Stores
        if len(files) > 10 and i % max(1, len(files) // 10) == 0:
            progress = (i / len(files)) * 100
            elapsed = time.time() - start_time
            print(f"📊 Cache progress: {progress:.0f}% ({i}/{len(files)}) - {elapsed:.1f}s elapsed")
            
        # Get file attributes (this is the expensive operation)
        try:
            attributes = get_vector_store_file_attributes(vector_store_id, file_id)
            metadata_fetch_count += 1
        except Exception as e:
            logger.error(f"Failed to get attributes for file {file_id}: {str(e)}")
            continue
        
        # Use lookup_source_url for deduplication cache, fallback to source_url for backward compatibility
        lookup_url = attributes.get('lookup_source_url') or attributes.get('source_url')
        if lookup_url:
            # Normalize the URL before caching it as the key
            normalized_lookup_url = normalize_url_query_params(lookup_url)
            url_to_file_cache[normalized_lookup_url] = {
                'file_id': file_id,
                'file_info': file_info,
                'attributes': attributes
            }
    
    elapsed_time = time.time() - start_time
    logger.info(f"Built Vector Store cache in {elapsed_time:.2f}s with {metadata_fetch_count} metadata fetches")
    logger.info(f"Cache contains {len(url_to_file_cache)} files with source URLs")
    print(f"✅ Cache built: {len(url_to_file_cache)} files indexed in {elapsed_time:.1f}s")
    
    return url_to_file_cache


def find_existing_file_by_url_cached(lookup_url, cache):
    """Find an existing file in vector store by lookup URL using pre-built cache (backward compatibility)."""
    normalized_lookup_url = normalize_url_query_params(lookup_url)
    logger.debug(f"Searching cache for existing file with normalized lookup URL: {normalized_lookup_url}")
    
    cached_file = cache.get(normalized_lookup_url)
    if cached_file:
        file_id = cached_file['file_id']
        logger.info(f"Found existing file {file_id} in cache for lookup URL: {lookup_url} (normalized: {normalized_lookup_url})")
        return cached_file['file_info']
    
    logger.debug(f"No existing file found in cache for lookup URL: {lookup_url}")
    return None


def find_existing_files_by_url_cached(lookup_url, cache):
    """Find ALL existing files in vector store by lookup URL using pre-built cache (including [A-Z] variants and summarized variants)."""
    
    # Normalize the input URL before extracting the base URL
    normalized_input_url = normalize_url_query_params(lookup_url)
    base_url = normalized_input_url.split('#')[0] if '#' in normalized_input_url else normalized_input_url
    
    logger.debug(f"Searching cache for ALL existing files with normalized base URL: {base_url}")
    
    matching_files = []
    for cached_url, cached_file in cache.items():
        # The cached_url is already normalized (Step 2)
        cached_base_url = cached_url.split('#')[0] if '#' in cached_url else cached_url
        
        # Match if base URLs are the same (covers original, #chunk[A-Z], and #summarized[A-Z] variants)
        if cached_base_url == base_url:
            file_id = cached_file['file_id']
            logger.info(f"Found existing file {file_id} in cache for base URL: {base_url} (cached as: {cached_url})")
            matching_files.append(cached_file['file_info'])
    
    if not matching_files:
        logger.debug(f"No existing files found in cache for base URL: {base_url}")
    else:
        logger.info(f"Found {len(matching_files)} existing files in cache for base URL: {base_url} (including all variants)")
    
    return matching_files


def create_chunking_strategy(strategy_type="auto", max_chunk_size=800, chunk_overlap=400):
    """
    Create a chunking strategy object for OpenAI Vector Store with full compliance to OpenAI documentation.
    
    Per OpenAI Vector Store API documentation:
    - AUTO strategy: Fixed 800 tokens per chunk, 400 overlap (not customizable)
    - STATIC strategy: Custom values with strict constraints:
      * max_chunk_size_tokens: 100-4096 (minimum 100, maximum 4096)
      * chunk_overlap_tokens: ≥0, must NOT exceed half of max_chunk_size_tokens
    
    Args:
        strategy_type (str): "auto" or "static"
        max_chunk_size (int): Only used for static strategy (100-4096)
        chunk_overlap (int): Only used for static strategy (≥0, ≤max_chunk_size/2)
    
    Returns:
        dict: Valid chunking strategy object for OpenAI Vector Store API
    
    Raises:
        ValueError: If static strategy parameters violate OpenAI constraints
    """
    if strategy_type.lower() == "auto":
        # AUTO strategy uses FIXED OpenAI defaults - custom values are ignored per documentation
        if max_chunk_size != 800 or chunk_overlap != 400:
            logger.warning(f"AUTO strategy ignores custom values. Using OpenAI fixed defaults: 800 tokens, 400 overlap")
        
        logger.info("Using AUTO chunking strategy (OpenAI default: 800 tokens per chunk, 400 overlap)")
        return {
            "type": "auto"
        }
    elif strategy_type.lower() == "static":
        # VALIDATE STATIC STRATEGY per OpenAI Vector Store API constraints
        
        # Constraint 1: max_chunk_size_tokens range validation
        if not isinstance(max_chunk_size, int) or max_chunk_size < 100 or max_chunk_size > 4096:
            raise ValueError(f"OpenAI constraint violation: max_chunk_size must be integer between 100-4096 tokens, got: {max_chunk_size}")
        
        # Constraint 2: chunk_overlap_tokens non-negative validation
        if not isinstance(chunk_overlap, int) or chunk_overlap < 0:
            raise ValueError(f"OpenAI constraint violation: chunk_overlap must be non-negative integer, got: {chunk_overlap}")
        
        # Constraint 3: CRITICAL OpenAI constraint - overlap must NOT exceed half of max_chunk_size
        max_allowed_overlap = max_chunk_size // 2
        if chunk_overlap > max_allowed_overlap:
            raise ValueError(f"OpenAI constraint violation: chunk_overlap ({chunk_overlap}) must NOT exceed half of max_chunk_size. "
                           f"For chunk_size={max_chunk_size}, maximum allowed overlap is {max_allowed_overlap}")
        
        logger.info(f"Using STATIC chunking strategy ({max_chunk_size} tokens per chunk, {chunk_overlap} overlap)")
        logger.info(f"OpenAI validation passed: chunk_overlap ({chunk_overlap}) <= max_chunk_size/2 ({max_allowed_overlap})")
        
        # Log optimization hints for user
        if chunk_overlap == 0:
            logger.info(f"OPTIMIZATION: Zero overlap configured for maximum processing efficiency")
        if max_chunk_size == 4096:
            logger.info(f"OPTIMIZATION: Maximum chunk size (4096) configured for largest context windows")
        
        return {
            "type": "static",
            "static": {
                "max_chunk_size_tokens": max_chunk_size,
                "chunk_overlap_tokens": chunk_overlap
            }
        }
    else:
        logger.warning(f"Unknown chunking strategy '{strategy_type}', falling back to AUTO (OpenAI default)")
        return {
            "type": "auto"
        }


def upload_and_add_to_vector_store(filepath, vector_store_id, url=None, title=None, enable_deduplication=True, chunking_strategy=None, vector_store_cache=None):
    """Complete process to upload file to OpenAI and add to vector store with deduplication."""
    logger.info(f"Starting upload and vector store process for: {filepath}")
    
    # 🚨 CRITICAL PRE-UPLOAD VALIDATION: Check file size before uploading to Vector Store
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                file_content = f.read()
                file_tokens = count_tokens_approximate(file_content)
                
            # Get the max chunk size from chunking strategy or use default
            max_allowed_tokens = DEFAULT_MAX_CHUNK_SIZE
            if chunking_strategy and chunking_strategy.get("type") == "static":
                static_config = chunking_strategy.get("static", {})
                max_allowed_tokens = static_config.get("max_chunk_size_tokens", DEFAULT_MAX_CHUNK_SIZE)
            
            logger.info(f"📊 PRE-UPLOAD VALIDATION: File has {file_tokens:,} tokens, limit is {max_allowed_tokens:,}")
            
            # REJECT files that exceed Vector Store limits
            if file_tokens > max_allowed_tokens:
                logger.error(f"🚨 VECTOR STORE SIZE VIOLATION: File {filepath} has {file_tokens:,} tokens > limit {max_allowed_tokens:,}")
                logger.error(f"❌ UPLOAD REJECTED: This file would be rejected by OpenAI Vector Store")
                logger.error(f"💡 File must be re-chunked before upload")
                print(f"🚨 UPLOAD BLOCKED: {os.path.basename(filepath)} ({file_tokens:,} tokens > {max_allowed_tokens:,} limit)")
                return False
            else:
                logger.info(f"✅ PRE-UPLOAD VALIDATION PASSED: File size within Vector Store limits")
        else:
            logger.error(f"❌ File does not exist for validation: {filepath}")
            return False
    except Exception as e:
        logger.error(f"❌ Error during pre-upload validation: {str(e)}")
        return False
    
    # Extract base URL and lookup URL for deduplication
    base_url = url.split('#')[0] if url and '#' in url else url
    lookup_url = url  # This will be used for deduplication (can include #chunkA, #summarizedB etc.)
    
    # Step 1: Check for existing files if deduplication is enabled (includes all variants: original, #chunk[A-Z], #summarized[A-Z])
    existing_files = []
    if enable_deduplication and lookup_url:
        if vector_store_cache is not None:
            # Use fast cache lookup to find ALL files for this URL (including all variants)
            existing_files = find_existing_files_by_url_cached(lookup_url, vector_store_cache)
        else:
            # Fallback to slow API lookup - now updated to handle variants
            existing_file = find_existing_file_by_url(vector_store_id, lookup_url)
            if existing_file:
                existing_files = [existing_file]
            
        if existing_files:
            logger.info(f"Found {len(existing_files)} existing file(s) for base URL: {base_url} (including all chunk variants)")
            print(f"🔄 Found {len(existing_files)} existing file(s) for base URL: {base_url}")
            
            # Delete ALL old files from vector store (including original, #chunk[A-Z], and #summarized[A-Z] variants)
            deleted_count = 0
            for existing_file_item in existing_files:
                existing_file_id = existing_file_item.get('id')
                if delete_vector_store_file(vector_store_id, existing_file_id):
                    logger.info(f"Deleted old file {existing_file_id} from vector store")
                    deleted_count += 1
                else:
                    logger.warning(f"Failed to delete old file {existing_file_id}, continuing with upload")
            
            print(f"🗑️  Deleted {deleted_count}/{len(existing_files)} old files (all variants)")
            
            # Remove ALL variants from cache to keep it accurate
            if vector_store_cache is not None:
                urls_to_remove = []
                for cached_url in vector_store_cache.keys():
                    cached_base_url = cached_url.split('#')[0] if '#' in cached_url else cached_url
                    if cached_base_url == base_url:
                        urls_to_remove.append(cached_url)
                
                for url_to_remove in urls_to_remove:
                    del vector_store_cache[url_to_remove]
                    logger.debug(f"Removed {url_to_remove} from Vector Store cache")
    
    # Step 2: Upload file to OpenAI
    file_id = upload_file_to_openai(filepath)
    if not file_id:
        logger.error(f"Failed to upload file to OpenAI: {filepath}")
        return False
    
    # Step 3: Prepare attributes for the vector store file
    attributes = {}
    if url:
        # Store clean original URL for display in metadata
        attributes['source_url'] = base_url[:512]  # Max 512 characters - clean original URL
        # Store lookup URL for deduplication (can include fragments)
        attributes['lookup_source_url'] = lookup_url[:512]  # Max 512 characters - for deduplication
    if title:
        attributes['title'] = title[:512]  # Max 512 characters
    
    # Add some metadata
    attributes['upload_timestamp'] = datetime.now().isoformat()[:512]
    attributes['script_name'] = SCRIPT_NAME[:512]
    
    # Step 4: Add file to vector store
    result = add_file_to_vector_store(file_id, vector_store_id, attributes, chunking_strategy)
    if result:
        action = "Replaced" if existing_files else "Uploaded"
        logger.info(f"Successfully processed file {filepath} to vector store")
        print(f"✅ {action} in Vector Store: {filepath} (File ID: {file_id})")
        return True
    else:
        logger.error(f"Failed to add file to vector store: {filepath}")
        return False

# ============================================================================
# OPENROUTER API FUNCTIONS
# ============================================================================

def count_tokens_approximate(text):
    """
    Approximate token counting function.
    Uses a rough estimate of ~4 characters per token which is typical for most languages.
    This is more conservative than the actual OpenAI tokenization but ensures we stay under limits.
    """
    if not text:
        return 0
    # Conservative estimate: 4 characters = 1 token
    return len(text) // 4

def create_summarization_prompt(markdown_content, target_tokens=4000, target_language="English", url="", title=""):
    """
    Create optimized summarization prompt for RAG content processing.
    Uses standardized instructions to ensure consistency across all summarization operations.
    
    Args:
        markdown_content (str): Original markdown content to summarize
        target_tokens (int): Target token limit (default 4000)
        target_language (str): Target language for the summary output
        url (str): Source URL for building questions
        title (str): Page title for building questions
    
    Returns:
        str: Formatted prompt for the LLM
    """
    
    # Get standardized instructions
    standard_instructions = get_standard_summarization_instructions(target_tokens, target_language, url, title)
    
    # Add the source content
    prompt = f"""{standard_instructions}

## SOURCE:
{markdown_content}"""

    return prompt

def _process_single_chunk_through_openrouter(chunk_content, target_tokens):
    """
    Process a single chunk of content through OpenRouter API.
    
    Args:
        chunk_content (str): The chunk content to process
        target_tokens (int): Target token limit for the output
    
    Returns:
        str: Processed content or original content if processing fails
    """
    logger.info(f"Processing single chunk through OpenRouter API (target: {target_tokens} tokens)")
    
    # Check if OpenRouter is configured
    if not OPENROUTER_API_KEY:
        logger.warning("OpenRouter API key not configured for chunk processing")
        return chunk_content
    
    try:
        # Prepare the OpenRouter API request
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "X-Title": "HypeDigitaly Content Chunk Processor"
        }
        
        # Prepare model configuration - support both single model and models array for fallbacks
        if isinstance(OPENROUTER_MODELS, list) and len(OPENROUTER_MODELS) > 1:
            # Use models array for automatic fallback
            payload = {
                "models": OPENROUTER_MODELS,
                "messages": [
                    {
                        "role": "user",
                        "content": chunk_content
                    }
                ],
                "max_tokens": target_tokens,
                "temperature": OPENROUTER_TEMPERATURE,
                "top_p": OPENROUTER_TOP_P
            }
            logger.info(f"Using OpenRouter models array for chunk: {OPENROUTER_MODELS}")
        else:
            # Use single model
            primary_model = OPENROUTER_MODELS[0] if isinstance(OPENROUTER_MODELS, list) else OPENROUTER_MODELS
            payload = {
                "model": primary_model,
                "messages": [
                    {
                        "role": "user",
                        "content": chunk_content
                    }
                ],
                "max_tokens": target_tokens,
                "temperature": OPENROUTER_TEMPERATURE,
                "top_p": OPENROUTER_TOP_P
            }
            logger.info(f"Using single OpenRouter model for chunk: {primary_model}")
        
        # Send request to OpenRouter API
        logger.debug(f"Sending chunk processing request to OpenRouter API")
        response = requests_retry_session().post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT * 2  # Allow more time for processing
        )
        response.raise_for_status()

        # Log the full API response
        logger.info("=== FULL OPENROUTER API RESPONSE FOR CHUNK PROCESSING ===")
        logger.info(f"Full response: {response.text}")
        logger.info("=== END FULL OPENROUTER API RESPONSE ===")

        data = response.json()

        # Log token usage for this API call
        log_openrouter_token_usage(data, "SINGLE_CHUNK_PROCESSING")

        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]
            processed_content = message.get("content", "").strip()
            
            # Handle reasoning mode responses (e.g., openai/gpt-5)
            if not processed_content and message.get("reasoning"):
                processed_content = message.get("reasoning", "").strip()
                logger.info("✅ Extracted processed content from reasoning field")
            
            # Basic validation
            if processed_content and len(processed_content) > 10:
                result_tokens = count_tokens_approximate(processed_content)
                logger.info(f"✅ Successfully processed chunk: {result_tokens} tokens")
                return processed_content
            else:
                logger.error("❌ Processed chunk content is too short or empty")
                return chunk_content
        else:
            logger.error("❌ No choices returned from OpenRouter API for chunk")
            return chunk_content
            
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Request error with OpenRouter API for chunk: {str(e)}")
        return chunk_content
    except Exception as e:
        logger.error(f"❌ Unexpected error with OpenRouter API for chunk: {str(e)}")
        return chunk_content


def summarize_content_via_openrouter(markdown_content, title="", url="", target_tokens=3800):
    """
    Summarize markdown content using OpenRouter API with strict token limits.
    
    Args:
        markdown_content (str): Original markdown content to summarize
        title (str): Page title for context
        url (str): Source URL for context
        target_tokens (int): Target token limit (default 3800 to leave room for metadata)
    
    Returns:
        tuple: (summarized_content, success_flag) - (str, bool)
    """
    logger.info(f"Summarizing content via OpenRouter API for URL: {url}")
    
    # Check if OpenRouter is configured
    if not OPENROUTER_API_KEY:
        logger.info(f"OpenRouter API key not configured, skipping summarization for {url}")
        return None, False  # Return None to trigger legacy behavior
    
    # UNIVERSAL LARGE CONTENT PROTECTION - Size estimation and analysis
    original_tokens = count_tokens_approximate(markdown_content)
    logger.info(f"Original content token count (approximate): {original_tokens}")
    
    # SIZE-AWARE PROCESSING with intelligent chunking decision
    content_analysis = analyze_content_for_massive_data_warning(markdown_content, url, title)
    logger.info(f"📊 Content analysis: {content_analysis['message']}")
    
    # DECISION POINT: Should we chunk this content based on size and structure?
    if content_analysis['recommendation'] == 'chunking_required':
        logger.info(f"🚨 TRIGGERING SECTION-BASED CHUNKING: {original_tokens:,} tokens requires intelligent splitting")
        logger.info(f"📋 Dominant structure: {content_analysis['dominant_structure']} ({content_analysis['dominant_count']} elements)")
        
        # Chunk the massive content by complete sections
        sections = chunk_massive_content_by_sections(markdown_content, min_section_tokens=500)
        
        if len(sections) > 1:
            logger.info(f"🗂️ Content split into {len(sections)} complete sections")
            logger.info(f"📊 Section sizes: {[s['tokens'] for s in sections]} tokens each")
            
            # Process each section immediately and save progressively
            saved_files = []
            for section in sections:
                section_file = process_and_save_chunk_immediately(
                    section,
                    title,
                    url,
                    target_tokens,
                    len(sections),
                    OPENROUTER_TARGET_LANGUAGE,
                    **globals()  # Pass all global variables
                )
                if section_file:
                    saved_files.append(section_file)
            
            logger.info(f"✅ Progressive chunking complete: {len(saved_files)} section files created")
            return saved_files, 'chunked_files_already_saved'
        else:
            logger.info(f"📋 Chunking resulted in single section, processing normally")
            # Fall through to normal processing
    elif original_tokens > 8000:
        logger.info(f"⚠️  LARGE CONTENT WARNING: {original_tokens:,} tokens detected")
        logger.info(f"📊 Processing with enhanced preservation attention")
        logger.info(f"💡 Monitor for potential information loss")
    
    # Normal processing for content that doesn't require chunking
    logger.info(f"Processing content through OpenRouter API for RAG optimization and formatting")
    
    # Create the summarization prompt with target language
    prompt = create_summarization_prompt(markdown_content, target_tokens, OPENROUTER_TARGET_LANGUAGE, url, title)
    
    # Prepare the OpenRouter API request
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": url if url else "https://api.openrouter.ai",
        "X-Title": "HypeDigitaly Content Summarizer"
    }
    
    # Prepare model configuration - support both single model and models array for fallbacks
    if isinstance(OPENROUTER_MODELS, list) and len(OPENROUTER_MODELS) > 1:
        # Use models array for automatic fallback
        payload = {
            "models": OPENROUTER_MODELS,  # Models array for fallback functionality
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": target_tokens + 200,  # Give some buffer for the model
            "temperature": OPENROUTER_TEMPERATURE,
            "top_p": OPENROUTER_TOP_P
        }
        logger.info(f"Using OpenRouter models array with fallbacks: {OPENROUTER_MODELS}")
    else:
        # Use single model
        primary_model = OPENROUTER_MODELS[0] if isinstance(OPENROUTER_MODELS, list) else OPENROUTER_MODELS
        payload = {
            "model": primary_model,  # Single model from config
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": target_tokens + 200,  # Give some buffer for the model
            "temperature": OPENROUTER_TEMPERATURE,
            "top_p": OPENROUTER_TOP_P
        }
        logger.info(f"Using single OpenRouter model: {primary_model}")
    
    try:
        logger.info(f"Sending summarization request to OpenRouter API")
        response = requests_retry_session().post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT * 2  # Allow more time for summarization
        )
        response.raise_for_status()

        # Log the full API response
        logger.info("=== FULL OPENROUTER API RESPONSE FOR CONTENT SUMMARIZATION ===")
        logger.info(f"Full response: {response.text}")
        logger.info("=== END FULL OPENROUTER API RESPONSE ===")

        data = response.json()

        # Log token usage for this API call
        log_openrouter_token_usage(data, "CONTENT_SUMMARIZATION", url)

        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]
            summarized_content = message.get("content", "").strip()
            
            # Handle reasoning mode responses (e.g., openai/gpt-5)
            if not summarized_content and message.get("reasoning"):
                summarized_content = message.get("reasoning", "").strip()
                logger.info("✅ Extracted summarized content from reasoning field")

            # Use OpenRouter response as-is (removed strict format validation)
            result_tokens = count_tokens_approximate(summarized_content)
            logger.info(f"OpenRouter response token count: {result_tokens}")
            logger.info(f"✅ Using OpenRouter response as-is (format validation removed)")
            return summarized_content, True
        else:
            logger.error("❌ No choices returned from OpenRouter API")
            return None, False
            
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Request error with OpenRouter API: {str(e)}")
        return None, False
    except Exception as e:
        logger.error(f"❌ Unexpected error with OpenRouter API: {str(e)}")
        return None, False

# ============================================================================
# SITEMAP PARSING FUNCTIONS
# ============================================================================

def extract_links_from_jina_summary(jina_data, url_last_modified_map={}, last_run_timestamp=None, local_files_cache=None, enable_resume=False):
    """
    Extract and process links from Jina AI links summary data.
    This is a generalized approach that works with any HTML sitemap structure.
    
    Args:
        jina_data (dict): Full data response from Jina AI API
        url_last_modified_map (dict): URL to last modified date mapping from XML sitemap
        last_run_timestamp (datetime): Last run timestamp for filtering
        local_files_cache (dict): Cache of already processed URLs for resume functionality
        enable_resume (bool): Whether resume functionality is enabled
    
    Returns:
        list: List of extracted URL dictionaries with metadata
    """
    logger.info("Extracting links from Jina AI links summary (generalized approach)")
    
    extracted_urls = []
    links_data = jina_data.get("links", [])
    
    if not links_data:
        logger.warning("No links found in Jina AI response")
        logger.debug(f"Available keys in jina_data: {list(jina_data.keys())}")
        return extracted_urls
    
    logger.info(f"Processing {len(links_data)} links from Jina AI summary")
    
    # Debug: Log the structure of the first few links to understand the format
    if logger.isEnabledFor(logging.DEBUG) and len(links_data) > 0:
        logger.debug(f"Structure of first link: {type(links_data[0])}")
        logger.debug(f"First link content: {links_data[0]}")
        if len(links_data) > 1:
            logger.debug(f"Second link content: {links_data[1]}")
    
    for i, link_info in enumerate(links_data, 1):
        try:
            # Debug: Check what type link_info actually is
            if not isinstance(link_info, dict):
                logger.error(f"Link {i} is not a dict but {type(link_info)}: {link_info}")
                continue
            
            # Extract URL and text from link info
            url = link_info.get("url", "").strip()
            text = link_info.get("text", "").strip()
            
            # Skip if no URL
            if not url:
                logger.debug(f"Skipping link {i}: No URL found in {link_info}")
                continue
            
            # Make URL absolute if it's relative
            absolute_url = urljoin(BASE_URL, url)
            
            # Skip if URL doesn't belong to our domain
            parsed_url = urlparse(absolute_url)
            if parsed_url.netloc not in [BASE_NETLOC, NON_WWW_BASE_NETLOC]:
                logger.debug(f"Skipping external URL: {absolute_url}")
                continue
            
            # Check if URL is blacklisted
            if absolute_url in BLACKLISTED_URLS:
                logger.info(f"URL {absolute_url} is blacklisted. Skipping.")
                continue
            
            # Use text as title, fallback to URL path if no text
            title = text if text else parsed_url.path.split('/')[-1] or 'Homepage'
            
            # Create a simple path for sitemap context
            path = f"HTML Sitemap > {title}"
            
            logger.info(f"\n=== Processing URL {i}/{len(links_data)}: {absolute_url} ===")
            logger.info(f"Title: {title}")
            logger.info(f"Path: {path}")
            
            # Find last modified date from XML sitemap
            last_modified = find_url_last_modified(absolute_url, url_last_modified_map)
            
            # Check if the URL should be processed
            if should_process_url_with_resume(absolute_url, last_modified, last_run_timestamp, local_files_cache, enable_resume, rss_published_date=None):
                extracted_urls.append({
                    'url': absolute_url,
                    'title': title,
                    'path': path,
                    'jina_link_data': link_info  # Keep original Jina data for debugging
                })
                logger.info(f"✅ Added URL: {absolute_url}")
            else:
                logger.info(f"⏭️  Skipping URL {absolute_url} (timestamp check or already processed locally).")
                
        except Exception as e:
            logger.error(f"Error processing link {i}: {str(e)}")
            logger.error(f"Link data type: {type(link_info)}")
            logger.error(f"Link data content: {link_info}")
            continue
    
    logger.info(f"Successfully extracted {len(extracted_urls)} URLs from Jina AI links summary")
    return extracted_urls

def parse_menu(html_content):
    """
    DEPRECATED: Legacy HTML sitemap parsing function.
    This function is kept for backward compatibility but is no longer recommended.
    Use extract_links_from_jina_summary() for generalized sitemap processing.
    """
    logger.warning("Using DEPRECATED parse_menu function. Consider using extract_links_from_jina_summary() instead.")
    
    soup = BeautifulSoup(html_content, "html.parser")
    
    selectors_to_try = [
        ".portlet-site-map ul",
        ".gov-container .portlet-site-map ul",
        ".portlet-body ul",
        ".gov-container ul",
        ".sitemap ul",
        "ul",
        ".gov-container",
    ]
    
    main_menu = None
    for selector in selectors_to_try:
        main_menu = soup.select_one(selector)
        if main_menu:
            logger.info(f"Found sitemap menu using selector: {selector}")
            break
    
    if not main_menu:
        logger.warning("Main menu not found using any selector")
        for tag in soup.find_all(['ul', 'div'], limit=10):
            classes = tag.get('class', [])
            logger.info(f"  {tag.name} with classes: {classes}")

    return main_menu

def extract_links(menu_item, path=[], url_last_modified_map={}, last_run_timestamp=None, local_files_cache=None, enable_resume=False, vector_store_id=None, vector_store_cache=None):
    """Recursively extract links from the sitemap menu structure."""
    extracted_urls = []
    
    if menu_item.name == "li":
        link_tag = None
        link_text = ""
        
        # Try different structures to find links
        selectors_to_try = [
            ("div.views-field span.field-content a", "div.views-field span.field-content"),
            ("a", ""),
            ("span a", "span"),
            ("div a", "div"),
        ]
        
        for link_selector, text_selector in selectors_to_try:
            link_tag = menu_item.select_one(link_selector)
            if link_tag:
                if text_selector:
                    link_text_element = menu_item.select_one(text_selector)
                    if link_text_element and link_text_element.text.strip():
                        link_text = link_text_element.text.strip()
                logger.debug(f"Found link using selector: {link_selector}")
                break
        
        if not link_tag:
            link_tag = menu_item.find("a")
        
        # Extract text
        if link_tag and link_tag.text.strip():
            link_text = link_tag.text.strip()
        elif not link_text and menu_item.text.strip():
            link_text = menu_item.get_text(separator=' ', strip=True)
            if menu_item.find():
                direct_text = ''.join(menu_item.find_all(text=True, recursive=False)).strip()
                if direct_text:
                    link_text = direct_text

        if link_text:
            current_path = path + [link_text]
            absolute_path = " > ".join(current_path)

            if link_tag and link_tag.has_attr("href"):
                absolute_url = urljoin(BASE_URL, link_tag["href"])
                logger.info(f"\n=== Processing URL: {absolute_url} ===")
                logger.info(f"Path: {absolute_path}")

                # Check if URL is blacklisted
                if absolute_url in BLACKLISTED_URLS:
                    logger.info(f"URL {absolute_url} is blacklisted. Skipping.")
                else:
                    # Find last modified date from sitemap.xml
                    last_modified = find_url_last_modified(absolute_url, url_last_modified_map)
                    
                    # Check if the URL should be processed
                    if should_process_url_with_resume(absolute_url, last_modified, last_run_timestamp, local_files_cache, enable_resume, rss_published_date=None, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache):
                        extracted_urls.append({
                            'url': absolute_url,
                            'title': link_text,
                            'path': absolute_path
                        })
                    else:
                        logger.info(f"Skipping URL {absolute_url} (timestamp check or already processed locally).")

            # Continue with submenu
            sub_menu = menu_item.find("ul", recursive=False)
            if sub_menu:
                extracted_urls.extend(extract_links(sub_menu, current_path, url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume, vector_store_id, vector_store_cache))

    elif menu_item.name == "ul":
        for item in menu_item.find_all("li", recursive=False):
            extracted_urls.extend(extract_links(item, path, url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume))
    
    return extracted_urls

# ============================================================================
# RSS FEED FUNCTIONS
# ============================================================================

def parse_rss_feed(rss_url):
    """Parse RSS/Atom feed and extract URLs with metadata."""
    logger.info(f"Parsing RSS feed: {rss_url}")
    
    try:
        response = requests_retry_session().get(rss_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        
        # Store raw content for potential re-parsing
        raw_content = response.content
        
        # CRITICAL FIX: Remove chrome extension script tags that corrupt XML parsing
        # These are browser-injected artifacts that break XML parsers
        if b'chrome-extension://' in raw_content or b'<script' in raw_content:
            # Decode to string for regex processing
            content_str = raw_content.decode('utf-8', errors='ignore')
            # Remove script tags (especially chrome extension injections)
            content_str = re.sub(r'<script[^>]*>.*?</script>', '', content_str, flags=re.DOTALL | re.IGNORECASE)
            content_str = re.sub(r'<script[^/>]*/>', '', content_str, flags=re.IGNORECASE)
            # Re-encode to bytes
            raw_content = content_str.encode('utf-8')
            logger.info("Removed chrome extension script tags from XML content")
        
        # Try to parse as XML first (standard approach)
        soup = BeautifulSoup(raw_content, 'xml')
        
        # Check if it's Atom feed
        if soup.find('feed') and soup.find('feed').get('xmlns') == 'http://www.w3.org/2005/Atom':
            logger.info(f"Detected Atom feed format for: {rss_url}")
            return parse_atom_feed(soup, rss_url)
        
        # Check if it's RSS 2.0 feed
        elif soup.find('rss') or soup.find('channel'):
            logger.info(f"Detected RSS 2.0 feed format for: {rss_url}")
            return parse_rss_2_0_feed(soup, rss_url)
        
        # Check if it's Events XML format
        elif soup.find('events') and soup.find('event'):
            logger.info(f"Detected Events XML format for: {rss_url}")
            return parse_events_feed(soup, rss_url)
        
        # NEW: Check for custom <response><item> format (e.g., Karlovarsky Kraj)
        elif soup.find('response') and soup.find('item'):
            logger.info(f"Detected Custom <response><item> feed format for: {rss_url}")
            # CRITICAL FIX: Use HTML parser for robustness against malformed XML/script tags
            # The URL extraction logic in parse_custom_response_feed must handle the void <link> tag behavior.
            logger.info("Applying HTML parser for robust item detection...")
            # Re-parse the already cleaned raw_content using html.parser
            html_soup = BeautifulSoup(raw_content, 'html.parser')
            return parse_custom_response_feed(html_soup, rss_url)
        
        # Try parsing as HTML/XML with generic approach
        else:
            logger.info(f"Attempting generic XML parsing for: {rss_url}")
            return parse_generic_feed(soup, rss_url)
            
    except Exception as e:
        logger.error(f"Error parsing RSS feed {rss_url}: {str(e)}")
        return []

def parse_atom_feed(soup, rss_url):
    """Parse Atom feed format."""
    extracted_urls = []
    
    entries = soup.find_all('entry')
    logger.info(f"Found {len(entries)} entries in Atom feed")
    
    for entry in entries:
        try:
            # Extract URL from link element
            link_elem = entry.find('link', {'rel': 'alternate'})
            if not link_elem:
                link_elem = entry.find('link')
            
            if link_elem:
                url = link_elem.get('href')
                if url:
                    # Make URL absolute
                    absolute_url = urljoin(BASE_URL, url)
                    
                    # Extract title
                    title_elem = entry.find('title')
                    title = title_elem.text.strip() if title_elem else 'No title'
                    
                    # Extract publication date - try multiple field names
                    published_elem = (entry.find('published') or 
                                    entry.find('updated') or 
                                    entry.find('dc:date') or 
                                    entry.find('dcterms:created') or 
                                    entry.find('dcterms:modified'))
                    published = published_elem.text.strip() if published_elem else None
                    
                    # Extract summary
                    summary_elem = entry.find('summary')
                    summary = summary_elem.text.strip() if summary_elem else None
                    
                    # Create navigation path for RSS items
                    path = f"RSS: {rss_url} > {title}"
                    
                    extracted_urls.append({
                        'url': absolute_url,
                        'title': title,
                        'path': path,
                        'published': published,
                        'summary': summary,
                        'source_feed': rss_url
                    })
                    
                    logger.debug(f"Extracted from Atom: {absolute_url} - {title}")
                    
        except Exception as e:
            logger.error(f"Error processing Atom entry: {str(e)}")
            continue
    
    return extracted_urls

def parse_rss_2_0_feed(soup, rss_url):
    """Parse RSS 2.0 feed format."""
    extracted_urls = []
    
    items = soup.find_all('item')
    logger.info(f"Found {len(items)} items in RSS 2.0 feed")
    
    for item in items:
        try:
            # Extract URL from link element (try multiple potential fields)
            url = None
            
            # 1. Try standard <link> tag
            link_elem = item.find('link')
            if link_elem:
                url = link_elem.text.strip() if link_elem.text else link_elem.get('href')
            
            # 2. Try <guid> tag if <link> failed and guid looks like a URL
            if not url:
                guid_elem = item.find('guid')
                if guid_elem and guid_elem.text.strip().startswith('http'):
                    url = guid_elem.text.strip()
            
            # 3. Try <id> tag if <link> and <guid> failed and id looks like a URL
            if not url:
                id_elem = item.find('id')
                if id_elem and id_elem.text.strip().startswith('http'):
                    url = id_elem.text.strip()

            if not url:
                logger.debug("Skipping RSS 2.0 item: No valid URL found in <link>, <guid>, or <id>.")
                continue

            # Make URL absolute
            absolute_url = urljoin(BASE_URL, url)
            
            # Extract title
            title_elem = item.find('title')
            title = title_elem.text.strip() if title_elem else 'No title'
            
            # Extract publication date - try multiple field names
            published_elem = (item.find('pubDate') or
                            item.find('dc:date') or
                            item.find('dcterms:created') or
                            item.find('dcterms:modified') or
                            item.find('published') or
                            item.find('createdDate'))
            published = None
            if published_elem:
                published_text = published_elem.text.strip()
                # Check for nested <time datetime="..."> structure inside CDATA
                time_match = re.search(r'<time\s+datetime=["\']([^"\']+)["\']', published_text)
                if time_match:
                    published = time_match.group(1)
                else:
                    published = published_text
            
            # Extract description
            description_elem = item.find('description')
            description = description_elem.text.strip() if description_elem else None
            
            # Create navigation path for RSS items
            path = f"RSS: {rss_url} > {title}"
            
            extracted_urls.append({
                'url': absolute_url,
                'title': title,
                'path': path,
                'published': published,
                'description': description,
                'source_feed': rss_url
            })
            
            logger.debug(f"Extracted from RSS 2.0: {absolute_url} - {title}")
        except Exception as e:
            logger.error(f"Error processing RSS 2.0 item: {str(e)}")
            continue
    
    return extracted_urls

def parse_events_feed(soup, rss_url):
    """Parse Events XML format - extracts structured event data and detail page URLs."""
    extracted_urls = []
    
    events = soup.find_all('event')
    logger.info(f"Found {len(events)} events in Events XML feed")
    
    for event in events:
        try:
            # Extract event URL from <details><url>
            details = event.find('details')
            if not details or not details.find('url'):
                logger.debug(f"Skipping event without details/url")
                continue
                
            url = details.find('url').text.strip()
            if not url:
                continue
            
            # Make URL absolute
            absolute_url = urljoin(BASE_URL, url)
            
            # Extract event ID
            event_id_elem = event.find('id')
            event_id = event_id_elem.text.strip() if event_id_elem else 'Unknown'
            
            # Extract event name (handle CDATA)
            name_elem = event.find('name')
            if name_elem:
                # Handle CDATA content
                name = name_elem.get_text().strip() if name_elem.get_text() else 'Unknown Event'
            else:
                name = f"Event {event_id}"
            
            # Extract date and time information
            start_date = 'Unknown'
            start_time = 'Unknown'
            dates = event.find('dates')
            if dates and dates.find('date'):
                date_elem = dates.find('date')
                if date_elem.find('start_date'):
                    start_date = date_elem.find('start_date').text.strip()
                if date_elem.find('start_time'):
                    start_time_elem = date_elem.find('start_time')
                    start_time = start_time_elem.get_text().strip() if start_time_elem else 'Unknown'
            
            # Extract organizer (handle CDATA)
            organizer = 'Unknown'
            org_elem = event.find('organizer')
            if org_elem and org_elem.find('name'):
                org_name_elem = org_elem.find('name')
                organizer = org_name_elem.get_text().strip() if org_name_elem else 'Unknown'
            
            # Extract place/location (handle CDATA)
            place = 'Unknown'
            place_elem = event.find('place')
            if place_elem:
                if place_elem.find('other'):
                    place = place_elem.find('other').get_text().strip()
                # Could also extract GPS coordinates if needed
                # wgs84 = place_elem.find('wgs84')
                # if wgs84:
                #     latitude = wgs84.find('latitude').text if wgs84.find('latitude') else None
                #     longitude = wgs84.find('longitude').text if wgs84.find('longitude') else None
            
            # Extract event type
            event_type = 'Unknown'
            types_elem = event.find('types')
            if types_elem and types_elem.find('type'):
                event_type = types_elem.find('type').text.strip()
            
            # Extract description (handle CDATA and embedded HTML)
            description = None
            desc_elem = event.find('description')
            if desc_elem:
                description = desc_elem.get_text().strip()
                # Clean up HTML tags if present but preserve links
                if description and len(description) > 500:
                    description = description[:500] + "..."
            
            # Create enhanced title with event info for better identification
            if start_date != 'Unknown' and start_time != 'Unknown':
                enhanced_title = f"{name} ({start_date} {start_time})"
            elif start_date != 'Unknown':
                enhanced_title = f"{name} ({start_date})"
            else:
                enhanced_title = name
            
            # Create comprehensive event metadata for RSS processing
            event_metadata = {
                'event_id': event_id,
                'event_name': name,
                'start_date': start_date,
                'start_time': start_time,
                'organizer': organizer,
                'place': place,
                'event_type': event_type,
                'source_feed': rss_url
            }
            
            # Create enhanced description combining XML data
            enhanced_description = []
            if description:
                enhanced_description.append(f"Popis: {description}")
            if organizer != 'Unknown':
                enhanced_description.append(f"Organizátor: {organizer}")
            if place != 'Unknown':
                enhanced_description.append(f"Místo: {place}")
            if event_type != 'Unknown':
                enhanced_description.append(f"Typ: {event_type}")
                
            combined_description = " | ".join(enhanced_description) if enhanced_description else description
            
            # Create navigation path for event items
            path = f"Events Feed: {rss_url} > {enhanced_title}"
            
            # Use event start_date as published date for timestamp comparison
            published_date = None
            if start_date != 'Unknown':
                try:
                    # Try to parse Czech date format (DD.MM.YYYY)
                    if '.' in start_date:
                        day, month, year = start_date.split('.')
                        if start_time != 'Unknown' and ':' in start_time:
                            # Include time if available
                            published_date = f"{year}-{month.zfill(2)}-{day.zfill(2)}T{start_time}:00"
                        else:
                            published_date = f"{year}-{month.zfill(2)}-{day.zfill(2)}T00:00:00"
                    else:
                        # Fallback: use as-is if already in ISO format
                        published_date = start_date
                except Exception as e:
                    logger.warning(f"Could not parse event date {start_date}: {str(e)}")
                    published_date = start_date  # Use as-is
            
            extracted_urls.append({
                'url': absolute_url,
                'title': enhanced_title,
                'path': path,
                'published': published_date,
                'summary': combined_description,
                'description': combined_description,
                'event_metadata': event_metadata,  # Rich event data for potential future use
                'source_feed': rss_url
            })
            
            logger.debug(f"Extracted from Events XML: {absolute_url} - {enhanced_title}")
            logger.debug(f"  Event ID: {event_id}, Date: {start_date} {start_time}")
            logger.debug(f"  Organizer: {organizer}, Place: {place}, Type: {event_type}")
                    
        except Exception as e:
            logger.error(f"Error processing Events XML entry: {str(e)}")
            continue
    
    logger.info(f"Successfully extracted {len(extracted_urls)} events from Events XML feed")
    return extracted_urls

def parse_custom_response_feed(soup, rss_url):
    """Parse custom <response><item> feed format."""
    extracted_urls = []
    
    items = soup.find_all('item')
    logger.info(f"Found {len(items)} items in custom response feed")
    
    for i, item in enumerate(items, 1):
        try:
            # Extract URL from link element - TRY MULTIPLE METHODS
            url = None
            
            # Method 1: Try standard find with get_text()
            link_elem = item.find('link')
            if link_elem:
                url_text = link_elem.get_text(strip=True)
                if url_text and url_text.startswith('http'):
                    url = url_text
                    logger.debug(f"Item {i}: Extracted URL via get_text(): {url}")
            
            # Method 2: Try .string attribute (sometimes works better with html.parser)
            if not url and link_elem:
                if link_elem.string and link_elem.string.strip().startswith('http'):
                    url = link_elem.string.strip()
                    logger.debug(f"Item {i}: Extracted URL via .string: {url}")
            
            # Method 3: Try .text property
            if not url and link_elem:
                try:
                    if link_elem.text and link_elem.text.strip().startswith('http'):
                        url = link_elem.text.strip()
                        logger.debug(f"Item {i}: Extracted URL via .text: {url}")
                except:
                    pass
            
            # Method 4: Try navigablestring content directly
            if not url and link_elem:
                try:
                    for content in link_elem.contents:
                        if content and hasattr(content, 'strip'):
                            content_str = str(content).strip()
                            if content_str.startswith('http'):
                                url = content_str
                                logger.debug(f"Item {i}: Extracted URL via contents: {url}")
                                break
                except:
                    pass
            
            # CRITICAL FIX: If the link element is empty, check the next sibling text node.
            # This handles cases where the URL is immediately following the <link> tag due to html.parser treating it as void.
            # This logic is only necessary when using html.parser (which we are now doing for this custom feed).
            if not url and link_elem:
                next_sibling = link_elem.next_sibling
                if next_sibling and hasattr(next_sibling, 'strip'):
                    sibling_text = str(next_sibling).strip()
                    if sibling_text.startswith('http'):
                        url = sibling_text
                        logger.debug(f"Item {i}: Extracted URL via next_sibling: {url}")
            
            if not url:
                logger.warning(f"Item {i}: No valid URL found in <link> element after trying all methods. Link element: {link_elem}")
                continue

            # Make URL absolute
            absolute_url = urljoin(BASE_URL, url)
            logger.debug(f"Item {i}: Converted to absolute URL: {absolute_url}")
            
            # Extract title
            title_elem = item.find('title')
            title = title_elem.get_text(strip=True) if title_elem else 'No title'
            
            # Extract publication date - uses the same logic as RSS 2.0 to handle CDATA <time datetime="...">
            published_elem = item.find('published')
            published = None
            if published_elem:
                published_text = published_elem.get_text(strip=True)
                # Check for nested <time datetime="..."> structure inside CDATA
                time_match = re.search(r'<time\s+datetime=["\']([^"\']+)["\']', published_text)
                if time_match:
                    published = time_match.group(1)
                    logger.debug(f"Item {i}: Extracted datetime from CDATA: {published}")
                else:
                    published = published_text
                    logger.debug(f"Item {i}: Using raw published text: {published}")
            
            # Extract description
            description_elem = item.find('description')
            description = description_elem.get_text(strip=True) if description_elem else None
            
            # Create navigation path for RSS items
            path = f"RSS: {rss_url} > {title}"
            
            extracted_urls.append({
                'url': absolute_url,
                'title': title,
                'path': path,
                'published': published,
                'description': description,
                'source_feed': rss_url
            })
            
            logger.info(f"✅ Item {i}/{len(items)}: Extracted from Custom Response: {absolute_url} - {title}")
        except Exception as e:
            logger.error(f"❌ Error processing custom response item {i}: {str(e)}")
            continue
    
    logger.info(f"Successfully extracted {len(extracted_urls)} URLs from custom response feed")
    return extracted_urls

def parse_generic_feed(soup, rss_url):
    """Generic parser for unknown feed formats - looks for any URLs."""
    extracted_urls = []
    
    # Look for any links in the feed
    all_links = []
    
    # Find all elements that might contain URLs
    for elem in soup.find_all(['link', 'a']):
        href = elem.get('href')
        if href:
            all_links.append(href)
        elif elem.text and elem.text.strip().startswith('http'):
            all_links.append(elem.text.strip())
    
    # Also look for text content that might be URLs
    import re
    url_pattern = r'https?://[^\s<>"\']*'
    text_urls = re.findall(url_pattern, str(soup))
    all_links.extend(text_urls)
    
    # Filter and process URLs
    seen_urls = set()
    for url in all_links:
        try:
            # Clean URL
            url = url.strip()
            if not url or url in seen_urls:
                continue
                
            # Make URL absolute
            absolute_url = urljoin(BASE_URL, url)
            
            # Filter out non-relevant URLs
            if (absolute_url.startswith(BASE_URL) and 
                absolute_url not in BLACKLISTED_URLS and
                absolute_url != rss_url):
                
                seen_urls.add(url)
                
                # Create basic metadata
                title = f"Link from {rss_url}"
                path = f"RSS: {rss_url} > Generic Link"
                
                extracted_urls.append({
                    'url': absolute_url,
                    'title': title,
                    'path': path,
                    'published': None,
                    'source_feed': rss_url
                })
                
                logger.debug(f"Extracted generic URL: {absolute_url}")
                
        except Exception as e:
            logger.error(f"Error processing generic URL {url}: {str(e)}")
            continue
    
    logger.info(f"Found {len(extracted_urls)} URLs in generic feed parsing")
    return extracted_urls

def process_rss_feeds(url_last_modified_map, rss_last_run_timestamp, local_files_cache=None, enable_resume=False):
    """Process all configured RSS feeds and extract URLs."""
    logger.info(f"Processing {len(RSS_FEEDS)} RSS feeds")
    
    all_rss_urls = []
    
    # Fetch the sitemap last run timestamp for the secondary check
    sitemap_last_run_timestamp = get_last_run_timestamp("sitemap")
    
    for rss_url in RSS_FEEDS:
        logger.info(f"Processing RSS feed: {rss_url}")
        
        # Parse the RSS feed
        feed_urls = parse_rss_feed(rss_url)
        
        # Filter URLs based on RSS-specific timestamp checking
        filtered_urls = []
        for url_info in feed_urls:
            url = url_info['url']
            
            # Check if URL is blacklisted
            if url in BLACKLISTED_URLS:
                logger.info(f"RSS URL {url} is blacklisted. Skipping.")
                continue
            
            # Get RSS published date from the feed item
            rss_published_date = url_info.get('published')
            
            # Look up XML sitemap last modified date for this URL
            xml_last_modified = find_url_last_modified(url, url_last_modified_map)
            
            # Use RSS-specific processing logic instead of XML sitemap matching
            if should_process_url_with_resume(
                url,
                xml_last_modified,
                rss_last_run_timestamp,
                local_files_cache,
                enable_resume,
                rss_published_date=rss_published_date,
                sitemap_last_run_timestamp=sitemap_last_run_timestamp
            ):
                filtered_urls.append(url_info)
            else:
                logger.info(f"Skipping RSS URL {url} (timestamp check or already processed locally).")
        
        all_rss_urls.extend(filtered_urls)
        
        logger.info(f"Extracted {len(filtered_urls)} URLs from RSS feed: {rss_url}")
    
    logger.info(f"Total RSS URLs to process: {len(all_rss_urls)}")
    return all_rss_urls

# ============================================================================
# XML SITEMAP FUNCTIONS
# ============================================================================

def parse_lastmod_date(date_str):
    """Parse a lastmod date string from the sitemap into a datetime object."""
    formats = [
        lambda s: datetime.fromisoformat(s.replace('Z', '+00:00')),
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ"),
        lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ"),
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S"),
        lambda s: datetime.strptime(s, "%Y-%m-%d"),
    ]
    
    for format_func in formats:
        try:
            return format_func(date_str)
        except (ValueError, TypeError):
            continue
    
    logger.warning(f"Could not parse lastmod date: {date_str}")
    return None

def fetch_xml_sitemap():
    """Fetch the XML sitemap and parse URLs with their last modified dates."""
    logger.info(f"Fetching XML sitemap from {XML_SITEMAP_URL}")
    url_last_modified = {}
    
    try:
        response = requests_retry_session().get(XML_SITEMAP_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        
        # Try parsing with different methods
        if "page=1" in response.text or "page=2" in response.text:
            logger.info("Detected simple sitemap index format")
            
            sitemap_page_urls = re.findall(r'https://[^/]+/sitemap\.xml\?page=\d+', response.text)
            if sitemap_page_urls:
                logger.info(f"Found {len(sitemap_page_urls)} sitemap page URLs")
                
                for page_url in sitemap_page_urls:
                    try:
                        page_response = requests_retry_session().get(page_url, timeout=REQUEST_TIMEOUT)
                        page_response.raise_for_status()
                        
                        url_matches = re.findall(r'<url>.*?<loc>(.*?)</loc>.*?<lastmod>(.*?)</lastmod>.*?</url>', 
                                               page_response.text, re.DOTALL)
                        
                        for url, lastmod in url_matches:
                            last_modified = parse_lastmod_date(lastmod)
                            if last_modified:
                                url_last_modified[url] = last_modified
                        
                        logger.info(f"Extracted {len(url_matches)} URLs from {page_url}")
                        
                    except Exception as e:
                        logger.error(f"Error processing sitemap page {page_url}: {str(e)}")
        
        # Try parsing with BeautifulSoup
        for parser in ["lxml-xml", "html.parser", "lxml"]:
            try:
                soup = BeautifulSoup(response.text, parser)
                
                # Try sitemap index format
                sitemaps = soup.find_all("sitemap")
                if sitemaps:
                    for sitemap in sitemaps:
                        loc = sitemap.find("loc")
                        if loc and loc.text:
                            sitemap_response = requests_retry_session().get(loc.text, timeout=REQUEST_TIMEOUT)
                            sitemap_response.raise_for_status()
                            
                            sitemap_soup = BeautifulSoup(sitemap_response.text, parser)
                            for url_entry in sitemap_soup.find_all("url"):
                                loc_elem = url_entry.find("loc")
                                lastmod_elem = url_entry.find("lastmod")
                                
                                if loc_elem and loc_elem.text:
                                    url = loc_elem.text
                                    last_modified = None
                                    
                                    if lastmod_elem and lastmod_elem.text:
                                        last_modified = parse_lastmod_date(lastmod_elem.text)
                                    
                                    if last_modified:
                                        url_last_modified[url] = last_modified
                    break
                
                # Try direct URL entries
                url_entries = soup.find_all("url")
                if url_entries:
                    for url_entry in url_entries:
                        loc_elem = url_entry.find("loc")
                        lastmod_elem = url_entry.find("lastmod")
                        
                        if loc_elem and loc_elem.text:
                            url = loc_elem.text
                            last_modified = None
                            
                            if lastmod_elem and lastmod_elem.text:
                                last_modified = parse_lastmod_date(lastmod_elem.text)
                            
                            if last_modified:
                                url_last_modified[url] = last_modified
                    break
                    
            except Exception as e:
                logger.error(f"Error parsing with {parser}: {str(e)}")
        
        logger.info(f"Successfully extracted {len(url_last_modified)} URLs with last modified dates")
        return url_last_modified
                
    except Exception as e:
        logger.error(f"Error fetching XML sitemap: {str(e)}")
        return {}

def find_url_last_modified(url, url_last_modified_map):
    """
    Find the last modified date for a given URL with improved matching.
    Handles domain variations (www vs non-www) and flexible path matching.
    """
    logger.debug(f"Finding last modified date for URL: {url}")
    last_modified = None
    matched_sitemap_url = None
    
    # Normalize the input URL
    normalized_url = url.rstrip('/')
    parsed_input = urlparse(normalized_url)
    input_domain = parsed_input.netloc.lower()
    input_path = parsed_input.path.lower()
    
    # Remove www prefix for comparison
    input_domain_no_www = input_domain[4:] if input_domain.startswith("www.") else input_domain
    
    # Try different matching strategies
    matching_strategies = [
        # 1. Exact match
        lambda: url if url in url_last_modified_map else None,
        
        # 2. Normalized exact match
        lambda: normalized_url if normalized_url in url_last_modified_map else None,
        
        # 3. Domain variation matching (www vs non-www)
        lambda: _find_domain_variation_match(normalized_url, url_last_modified_map),
        
        # 4. Path-based matching (same path, different domain)
        lambda: _find_path_based_match(input_path, input_domain_no_www, url_last_modified_map),
        
        # 5. Flexible substring matching (improved)
        lambda: _find_flexible_substring_match(normalized_url, input_domain_no_www, input_path, url_last_modified_map),
        
        # 6. Legacy substring matching (fallback)
        lambda: _find_legacy_substring_match(normalized_url, url_last_modified_map)
    ]
    
    # Try each strategy until we find a match
    for i, strategy in enumerate(matching_strategies, 1):
        try:
            result = strategy()
            if result:
                matched_sitemap_url = result
                last_modified = url_last_modified_map.get(matched_sitemap_url)
                logger.info(f"URL matched using strategy {i}: {matched_sitemap_url}")
                break
        except Exception as e:
            logger.debug(f"Strategy {i} failed: {str(e)}")
            continue

    # Debug output (can be controlled by debug level or verbose flag)
    # Check if verbose URL matching is enabled (we'll set this as a global variable)
    verbose_url_matching = getattr(globals(), 'VERBOSE_URL_MATCHING', False)
    show_debug = (logger.isEnabledFor(logging.DEBUG) or 
                  not matched_sitemap_url or  # Always show if no match found
                  verbose_url_matching)  # Show if verbose flag is set
    
    if show_debug:
        print(f"\n=== URL MATCHING ===")
        print(f"SITEMAP_URL: {url}")
        if matched_sitemap_url:
            print(f"XML_SITEMAP_URL: {matched_sitemap_url}")
            print(f"Last modified date: {last_modified}")
            # Show which strategy worked
            strategy_names = [
                "Exact match",
                "Normalized exact match", 
                "Domain variation match",
                "Path-based match",
                "Flexible substring match",
                "Legacy substring match"
            ]
            for i, strategy in enumerate(matching_strategies, 1):
                try:
                    if strategy() == matched_sitemap_url:
                        print(f"Matching strategy: {i}. {strategy_names[i-1]}")
                        break
                except:
                    continue
        else:
            print(f"XML_SITEMAP_URL: No matching URL found")
            print(f"Available XML URLs count: {len(url_last_modified_map)}")
            # Show a few sample XML URLs for debugging
            if len(url_last_modified_map) > 0:
                sample_urls = list(url_last_modified_map.keys())[:3]
                print(f"Sample XML URLs: {sample_urls}")
        print("===================\n")
        
    return last_modified


def _find_domain_variation_match(normalized_url, url_last_modified_map):
    """Find match by trying www/non-www domain variations."""
    parsed = urlparse(normalized_url)
    domain = parsed.netloc.lower()
    
    if domain.startswith("www."):
        # Try without www
        non_www_url = normalized_url.replace(f"www.{domain[4:]}", domain[4:], 1)
        if non_www_url in url_last_modified_map:
            return non_www_url
    else:
        # Try with www
        www_url = normalized_url.replace(domain, f"www.{domain}", 1)
        if www_url in url_last_modified_map:
            return www_url
    
    return None


def _find_path_based_match(input_path, input_domain_no_www, url_last_modified_map):
    """Find match based on path similarity, ignoring domain differences."""
    for sitemap_url in url_last_modified_map.keys():
        try:
            parsed_sitemap = urlparse(sitemap_url)
            sitemap_domain = parsed_sitemap.netloc.lower()
            sitemap_path = parsed_sitemap.path.lower()
            
            # Remove www for domain comparison
            sitemap_domain_no_www = sitemap_domain[4:] if sitemap_domain.startswith("www.") else sitemap_domain
            
            # Check if domains are related (same base domain)
            if sitemap_domain_no_www == input_domain_no_www and sitemap_path == input_path:
                return sitemap_url
                
        except Exception:
            continue
    
    return None


def _find_flexible_substring_match(normalized_url, input_domain_no_www, input_path, url_last_modified_map):
    """Improved substring matching with domain awareness."""
    best_match = None
    best_score = 0
    
    for sitemap_url in url_last_modified_map.keys():
        try:
            parsed_sitemap = urlparse(sitemap_url)
            sitemap_domain = parsed_sitemap.netloc.lower()
            sitemap_path = parsed_sitemap.path.lower()
            
            # Remove www for comparison
            sitemap_domain_no_www = sitemap_domain[4:] if sitemap_domain.startswith("www.") else sitemap_domain
            
            score = 0
            
            # Domain matching bonus
            if sitemap_domain_no_www == input_domain_no_www:
                score += 100
            elif input_domain_no_www in sitemap_domain_no_www or sitemap_domain_no_www in input_domain_no_www:
                score += 50
            
            # Path matching
            if sitemap_path == input_path:
                score += 200  # Exact path match
            elif input_path in sitemap_path:
                score += 100  # Input path is substring of sitemap path
            elif sitemap_path in input_path:
                score += 80   # Sitemap path is substring of input path
            else:
                # Check for common path segments
                input_segments = [seg for seg in input_path.split('/') if seg]
                sitemap_segments = [seg for seg in sitemap_path.split('/') if seg]
                
                common_segments = set(input_segments) & set(sitemap_segments)
                if len(common_segments) > 0:
                    score += len(common_segments) * 10
            
            # Only consider matches with minimum score
            if score > best_score and score >= 150:  # Require domain match + some path similarity
                best_score = score
                best_match = sitemap_url
                
        except Exception:
            continue
    
    return best_match


def _find_legacy_substring_match(normalized_url, url_last_modified_map):
    """Original substring matching logic as fallback."""
    matching_urls = [sitemap_url for sitemap_url in url_last_modified_map.keys() 
                     if normalized_url in sitemap_url or sitemap_url in normalized_url]
    
    return matching_urls[0] if matching_urls else None


def should_process_rss_url(url, rss_published_date, rss_last_run_timestamp, xml_last_modified, sitemap_last_run_timestamp, local_cache=None, vector_store_id=None, vector_store_cache=None):
    """
    Decide whether an RSS URL should be processed based on:
    1. RSS Date Threshold (if set)
    2. RSS publication date vs RSS last run timestamp
    3. XML sitemap last modified date vs Sitemap last run timestamp (if available)
    4. NEW: Existence check if XML last modified date is missing.
    """
    logger.debug(f"Checking if RSS URL should be processed: {url}")
    
    global RSS_DATE_THRESHOLD
    
    # --- Step 1: Parse RSS published date ---
    published_date = None
    if rss_published_date:
        if isinstance(rss_published_date, str):
            # Try to parse the RSS date string
            try:
                if dateutil_parser:
                    published_date = dateutil_parser.parse(rss_published_date)
                else:
                    # Fallback parsing for common RSS date formats
                    import email.utils
                    try:
                        published_date = datetime.fromtimestamp(email.utils.mktime_tz(email.utils.parsedate_tz(rss_published_date)))
                    except:
                        logger.warning(f"Could not parse RSS published date: {rss_published_date}")
                        # If we can't parse the date, process the item to be safe
                        logger.info(f"Unable to parse RSS date, processing RSS URL {url} to be safe")
                        return True
            except Exception as e:
                logger.warning(f"Error parsing RSS published date {rss_published_date}: {str(e)}")
                # If we can't parse the date, process the item to be safe
                logger.info(f"Unable to parse RSS date, processing RSS URL {url} to be safe")
                return True
        else:
            published_date = rss_published_date
    
    # If no published date found, process to be safe
    if published_date is None:
        logger.info(f"No RSS published date found for {url}. Processing to be safe.")
        print(f"\n=== RSS URL PROCESSING STATUS ===")
        print(f"URL: {url}")
        print(f"RSS Published: Unknown")
        print(f"Last Run: {rss_last_run_timestamp}")
        print(f"Processing: WILL PROCEED (safety)")
        print("=================================\n")
        return True

    # Convert to UTC for comparison
    if published_date.tzinfo is None:
        published_date_utc = published_date.replace(tzinfo=timezone.utc)
    else:
        published_date_utc = published_date.astimezone(timezone.utc)
        
    # --- Step 2: Check against RSS_DATE_THRESHOLD (mandatory filter if set) ---
    if RSS_DATE_THRESHOLD:
        if published_date_utc < RSS_DATE_THRESHOLD:
            logger.info(f"RSS URL {url} published date {published_date_utc} is BEFORE RSS Date Threshold {RSS_DATE_THRESHOLD}. Skipping.")
            print(f"\n=== RSS URL PROCESSING STATUS (THRESHOLD) ===")
            print(f"URL: {url}")
            print(f"RSS Published: {published_date}")
            print(f"Date Threshold: {RSS_DATE_THRESHOLD.strftime('%d-%m-%Y %H:%M:%S UTC')}")
            print(f"Processing: SKIPPED (BELOW THRESHOLD)")
            print("=============================================\n")
            return False
        else:
            logger.info(f"RSS URL {url} passed RSS Date Threshold check.")
            
    # --- Step 3: Check against RSS last run timestamp (only if CHECK_LAST_MODIFIED is True) ---
    
    # If checking is disabled, and we passed the threshold check, always process
    if not CHECK_LAST_MODIFIED:
        logger.info(f"CHECK_LAST_MODIFIED is False. Processing RSS URL {url}")
        print(f"\n=== RSS URL PROCESSING STATUS ===")
        print(f"URL: {url}")
        print(f"CHECK_LAST_MODIFIED is disabled")
        print(f"Processing: WILL PROCEED")
        print("=================================\n")
        return True
    
    # If CHECK_LAST_MODIFIED is True, check RSS last run timestamp
    if rss_last_run_timestamp:
        is_newer_rss = published_date_utc > rss_last_run_timestamp
        
        print(f"\n=== RSS URL PROCESSING STATUS (RSS LAST RUN) ===")
        print(f"URL: {url}")
        print(f"RSS Published: {published_date}")
        print(f"RSS Last Run: {rss_last_run_timestamp}")
        print(f"Is Published Since RSS Last Run: {'YES' if is_newer_rss else 'NO'}")
        
        if not is_newer_rss:
            logger.info(f"RSS URL {url} was published before RSS last run. Skipping.")
            print(f"Processing: SKIPPED (RSS TIMESTAMP)")
            print("============================================\n")
            return False
        else:
            logger.info(f"RSS URL {url} was published after RSS last run. Proceeding to XML check.")
            print(f"Processing: PASSED RSS TIMESTAMP CHECK")
            print("============================================\n")
    else:
        logger.info(f"No RSS last run timestamp found. Proceeding to XML check.")
        
    # --- Step 4: Check against XML sitemap last modified date (if available) ---
    
    if xml_last_modified is None:
        # --- NEW LOGIC: Handle missing XML last modified date for RSS entries ---
        is_local_file_present = is_url_already_processed_locally(url, local_cache) if local_cache is not None else False
        
        is_vector_store_file_present = False
        if vector_store_id and vector_store_cache is not None:
            # Use find_existing_files_by_url_cached (L5452)
            if find_existing_files_by_url_cached(url, vector_store_cache):
                is_vector_store_file_present = True
        
        if is_local_file_present or is_vector_store_file_present:
            # Exists in cache/store, and lastmod is missing -> SKIP (as requested by user)
            logger.info(f"No XML last modified date found for {url}. Skipping because it exists locally ({is_local_file_present}) or in Vector Store ({is_vector_store_file_present}).")
            print(f"\n=== RSS URL PROCESSING STATUS (MISSING XML LASTMOD) ===")
            print(f"URL: {url}")
            print(f"XML Last Modified: Unknown")
            print(f"Sitemap Last Run: {sitemap_last_run_timestamp}")
            print(f"Local/Vector Store Exists: YES")
            print(f"Processing: SKIPPED (Missing XML lastmod + Exists)")
            print("================================================\n")
            return False
        else:
            # Does not exist in cache/store, and lastmod is missing -> PROCESS (as requested by user)
            logger.info(f"No XML last modified date found for {url}. Processing because it is NEW (not found locally or in Vector Store).")
            print(f"\n=== RSS URL PROCESSING STATUS (MISSING XML LASTMOD) ===")
            print(f"URL: {url}")
            print(f"XML Last Modified: Unknown")
            print(f"Sitemap Last Run: {sitemap_last_run_timestamp}")
            print(f"Local/Vector Store Exists: NO")
            print(f"Processing: WILL PROCEED (Missing XML lastmod + New URL)")
            print("================================================\n")
            return True
        
    if sitemap_last_run_timestamp is None:
        logger.info(f"No Sitemap last run timestamp found. Processing based on RSS checks only.")
        return True # Process if it passed RSS checks

    # Convert XML last modified date to UTC for comparison
    if xml_last_modified.tzinfo is None:
        xml_last_modified_utc = xml_last_modified.replace(tzinfo=timezone.utc)
    else:
        xml_last_modified_utc = xml_last_modified.astimezone(timezone.utc)
        
    is_modified_xml = xml_last_modified_utc > sitemap_last_run_timestamp
    
    print(f"\n=== RSS URL PROCESSING STATUS (XML LAST MOD) ===")
    print(f"URL: {url}")
    print(f"XML Last Modified: {xml_last_modified}")
    print(f"Sitemap Last Run: {sitemap_last_run_timestamp}")
    print(f"Is Modified Since Sitemap Last Run: {'YES' if is_modified_xml else 'NO'}")
    print(f"Processing: {'WILL PROCEED' if is_modified_xml else 'SKIPPED'}")
    print("================================================\n")
    
    if is_modified_xml:
        logger.info(f"RSS URL {url} XML last modified date is newer than Sitemap last run. Processing.")
        return True
    else:
        logger.info(f"RSS URL {url} XML last modified date is NOT newer than Sitemap last run. Skipping.")
        return False

def extract_urls_from_xml_sitemap(url_last_modified_map, last_run_timestamp=None, local_files_cache=None, enable_resume=False, vector_store_id=None, vector_store_cache=None):
    """Extract URLs from XML sitemap data for processing."""
    extracted_urls = []
    
    logger.info(f"Extracting URLs from XML sitemap data")
    
    for url, last_modified in url_last_modified_map.items():
        logger.info(f"\n=== Processing URL: {url} ===")
        
        # Check if URL is blacklisted
        if url in BLACKLISTED_URLS:
            logger.info(f"URL {url} is blacklisted. Skipping.")
            continue
        
        # Check if the URL should be processed
        if should_process_url_with_resume(url, last_modified, last_run_timestamp, local_files_cache, enable_resume, rss_published_date=None, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache):
            # Extract title from URL path as fallback
            parsed_url = urlparse(url)
            path_parts = [part for part in parsed_url.path.split('/') if part]
            title = path_parts[-1] if path_parts else 'Homepage'
            
            # Create a basic path from URL structure
            path = f"XML Sitemap > {' > '.join(path_parts)}" if path_parts else "XML Sitemap > Homepage"
            
            extracted_urls.append({
                'url': url,
                'title': title,
                'path': path
            })
            logger.info(f"Added URL from XML sitemap: {url}")
        else:
            logger.info(f"Skipping URL {url} (timestamp check or already processed locally).")
    
    logger.info(f"Extracted {len(extracted_urls)} URLs from XML sitemap")
    return extracted_urls

# ============================================================================
# TIMESTAMP FUNCTIONS
# ============================================================================

def get_last_run_timestamp(timestamp_type="combined"):
    """Get the timestamp of the last script run.
    
    Args:
        timestamp_type: "combined", "rss", or "sitemap"
    """
    try:
        if timestamp_type == "rss":
            timestamp_file = RSS_LAST_RUN_FILE
        elif timestamp_type == "sitemap":
            timestamp_file = SITEMAP_LAST_RUN_FILE
        else:
            timestamp_file = LAST_RUN_FILE
            
        if os.path.exists(timestamp_file):
            with open(timestamp_file, 'r') as f:
                timestamp_str = f.read().strip()
                return datetime.fromisoformat(timestamp_str)
        return None
    except Exception as e:
        logger.error(f"Error reading {timestamp_type} timestamp: {str(e)}")
        return None

def save_last_run_timestamp(timestamp_type="combined"):
    """Save the current timestamp as the last run timestamp.
    
    Args:
        timestamp_type: "combined", "rss", or "sitemap"
    """
    try:
        if timestamp_type == "rss":
            timestamp_file = RSS_LAST_RUN_FILE
        elif timestamp_type == "sitemap":
            timestamp_file = SITEMAP_LAST_RUN_FILE
        else:
            timestamp_file = LAST_RUN_FILE
            
        with open(timestamp_file, 'w') as f:
            f.write(datetime.now().astimezone(timezone(timedelta(hours=2))).isoformat())
        logger.info(f"Saved current UTC+02:00 timestamp to {timestamp_file}")
    except Exception as e:
        logger.error(f"Error saving {timestamp_type} timestamp: {str(e)}")

# ============================================================================
# RESUME FUNCTIONALITY
# ============================================================================

def build_local_files_cache(output_dir):
    """Build a cache of already processed URLs from local files with multiple format support."""
    logger.info(f"Building local files cache from {output_dir}")
    print(f"🔄 Building local files cache...")
    start_time = time.time()
    
    url_to_file_cache = {}
    processed_files = 0
    format_stats = {"current_format": 0, "legacy_format": 0, "filename_based": 0, "no_url_found": 0}
    
    if not os.path.exists(output_dir):
        logger.info("Output directory doesn't exist, no local files to cache")
        return {}
    
    # Define multiple URL extraction patterns for different file formats
    url_patterns = [
        # Current format (most specific first)
        r'## 🔗 \*\*ZDROJOVÁ URL:\*\*\s*\n### \*\*(.+?)\*\*',
        
        # Legacy formats (broader patterns)
        r'ZDROJOVÁ URL:\s*\n.*?(\bhttps?://[^\s\n]+)',
        r'Source URL:\s*(\bhttps?://[^\s\n]+)',
        r'URL:\s*(\bhttps?://[^\s\n]+)',
        
        # Very broad patterns for any HTTP URL in first 2KB
        r'(\bhttps?://[^\s<>"\']+)',
    ]
    
    # Scan all .txt files in output directory
    for filename in os.listdir(output_dir):
        if not filename.endswith('.txt'):
            continue
            
        filepath = os.path.join(output_dir, filename)
        source_url = None
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                # Read first few KB to find URL in metadata
                content = f.read(3000)  # Read first 3KB to find metadata
                
                # Try each pattern in order of specificity
                for i, pattern in enumerate(url_patterns):
                    url_matches = re.findall(pattern, content, re.IGNORECASE | re.MULTILINE)
                    
                    if url_matches:
                        # Take the first URL that looks like it belongs to the target domain
                        for url_candidate in url_matches:
                            # Clean up the URL
                            url_candidate = url_candidate.strip().rstrip('.,;)')
                            
                            # Basic URL validation
                            if (url_candidate.startswith(('http://', 'https://')) and 
                                len(url_candidate) > 10 and
                                '.' in url_candidate):
                                
                                source_url = url_candidate
                                
                                # Track which pattern worked
                                if i == 0:
                                    format_stats["current_format"] += 1
                                elif i < 4:
                                    format_stats["legacy_format"] += 1
                                else:
                                    format_stats["filename_based"] += 1
                                break
                        
                        if source_url:
                            break
                
# If we found a URL, add to cache
                if source_url:
                    # Normalize the URL before caching it as the key
                    normalized_source_url = normalize_url_query_params(source_url)
                    url_to_file_cache[normalized_source_url] = {
                        'filepath': filepath,
                        'filename': filename
                    }
                    processed_files += 1
                    logger.debug(f"Found URL in {filename}: {source_url} (Normalized: {normalized_source_url})")
                else:
                    format_stats["no_url_found"] += 1
                    logger.debug(f"No URL found in {filename}")
                    
        except Exception as e:
            logger.warning(f"Error reading file {filepath}: {str(e)}")
            format_stats["no_url_found"] += 1
            continue
    
    elapsed_time = time.time() - start_time
    logger.info(f"Built local files cache in {elapsed_time:.2f}s")
    logger.info(f"Cache contains {len(url_to_file_cache)} processed URLs from {processed_files} files")
    
    # Log format statistics
    total_files = sum(format_stats.values())
    if total_files > 0:
        logger.info(f"File format breakdown:")
        logger.info(f"  Current format: {format_stats['current_format']} files")
        logger.info(f"  Legacy format: {format_stats['legacy_format']} files") 
        logger.info(f"  Filename-based: {format_stats['filename_based']} files")
        logger.info(f"  No URL found: {format_stats['no_url_found']} files")
        
        print(f"✅ Local cache built: {len(url_to_file_cache)} URLs indexed in {elapsed_time:.1f}s")
        print(f"📊 Format compatibility: {processed_files}/{total_files} files had extractable URLs")
    
    return url_to_file_cache


def is_url_already_processed_locally(url, local_cache):
    """Check if URL has already been processed based on local files cache."""
    # Normalize the URL before checking the cache for consistent lookup
    normalized_url = normalize_url_query_params(url)
    return normalized_url in local_cache

def is_url_from_test_urls(url):
    """Check if a URL is from the test_urls array in config."""
    if not TEST_URLS:
        return False
    
    # Normalize URLs for comparison (remove trailing slashes, etc.)
    normalized_url = url.rstrip('/')
    
    for test_url in TEST_URLS:
        normalized_test_url = test_url.rstrip('/')
        if normalized_url == normalized_test_url:
            return True
    
    return False

def should_process_url_with_resume(url, last_modified, last_run_timestamp, local_cache=None, enable_resume=False, rss_published_date=None, sitemap_last_run_timestamp=None, vector_store_id=None, vector_store_cache=None):
    """Enhanced version of URL processing decision logic, incorporating resume, RSS, and new missing lastmod rules."""
    logger.debug(f"Checking if URL should be processed (with resume): {url}")
    
    # --- NEW LOGIC: Skip if URL is the BASE_URL/domain itself ---
    # Normalize the URL by stripping query and fragment for comparison against canonical base URLs
    parsed_url = urlparse(url)
    normalized_url_for_base_check = urlunparse((parsed_url.scheme, parsed_url.netloc, parsed_url.path, '', '', ''))
    
    if normalized_url_for_base_check in CANONICAL_BASE_URLS:
        logger.info(f"🚫 Skipping URL {url}: Detected as BASE_URL/domain itself.")
        print(f"\n=== URL PROCESSING STATUS (BASE URL) ===")
        print(f"URL: {url}")
        print(f"Processing: SKIPPED (BASE URL)")
        print("=====================================\n")
        return False
    
    # --- NEW LOGIC: Skip if URL points to a file (PDF, DOCX, .ashx, etc.) ---
    if is_file_url(url):
        logger.info(f"🚫 Skipping URL {url}: Detected as file URL.")
        print(f"\n=== URL PROCESSING STATUS (FILE URL) ===")
        print(f"URL: {url}")
        print(f"Processing: SKIPPED (FILE URL)")
        print("=====================================\n")
        return False
    
    # SPECIAL HANDLING FOR TEST URLs: Bypass last modified check for test_urls from config
    if is_url_from_test_urls(url):
        logger.info(f"🧪 TEST URL DETECTED: {url} is from test_urls array - BYPASSING last modified check")
        print(f"\n=== TEST URL PROCESSING STATUS ===")
        print(f"URL: {url}")
        print(f"Source: test_urls array in config")
        print(f"Last modified check: BYPASSED")
        print(f"Processing: WILL PROCEED (TEST URL)")
        print("==================================\n")
        
        # Still check local resume cache if enabled
        if enable_resume and local_cache is not None:
            if is_url_already_processed_locally(url, local_cache):
                logger.info(f"URL {url} already processed locally. Skipping (RESUME).")
                print(f"\n=== TEST URL PROCESSING STATUS (RESUME) ===")
                print(f"URL: {url}")
                print(f"Local file exists: YES")
                print(f"Processing: SKIPPED (RESUME)")
                print("============================================\n")
                return False
            else:
                logger.info(f"Test URL {url} not found in local cache. Will process.")
        
        # For test URLs, always process (bypass last modified check)
        return True
    
    # Step 1: Check local resume cache first (if enabled)
    if enable_resume and local_cache is not None:
        if is_url_already_processed_locally(url, local_cache):
            logger.info(f"URL {url} already processed locally. Skipping (RESUME).")
            print(f"\n=== URL PROCESSING STATUS (RESUME) ===")
            print(f"URL: {url}")
            print(f"Local file exists: YES")
            print(f"Processing: SKIPPED (RESUME)")
            print("=====================================\n")
            return False
        else:
            logger.info(f"URL {url} not found in local cache. Will process.")
    
    # Step 2: Special handling for RSS URLs with published dates
    if rss_published_date is not None:
        # Pass the XML sitemap last_modified date and the sitemap last run timestamp for the secondary check
        return should_process_rss_url(url, rss_published_date, last_run_timestamp, last_modified, sitemap_last_run_timestamp, local_cache, vector_store_id, vector_store_cache)
    
    # Step 3: Timestamp-based logic for regular URLs (inlined from former should_process_url)
    
    # If checking is disabled, always process
    if not CHECK_LAST_MODIFIED:
        logger.info(f"CHECK_LAST_MODIFIED is False. Processing {url}")
        print(f"\n=== URL PROCESSING STATUS ===")
        print(f"URL: {url}")
        print(f"CHECK_LAST_MODIFIED is disabled")
        print(f"Processing: WILL PROCEED")
        print("=============================\n")
        return True
    
    # If it's the first run, always process
    if not last_run_timestamp:
        logger.info(f"No last run timestamp. Processing {url}")
        print(f"\n=== URL PROCESSING STATUS ===")
        print(f"URL: {url}")
        print(f"Last Run Timestamp: None (first run)")
        print(f"Processing: WILL PROCEED")
        print("=============================\n")
        return True
    
    # --- NEW LOGIC: Handle missing last_modified date (lastmod is None) ---
    if last_modified is None:
        # Check if the URL exists in the Vector Store or local cache
        is_local_file_present = is_url_already_processed_locally(url, local_cache) if local_cache is not None else False
        
        is_vector_store_file_present = False
        if vector_store_id and vector_store_cache is not None:
            # Use find_existing_files_by_url_cached (L5452)
            if find_existing_files_by_url_cached(url, vector_store_cache):
                is_vector_store_file_present = True
        
        if is_local_file_present or is_vector_store_file_present:
            # Exists in cache/store, and lastmod is missing -> SKIP (as requested by user)
            logger.info(f"No last modified date found for {url}. Skipping because it exists locally ({is_local_file_present}) or in Vector Store ({is_vector_store_file_present}).")
            print(f"\n=== URL PROCESSING STATUS (MISSING LASTMOD) ===")
            print(f"URL: {url}")
            print(f"Last Modified: Unknown")
            print(f"Last Run: {last_run_timestamp}")
            print(f"Local/Vector Store Exists: YES")
            print(f"Processing: SKIPPED (Missing lastmod + Exists)")
            print("=============================================\n")
            return False
        else:
            # Does not exist in cache/store, and lastmod is missing -> PROCESS (as requested by user)
            logger.info(f"No last modified date found for {url}. Processing because it is NEW (not found locally or in Vector Store).")
            print(f"\n=== URL PROCESSING STATUS (MISSING LASTMOD) ===")
            print(f"URL: {url}")
            print(f"Last Modified: Unknown")
            print(f"Last Run: {last_run_timestamp}")
            print(f"Local/Vector Store Exists: NO")
            print(f"Processing: WILL PROCEED (Missing lastmod + New URL)")
            print("=============================================\n")
            return True

    # Convert to UTC for comparison
    if last_modified.tzinfo is None:
        last_modified_utc = last_modified.replace(tzinfo=timezone.utc)
    else:
        last_modified_utc = last_modified.astimezone(timezone.utc)
        
    is_modified = last_modified_utc > last_run_timestamp
    
    print(f"\n=== URL PROCESSING STATUS ===")
    print(f"URL: {url}")
    print(f"Last Modified: {last_modified}")
    print(f"Last Run: {last_run_timestamp}")
    print(f"Is Modified Since Last Run: {'YES' if is_modified else 'NO'}")
    print(f"Processing: {'WILL PROCEED' if is_modified else 'SKIPPED'}")
    print("=============================\n")
    
    if is_modified:
        logger.info(f"URL {url} has been modified. Processing.")
    else:
        logger.info(f"URL {url} has NOT been modified. Skipping.")
        
    return is_modified

# ============================================================================
# FILE SAVING FUNCTIONS
# ============================================================================

def create_metadata_header(url, title, last_modified=None, path=None, provider_used=None, rss_metadata=None, source_page_summary=None, file_order=None, total_files=None, current_file_summary=None, overlap_summary=None):
    """
    Create a Markdown-formatted metadata header for the file.
    
    Args:
        url (str): Source URL of the content
        title (str): Page title
        last_modified (datetime, optional): Last modification date from sitemap
        path (str, optional): Navigation path from sitemap
        provider_used (str, optional): API provider used (jina/firecrawl)
        rss_metadata (dict, optional): RSS-specific metadata (published, summary, source_feed)
        source_page_summary (str, optional): Short 1-2 paragraph summary of the entire page content
        file_order (int, optional): Current file position in sequence (e.g., 1, 2, 3...)
        total_files (int, optional): Total number of files in sequence
        current_file_summary (str, optional): Summary of the current file in context of sequence
        overlap_summary (str, optional): Overlap summary for next file (not for last file)
    
    Returns:
        str: Formatted Markdown metadata header with table and emojis
    """
    from datetime import datetime
    
    # Current processing time
    processing_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Format last modified date
    last_mod_str = "Unknown"
    if last_modified:
        if hasattr(last_modified, 'strftime'):
            last_mod_str = last_modified.strftime("%Y-%m-%d %H:%M:%S")
        else:
            last_mod_str = str(last_modified)
    
    # Provider emoji mapping
    provider_emoji = {
        "jina": "🤖",
        "firecrawl": "🔥",
    }
    provider_display = f"{provider_emoji.get(provider_used or '', '📄')} {provider_used or 'N/A'}"
    
    # Create Markdown metadata section
    metadata_lines = [
        "# 📄 Metadata souboru",
        "",
        f"## 🔗 **ZDROJOVÁ URL:**",
        f"### **{url}**",
        "",
        f"## 📝 **TITULEK:**",
        f"### **{title or 'N/A'}**",
        "",
        f"## 🧭 **CESTA/NAVIGACE KE ZDROJOVÉ URL NA WEBU:**",
        f"### **{path or 'N/A'}**",
        "",
        f"## 📅 **DATUM POSLEDNÍ MODIFIKACE URL NA WEBU:**",
        f"### **{last_mod_str}**",
        ""
    ]
    
    # Add file order information if this is a chunked file
    if file_order is not None and total_files is not None:
        metadata_lines.extend([
            f"## 🗂️ **FILE ORDER INFORMATION:**",
            f"### **This is file {file_order} of {total_files} total files from the original document**",
            ""
        ])
    
    # Add SOURCE PAGE SUMMARY if available
    if source_page_summary and source_page_summary.strip():
        metadata_lines.extend([
            f"## 📊 **SOURCE PAGE SUMMARY:**",
            f"### **{source_page_summary.strip()}**",
            ""
        ])
    
    # Add CURRENT FILE SUMMARY if available (for chunked files)
    if current_file_summary and current_file_summary.strip():
        metadata_lines.extend([
            f"## 📋 **CURRENT FILE SUMMARY:**",
            f"### **{current_file_summary.strip()}**",
            ""
        ])
    
    # Add PREVIOUS FILE / INFORMATION OVERLAP SUMMARY if available (not for last file)
    if overlap_summary and overlap_summary.strip():
        metadata_lines.extend([
            f"## 🔄 **PREVIOUS FILE / INFORMATION OVERLAP SUMMARY:**",
            f"### **{overlap_summary.strip()}**",
            ""
        ])
    
    # Add RSS-specific metadata if available
    if rss_metadata:
        # Determine source type
        source_type = "📡 RSS Feed" if rss_metadata.get('source_feed') else "🗺️ Sitemap"
        metadata_lines.extend([
            f"## 📊 **TYP ZDROJE:**",
            f"### **{source_type}**",
            ""
        ])
        
        # Add RSS publication date if available
        if rss_metadata.get('published'):
            pub_date = rss_metadata['published']
            # Try to parse and format the date if it's a string
            if isinstance(pub_date, str) and dateutil_parser:
                try:
                    parsed_date = dateutil_parser.parse(pub_date)
                    pub_date = parsed_date.strftime("%Y-%m-%d %H:%M:%S")
                except:
                    # If parsing fails, use the original string
                    pass
            
            metadata_lines.extend([
                f"## 📅 **DATUM PUBLIKACE (RSS):**",
                f"### **{pub_date}**",
                ""
            ])
        
        # Add RSS source feed if available
        if rss_metadata.get('source_feed'):
            metadata_lines.extend([
                f"## 📡 **ZDROJOVÝ RSS FEED:**",
                f"### **{rss_metadata['source_feed']}**",
                ""
            ])
        
        # Add RSS summary/description if available
        if rss_metadata.get('summary') or rss_metadata.get('description'):
            summary = rss_metadata.get('summary') or rss_metadata.get('description')
            # Truncate if too long
            if len(summary) > 200:
                summary = summary[:200] + "..."
            metadata_lines.extend([
                f"## 📝 **POPIS/SHRNUTÍ (RSS):**",
                f"### **{summary}**",
                ""
            ])
        
        # Add Events XML specific metadata if available
        if rss_metadata.get('event_metadata'):
            event_data = rss_metadata['event_metadata']
            metadata_lines.extend([
                f"## 🎪 **INFORMACE O UDÁLOSTI:**",
                ""
            ])
            
            # Add event details in a structured format
            if event_data.get('event_id', 'Unknown') != 'Unknown':
                metadata_lines.append(f"- **ID události**: {event_data['event_id']}")
            if event_data.get('start_date', 'Unknown') != 'Unknown':
                date_time = f"{event_data['start_date']}"
                if event_data.get('start_time', 'Unknown') != 'Unknown':
                    date_time += f" {event_data['start_time']}"
                metadata_lines.append(f"- **Datum a čas**: {date_time}")
            if event_data.get('organizer', 'Unknown') != 'Unknown':
                metadata_lines.append(f"- **Organizátor**: {event_data['organizer']}")
            if event_data.get('place', 'Unknown') != 'Unknown':
                metadata_lines.append(f"- **Místo**: {event_data['place']}")
            if event_data.get('event_type', 'Unknown') != 'Unknown':
                metadata_lines.append(f"- **Typ události**: {event_data['event_type']}")
            
            metadata_lines.append("")
        
        # Add Events XML specific metadata if available
        if rss_metadata.get('event_metadata'):
            event_data = rss_metadata['event_metadata']
            metadata_lines.extend([
                f"## 🎪 **INFORMACE O UDÁLOSTI:**",
                ""
            ])
            
            # Add event details in a structured format
            if event_data.get('event_id', 'Unknown') != 'Unknown':
                metadata_lines.append(f"- **ID události**: {event_data['event_id']}")
            if event_data.get('start_date', 'Unknown') != 'Unknown':
                date_time = f"{event_data['start_date']}"
                if event_data.get('start_time', 'Unknown') != 'Unknown':
                    date_time += f" {event_data['start_time']}"
                metadata_lines.append(f"- **Datum a čas**: {date_time}")
            if event_data.get('organizer', 'Unknown') != 'Unknown':
                metadata_lines.append(f"- **Organizátor**: {event_data['organizer']}")
            if event_data.get('place', 'Unknown') != 'Unknown':
                metadata_lines.append(f"- **Místo**: {event_data['place']}")
            if event_data.get('event_type', 'Unknown') != 'Unknown':
                metadata_lines.append(f"- **Typ události**: {event_data['event_type']}")
            
            metadata_lines.append("")
    else:
        # If no RSS metadata, indicate it's from sitemap
        metadata_lines.extend([
            f"## 📊 **TYP ZDROJE:**",
            f"### **🗺️ Sitemap**",
            ""
        ])
    
    metadata_lines.extend([
        "---",
        "",
        ""   # Empty line before content
    ])
    
    return "\n".join(metadata_lines)


def save_markdown_to_file(content, title, url, question_text=None, upload_to_vector_store=False, vector_store_id=None, enable_deduplication=True, chunking_strategy=None, vector_store_cache=None, last_modified=None, path=None, provider_used=None, rss_metadata=None, lookup_url=None, source_page_summary=None, file_order=None, total_files=None, current_file_summary=None, overlap_summary=None, filename=None):
    """Save markdown content to a txt file with metadata header and optionally upload to OpenAI Vector Store.
    
    Args:
        question_text (str, optional): Pipe-separated keywords and questions generated separately
        url (str): Clean original URL for display in metadata
        lookup_url (str, optional): URL with fragments for vector store deduplication. Defaults to url if not provided.
        source_page_summary (str, optional): Short 1-2 paragraph summary of the entire page content
        file_order (int, optional): Current file position in sequence (e.g., 1, 2, 3...)
        total_files (int, optional): Total number of files in sequence
        current_file_summary (str, optional): Summary of the current file in context of sequence
        overlap_summary (str, optional): Overlap summary for next file (not for last file)
        filename (str, optional): Pre-constructed filename (without extension) to use instead of generating from title
        ... (other parameters remain the same)
    """
    try:
        # Use lookup_url for vector store operations, default to url if not provided
        vector_store_url = lookup_url if lookup_url is not None else url
        
        # Use pre-constructed filename if provided, otherwise create from URL/title
        if filename:
            # Pre-constructed filename provided (from chunking logic)
            filename_without_ext = filename
            filename_with_ext = f"{filename}.txt"
            filepath = os.path.join(OUTPUT_DIR, filename_with_ext)
            logger.info(f"🏷️ Using pre-constructed filename: {filename}")
        else:
            # Create base filename from URL path instead of title
            base_filename = create_filename_from_url(url, title, rss_metadata)
            
            # Check if this is a paginated file and extract page number
            is_paginated = False
            page_number = None
            chunk_postfix = None
            
            if "_PAGE" in title:
                is_paginated = True
                # Extract page number from title (e.g., "aktuality_PAGE2" → page_number = 2)
                try:
                    page_part = title.split("_PAGE")[-1]
                    # Handle cases like "PAGE2A" where there might be chunk postfix
                    page_match = re.match(r'^(\d+)([A-Z]?).*$', page_part)
                    if page_match:
                        page_number = int(page_match.group(1))
                        if page_match.group(2):  # If there's a chunk postfix
                            chunk_postfix = page_match.group(2)
                    else:
                        page_number = int(page_part) if page_part.isdigit() else None
                except (ValueError, IndexError):
                    logger.warning(f"⚠️ Could not extract page number from title: {title}")
                    page_number = None
            
            # Check if this has chunk postfix (for non-paginated chunked content)
            elif title and len(title) > 0 and title[-1].isupper() and title[-1].isalpha():
                # Check if last character is A-Z (chunk postfix)
                potential_chunk = title[-1]
                if potential_chunk in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                    chunk_postfix = potential_chunk
                    # Remove chunk postfix from base filename creation
                    base_filename = create_filename_from_url(url, title[:-1])
            
            # Construct the final filename with all postfixes
            filename_without_ext = construct_final_filename(
                base_filename=base_filename,
                is_paginated=is_paginated,
                page_number=page_number,
                chunk_postfix=chunk_postfix,
                dedup_counter=None  # Will be handled below
            )
            
            # Add .txt extension
            filename_with_ext = f"{filename_without_ext}.txt"
            filepath = os.path.join(OUTPUT_DIR, filename_with_ext)
        
        # Check for collisions (local filesystem AND Vector Store) and apply V[N] versioning ONLY if needed
        collision_detected = False
        version_counter = 1
        
        # Check 1: Local filesystem collision
        local_collision = os.path.exists(filepath)
        if local_collision:
            logger.info(f"🔄 LOCAL FILE COLLISION detected: {filepath}")
            collision_detected = True
        
        # Check 2: Vector Store collision (if Vector Store is enabled)
        vector_collision = False
        if upload_to_vector_store and vector_store_id and enable_deduplication:
            # Check if this URL already exists in Vector Store
            if vector_store_cache is not None:
                existing_files = find_existing_files_by_url_cached(vector_store_url, vector_store_cache)
                if existing_files:
                    logger.info(f"🔄 VECTOR STORE COLLISION detected: {len(existing_files)} existing files for {vector_store_url}")
                    vector_collision = True
                    collision_detected = True
        
        # Apply V[N] versioning ONLY if collision was detected
        if collision_detected:
            logger.info(f"⚠️ COLLISION DETECTED - Applying V[N] versioning (local: {local_collision}, vector: {vector_collision})")
            
            # For collisions, reconstruct filename with V[N] versioning if we have the components
            if filename:
                # Pre-constructed filename - need to parse and add versioning
                original_filepath = filepath
                while os.path.exists(filepath):
                    name, ext = os.path.splitext(original_filepath)
                    filepath = f"{name}_V{version_counter}{ext}"
                    version_counter += 1
            else:
                # Traditional filename construction with V[N] versioning
                original_filepath = filepath
                while os.path.exists(filepath):
                    filename_with_version = construct_final_filename(
                        base_filename=base_filename,
                        is_paginated=is_paginated,
                        page_number=page_number,
                        chunk_postfix=chunk_postfix,
                        dedup_counter=version_counter
                    )
                    filepath = os.path.join(OUTPUT_DIR, f"{filename_with_version}.txt")
                    version_counter += 1
        else:
            logger.info(f"✅ NO COLLISION - Using original filename without versioning")
        
        # Create metadata header with clean URL for display and include all new metadata fields
        metadata_header = create_metadata_header(url, title, last_modified, path, provider_used, rss_metadata, source_page_summary, file_order, total_files, current_file_summary, overlap_summary)
        
        # Insert QUESTION section if provided
        question_section = ""
        if question_text:
            question_section = f"# **QUESTION:**\n\n{question_text}\n\n# **ANSWER:**\n\n"
        
        # Combine metadata header + QUESTION + content
        full_content = metadata_header + question_section + content
        
        # Save content to file
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(full_content)
        
        logger.info(f"Saved markdown content to: {filepath}")
        print(f"✅ Saved: {filepath}")
        
        # Upload to OpenAI Vector Store if requested (use lookup_url for deduplication)
        if upload_to_vector_store and vector_store_id:
            upload_success = upload_and_add_to_vector_store(filepath, vector_store_id, vector_store_url, title, enable_deduplication, chunking_strategy, vector_store_cache)
            if not upload_success:
                logger.warning(f"Failed to upload {filepath} to vector store, but file was saved locally")
        
        return filepath
        
    except Exception as e:
        logger.error(f"Error saving markdown to file for URL {url}: {str(e)}")
        return None

# ============================================================================
# TEST URLs PROCESSING FUNCTIONS
# ============================================================================

def validate_test_urls(test_urls):
    """
    Validate test URLs array for proper format and domain compatibility.
    
    Args:
        test_urls (list): List of URLs to validate
    
    Returns:
        tuple: (valid_urls, validation_errors)
    """
    logger.info(f"Validating {len(test_urls)} test URLs")
    
    valid_urls = []
    validation_errors = []
    
    for i, url in enumerate(test_urls, 1):
        try:
            # Basic URL validation
            if not url or not url.strip():
                validation_errors.append(f"URL {i}: Empty URL")
                continue
                
            url = url.strip()
            
            # Check if URL has proper protocol
            if not url.startswith(('http://', 'https://')):
                validation_errors.append(f"URL {i}: Missing protocol (http:// or https://): {url}")
                continue
            
            # Parse URL for further validation
            try:
                parsed_url = urlparse(url)
                
                # Check if URL has valid domain
                if not parsed_url.netloc:
                    validation_errors.append(f"URL {i}: Invalid domain: {url}")
                    continue
                
                # Check if URL belongs to the target domain (optional warning)
                if parsed_url.netloc not in [BASE_NETLOC, NON_WWW_BASE_NETLOC]:
                    logger.warning(f"URL {i} is from different domain ({parsed_url.netloc}) than base URL ({BASE_NETLOC})")
                    # Don't add to validation_errors - allow cross-domain testing
                
                # Check if URL is blacklisted
                if url in BLACKLISTED_URLS:
                    validation_errors.append(f"URL {i}: URL is blacklisted: {url}")
                    continue
                
                valid_urls.append(url)
                logger.info(f"✅ Valid test URL {i}: {url}")
                
            except Exception as e:
                validation_errors.append(f"URL {i}: Failed to parse URL '{url}': {str(e)}")
                continue
                
        except Exception as e:
            validation_errors.append(f"URL {i}: Validation error: {str(e)}")
            continue
    
    logger.info(f"Test URL validation complete: {len(valid_urls)} valid, {len(validation_errors)} errors")
    return valid_urls, validation_errors

def process_test_urls(test_urls, url_last_modified_map={}, last_run_timestamp=None, local_files_cache=None, enable_resume=False, vector_store_id=None, vector_store_cache=None):
    """
    Process test URLs for testing purposes, overriding HTML sitemap processing.
    
    Args:
        test_urls (list): List of test URLs to process
        url_last_modified_map (dict): Mapping of URLs to their last modified dates from XML sitemap
        last_run_timestamp (datetime): Timestamp of the last script run
        local_files_cache (dict): Cache of already processed URLs for resume functionality
        enable_resume (bool): Whether resume functionality is enabled
    
    Returns:
        list: List of processed URL dictionaries with metadata
    """
    logger.info(f"🧪 TEST MODE: Processing {len(test_urls)} test URLs (overriding HTML sitemap)")
    print(f"🧪 TEST MODE ACTIVATED: Processing {len(test_urls)} test URLs")
    print(f"⚠️  HTML sitemap processing is BYPASSED")
    
    # Validate test URLs first
    valid_urls, validation_errors = validate_test_urls(test_urls)
    
    # Report validation results
    if validation_errors:
        logger.warning(f"Test URL validation found {len(validation_errors)} errors:")
        print(f"⚠️  Test URL validation errors:")
        for error in validation_errors:
            logger.warning(f"  - {error}")
            print(f"   ❌ {error}")
    
    if not valid_urls:
        logger.error("No valid test URLs found after validation")
        print(f"❌ No valid test URLs to process")
        return []
    
    logger.info(f"Processing {len(valid_urls)} valid test URLs")
    print(f"✅ Processing {len(valid_urls)} valid test URLs")
    
    # Convert URLs to the same format as sitemap extraction
    extracted_urls = []
    
    for i, url in enumerate(valid_urls, 1):
        try:
            # Make URL absolute if it's relative (though test URLs should be absolute)
            absolute_url = urljoin(BASE_URL, url)
            
            # Extract title from URL path as fallback
            parsed_url = urlparse(absolute_url)
            path_parts = [part for part in parsed_url.path.split('/') if part]
            title = path_parts[-1] if path_parts else 'Homepage'
            
            # Create a test-specific path
            path = f"TEST URL {i}/{len(valid_urls)} > {title}"
            
            logger.info(f"\n=== Processing Test URL {i}/{len(valid_urls)}: {absolute_url} ===")
            logger.info(f"Title: {title}")
            logger.info(f"Path: {path}")
            
            # Find last modified date from XML sitemap (if available)
            last_modified = find_url_last_modified(absolute_url, url_last_modified_map)
            
            # Check if the URL should be processed
            if should_process_url_with_resume(absolute_url, last_modified, last_run_timestamp, local_files_cache, enable_resume, rss_published_date=None, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache):
                extracted_urls.append({
                    'url': absolute_url,
                    'title': title,
                    'path': path,
                    'last_modified': last_modified,
                    'test_url': True  # Mark as test URL for identification
                })
                logger.info(f"✅ Added test URL: {absolute_url}")
            else:
                logger.info(f"⏭️  Skipping test URL {absolute_url} (timestamp check or already processed locally).")
                
        except Exception as e:
            logger.error(f"Error processing test URL {i} ({url}): {str(e)}")
            continue
    
    logger.info(f"🧪 TEST MODE: Successfully prepared {len(extracted_urls)} test URLs for processing")
    print(f"🧪 Test URL preparation complete: {len(extracted_urls)} URLs ready for processing")
    
    return extracted_urls

# ============================================================================
# MAIN FUNCTION
# ============================================================================

def main(args=None):
    """Main function to orchestrate the scraping process."""
    global CONFIG, JINA_REMOVE_SELECTORS, OPENAI_VECTOR_STORE_ID, ENABLE_DEDUPLICATION
    global DEFAULT_CHUNKING_STRATEGY, DEFAULT_MAX_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP
    global BASE_URL, SITEMAP_URL, XML_SITEMAP_URL, OUTPUT_DIR, CHECK_LAST_MODIFIED
    global JINA_AI_API_KEY, FIRECRAWL_API_KEY, OPENAI_API_KEY, MARKDOWN_PROVIDERS
    
    # Load configuration
    config_file = "config.json"
    if args and args.config:
        config_file = args.config
    
    try:
        load_configuration(config_file)
        setup_logging()
    except Exception as e:
        print(f"❌ Error loading configuration: {str(e)}")
        return
    
    # Validate mutually exclusive arguments
    if args:
        exclusive_modes = [args.rss_only, args.sitemap_only, getattr(args, 'xml_only', False)]
        if sum(exclusive_modes) > 1:
            print("❌ Error: --rss-only, --sitemap-only, and --xml-only are mutually exclusive")
            return
    
    # Override config with command line arguments
    if args:
        # Test resume functionality and exit early if requested
        if args.test_resume:
            print(f"🧪 Testing resume cache functionality...")
            print(f"📁 Output directory: {OUTPUT_DIR}")
            
            if os.path.exists(OUTPUT_DIR):
                test_cache = build_local_files_cache(OUTPUT_DIR)
                print(f"\n📊 RESUME CACHE TEST RESULTS:")
                print(f"✅ Cache would contain {len(test_cache)} URLs")
                print(f"📁 Scanned directory: {os.path.abspath(OUTPUT_DIR)}")
                
                if len(test_cache) > 0:
                    print(f"\n📝 Sample URLs found:")
                    sample_urls = list(test_cache.keys())[:5]  # Show first 5 URLs
                    for i, url in enumerate(sample_urls, 1):
                        filename = test_cache[url]['filename']
                        print(f"   {i}. {url}")
                        print(f"      → {filename}")
                    
                    if len(test_cache) > 5:
                        print(f"   ... and {len(test_cache) - 5} more URLs")
                        
                print(f"\n✅ Resume functionality test completed!")
                print(f"💡 Use --resume flag to actually skip these URLs during processing")
            else:
                print(f"❌ Output directory does not exist: {OUTPUT_DIR}")
                print(f"💡 Run the script normally first to create files, then test resume")
            
            return  # Exit early for test mode
        
        # Override API keys if provided
        if args.jina_api_key:
            JINA_AI_API_KEY = args.jina_api_key
            MARKDOWN_PROVIDERS["jina"]["api_key"] = args.jina_api_key
            logger.info("Jina API key overridden from command line")
        
        if args.firecrawl_api_key:
            FIRECRAWL_API_KEY = args.firecrawl_api_key
            MARKDOWN_PROVIDERS["firecrawl"]["api_key"] = args.firecrawl_api_key
            logger.info("Firecrawl API key overridden from command line")
        
        if args.openai_api_key:
            OPENAI_API_KEY = args.openai_api_key
            logger.info("OpenAI API key overridden from command line")
        
        # Override URLs if provided
        if args.base_url:
            BASE_URL = args.base_url
            logger.info(f"Base URL overridden: {BASE_URL}")
        
        if args.sitemap_url:
            SITEMAP_URL = args.sitemap_url
            logger.info(f"Sitemap URL overridden: {SITEMAP_URL}")
        
        if args.xml_sitemap_url:
            XML_SITEMAP_URL = args.xml_sitemap_url
            logger.info(f"XML Sitemap URL overridden: {XML_SITEMAP_URL}")
        
        # Override output directory if provided
        if args.output_dir:
            OUTPUT_DIR = args.output_dir
            logger.info(f"Output directory overridden: {OUTPUT_DIR}")
            # Create new output directory
            os.makedirs(OUTPUT_DIR, exist_ok=True)
        
        # Set debug level if requested
        if args.debug:
            logger.setLevel(logging.DEBUG)
            console_handler.setLevel(logging.DEBUG)
            file_handler.setLevel(logging.DEBUG)
            logger.info("Debug mode enabled")
        
        # Disable last modified checking if requested
        if args.no_check_modified:
            CHECK_LAST_MODIFIED = False
            logger.info("Last modified checking disabled - will process all URLs")
        
        # Set verbose URL matching if requested
        if args.verbose_url_matching:
            global VERBOSE_URL_MATCHING
            VERBOSE_URL_MATCHING = True
            logger.info("Verbose URL matching enabled - will show detailed matching info")
        
        # Override test URLs if provided via command line
        if args.test_urls:
            global TEST_URLS
            TEST_URLS = args.test_urls
            logger.info(f"Test URLs overridden from command line: {len(TEST_URLS)} URLs")
            print(f"🧪 Command line test URLs: {len(TEST_URLS)} URLs provided")
            for i, url in enumerate(TEST_URLS, 1):
                print(f"   {i}. {url}")
    
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRIPT_NAME} at {start_time} ===")
    
    # Set remove selectors (from config or command line)
    remove_selectors = JINA_REMOVE_SELECTORS
    if args and args.jina_remove_selectors:
        remove_selectors = args.jina_remove_selectors
        logger.info(f"Using custom remove selectors: {remove_selectors}")
        print(f"🎯 Custom remove selectors: {remove_selectors}")
    
    # Set configuration based on arguments (override config file)
    vector_store_id = args.vector_store_id if args and args.vector_store_id else OPENAI_VECTOR_STORE_ID
    deduplication_enabled = not args.disable_deduplication if args and args.disable_deduplication else ENABLE_DEDUPLICATION
    
    # Create chunking strategy
    chunking_strategy = None
    if vector_store_id:
        strategy_type = args.chunking_strategy if args and args.chunking_strategy else DEFAULT_CHUNKING_STRATEGY
        max_chunk_size = args.max_chunk_size if args and args.max_chunk_size else DEFAULT_MAX_CHUNK_SIZE
        chunk_overlap = args.chunk_overlap if args and args.chunk_overlap else DEFAULT_CHUNK_OVERLAP
        
        chunking_strategy = create_chunking_strategy(
            strategy_type=strategy_type,
            max_chunk_size=max_chunk_size,
            chunk_overlap=chunk_overlap
        )
    
    # Build Vector Store cache for fast lookups (only if using vector store)
    vector_store_cache = None
    skip_cache = args.skip_vector_cache if args else False
    
    if vector_store_id and deduplication_enabled and not skip_cache:
        logger.info("Building Vector Store cache for optimized deduplication...")
        vector_store_cache = build_vector_store_cache(vector_store_id)
        print(f"🚀 Vector Store cache built with {len(vector_store_cache)} files")
    elif skip_cache and vector_store_id:
        logger.info("Skipping Vector Store cache build (--skip-vector-cache enabled)")
        print(f"⚡ Skipping Vector Store cache build for faster startup")
        print(f"⚠️  Deduplication will use slower API lookups")
    
    # Build local files cache for resume functionality AND existence checks
    # The cache is needed for:
    # 1. Resume mode (--resume flag)
    # 2. Existence checking for URLs with missing lastmod (RSS URLs and regular URLs)
    local_files_cache = None
    enable_resume = args.resume if args else False
    
    # Determine if cache is needed
    cache_needed_for_existence_check = bool(vector_store_id) or bool(RSS_FEEDS)
    
    if enable_resume or cache_needed_for_existence_check:
        if enable_resume:
            logger.info("Resume mode enabled - building local files cache...")
        else:
            logger.info("Building local files cache for existence checks (URLs without lastmod)...")
        
        local_files_cache = build_local_files_cache(OUTPUT_DIR)
        
        if enable_resume:
            print(f"🔄 Resume cache built with {len(local_files_cache)} processed URLs")
        else:
            print(f"📋 Local existence cache built with {len(local_files_cache)} processed URLs")
    
    # Determine processing mode
    rss_only = args.rss_only if args else False
    sitemap_only = args.sitemap_only if args else False
    xml_only = getattr(args, 'xml_only', False) if args else False
    
    # Get appropriate last run timestamp
    if rss_only:
        last_run_timestamp = get_last_run_timestamp("rss")
        logger.info("RSS-only mode enabled")
    elif sitemap_only:
        last_run_timestamp = get_last_run_timestamp("sitemap")
        logger.info("Sitemap-only mode enabled")
    elif xml_only:
        last_run_timestamp = get_last_run_timestamp("sitemap")
        logger.info("XML-only mode enabled")
    else:
        last_run_timestamp = get_last_run_timestamp("combined")
        logger.info("Combined mode (RSS + Sitemap)")
    
    if last_run_timestamp:
        logger.info(f"Last run timestamp: {last_run_timestamp.isoformat()}")
    else:
        logger.info("No last run timestamp found, will process all URLs")
    
    try:
        extracted_urls = []
        
        # Initialize sitemap processing variables
        main_menu = None
        url_last_modified_map = {}
        
        # Step 0: Check for test URLs (overrides HTML sitemap processing)
        test_urls_to_use = []
        
        # Collect test URLs from both config and command line (command line takes priority)
        if TEST_URLS:
            test_urls_to_use = TEST_URLS
            logger.info(f"🧪 Test URLs found in configuration: {len(test_urls_to_use)}")
        
        # Step 1-4: Process sitemap OR test URLs (unless RSS-only mode)
        if not rss_only:
            # Check if we should use test URLs instead of normal sitemap processing
            if test_urls_to_use:
                logger.info("🧪 TEST MODE: Using test URLs instead of HTML sitemap processing")
                print(f"🧪 TEST MODE ENABLED: Processing {len(test_urls_to_use)} test URLs")
                
                # Still fetch XML sitemap for last modified dates (needed for test URLs)
                logger.info("Step 1: Fetching XML sitemap for last modified dates (for test URLs)")
                url_last_modified_map = fetch_xml_sitemap()
                logger.info(f"Fetched last modified dates for {len(url_last_modified_map)} URLs")
                
                # Step 2: Process test URLs instead of HTML sitemap
                logger.info("Step 2: Processing test URLs (overriding HTML sitemap)")
                extracted_urls = process_test_urls(test_urls_to_use, url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache)
                
                logger.info(f"🧪 TEST MODE: Found {len(extracted_urls)} test URLs to process")
            elif xml_only:
                # XML-only mode: Skip HTML sitemap, only use XML sitemap
                logger.info("XML-only mode: Skipping HTML sitemap processing")
                
                # Step 3: Fetch XML sitemap data
                logger.info("Step 3: Fetching XML sitemap for URLs and last modified dates")
                url_last_modified_map = fetch_xml_sitemap()
                logger.info(f"Fetched last modified dates for {len(url_last_modified_map)} URLs")
                
                # Step 4: Extract URLs from XML sitemap
                logger.info("Step 4: Extracting URLs from XML sitemap")
                extracted_urls = extract_urls_from_xml_sitemap(url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache)
                
                logger.info(f"Found {len(extracted_urls)} URLs from XML sitemap")
            elif SITEMAP_URL and SITEMAP_URL.strip():
                # Normal sitemap processing with HTML sitemap (only if URL is provided)
                # Step 1: Get HTML sitemap content with Jina AI
                logger.info(f"Step 1: Fetching HTML sitemap from {SITEMAP_URL}")
                html_content = get_html_content_via_jina(SITEMAP_URL, remove_selectors)
                if not html_content:
                    logger.error(f"Failed to get HTML sitemap content from {SITEMAP_URL}")
                    logger.info("Falling back to XML-only processing...")
                    
                    # Fallback to XML-only processing
                    url_last_modified_map = fetch_xml_sitemap()
                    logger.info(f"Fetched last modified dates for {len(url_last_modified_map)} URLs")
                    extracted_urls = extract_urls_from_xml_sitemap(url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache)
                    logger.info(f"Found {len(extracted_urls)} URLs from XML sitemap (fallback)")
                else:
                    # Step 2: Fetch XML sitemap data for last modified dates
                    logger.info("Step 2: Fetching XML sitemap for last modified dates")
                    url_last_modified_map = fetch_xml_sitemap()
                    logger.info(f"Fetched last modified dates for {len(url_last_modified_map)} URLs")
                    
                    # Step 3: Choose parsing approach based on arguments
                    if args and args.legacy_html_parsing:
                        # Use legacy HTML parsing approach
                        logger.info("Step 3: Using legacy HTML sitemap parsing (as requested)")
                        main_menu = parse_menu(html_content)
                        if main_menu:
                            extracted_urls = extract_links(main_menu, url_last_modified_map=url_last_modified_map,
                                                         last_run_timestamp=last_run_timestamp, local_files_cache=local_files_cache, enable_resume=enable_resume, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache)
                            logger.info(f"Found {len(extracted_urls)} URLs from legacy parsing")
                        else:
                            logger.error("Legacy parsing failed, falling back to XML-only")
                            extracted_urls = extract_urls_from_xml_sitemap(url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache)
                    else:
                        # Use new HTML anchor tag extraction approach (DEFAULT)
                        logger.info("Step 3: Extracting URLs from HTML sitemap by parsing <a> anchor tags")
                        extracted_urls = extract_links_from_html_sitemap(html_content,
                                                                       url_last_modified_map=url_last_modified_map,
                                                                       last_run_timestamp=last_run_timestamp,
                                                                       local_files_cache=local_files_cache,
                                                                       enable_resume=enable_resume,
                                                                       vector_store_id=vector_store_id,
                                                                       vector_store_cache=vector_store_cache)
                        
                        logger.info(f"Found {len(extracted_urls)} URLs from HTML sitemap (anchor tag parsing)")
                        
                        # Enhanced fallback logic for when anchor tag parsing fails
                        if len(extracted_urls) == 0:
                            logger.warning("No links found with anchor tag parsing, trying alternative approaches...")
                            
                            # Fallback to legacy parsing
                            logger.info("Attempting legacy HTML parsing as fallback...")
                            main_menu = parse_menu(html_content)
                            if main_menu:
                                extracted_urls = extract_links(main_menu, url_last_modified_map=url_last_modified_map,
                                                              last_run_timestamp=last_run_timestamp, local_files_cache=local_files_cache, enable_resume=enable_resume, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache)
                                logger.info(f"Found {len(extracted_urls)} URLs from legacy parsing")
                            else:
                                logger.warning("Legacy parsing also failed, falling back to XML-only")
                                extracted_urls = extract_urls_from_xml_sitemap(url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache)
            else:
                # No HTML sitemap URL provided, use XML-only processing
                logger.info("No HTML sitemap URL provided, using XML-only processing")
                
                # Step 3: Fetch XML sitemap data
                logger.info("Step 3: Fetching XML sitemap for URLs and last modified dates")
                url_last_modified_map = fetch_xml_sitemap()
                logger.info(f"Fetched last modified dates for {len(url_last_modified_map)} URLs")
                
                # Step 4: Extract URLs from XML sitemap
                logger.info("Step 4: Extracting URLs from XML sitemap")
                extracted_urls = extract_urls_from_xml_sitemap(url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume, vector_store_id=vector_store_id, vector_store_cache=vector_store_cache)
                
                logger.info(f"Found {len(extracted_urls)} URLs from XML sitemap")
        else:
            logger.info("Skipping sitemap processing (RSS-only mode)")
            # Still fetch XML sitemap for RSS last modified dates
            url_last_modified_map = fetch_xml_sitemap()
        
        # Step 4.5: Process RSS feeds (unless sitemap-only or xml-only mode)
        if not sitemap_only and not xml_only and RSS_FEEDS:
            logger.info("Step 4.5: Processing RSS feeds")
            rss_urls = process_rss_feeds(url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume)
            extracted_urls.extend(rss_urls)
            logger.info(f"Added {len(rss_urls)} URLs from RSS feeds")
        elif sitemap_only:
            logger.info("Skipping RSS processing (sitemap-only mode)")
        elif xml_only:
            logger.info("Skipping RSS processing (XML-only mode)")
        else:
            logger.info("Step 4.5: No RSS feeds configured, skipping RSS processing")
        
        logger.info(f"Total URLs to process: {len(extracted_urls)}")
        
        # Step 5: Process each URL with ENHANCED PAGINATION SUPPORT
        logger.info("Step 5: Processing individual URLs with pagination detection")
        processed_count = 0
        success_count = 0
        
        for url_info in extracted_urls:
            url = url_info['url']
            title = url_info['title']
            path = url_info['path']
            
            logger.info(f"\n--- Processing URL {processed_count + 1}/{len(extracted_urls)} ---")
            logger.info(f"URL: {url}")
            logger.info(f"Title: {title}")
            logger.info(f"Path: {path}")
            
            # Prepare RSS metadata if available
            rss_metadata = None
            if any(key in url_info for key in ['published', 'summary', 'description', 'source_feed', 'event_metadata']):
                rss_metadata = {
                    'published': url_info.get('published'),
                    'summary': url_info.get('summary'),
                    'description': url_info.get('description'),
                    'source_feed': url_info.get('source_feed'),
                    'event_metadata': url_info.get('event_metadata')  # Include event-specific metadata
                }
                logger.info(f"RSS metadata available - Published: {rss_metadata['published']}, Source: {rss_metadata['source_feed']}")
                
                # Log event metadata if present
                if rss_metadata.get('event_metadata'):
                    event_data = rss_metadata['event_metadata']
                    logger.info(f"Event metadata - ID: {event_data.get('event_id')}, Type: {event_data.get('event_type')}, Organizer: {event_data.get('organizer')}")
            
            # NEW ENHANCED PROCESSING WITH PAGINATION SUPPORT
            try:
                # Process URL with pagination detection and handling
                pagination_result = process_paginated_url(
                    url, title, path, url_last_modified_map, last_run_timestamp,
                    local_files_cache, enable_resume, vector_store_id, deduplication_enabled,
                    chunking_strategy, vector_store_cache, remove_selectors, rss_metadata,
                    current_depth=0  # Start at root level (depth 0)
                )
                
                # Update counters based on pagination processing results
                url_success_count, url_total_processed = pagination_result
                success_count += url_success_count
                # Note: processed_count tracks URLs, not individual files/pages
                
                if url_success_count > 0:
                    logger.info(f"✅ Successfully processed URL with potential pagination: {url} ({url_success_count} files/pages)")
                else:
                    logger.error(f"❌ Failed to process URL: {url}")
                    
            except Exception as e:
                logger.error(f"❌ Error in enhanced pagination processing for {url}: {str(e)}")
            
            processed_count += 1
            
            # Small delay between URL requests
            time.sleep(1)
        
        # Save timestamp of this run
        if rss_only:
            save_last_run_timestamp("rss")
        elif sitemap_only:
            save_last_run_timestamp("sitemap")
        elif xml_only:
            save_last_run_timestamp("sitemap")
        else:
            save_last_run_timestamp("combined")
            # Also save separate timestamps for future selective runs
            save_last_run_timestamp("rss")
            save_last_run_timestamp("sitemap")
        
        end_time = datetime.now()
        duration = end_time - start_time
        
        logger.info(f"\n=== PROCESSING COMPLETE ===")
        logger.info(f"Start time: {start_time}")
        logger.info(f"End time: {end_time}")
        logger.info(f"Duration: {duration}")
        logger.info(f"URLs processed: {processed_count}")
        logger.info(f"Files saved successfully: {success_count}")
        logger.info(f"Success rate: {(success_count/processed_count*100):.1f}%" if processed_count > 0 else "N/A")
        logger.info(f"Files saved to: {os.path.abspath(OUTPUT_DIR)}")
        logger.info(f"Config file used: {config_file}")
        logger.info(f"Target website: {BASE_URL}")
        
        # Log processing mode
        if test_urls_to_use:
            logger.info(f"Processing mode: TEST MODE ({len(test_urls_to_use)} test URLs)")
        elif rss_only:
            logger.info("Processing mode: RSS-only")
        elif sitemap_only:
            logger.info("Processing mode: Sitemap-only")
        elif xml_only:
            logger.info("Processing mode: XML-only")
        else:
            logger.info("Processing mode: Combined (RSS + Sitemap)")
            
        # Log resume mode
        if enable_resume:
            logger.info(f"Resume mode: ENABLED - Skipped {len(local_files_cache) if local_files_cache else 0} already processed URLs")
            
        logger.info(f"RSS feeds processed: {len(RSS_FEEDS) if not sitemap_only and not xml_only else 0}")
        if vector_store_id:
            logger.info(f"Vector Store uploads enabled: {vector_store_id}")

        # Log final OpenRouter token usage summary
        if global_token_usage['api_calls_count'] > 0:
            logger.info(f"=== OPENROUTER TOKEN USAGE SUMMARY ===")
            logger.info(f"Total API calls made: {global_token_usage['api_calls_count']}")
            logger.info(f"Total input tokens (prompts): {global_token_usage['total_prompt_tokens']:,}")
            logger.info(f"Total output tokens (completions): {global_token_usage['total_completion_tokens']:,}")
            logger.info(f"Total tokens used: {global_token_usage['total_tokens']:,}")
            logger.info(f"Average tokens per call: {global_token_usage['total_tokens'] / global_token_usage['api_calls_count']:.1f}")
            logger.info("=====================================")
        
        print(f"\n🎉 Processing complete!")
        print(f"📁 Files saved to: {os.path.abspath(OUTPUT_DIR)}")
        print(f"📊 Success: {success_count}/{processed_count} URLs processed")
        print(f"🌐 Target website: {BASE_URL}")
        
        # Print processing mode
        if test_urls_to_use:
            print(f"🧪 Mode: TEST MODE ({len(test_urls_to_use)} test URLs)")
        elif rss_only:
            print(f"📡 Mode: RSS-only ({len(RSS_FEEDS)} feeds)")
        elif sitemap_only:
            print(f"🗺️  Mode: Sitemap-only")
        elif xml_only:
            print(f"🗂️  Mode: XML-only")
        else:
            print(f"🔄 Mode: Combined (RSS + Sitemap)")
            print(f"📡 RSS feeds processed: {len(RSS_FEEDS)}")
            
        print(f"⚙️  Config file: {config_file}")
        
        # Print HTML parsing mode
        if args and args.legacy_html_parsing:
            print(f"🔧 HTML parsing: Legacy (BeautifulSoup selectors)")
        else:
            print(f"🚀 HTML parsing: Generalized (Jina AI links summary)")
            
        if vector_store_id:
            print(f"🔄 Vector Store uploads: {vector_store_id}")
        if enable_resume:
            print(f"🔄 Resume mode: ENABLED - Skipped {len(local_files_cache) if local_files_cache else 0} already processed URLs")

        # Print OpenRouter token usage summary to console
        if global_token_usage['api_calls_count'] > 0:
            print(f"\n🤖 OpenRouter API Usage:")
            print(f"   📞 API calls: {global_token_usage['api_calls_count']}")
            print(f"   📥 Input tokens: {global_token_usage['total_prompt_tokens']:,}")
            print(f"   📤 Output tokens: {global_token_usage['total_completion_tokens']:,}")
            print(f"   🔄 Total tokens: {global_token_usage['total_tokens']:,}")
            print(f"   📊 Avg per call: {global_token_usage['total_tokens'] / global_token_usage['api_calls_count']:.1f} tokens")
        
    except Exception as e:
        logger.error(f"Critical error in main function: {str(e)}", exc_info=True)
        print(f"❌ Critical error: {str(e)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Universal sitemap scraper with configurable settings")
    
    # Configuration file argument
    parser.add_argument("--config", type=str, default="config.json",
                        help="Path to configuration file (default: config.json)")
    
    # Override arguments for config values
    parser.add_argument("--base-url", type=str,
                        help="Override base URL from config")
    parser.add_argument("--sitemap-url", type=str,
                        help="Override sitemap URL from config")
    parser.add_argument("--xml-sitemap-url", type=str,
                        help="Override XML sitemap URL from config")
    
    # API keys (override config)
    parser.add_argument("--jina-api-key", type=str,
                        help="Override Jina AI API key from config")
    parser.add_argument("--firecrawl-api-key", type=str,
                        help="Override Firecrawl API key from config")
    parser.add_argument("--openai-api-key", type=str,
                        help="Override OpenAI API key from config")
    
    # Processing options
    parser.add_argument("--debug", action="store_true", 
                        help="Enable debug mode with verbose output")
    parser.add_argument("--no-check-modified", action="store_true", 
                        help="Disable last modified checking (process all URLs)")
    parser.add_argument("--rss-only", action="store_true",
                        help="Process only RSS feeds, skip sitemap processing")
    parser.add_argument("--sitemap-only", action="store_true",
                        help="Process only sitemap, skip RSS feeds processing")
    parser.add_argument("--xml-only", action="store_true",
                        help="Process only XML sitemap URLs, skip HTML sitemap and RSS feeds")
    parser.add_argument("--legacy-html-parsing", action="store_true",
                        help="Use legacy HTML parsing instead of generalized Jina AI links summary")
    parser.add_argument("--verbose-url-matching", action="store_true",
                        help="Show detailed URL matching information for all URLs")
    parser.add_argument("--resume", action="store_true",
                        help="Resume processing by skipping URLs that already have local files")
    parser.add_argument("--test-resume", action="store_true",
                        help="Test resume cache building and show statistics without processing")
    parser.add_argument("--test-events-xml", action="store_true",
                        help="Test Events XML parsing functionality with sample data")
    
    # Vector Store options
    parser.add_argument("--vector-store-id", type=str,
                        help="OpenAI Vector Store ID to upload processed files to")
    parser.add_argument("--disable-deduplication", action="store_true",
                        help="Disable deduplication (allow duplicate files for same URL)")
    parser.add_argument("--skip-vector-cache", action="store_true",
                        help="Skip Vector Store cache building (faster but no deduplication)")
    parser.add_argument("--chunking-strategy", type=str, choices=["auto", "static"],
                        help="Chunking strategy for Vector Store")
    parser.add_argument("--max-chunk-size", type=int,
                        help="Max tokens per chunk for static chunking")
    parser.add_argument("--chunk-overlap", type=int,
                        help="Token overlap between chunks for static chunking")
    
    # Content processing options
    parser.add_argument("--jina-remove-selectors", type=str,
                        help="CSS selektory pro odstranění částí stránky (oddělené čárkami)")
    parser.add_argument("--output-dir", type=str,
                        help="Override output directory from config")
    
    # Testing options
    parser.add_argument("--test-urls", nargs='+', type=str,
                        help="Array of URLs for testing purposes (overrides HTML sitemap processing)")
    
    args = parser.parse_args()
    
    # Run main function (config loading and logging setup happens inside main)
    main(args)

