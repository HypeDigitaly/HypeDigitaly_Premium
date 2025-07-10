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

# ============================================================================
# SCRIPT IDENTIFICATION
# ============================================================================
SCRIPT_NAME = "scrape_sitemap_GPT"
LOG_DIR = f"{SCRIPT_NAME}_logs"
LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")
OUTPUT_DIR = "files"

# USAGE EXAMPLES:
# Basic usage: python scrape_sitemap_GPT.py
# With debug mode: python scrape_sitemap_GPT.py --debug
# Skip last modified check: python scrape_sitemap_GPT.py --no-check-modified
# Upload to Vector Store: python scrape_sitemap_GPT.py --vector-store-id vs_abc123
# Upload with deduplication disabled: python scrape_sitemap_GPT.py --vector-store-id vs_abc123 --disable-deduplication
# Custom chunking strategy: python scrape_sitemap_GPT.py --vector-store-id vs_abc123 --chunking-strategy static --max-chunk-size 1200 --chunk-overlap 200
#
# OUTPUT: Each .txt file now includes metadata header with:
# - Source URL, title, navigation path, last modified date
# - Processing timestamp, content provider used (Jina/Firecrawl)
# - Script version for audit trail and troubleshooting

# ============================================================================
# API KEYS
# ============================================================================
JINA_AI_API_KEY = "REMOVED-JINA-KEY"
FIRECRAWL_API_KEY = "REMOVED-FIRECRAWL-KEY"
OPENAI_API_KEY = "REMOVED-OPENAI-KEY"

# ============================================================================
# JINA AI CONFIGURATION
# ============================================================================
# CSS selektory pro odstranění nežádoucích částí stránky
# Oddělené čárkou, např: "#header, .sidebar, .ads, footer"
JINA_REMOVE_SELECTORS = ""

# ============================================================================
# OPENAI VECTOR STORE CONFIGURATION
# ============================================================================
OPENAI_VECTOR_STORE_ID = None  # Will be set via command line parameter
OPENAI_API_BASE_URL = "https://api.openai.com/v1"
ENABLE_DEDUPLICATION = True  # Enable smart deduplication based on source_url

# Chunking strategy configuration
DEFAULT_CHUNKING_STRATEGY = "auto"  # Options: "auto", "static"
DEFAULT_MAX_CHUNK_SIZE = 800       # For static chunking
DEFAULT_CHUNK_OVERLAP = 400        # For static chunking

# ============================================================================
# URL CONFIGURATION
# ============================================================================
BASE_URL = "https://stredoceskykraj.cz/"

# Helper variables for domain parts
PARSED_BASE_URL = urlparse(BASE_URL)
BASE_NETLOC = PARSED_BASE_URL.netloc  # e.g., "stredoceskykraj.cz"
NON_WWW_BASE_NETLOC = BASE_NETLOC[4:] if BASE_NETLOC.startswith("www.") else BASE_NETLOC
BASE_SCHEME = PARSED_BASE_URL.scheme   # e.g., "https"

SITEMAP_URL = f"{BASE_SCHEME}://{NON_WWW_BASE_NETLOC}/web/urad/mapa-stranek"
XML_SITEMAP_URL = f"{BASE_SCHEME}://{BASE_NETLOC}/sitemap.xml"

# ============================================================================
# MARKDOWN CONTENT PROVIDER CONFIGURATION
# ============================================================================
# Define available Markdown content providers
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

# Provider sequence - comma-separated IDs from MARKDOWN_PROVIDERS
MARKDOWN_PROVIDER_SEQUENCE = "jina,firecrawl"  # Default: try Jina first, then Firecrawl

# ============================================================================
# BLACKLISTED URLS
# ============================================================================
BLACKLISTED_URLS = [
    
]

# ============================================================================
# HTTP REQUEST SETTINGS
# ============================================================================
REQUEST_TIMEOUT = 150  # SECONDS
REQUEST_RETRY_CODES = (500, 502, 503, 504, 524)
REQUEST_RETRY_COUNT = 3
REQUEST_BACKOFF_FACTOR = 0.3

# ============================================================================
# PROCESSING FLAGS
# ============================================================================
CHECK_LAST_MODIFIED = True  # Check last modified date from sitemap.xml before processing
MAX_FILENAME_LENGTH = 200  # Maximum length for generated filenames

