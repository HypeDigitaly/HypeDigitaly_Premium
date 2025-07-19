import requests
from bs4 import BeautifulSoup
import logging
import json
import time
import os
from urllib.parse import urljoin, urlparse
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
    """Validate that all required configuration keys are present."""
    required_keys = [
        'website.base_url',
        'api_keys.jina_ai', 
        'api_keys.firecrawl',
        'api_keys.openai'
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
    
    missing_keys = []
    for key in required_keys:
        if get_nested_value(config, key) is None:
            missing_keys.append(key)
    
    if missing_keys:
        raise ValueError(f"Missing required configuration keys: {missing_keys}")
    
    return True

# ============================================================================
# GLOBAL CONFIGURATION VARIABLES
# ============================================================================

# This will be populated by load_configuration() function
CONFIG = None

# ============================================================================
# SCRIPT IDENTIFICATION
# ============================================================================

# These will be set dynamically from config
SCRIPT_NAME = "scrape_sitemap_universal"
LOG_DIR = None
LOG_FILE = None
OUTPUT_DIR = None

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
#
# OUTPUT: Each .txt file now includes metadata header with:
# - Source URL, title, navigation path, last modified date
# - Processing timestamp, content provider used (Jina/Firecrawl)
# - Script version for audit trail and troubleshooting

# ============================================================================
# CONFIGURATION INITIALIZATION
# ============================================================================

def load_configuration(config_file="config.json"):
    """Load and initialize global configuration."""
    global CONFIG, SCRIPT_NAME, LOG_DIR, LOG_FILE, OUTPUT_DIR
    global JINA_AI_API_KEY, FIRECRAWL_API_KEY, OPENAI_API_KEY
    global JINA_REMOVE_SELECTORS, OPENAI_VECTOR_STORE_ID, ENABLE_DEDUPLICATION
    global DEFAULT_CHUNKING_STRATEGY, DEFAULT_MAX_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP
    global BASE_URL, PARSED_BASE_URL, BASE_NETLOC, NON_WWW_BASE_NETLOC, BASE_SCHEME
    global SITEMAP_URL, XML_SITEMAP_URL, MARKDOWN_PROVIDERS, MARKDOWN_PROVIDER_SEQUENCE
    global BLACKLISTED_URLS, RSS_FEEDS, REQUEST_TIMEOUT, REQUEST_RETRY_CODES, REQUEST_RETRY_COUNT
    global REQUEST_BACKOFF_FACTOR, CHECK_LAST_MODIFIED, MAX_FILENAME_LENGTH, LAST_RUN_FILE
    global VERBOSE_URL_MATCHING
    
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
    
    # Set content provider configuration
    JINA_REMOVE_SELECTORS = CONFIG["content_providers"]["jina"]["remove_selectors"]
    MARKDOWN_PROVIDER_SEQUENCE = CONFIG["content_providers"]["provider_sequence"]
    
    # Set vector store configuration
    OPENAI_VECTOR_STORE_ID = CONFIG["vector_store"]["id"]
    ENABLE_DEDUPLICATION = CONFIG["vector_store"]["enable_deduplication"]
    DEFAULT_CHUNKING_STRATEGY = CONFIG["vector_store"]["chunking_strategy"]
    DEFAULT_MAX_CHUNK_SIZE = CONFIG["vector_store"]["max_chunk_size"]
    DEFAULT_CHUNK_OVERLAP = CONFIG["vector_store"]["chunk_overlap"]
    
    # Set URL configuration
    BASE_URL = CONFIG["website"]["base_url"]
    PARSED_BASE_URL = urlparse(BASE_URL)
    BASE_NETLOC = PARSED_BASE_URL.netloc
    NON_WWW_BASE_NETLOC = BASE_NETLOC[4:] if BASE_NETLOC.startswith("www.") else BASE_NETLOC
    BASE_SCHEME = PARSED_BASE_URL.scheme
    
    SITEMAP_URL = CONFIG["website"]["sitemap_url"]
    XML_SITEMAP_URL = CONFIG["website"]["xml_sitemap_url"]
    
    # Set blacklisted URLs
    BLACKLISTED_URLS = CONFIG["website"]["blacklisted_urls"]
    
    # Set RSS feeds
    RSS_FEEDS = CONFIG["website"].get("rss_feeds", [])
    
    # Set HTTP settings
    REQUEST_TIMEOUT = CONFIG["http_settings"]["request_timeout"]
    REQUEST_RETRY_CODES = tuple(CONFIG["http_settings"]["retry_codes"])
    REQUEST_RETRY_COUNT = CONFIG["http_settings"]["retry_count"]
    REQUEST_BACKOFF_FACTOR = CONFIG["http_settings"]["backoff_factor"]
    
    # Set processing flags
    CHECK_LAST_MODIFIED = CONFIG["processing"]["check_last_modified"]
    MAX_FILENAME_LENGTH = CONFIG["processing"]["max_filename_length"]
    
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

# ============================================================================
# CONSTANTS (will be set by load_configuration)
# ============================================================================
OPENAI_API_BASE_URL = "https://api.openai.com/v1"

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

# ============================================================================
# JINA AI API FUNCTIONS
# ============================================================================

def get_html_content_via_jina(url, remove_selectors=None):
    """Fetch HTML content using Jina AI API with links summary for sitemap processing."""
    api_url = f"https://eu-r-beta.jina.ai/{url}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {JINA_AI_API_KEY}",
        "X-Return-Format": "html",
        "X-Engine": "browser",
        "X-With-Links-Summary": "all"
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
        
        data = response.json()
        if response.status_code == 200 and data.get("data"):
            content = data["data"].get("html", "")
            links_data = data["data"].get("links", [])
            
            logger.info(f"Successfully fetched HTML content from {url}")
            logger.info(f"Found {len(links_data)} links in links summary")
            
            if content:
                return content, data["data"]
            else:
                logger.error(f"Jina API returned successful status but HTML content is missing for {url}")
                return None, None
        else:
            error_message = data.get("error", {}).get("message", f"HTTP Status {response.status_code}")
            logger.error(f"Jina API error for {url}: {error_message}")
            return None, None
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error when fetching HTML from {url}: {str(e)}")
        return None, None
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error for {url}: {str(e)}")
        return None, None
    except Exception as e:
        logger.error(f"Unexpected error fetching HTML from {url}: {str(e)}")
        return None, None

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
    """Fetch markdown content using Jina AI API."""
    api_url = f"https://r.jina.ai/{url}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Return-Format": "markdown",
        "X-Engine": "browser"
    }
    
    # Přidání CSS selektorů pro odstranění nežádoucích částí stránky
    selectors = remove_selectors or JINA_REMOVE_SELECTORS
    if selectors and selectors.strip():
        headers["X-Remove-Selector"] = selectors
        logger.debug(f"Using remove selectors: {selectors}")
    
    response = requests_retry_session().get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    
    data = response.json()
    if response.status_code == 200 and data.get("data"):
        content = data["data"].get("content", "")
        title = data["data"].get("title", "")
        if content:
            return content, title, data["data"]
        else:
            logger.error("Jina API returned successful status but content is missing")
            return None, None, None
    else:
        error_message = data.get("error", {}).get("message", f"HTTP Status {response.status_code}")
        logger.error(f"Jina API error: {error_message}")
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


def find_existing_file_by_url(vector_store_id, source_url):
    """
    Find an existing file in vector store by source URL.
    
    ⚠️ WARNING: This is the SLOW legacy method!
    Use find_existing_file_by_url_cached() with pre-built cache instead.
    This method is O(n*m) complexity and makes many API calls.
    """
    logger.warning(f"Using SLOW legacy lookup for URL: {source_url} (consider using cache)")
    
    # Get all files in the vector store
    files = list_vector_store_files(vector_store_id)
    
    for file_info in files:
        file_id = file_info.get('id')
        if not file_id:
            continue
            
        # Get file attributes
        attributes = get_vector_store_file_attributes(vector_store_id, file_id)
        file_source_url = attributes.get('source_url')
        
        if file_source_url == source_url:
            logger.info(f"Found existing file {file_id} with same source URL")
            return file_info
    
    logger.info(f"No existing file found with source URL: {source_url}")
    return None


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
            print(f"📊 Cache progress: {progress:.0f}% ({i}/{len(files)})")
            
        # Get file attributes (this is the expensive operation)
        attributes = get_vector_store_file_attributes(vector_store_id, file_id)
        metadata_fetch_count += 1
        
        source_url = attributes.get('source_url')
        if source_url:
            url_to_file_cache[source_url] = {
                'file_id': file_id,
                'file_info': file_info,
                'attributes': attributes
            }
    
    elapsed_time = time.time() - start_time
    logger.info(f"Built Vector Store cache in {elapsed_time:.2f}s with {metadata_fetch_count} metadata fetches")
    logger.info(f"Cache contains {len(url_to_file_cache)} files with source URLs")
    print(f"✅ Cache built: {len(url_to_file_cache)} files indexed in {elapsed_time:.1f}s")
    
    return url_to_file_cache