# ============================================================================
# FILE PATHS
# ============================================================================
LAST_RUN_FILE = "scrape_sitemap_GPT_last_run_time.txt"  # File to store the last run timestamp

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

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def requests_retry_session(retries=REQUEST_RETRY_COUNT, backoff_factor=REQUEST_BACKOFF_FACTOR):
    """Create a requests session with retry configuration."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=REQUEST_RETRY_CODES
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
    return filename[:MAX_FILENAME_LENGTH]

# ============================================================================
# JINA AI API FUNCTIONS
# ============================================================================

def get_html_content_via_jina(url, remove_selectors=None):
    """Fetch HTML content using Jina AI API."""
    api_url = f"https://r.jina.ai/{url}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {JINA_AI_API_KEY}",
        "X-Return-Format": "html",
        "X-Engine": "browser"
    }
    
    # Přidání CSS selektorů pro odstranění nežádoucích částí stránky
    selectors = remove_selectors or JINA_REMOVE_SELECTORS
    if selectors and selectors.strip():
        headers["X-Remove-Selector"] = selectors
        logger.debug(f"Using remove selectors for HTML: {selectors}")
    
    logger.info(f"Fetching HTML content from: {url}")
    
    try:
        response = requests_retry_session().get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        
        data = response.json()
        if response.status_code == 200 and data.get("data"):
            content = data["data"].get("html", "")
            if content:
                logger.info(f"Successfully fetched HTML content from {url}")
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

def parse_menu(html_content):
    """Parse the HTML sitemap to find the main menu structure."""
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

def extract_links(menu_item, path=[], url_last_modified_map={}, last_run_timestamp=None):
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
                    if should_process_url(absolute_url, last_modified, last_run_timestamp):
                        extracted_urls.append({
                            'url': absolute_url,
                            'title': link_text,
                            'path': absolute_path
                        })
                    else:
                        logger.info(f"Skipping URL {absolute_url} as it hasn't changed since last run.")

            # Continue with submenu
            sub_menu = menu_item.find("ul", recursive=False)
            if sub_menu:
                extracted_urls.extend(extract_links(sub_menu, current_path, url_last_modified_map, last_run_timestamp))

    elif menu_item.name == "ul":
        for item in menu_item.find_all("li", recursive=False):
            extracted_urls.extend(extract_links(item, path, url_last_modified_map, last_run_timestamp))
    
    return extracted_urls

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
    """Find the last modified date for a given URL."""
    logger.debug(f"Finding last modified date for URL: {url}")
    last_modified = None
    matched_sitemap_url = None
    
    normalized_url = url.rstrip('/')
    
    # Try exact match
    if url in url_last_modified_map:
        last_modified = url_last_modified_map[url]
        matched_sitemap_url = url
    elif normalized_url in url_last_modified_map:
        last_modified = url_last_modified_map[normalized_url]
        matched_sitemap_url = normalized_url
    else:
        # Try substring matching
        matching_urls = [sitemap_url for sitemap_url in url_last_modified_map.keys() 
                         if normalized_url in sitemap_url or sitemap_url in normalized_url]
        
        if matching_urls:
            matched_sitemap_url = matching_urls[0]
            last_modified = url_last_modified_map.get(matched_sitemap_url)

    print(f"\n=== URL MATCHING ===")
    print(f"SITEMAP_URL: {url}")
    if matched_sitemap_url:
        print(f"XML_SITEMAP_URL: {matched_sitemap_url}")
        print(f"Last modified date: {last_modified}")
    else:
        print(f"XML_SITEMAP_URL: No matching URL found")
    print("===================\n")
        
    return last_modified

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

# ============================================================================
# TIMESTAMP FUNCTIONS
# ============================================================================

def get_last_run_timestamp():
    """Get the timestamp of the last script run."""
    try:
        if os.path.exists(LAST_RUN_FILE):
            with open(LAST_RUN_FILE, 'r') as f:
                timestamp_str = f.read().strip()
                return datetime.fromisoformat(timestamp_str)
        return None
    except Exception as e:
        logger.error(f"Error reading last run timestamp: {str(e)}")
        return None

def save_last_run_timestamp():
    """Save the current timestamp as the last run timestamp."""
    try:
        with open(LAST_RUN_FILE, 'w') as f:
            f.write(datetime.now().astimezone(timezone(timedelta(hours=2))).isoformat())
        logger.info(f"Saved current UTC+02:00 timestamp to {LAST_RUN_FILE}")
    except Exception as e:
        logger.error(f"Error saving last run timestamp: {str(e)}")

# ============================================================================
# FILE SAVING FUNCTIONS
# ============================================================================

def create_metadata_header(url, title, last_modified=None, path=None, provider_used=None):
    """
    Create a Markdown-formatted metadata header for the file.
    
    Args:
        url (str): Source URL of the content
        title (str): Page title
        last_modified (datetime, optional): Last modification date from sitemap
        path (str, optional): Navigation path from sitemap
        provider_used (str, optional): API provider used (jina/firecrawl)
    
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
        "",
        "---",
        "",
        ""   # Empty line before content
    ]
    
    return "\n".join(metadata_lines)