def find_existing_file_by_url_cached(source_url, cache):
    """Find an existing file in vector store by source URL using pre-built cache."""
    logger.debug(f"Searching cache for existing file with URL: {source_url}")
    
    cached_file = cache.get(source_url)
    if cached_file:
        file_id = cached_file['file_id']
        logger.info(f"Found existing file {file_id} in cache for URL: {source_url}")
        return cached_file['file_info']
    
    logger.debug(f"No existing file found in cache for URL: {source_url}")
    return None


def create_chunking_strategy(strategy_type="auto", max_chunk_size=800, chunk_overlap=400):
    """Create a chunking strategy object for OpenAI Vector Store."""
    if strategy_type.lower() == "auto":
        logger.info("Using AUTO chunking strategy (800 tokens per chunk, 400 overlap)")
        return {
            "type": "auto"
        }
    elif strategy_type.lower() == "static":
        logger.info(f"Using STATIC chunking strategy ({max_chunk_size} tokens per chunk, {chunk_overlap} overlap)")
        return {
            "type": "static",
            "static": {
                "max_chunk_size_tokens": max_chunk_size,
                "chunk_overlap_tokens": chunk_overlap
            }
        }
    else:
        logger.warning(f"Unknown chunking strategy '{strategy_type}', falling back to AUTO")
        return {
            "type": "auto"
        }


def upload_and_add_to_vector_store(filepath, vector_store_id, url=None, title=None, enable_deduplication=True, chunking_strategy=None, vector_store_cache=None):
    """Complete process to upload file to OpenAI and add to vector store with deduplication."""
    logger.info(f"Starting upload and vector store process for: {filepath}")
    
    # Step 1: Check for existing file if deduplication is enabled
    existing_file = None
    if enable_deduplication and url:
        if vector_store_cache is not None:
            # Use fast cache lookup
            existing_file = find_existing_file_by_url_cached(url, vector_store_cache)
        else:
            # Fallback to slow API lookup
            existing_file = find_existing_file_by_url(vector_store_id, url)
            
        if existing_file:
            existing_file_id = existing_file.get('id')
            logger.info(f"Found existing file {existing_file_id} for URL: {url}")
            print(f"🔄 Found existing file for URL: {url}")
            
            # Delete the old file from vector store
            if delete_vector_store_file(vector_store_id, existing_file_id):
                logger.info(f"Deleted old file {existing_file_id} from vector store")
                print(f"🗑️  Deleted old file: {existing_file_id}")
                
                # Remove from cache to keep it accurate
                if vector_store_cache is not None and url in vector_store_cache:
                    del vector_store_cache[url]
                    logger.debug(f"Removed {url} from Vector Store cache")
            else:
                logger.warning(f"Failed to delete old file {existing_file_id}, continuing with upload")
    
    # Step 2: Upload file to OpenAI
    file_id = upload_file_to_openai(filepath)
    if not file_id:
        logger.error(f"Failed to upload file to OpenAI: {filepath}")
        return False
    
    # Step 3: Prepare attributes for the vector store file
    attributes = {}
    if url:
        attributes['source_url'] = url[:512]  # Max 512 characters
    if title:
        attributes['title'] = title[:512]  # Max 512 characters
    
    # Add some metadata
    attributes['upload_timestamp'] = datetime.now().isoformat()[:512]
    attributes['script_name'] = SCRIPT_NAME[:512]
    
    # Step 4: Add file to vector store
    result = add_file_to_vector_store(file_id, vector_store_id, attributes, chunking_strategy)
    if result:
        action = "Replaced" if existing_file else "Uploaded"
        logger.info(f"Successfully processed file {filepath} to vector store")
        print(f"✅ {action} in Vector Store: {filepath} (File ID: {file_id})")
        return True
    else:
        logger.error(f"Failed to add file to vector store: {filepath}")
        return False

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
        return extracted_urls
    
    logger.info(f"Processing {len(links_data)} links from Jina AI summary")
    
    for i, link_info in enumerate(links_data, 1):
        try:
            # Extract URL and text from link info
            url = link_info.get("url", "").strip()
            text = link_info.get("text", "").strip()
            
            # Skip if no URL
            if not url:
                logger.debug(f"Skipping link {i}: No URL found")
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
            if should_process_url_with_resume(absolute_url, last_modified, last_run_timestamp, local_files_cache, enable_resume):
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

def extract_links(menu_item, path=[], url_last_modified_map={}, last_run_timestamp=None, local_files_cache=None, enable_resume=False):
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
                    if should_process_url_with_resume(absolute_url, last_modified, last_run_timestamp, local_files_cache, enable_resume):
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
                extracted_urls.extend(extract_links(sub_menu, current_path, url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume))

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
        
        # Try to parse as XML
        soup = BeautifulSoup(response.content, 'xml')
        
        # Check if it's Atom feed
        if soup.find('feed') and soup.find('feed').get('xmlns') == 'http://www.w3.org/2005/Atom':
            logger.info(f"Detected Atom feed format for: {rss_url}")
            return parse_atom_feed(soup, rss_url)
        
        # Check if it's RSS 2.0 feed
        elif soup.find('rss') or soup.find('channel'):
            logger.info(f"Detected RSS 2.0 feed format for: {rss_url}")
            return parse_rss_2_0_feed(soup, rss_url)
        
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
                    
                    # Extract publication date
                    published_elem = entry.find('published') or entry.find('updated')
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
            # Extract URL from link element
            link_elem = item.find('link')
            if link_elem:
                url = link_elem.text.strip() if link_elem.text else link_elem.get('href')
                if url:
                    # Make URL absolute
                    absolute_url = urljoin(BASE_URL, url)
                    
                    # Extract title
                    title_elem = item.find('title')
                    title = title_elem.text.strip() if title_elem else 'No title'
                    
                    # Extract publication date
                    published_elem = item.find('pubDate')
                    published = published_elem.text.strip() if published_elem else None
                    
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