def save_markdown_to_file(content, title, url, upload_to_vector_store=False, vector_store_id=None, enable_deduplication=True, chunking_strategy=None, vector_store_cache=None, last_modified=None, path=None, provider_used=None):
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
        metadata_header = create_metadata_header(url, title, last_modified, path, provider_used)
        
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
    start_time = datetime.now()
    logger.info(f"=== Starting scrape_sitemap_GPT at {start_time} ===")
    
    # Set remove selectors if provided
    remove_selectors = JINA_REMOVE_SELECTORS
    if args and args.jina_remove_selectors:
        remove_selectors = args.jina_remove_selectors
        logger.info(f"Using custom remove selectors: {remove_selectors}")
        print(f"🎯 Custom remove selectors: {remove_selectors}")
    
    # Set configuration based on arguments
    vector_store_id = args.vector_store_id if args else None
    deduplication_enabled = not args.disable_deduplication if args else True
    
    # Create chunking strategy
    chunking_strategy = None
    if vector_store_id and args:
        chunking_strategy = create_chunking_strategy(
            strategy_type=args.chunking_strategy,
            max_chunk_size=args.max_chunk_size,
            chunk_overlap=args.chunk_overlap
        )
    
    # Build Vector Store cache for fast lookups (only if using vector store)
    vector_store_cache = None
    if vector_store_id and deduplication_enabled:
        logger.info("Building Vector Store cache for optimized deduplication...")
        vector_store_cache = build_vector_store_cache(vector_store_id)
        print(f"🚀 Vector Store cache built with {len(vector_store_cache)} files")
    
    # Get last run timestamp
    last_run_timestamp = get_last_run_timestamp()
    if last_run_timestamp:
        logger.info(f"Last run timestamp: {last_run_timestamp.isoformat()}")
    else:
        logger.info("No last run timestamp found, will process all URLs")
    
    try:
        # Step 1: Get HTML sitemap content
        logger.info(f"Step 1: Fetching HTML sitemap from {SITEMAP_URL}")
        html_content, _ = get_html_content_via_jina(SITEMAP_URL, remove_selectors)
        if not html_content:
            logger.error(f"Failed to get HTML sitemap content from {SITEMAP_URL}")
            return
        
        # Step 2: Parse the sitemap menu
        logger.info("Step 2: Parsing sitemap menu structure")
        main_menu = parse_menu(html_content)
        if not main_menu:
            logger.error("Failed to find main menu in sitemap")
            return
        
        # Step 3: Fetch XML sitemap data
        logger.info("Step 3: Fetching XML sitemap for last modified dates")
        url_last_modified_map = fetch_xml_sitemap()
        logger.info(f"Fetched last modified dates for {len(url_last_modified_map)} URLs")
        
        # Step 4: Extract URLs from HTML sitemap
        logger.info("Step 4: Extracting URLs from HTML sitemap")
        extracted_urls = extract_links(main_menu, url_last_modified_map=url_last_modified_map, 
                                     last_run_timestamp=last_run_timestamp)
        
        logger.info(f"Found {len(extracted_urls)} URLs to process")
        
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
                                                 provider_used=provider_used)
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
        save_last_run_timestamp()
        
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
        if vector_store_id:
            logger.info(f"Vector Store uploads enabled: {vector_store_id}")
        
        print(f"\n🎉 Processing complete!")
        print(f"📁 Files saved to: {os.path.abspath(OUTPUT_DIR)}")
        print(f"📊 Success: {success_count}/{processed_count} URLs processed")
        if vector_store_id:
            print(f"🔄 Vector Store uploads: {vector_store_id}")
        
    except Exception as e:
        logger.error(f"Critical error in main function: {str(e)}", exc_info=True)
        print(f"❌ Critical error: {str(e)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape sitemap and save markdown content to files")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode with verbose output")
    parser.add_argument("--no-check-modified", action="store_true", 
                        help="Disable last modified checking (process all URLs)")
    parser.add_argument("--vector-store-id", type=str,
                        help="OpenAI Vector Store ID to upload processed files to")
    parser.add_argument("--disable-deduplication", action="store_true",
                        help="Disable deduplication (allow duplicate files for same URL)")
    parser.add_argument("--chunking-strategy", type=str, choices=["auto", "static"], default="auto",
                        help="Chunking strategy for Vector Store (default: auto)")
    parser.add_argument("--max-chunk-size", type=int, default=800,
                        help="Max tokens per chunk for static chunking (default: 800)")
    parser.add_argument("--chunk-overlap", type=int, default=400,
                        help="Token overlap between chunks for static chunking (default: 400)")
    parser.add_argument("--jina-remove-selectors", type=str,
                        help="CSS selektory pro odstranění částí stránky (oddělené čárkami)")
    
    args = parser.parse_args()
    
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
    
    # Set vector store ID if provided
    if args.vector_store_id:
        logger.info(f"Vector Store upload enabled. Vector Store ID: {args.vector_store_id}")
        print(f"🔄 Vector Store upload enabled. ID: {args.vector_store_id}")
    
    # Disable deduplication if requested
    if args.disable_deduplication:
        logger.info("Deduplication disabled - duplicate files will be allowed")
        print("🔄 Deduplication disabled - duplicate files will be allowed")
    
    # Log chunking strategy if Vector Store is enabled
    if args.vector_store_id:
        logger.info(f"Chunking strategy: {args.chunking_strategy}")
        if args.chunking_strategy == "static":
            logger.info(f"Max chunk size: {args.max_chunk_size} tokens")
            logger.info(f"Chunk overlap: {args.chunk_overlap} tokens")
            print(f"🧩 Custom chunking: {args.max_chunk_size} tokens/chunk, {args.chunk_overlap} overlap")
        else:
            print(f"🧩 Auto chunking: 800 tokens/chunk, 400 overlap")
    
    main(args)