def process_rss_feeds(url_last_modified_map, last_run_timestamp, local_files_cache=None, enable_resume=False):
    """Process all configured RSS feeds and extract URLs."""
    logger.info(f"Processing {len(RSS_FEEDS)} RSS feeds")
    
    all_rss_urls = []
    
    for rss_url in RSS_FEEDS:
        logger.info(f"Processing RSS feed: {rss_url}")
        
        # Parse the RSS feed
        feed_urls = parse_rss_feed(rss_url)
        
        # Filter URLs based on last modified check if available
        filtered_urls = []
        for url_info in feed_urls:
            url = url_info['url']
            
            # Check if URL is blacklisted
            if url in BLACKLISTED_URLS:
                logger.info(f"RSS URL {url} is blacklisted. Skipping.")
                continue
            
            # Find last modified date from sitemap.xml (if available)
            last_modified = find_url_last_modified(url, url_last_modified_map)
            
            # For RSS feeds, we'll be more lenient with last modified check
            # since RSS items are typically recent
            if should_process_url_with_resume(url, last_modified, last_run_timestamp, local_files_cache, enable_resume):
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

def should_process_url(url, last_modified, last_run_timestamp):
    """Decide whether a URL should be processed based on modification time."""
    logger.debug(f"Checking if URL should be processed: {url}")
    
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
    
    # If no last modified date found, skip
    if last_modified is None:
        logger.info(f"No last modified date found for {url}. Skipping.")
        print(f"\n=== URL PROCESSING STATUS ===")
        print(f"URL: {url}")
        print(f"Last Modified: Unknown")
        print(f"Last Run: {last_run_timestamp}")
        print(f"Processing: SKIPPED")
        print("=============================\n")
        return False

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

def extract_urls_from_xml_sitemap(url_last_modified_map, last_run_timestamp=None, local_files_cache=None, enable_resume=False):
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
        if should_process_url_with_resume(url, last_modified, last_run_timestamp, local_files_cache, enable_resume):
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
                    url_to_file_cache[source_url] = {
                        'filepath': filepath,
                        'filename': filename
                    }
                    processed_files += 1
                    logger.debug(f"Found URL in {filename}: {source_url}")
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
    return url in local_cache


def should_process_url_with_resume(url, last_modified, last_run_timestamp, local_cache=None, enable_resume=False):
    """Enhanced version of should_process_url that supports resume functionality."""
    logger.debug(f"Checking if URL should be processed (with resume): {url}")
    
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
    
    # Step 2: Fall back to original timestamp-based logic
    return should_process_url(url, last_modified, last_run_timestamp)

# ============================================================================
# FILE SAVING FUNCTIONS
# ============================================================================

def create_metadata_header(url, title, last_modified=None, path=None, provider_used=None, rss_metadata=None):
    """
    Create a Markdown-formatted metadata header for the file.
    
    Args:
        url (str): Source URL of the content
        title (str): Page title
        last_modified (datetime, optional): Last modification date from sitemap
        path (str, optional): Navigation path from sitemap
        provider_used (str, optional): API provider used (jina/firecrawl)
        rss_metadata (dict, optional): RSS-specific metadata (published, summary, source_feed)
    
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


def save_markdown_to_file(content, title, url, upload_to_vector_store=False, vector_store_id=None, enable_deduplication=True, chunking_strategy=None, vector_store_cache=None, last_modified=None, path=None, provider_used=None, rss_metadata=None):
    """Save markdown content to a txt file with metadata header and optionally upload to OpenAI Vector Store."""
    try:
        # Create filename from title
        if not title or not title.strip():
            # Fallback to URL-based filename
            parsed_url = urlparse(url)
            title = parsed_url.path.split('/')[-1] or 'homepage'
        
        filename = sanitize_filename(title)
        if not filename:
            filename = "untitled"
        
        # Ensure .txt extension
        if not filename.endswith('.txt'):
            filename += '.txt'
        
        filepath = os.path.join(OUTPUT_DIR, filename)
        
        # Handle duplicate filenames
        counter = 1
        original_filepath = filepath
        while os.path.exists(filepath):
            name, ext = os.path.splitext(original_filepath)
            filepath = f"{name}_{counter}{ext}"
            counter += 1
        
        # Create metadata header
        metadata_header = create_metadata_header(url, title, last_modified, path, provider_used, rss_metadata)
        
        # Combine metadata header with content
        full_content = metadata_header + content
        
        # Save content to file
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(full_content)
        
        logger.info(f"Saved markdown content to: {filepath}")
        print(f"✅ Saved: {filepath}")
        
        # Upload to OpenAI Vector Store if requested
        if upload_to_vector_store and vector_store_id:
            upload_success = upload_and_add_to_vector_store(filepath, vector_store_id, url, title, enable_deduplication, chunking_strategy, vector_store_cache)
            if not upload_success:
                logger.warning(f"Failed to upload {filepath} to vector store, but file was saved locally")
        
        return filepath
        
    except Exception as e:
        logger.error(f"Error saving markdown to file for URL {url}: {str(e)}")
        return None

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
    if vector_store_id and deduplication_enabled:
        logger.info("Building Vector Store cache for optimized deduplication...")
        vector_store_cache = build_vector_store_cache(vector_store_id)
        print(f"🚀 Vector Store cache built with {len(vector_store_cache)} files")
    
    # Build local files cache for resume functionality (if enabled)
    local_files_cache = None
    enable_resume = args.resume if args else False
    if enable_resume:
        logger.info("Resume mode enabled - building local files cache...")
        local_files_cache = build_local_files_cache(OUTPUT_DIR)
        print(f"🔄 Resume cache built with {len(local_files_cache)} processed URLs")
    
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
        
        # Step 1-4: Process sitemap (unless RSS-only mode)
        if not rss_only:
            if xml_only:
                # XML-only mode: Skip HTML sitemap, only use XML sitemap
                logger.info("XML-only mode: Skipping HTML sitemap processing")
                
                # Step 3: Fetch XML sitemap data
                logger.info("Step 3: Fetching XML sitemap for URLs and last modified dates")
                url_last_modified_map = fetch_xml_sitemap()
                logger.info(f"Fetched last modified dates for {len(url_last_modified_map)} URLs")
                
                # Step 4: Extract URLs from XML sitemap
                logger.info("Step 4: Extracting URLs from XML sitemap")
                extracted_urls = extract_urls_from_xml_sitemap(url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume)
                
                logger.info(f"Found {len(extracted_urls)} URLs from XML sitemap")
            elif SITEMAP_URL and SITEMAP_URL.strip():
                # Normal sitemap processing with HTML sitemap (only if URL is provided)
                # Step 1: Get HTML sitemap content with Jina AI links summary
                logger.info(f"Step 1: Fetching HTML sitemap with links summary from {SITEMAP_URL}")
                html_content, jina_data = get_html_content_via_jina(SITEMAP_URL, remove_selectors)
                if not html_content or not jina_data:
                    logger.error(f"Failed to get HTML sitemap content from {SITEMAP_URL}")
                    logger.info("Falling back to XML-only processing...")
                    
                    # Fallback to XML-only processing
                    url_last_modified_map = fetch_xml_sitemap()
                    logger.info(f"Fetched last modified dates for {len(url_last_modified_map)} URLs")
                    extracted_urls = extract_urls_from_xml_sitemap(url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume)
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
                                                         last_run_timestamp=last_run_timestamp, local_files_cache=local_files_cache, enable_resume=enable_resume)
                            logger.info(f"Found {len(extracted_urls)} URLs from legacy parsing")
                        else:
                            logger.error("Legacy parsing failed, falling back to XML-only")
                            extracted_urls = extract_urls_from_xml_sitemap(url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume)
                    else:
                        # Use new generalized Jina AI links summary approach (DEFAULT)
                        logger.info("Step 3: Extracting URLs from Jina AI links summary (generalized approach)")
                        extracted_urls = extract_links_from_jina_summary(jina_data, 
                                                                       url_last_modified_map=url_last_modified_map, 
                                                                       last_run_timestamp=last_run_timestamp, 
                                                                       local_files_cache=local_files_cache, 
                                                                       enable_resume=enable_resume)
                        
                        logger.info(f"Found {len(extracted_urls)} URLs from HTML sitemap (generalized approach)")
                        
                        # Optional: Fallback to legacy parsing if no links found
                        if len(extracted_urls) == 0:
                            logger.warning("No links found with generalized approach, trying legacy parsing...")
                            main_menu = parse_menu(html_content)
                            if main_menu:
                                extracted_urls = extract_links(main_menu, url_last_modified_map=url_last_modified_map, 
                                                             last_run_timestamp=last_run_timestamp, local_files_cache=local_files_cache, enable_resume=enable_resume)
                                logger.info(f"Found {len(extracted_urls)} URLs from legacy parsing")
                            else:
                                logger.warning("Legacy parsing also failed, falling back to XML-only")
                                extracted_urls = extract_urls_from_xml_sitemap(url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume)
            else:
                # No HTML sitemap URL provided, use XML-only processing
                logger.info("No HTML sitemap URL provided, using XML-only processing")
                
                # Step 3: Fetch XML sitemap data
                logger.info("Step 3: Fetching XML sitemap for URLs and last modified dates")
                url_last_modified_map = fetch_xml_sitemap()
                logger.info(f"Fetched last modified dates for {len(url_last_modified_map)} URLs")
                
                # Step 4: Extract URLs from XML sitemap
                logger.info("Step 4: Extracting URLs from XML sitemap")
                extracted_urls = extract_urls_from_xml_sitemap(url_last_modified_map, last_run_timestamp, local_files_cache, enable_resume)
                
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
        
        # Step 5: Process each URL
        logger.info("Step 5: Processing individual URLs")
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
            if any(key in url_info for key in ['published', 'summary', 'description', 'source_feed']):
                rss_metadata = {
                    'published': url_info.get('published'),
                    'summary': url_info.get('summary'),
                    'description': url_info.get('description'),
                    'source_feed': url_info.get('source_feed')
                }
                logger.info(f"RSS metadata available - Published: {rss_metadata['published']}, Source: {rss_metadata['source_feed']}")
            
            # Get markdown content
            content, api_title, metadata, provider_used = get_markdown_content(url, remove_selectors)
            
            if content:
                # Use title from API response if available, otherwise use sitemap title
                final_title = api_title if api_title and api_title.strip() else title
                
                # Find last modified date for this URL
                last_modified = find_url_last_modified(url, url_last_modified_map)
                
                # Save to file with metadata
                saved_file = save_markdown_to_file(content, final_title, url, 
                                                 upload_to_vector_store=bool(vector_store_id), 
                                                 vector_store_id=vector_store_id,
                                                 enable_deduplication=deduplication_enabled,
                                                 chunking_strategy=chunking_strategy,
                                                 vector_store_cache=vector_store_cache,
                                                 last_modified=last_modified,
                                                 path=path,
                                                 provider_used=provider_used,
                                                 rss_metadata=rss_metadata)
                if saved_file:
                    success_count += 1
                    logger.info(f"✅ Successfully processed: {url}")
                else:
                    logger.error(f"❌ Failed to save file for: {url}")
            else:
                logger.error(f"❌ Failed to fetch content for: {url}")
            
            processed_count += 1
            
            # Small delay between requests
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
        if rss_only:
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
        
        print(f"\n🎉 Processing complete!")
        print(f"📁 Files saved to: {os.path.abspath(OUTPUT_DIR)}")
        print(f"📊 Success: {success_count}/{processed_count} URLs processed")
        print(f"🌐 Target website: {BASE_URL}")
        
        # Print processing mode
        if rss_only:
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
    
    # Vector Store options
    parser.add_argument("--vector-store-id", type=str,
                        help="OpenAI Vector Store ID to upload processed files to")
    parser.add_argument("--disable-deduplication", action="store_true",
                        help="Disable deduplication (allow duplicate files for same URL)")
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
    
    args = parser.parse_args()
    
    # Run main function (config loading and logging setup happens inside main)
    main(args)
