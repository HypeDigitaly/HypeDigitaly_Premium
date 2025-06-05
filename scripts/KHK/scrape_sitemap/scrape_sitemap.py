import requests
from bs4 import BeautifulSoup
import logging
import json
import anthropic
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
import random
from anthropic import InternalServerError, RateLimitError

# Import utility modules
from llmUtils.llm_utils import call_llm_api, requests_retry_session, assess_content_type
from llmUtils.categorization_utils import categorize_link, generate_rag_question
from llmUtils.qa_extraction_utils import convert_to_qa, convert_to_qa_with_retry, extract_json_from_text
from llmUtils.contact_extraction_utils import extract_contact_details_llm
from llmUtils.resolutions_extraction_utils import extract_resolution_details_llm, save_resolutions_payload, extract_default_date_year_from_text

# ============================================================================
# SCRIPT IDENTIFICATION
# ============================================================================
SCRIPT_NAME = "scrape_sitemap"
LOG_DIR = f"{SCRIPT_NAME}_logs"
LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")

# ============================================================================
# API KEYS
# ============================================================================
CLAUDE_API_KEY = "REMOVED-ANTHROPIC-KEY"
GROQ_API_KEY = "REMOVED-GROQ-KEY" # Added Groq API Key
JINA_AI_API_KEY = "REMOVED-JINA-KEY"
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"
FIRECRAWL_API_KEY = "REMOVED-FIRECRAWL-KEY" 

# ============================================================================
# LLM PROVIDER CONFIGURATION
# ============================================================================
# Define available LLM providers with their configurations
LLM_PROVIDERS = {
    "1": {
        "name": "anthropic",
        "api_key": CLAUDE_API_KEY,
        "model": "claude-sonnet-4-20250514", # Default Anthropic model (Haiku)
        "api_url": "https://api.anthropic.com/v1/messages", # Added API URL
        # "model": "claude-3-sonnet-20240229", # Alternative
        # "model": "claude-3-opus-20240229",  # Alternative
    },
    "2": {
        "name": "groq",
        "api_key": GROQ_API_KEY,
        "model": "meta-llama/llama-4-maverick-17b-128e-instruct", # Default Groq model
        "api_url": "https://api.groq.com/openai/v1/chat/completions", # Added API URL
        # "model": "mixtral-8x7b-32768", # Alternative
        # "model": "gemma-7b-it",     # Alternative
    }
    # Add more providers here if needed, assigning unique IDs (e.g., "3")
}

#!====================!
LLM_SEQUENCE = "1,2"
#!====================!

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
    # Add more providers here if needed
}

#!=================================!
MARKDOWN_PROVIDER_SEQUENCE = "firecrawl,jina" # Comma-separated IDs from MARKDOWN_PROVIDERS
#MARKDOWN_PROVIDER_SEQUENCE = "jina, firecrawl" # Comma-separated IDs from MARKDOWN_PROVIDERS
#!=================================!

# ============================================================================
# URL CONFIGURATION
# ============================================================================
BASE_URL = "https://www.khk.cz"

# Helper variables for domain parts
PARSED_BASE_URL = urlparse(BASE_URL)
BASE_NETLOC = PARSED_BASE_URL.netloc  # e.g., "www.khk.cz"
# Ensure NON_WWW_BASE_NETLOC correctly handles cases
if BASE_NETLOC.startswith("www."):
    NON_WWW_BASE_NETLOC = BASE_NETLOC[4:]
else:
    NON_WWW_BASE_NETLOC = BASE_NETLOC
BASE_SCHEME = PARSED_BASE_URL.scheme   # e.g., "https"

SITEMAP_URL = f"{BASE_SCHEME}://{NON_WWW_BASE_NETLOC}/mapa-webu" # Original: "https://khk.cz/mapa-webu"
XML_SITEMAP_URL = f"{BASE_SCHEME}://{BASE_NETLOC}/sitemap.xml" # Original: "https://www.khk.cz/sitemap.xml"

# ============================================================================
# CUSTOM URLS LIST
# ============================================================================
# When this list is not empty, the script will only process these URLs instead of scraping the sitemap
# Example: ["https://www.khk.cz/page1", "https://www.khk.cz/page2"]
CUSTOM_URLS = [
   
]

# Global list for blacklisted URLs => ZAKÁZANÉ URL PRO SCRAPE - PRO NÍŽE ZMÍNĚNÉ URL MÁME JINÉ SKRIPTY
BLACKLISTED_URLS = [
    "https://khk.cz/urad/kontakty-telefonni-seznam",
    "https://www.khk.cz/kraj/zastupitelstvo/vybory-zastupitelstva-kralovehradeckeho-kraje",
    "https://www.khk.cz/kraj/rada/komise-rady",
    "https://www.khk.cz/kraj/zastupitelstvo/seznam-clenu-zastupitelstva-kralovehradeckeho-kraje-2024-2028"
]

# ============================================================================
# API CALL SETTINGS
# ============================================================================
API_CALL_DELAY = 5  # Fixed delay between API calls in seconds
VOICEFLOW_UPLOAD_DELAY = 10 # Delay between Voiceflow upload API calls in seconds
MAX_RETRIES = 3  # Maximum number of retry attempts
INITIAL_RETRY_DELAY = 5  # Initial retry delay in seconds

# ============================================================================
# PROCESSING FLAGS
# ============================================================================
ENABLE_QA_PROCESSING = True  # Enable Q/A processing
UPLOAD_IMMEDIATELY = False  # Skip processing and only upload existing payloads
COMPILE_SEARCH_QUERIES = True  # Enable compilation of search queries
CHECK_LAST_MODIFIED = True  # Check last modified date from sitemap.xml before Q/A extraction
ENABLE_CONTENT_CHUNKING = True # NEW: Enable/disable content chunking for Q/A

# ============================================================================
# HTTP REQUEST SETTINGS
# ============================================================================
REQUEST_TIMEOUT = 150  # SECONDS
REQUEST_RETRY_CODES = (500, 502, 503, 504, 524)
REQUEST_RETRY_COUNT = 3
REQUEST_BACKOFF_FACTOR = 0.3

# ============================================================================
# CONTENT PROCESSING SETTINGS
# ============================================================================
MAX_TOKENS = 199000  # Maximum tokens for content processing
MAX_FILENAME_LENGTH = 200  # Maximum length for generated filenames
MAX_CHUNK_CHAR_LIMIT = 6000 # NEW: Maximum characters per chunk before sub-chunking
MARKDOWN_CONTENT_SELECTORS = ["#block-khk-content", "#block-khk-sectionmenu"] # Default CSS selectors for Markdown content fetching
HTML_SITEMAP_SELECTOR = ".sitemap" # Default CSS selector for the HTML sitemap page

# ============================================================================
# CATEGORIES
# ============================================================================
CATEGORIES = [
    "Administrativa_Uredni_Zalezitosti",
    "Charakteristika_Kraje",
    "Doprava",
    "Dotace",
    "Finance_Hospodareni",
    "Kontakt",
    "Krizove_Situace",
    "Kultura_Pamatkova_Pece",
    "Media_Komunikace",
    "Rozvoj_Projekty",
    "Socialni_Pece",
    "Strategicke_Dokumenty",
    "Ukrajina",
    "Uzemni_Planovani_Stavebni_Rad",
    "Verejne_Zakazky",
    "Vzdelavani",
    "Zdravotnictvi",
    "Zivotni_Prostredi_Zemedelstvi"
]

# ============================================================================
# FILE PATHS
# ============================================================================
LAST_RUN_FILE = "scrape_sitemap_last_run_time.txt"  # File to store the last run timestamp

# Global set to track table files modified in the current run
modified_table_files_this_run = set()

# Create log directory if it doesn't exist
os.makedirs(LOG_DIR, exist_ok=True)

# Clear the log file if it exists
if os.path.exists(LOG_FILE):
    open(LOG_FILE, 'w').close()

# Update logging format and handlers
logging.basicConfig(level=logging.INFO)  # Change base level to INFO
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # Change logger level to INFO

# Clear any existing handlers
logger.handlers.clear()

# Create custom formatter that only includes essential information
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# Configure rotating file handler with more focused filtering
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)
logger.addHandler(file_handler)

# Configure console handler with same filter
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
console_handler.setLevel(logging.INFO)
logger.addHandler(console_handler)

# ============================================================================
# HTML/MARKDOWN CONTENT & PARSING
# ============================================================================

def get_content(url, content_format="html", target_selector=".sitemap"):
    """
    Fetches content (HTML or Markdown) from a URL using configured providers.

    Args:
        url (str): The URL to fetch content from.
        content_format (str): The desired format ('html' or 'markdown').
        target_selector (str): CSS selector for the target content area.
                                Used differently by providers (Jina uses it directly,
                                Firecrawl uses includeTags).

    Returns:
        tuple: (content, metadata) or (None, None) if all providers fail.
               content is the HTML string or Markdown string.
               metadata is a dictionary with 'title', 'url', etc.
    """
    logger.info(f"Získávání obsahu (formát: {content_format}) z URL: {url}")

    sequence_ids = []
    provider_configs = {}
    if content_format == "markdown":
        sequence_ids = [id.strip() for id in MARKDOWN_PROVIDER_SEQUENCE.split(',') if id.strip()]
        provider_configs = MARKDOWN_PROVIDERS
        logger.info(f"Použije se sekvence providerů pro Markdown: {MARKDOWN_PROVIDER_SEQUENCE}")
    elif content_format == "html":
        # Currently only Jina supports HTML for sitemap parsing as needed
        sequence_ids = ["jina"] # Hardcode Jina for HTML sitemap
        provider_configs = {"jina": MARKDOWN_PROVIDERS["jina"]} # Only provide Jina config
        logger.info("Použije se Jina pro HTML obsah sitemapu.")
    else:
        logger.error(f"Nepodporovaný formát obsahu: {content_format}")
        return None, None

    if not sequence_ids:
        logger.error(f"Sekvence providerů pro {content_format} je prázdná nebo neplatná.")
        return None, None

    for provider_id in sequence_ids:
        config = provider_configs.get(provider_id)
        if not config:
            logger.warning(f"Provider ID '{provider_id}' ze sekvence nebyl nalezen v konfiguraci. Přeskakuji.")
            continue

        provider_name = config["name"]
        api_key = config.get("api_key")

        logger.info(f"Pokus o získání obsahu s providerem ID: {provider_id} (Název: {provider_name})")

        if not api_key:
            logger.error(f"Chybí API klíč pro providera '{provider_name}' (ID: {provider_id}). Přeskakuji.")
            continue

        # Inner retry loop for the current provider
        for retry_attempt in range(REQUEST_RETRY_COUNT + 1):  # +1 for the initial attempt
            if retry_attempt > 0:
                logger.warning(f"Retry attempt {retry_attempt}/{REQUEST_RETRY_COUNT} for URL: {url} with {provider_name}")
                backoff_time = REQUEST_BACKOFF_FACTOR * (2 ** (retry_attempt - 1))
                logger.info(f"Waiting {backoff_time:.2f} seconds before retry...")
                time.sleep(backoff_time)

            try:
                content, metadata = None, None

                if provider_name == "jina":
                    api_url = config.get("api_url_template", "").format(url=url)
                    if not api_url:
                        logger.error(f"Chybí 'api_url_template' pro Jina. Přeskakuji.")
                        break # Skip Jina if URL template is missing

                    headers = {
                        "Accept": "application/json",
                        "Authorization": f"Bearer {api_key}",
                        "X-Engine": "browser", # Use browser engine for potentially better results
                    }
                    if content_format == "markdown":
                        headers["X-Return-Format"] = "markdown"
                        # Use global MARKDOWN_CONTENT_SELECTORS for Jina when format is markdown
                        if MARKDOWN_CONTENT_SELECTORS:
                            headers["X-Target-Selector"] = ",".join(MARKDOWN_CONTENT_SELECTORS)
                        else:
                            headers["X-Target-Selector"] = "" # Fallback to empty if global list is empty
                    else: # html
                        headers["X-Return-Format"] = "html"
                        headers["X-Target-Selector"] = target_selector # Use passed selector for sitemap

                    logger.debug(f"Volání Jina API: URL={api_url}, Headers={headers}")
                    response = requests_retry_session().get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
                    logger.debug(f"Jina status code: {response.status_code}")

                    try:
                        response_text = response.text
                        data = response.json()
                        logger.debug(f"Jina Raw Response: {response_text[:500]}...")
                        logger.debug(f"Jina Parsed JSON: {json.dumps(data, indent=2, ensure_ascii=False)[:500]}...")

                        if response.status_code == 200 and data.get("data"):
                            if content_format == "markdown":
                                content = data["data"].get("content", "")
                                metadata = {
                                    "title": data["data"].get("title", url.split('/')[-1]),
                                    "url": data["data"].get("url", url)
                                }
                                if content:
                                     print(f"\n=== JINA MARKDOWN CONTENT PREVIEW FOR {url} ===")
                                     print(f"{content[:500]}...\n")
                            else: # html
                                content = data["data"].get("html", "")
                                # Extract title from HTML for sitemap parsing
                                title = "Sitemap"
                                try:
                                    soup = BeautifulSoup(content, "html.parser")
                                    title_element = soup.title
                                    if title_element and title_element.string:
                                        title = title_element.string
                                except Exception as e:
                                    logger.warning(f"Error extracting title from Jina HTML: {str(e)}")
                                metadata = {
                                    "title": title,
                                    "url": data["data"].get("url", url)
                                }

                            if content:
                                logger.info(f"Úspěšně získán obsah ({content_format}) od Jina.")
                                return content, metadata
                            else:
                                logger.error(f"Jina API vrátilo úspěšný status, ale obsah ({content_format}) chybí.")
                                # Don't retry if content is missing, maybe the page is empty
                                break # Go to next provider
                        else:
                            error_message = data.get("error", {}).get("message", f"HTTP Status {response.status_code}")
                            logger.error(f"Chyba Jina API: {error_message}")
                            # Retry on server errors or rate limits
                            if response.status_code >= 500 or response.status_code == 429:
                                if retry_attempt < REQUEST_RETRY_COUNT:
                                    logger.warning("Retrying due to Jina API error...")
                                    continue
                                else:
                                    logger.error("Max retries reached for Jina.")
                                    break # Go to next provider after max retries
                            else:
                                logger.warning(f"Non-retryable Jina error ({response.status_code}).")
                                break # Go to next provider

                    except json.JSONDecodeError as e:
                        logger.error(f"Chyba při parsování JSON odpovědi od Jina: {str(e)}")
                        logger.error(f"Kompletní response text: {response_text}")
                        if retry_attempt < REQUEST_RETRY_COUNT:
                            logger.warning("Retrying due to Jina JSON parse error...")
                            continue
                        break # Go to next provider
                    except requests.exceptions.RequestException as e:
                        logger.error(f"Chyba při volání Jina API: {str(e)}")
                        if retry_attempt < REQUEST_RETRY_COUNT:
                           logger.warning("Retrying due to Jina request exception...")
                           continue
                        break # Go to next provider

                elif provider_name == "firecrawl":
                    # Firecrawl only supports markdown format in this implementation
                    if content_format != "markdown":
                        logger.warning("Firecrawl provider currently only supports 'markdown' format. Skipping.")
                        break # Skip Firecrawl if HTML is requested

                    api_url = config.get("api_url")
                    if not api_url:
                        logger.error(f"Chybí 'api_url' pro Firecrawl. Přeskakuji.")
                        break # Skip Firecrawl if URL is missing

                    headers = {
                        'Content-Type': 'application/json',
                        'Authorization': f'Bearer {api_key}'
                    }
                    # Use global MARKDOWN_CONTENT_SELECTORS for Firecrawl includeTags
                    # Firecrawl expects a list of strings, which MARKDOWN_CONTENT_SELECTORS is.
                    effective_include_tags = MARKDOWN_CONTENT_SELECTORS if MARKDOWN_CONTENT_SELECTORS else []
                    payload = {
                        "url": url,
                        "formats": ["markdown"],
                        "includeTags": effective_include_tags
                    }

                    logger.debug(f"Volání Firecrawl API: URL={api_url}, Headers={headers}, Payload={json.dumps(payload)}")
                    response = requests_retry_session().post(api_url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
                    logger.debug(f"Firecrawl status code: {response.status_code}")

                    try:
                        response_text = response.text
                        data = response.json()
                        logger.debug(f"Firecrawl Raw Response: {response_text[:500]}...")
                        logger.debug(f"Firecrawl Parsed JSON: {json.dumps(data, indent=2, ensure_ascii=False)[:500]}...")

                        if response.status_code == 200 and data.get("success") and data.get("data"):
                            content = data["data"].get("markdown", "")
                            # Use metadata from Firecrawl if available
                            fc_metadata = data["data"].get("metadata", {})
                            metadata = {
                                "title": fc_metadata.get("title", url.split('/')[-1]),
                                "url": fc_metadata.get("sourceURL", url),
                                "description": fc_metadata.get("description"),
                                "language": fc_metadata.get("language")
                            }
                            if content:
                                print(f"\n=== FIRECRAWL MARKDOWN CONTENT PREVIEW FOR {url} ===")
                                print(f"{content[:500]}...\n")
                                logger.info("Úspěšně získán obsah (markdown) od Firecrawl.")
                                return content, metadata
                            else:
                                logger.error("Firecrawl API vrátilo úspěšný status, ale markdown obsah chybí.")
                                # Don't retry if content is missing
                                break # Go to next provider
                        else:
                            error_message = data.get("error", f"HTTP Status {response.status_code} or success=false")
                            logger.error(f"Chyba Firecrawl API: {error_message}")
                            # Retry on server errors or rate limits
                            if response.status_code >= 500 or response.status_code == 429:
                                if retry_attempt < REQUEST_RETRY_COUNT:
                                    logger.warning("Retrying due to Firecrawl API error...")
                                    continue
                                else:
                                    logger.error("Max retries reached for Firecrawl.")
                                    break # Go to next provider after max retries
                            else:
                                logger.warning(f"Non-retryable Firecrawl error ({response.status_code}).")
                                break # Go to next provider

                    except json.JSONDecodeError as e:
                        logger.error(f"Chyba při parsování JSON odpovědi od Firecrawl: {str(e)}")
                        logger.error(f"Kompletní response text: {response_text}")
                        if retry_attempt < REQUEST_RETRY_COUNT:
                            logger.warning("Retrying due to Firecrawl JSON parse error...")
                            continue
                        break # Go to next provider
                    except requests.exceptions.RequestException as e:
                        logger.error(f"Chyba při volání Firecrawl API: {str(e)}")
                        if retry_attempt < REQUEST_RETRY_COUNT:
                           logger.warning("Retrying due to Firecrawl request exception...")
                           continue
                        break # Go to next provider

                # --- End of Provider Specific Logic --- #

            except requests.exceptions.Timeout as e:
                logger.error(f"Timeout při volání {provider_name} API (pokus {retry_attempt+1}/{REQUEST_RETRY_COUNT+1}): {str(e)}")
                if retry_attempt < REQUEST_RETRY_COUNT:
                    logger.warning(f"Retrying {provider_name} after timeout...")
                    continue
                logger.error(f"Max retries reached for timeout with {provider_name}. Trying next provider.")
                break # Go to next provider

            except Exception as e:
                logger.error(f"Neočekávaná chyba při zpracování s providerem {provider_name}: {str(e)}", exc_info=True)
                if retry_attempt < REQUEST_RETRY_COUNT:
                    logger.warning(f"Retrying {provider_name} after unexpected error...")
                    continue
                logger.error(f"Max retries reached after unexpected error with {provider_name}. Trying next provider.")
                break # Go to next provider

        # If the inner loop finished without returning (all retries failed for this provider),
        # the outer loop continues to the next provider_id.

    # If the outer loop finishes, all providers in the sequence failed.
    logger.error(f"Nepodařilo se získat obsah ({content_format}) pro URL {url} od žádného providera v sekvenci.")
    return None, None

# --- Keep the old function name for calls that specifically need HTML via Jina --- 
# --- This avoids refactoring the sitemap parsing logic extensively now --- 
def get_html_content_via_jina(url, target_selector=None):
    # Use the global HTML_SITEMAP_SELECTOR if no specific target_selector is provided
    effective_target_selector = target_selector if target_selector is not None else HTML_SITEMAP_SELECTOR
    return get_content(url, content_format="html", target_selector=effective_target_selector)

def parse_menu(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    # Adjusted selector to be slightly more flexible
    main_menu = soup.select_one(".sitemap ul") 
    
    if not main_menu:
        logger.warning("Hlavní menu nebylo nalezeno pomocí selektoru '.sitemap ul'.")
        # Optional: Add fallback logic if needed
        # main_menu = soup.find('ul') 
        # if not main_menu:
        #     logger.error("Nebylo nalezeno žádné <ul> menu na stránce.")
        # else:
        #     logger.info("Používá se první nalezený <ul> jako záložní řešení.")

    return main_menu

def extract_links(menu_item, path=[], categorized_links={}, url_last_modified_map={}, last_run_timestamp=None, process_missing=False):
    """
    Recursively extracts links from the sitemap menu structure, categorizes them,
    generates questions, and updates the categorized_links dictionary.

    Args:
        menu_item: The BeautifulSoup tag representing the current menu item (li or ul).
        path: The current navigation path (list of strings).
        categorized_links: Dictionary holding the categorized links loaded from files
                           and updated during the run ({category: [link_items]}).
        url_last_modified_map: Dictionary mapping URLs to their last modified dates.
        last_run_timestamp: The timestamp of the last script run.
        process_missing: Flag to process missing URLs

    Returns:
        The updated categorized_links dictionary.
    """
    if menu_item.name == "li":
        # Updated selector to find the link within the new structure
        link_tag = menu_item.select_one("div.views-field span.field-content a")
        link_text_element = menu_item.select_one("div.views-field span.field-content") # Get the span to extract text, even if 'a' is missing

        # Use the text from the span directly if link_tag is found, otherwise try to get text from span
        link_text = link_tag.text.strip() if link_tag else (link_text_element.text.strip() if link_text_element else "")

        if link_text: # Process if we found text, even without a link
            current_path = path + [link_text]
            absolute_path = " > ".join(current_path)

            if link_tag and link_tag.has_attr("href"): # Process link only if 'a' tag with href exists
                absolute_url = urljoin(BASE_URL, link_tag["href"])
                logger.info(f"\n=== Starting initial check for URL: {absolute_url} ===")
                logger.info(f"Path: {absolute_path}")

                # 1. Find last modified date from sitemap.xml
                last_modified = find_url_last_modified(absolute_url, url_last_modified_map)

                # 2. Check if the URL should be processed based on modification date
                if not should_process_url(absolute_url, last_modified, last_run_timestamp):
                    logger.info(f"Skipping all further processing for URL {absolute_url} as it hasn't changed.")
                    # Find the next 'ul' and continue recursion if it exists
                    sub_menu = menu_item.find("ul", recursive=False)
                    if sub_menu:
                        extract_links(sub_menu, current_path, categorized_links, url_last_modified_map, last_run_timestamp, process_missing)
                    return categorized_links # Return early for this 'li' branch if skipped
                
                # If the check passes, proceed with full processing
                logger.info(f"Proceeding with full processing for URL: {absolute_url}")
                try:
                    # 3. Categorize URL - Updated to use utility function
                    category = categorize_link(current_path, CATEGORIES, LLM_PROVIDERS, LLM_SEQUENCE, MAX_RETRIES, INITIAL_RETRY_DELAY, API_CALL_DELAY)
                    logger.info(f"Assigned category: {category} based on path")

                    # 4. Generate RAG question - Updated to use utility function
                    rag_question = generate_rag_question(current_path, LLM_PROVIDERS, LLM_SEQUENCE, MAX_RETRIES, INITIAL_RETRY_DELAY, API_CALL_DELAY)
                    logger.info(f"Generated RAG question: {rag_question}")

                    # 5. Only get metadata - no HTML content call, as we'll use Markdown mode for content
                    metadata = {"title": link_text, "url": absolute_url}

                    # 6. Add or Update in categorized_links
                    if category not in categorized_links:
                        categorized_links[category] = []

                    # Check if URL already exists
                    existing_entry = None
                    for i, entry in enumerate(categorized_links[category]):
                        if entry.get("URL") == absolute_url:
                            existing_entry = entry
                            entry_index = i
                            break
                            
                    link_data = {
                        "Title": link_text, # Use extracted link_text
                        "URL": absolute_url,
                        "Category": category,
                        "Question": rag_question,
                        "Navigation": absolute_path
                    }

                    if existing_entry:
                        # Update existing entry
                        logger.info(f"Updating existing entry for URL: {absolute_url} in category {category}")
                        categorized_links[category][entry_index] = link_data
                    else:
                        # Append new entry
                        logger.info(f"Adding new entry for URL: {absolute_url} to category {category}")
                        categorized_links[category].append(link_data)

                    # --- Save the updated category table payload incrementally --- 
                    save_payloads_to_files(categorized_links, category_to_save=category)
                    # ------------------------------------------------------------

                    # 8. Q/A Processing - only if enabled and not a sitemap
                    if ENABLE_QA_PROCESSING:
                        # Check domain and skip if not exactly "www.khk.cz" or "khk.cz"
                        parsed_url = urlparse(absolute_url)
                        domain = parsed_url.netloc.lower()
                        
                        # Skip Q/A extraction for sitemaps, subdomains, or non-khk.cz domains
                        is_sitemap = "mapa-stranek" in absolute_url.lower() or "sitemap" in absolute_url.lower()
                        is_main_domain = domain == BASE_NETLOC or domain == NON_WWW_BASE_NETLOC
                        
                        if is_sitemap:
                            logger.info("Přeskakuji Q/A extrakci pro mapu stránek")
                        elif not is_main_domain:
                            logger.info(f"Přeskakuji Q/A extrakci pro subdoménu nebo jinou doménu: {domain}")
                        else:
                            # Direct Q/A content processing without redundant HTML call
                            logger.info("Spouštím Q/A extrakci (URL processing check passed)...")
                            process_url_content(absolute_url, category, metadata)
                    else:
                        logger.info("Q/A zpracování je vypnuto")

                    logger.info(f"=== Dokončeno zpracování URL: {absolute_url} ===\n")

                except Exception as e:
                    logger.error(f"Chyba při zpracování URL {absolute_url}: {str(e)}")
                    logger.error("Pokračuji na další URL...")
            else:
                # Log items that have text but no link (they might be headers for submenus)
                 logger.info(f"Skipping item with no link: {absolute_path}")


            # Pokračování v procházení submenu - moved here to ensure recursion happens even if URL is skipped
            sub_menu = menu_item.find("ul", recursive=False)
            if sub_menu:
                extract_links(sub_menu, current_path, categorized_links, url_last_modified_map, last_run_timestamp, process_missing)

    elif menu_item.name == "ul":
        for item in menu_item.find_all("li", recursive=False):
            # Pass the potentially modified categorized_links down the recursion
            categorized_links = extract_links(item, path, categorized_links, url_last_modified_map, last_run_timestamp, process_missing)
    
    return categorized_links

def save_payloads_to_files(categorized_links, category_to_save=None):
    """Saves categorized links table payloads to JSON files.

    If category_to_save is specified, it performs an incremental update/add
    for only the links associated with that specific category within the 
    categorized_links dictionary (assumed to be the newly processed links).
    Otherwise, it saves the complete payload for all categories.

    Args:
        categorized_links (dict): Dictionary containing categorized links.
                                  If category_to_save is set, this dictionary
                                  should ideally only contain the links for that category.
        category_to_save (str, optional): If specified, only process this category incrementally.
    """
    output_dir = "payloads"
    os.makedirs(output_dir, exist_ok=True)

    if category_to_save:
        # --- Incremental Update Logic --- 
        if category_to_save not in categorized_links:
            logger.warning(f"Category '{category_to_save}' not found for incremental save.")
            return

        category = category_to_save
        current_links_for_category = categorized_links[category]
        if not isinstance(current_links_for_category, list):
            logger.error(f"Data for category '{category}' is not a list ({type(current_links_for_category)}). Skipping incremental save.")
            return
            
        table_name = f"{category.lower()}_table"
        filename = f"{output_dir}/{table_name}.json"
        
        # Load existing data
        existing_items = []
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    existing_payload = json.load(f)
                    if (isinstance(existing_payload, dict) and 
                        'data' in existing_payload and 
                        'items' in existing_payload['data'] and
                        isinstance(existing_payload['data']['items'], list)):
                        existing_items = existing_payload['data']['items']
                        logger.info(f"Loaded {len(existing_items)} existing items from {filename} for incremental update.")
                    else:
                        logger.warning(f"Existing file {filename} has invalid structure. Will be overwritten.")
                        existing_items = [] # Start fresh if structure is bad
            except Exception as e:
                logger.warning(f"Error reading existing file {filename}: {str(e)}. Will create new file.")
                existing_items = []

        # Create a dictionary of existing items by URL for quick lookup
        existing_items_dict = {item.get('URL'): item for item in existing_items if isinstance(item, dict) and item.get('URL')}
        
        updated_count = 0
        added_count = 0

        # Process only the links passed in for the specific category
        for link_data in current_links_for_category:
            if not isinstance(link_data, dict):
                logger.warning(f"Skipping non-dictionary item during incremental save for category '{category}': {link_data}")
                continue
                
            url = link_data.get('URL')
            if not url:
                logger.warning(f"Skipping item without URL during incremental save for category '{category}': {link_data}")
                continue

            # Create the standardized item structure
            new_item = {
                "Title": link_data.get("Title", ""), 
                "URL": url,
                "Category": category,
                "Question": link_data.get("Question", "Default question | Default question in English"),
                "Navigation": link_data.get("Navigation", ""),
                "Type": "Data"  # Add static Type field
            }

            # Check if URL exists and update/add in the dictionary
            if url in existing_items_dict:
                existing_items_dict[url] = new_item # Update
                updated_count += 1
            else:
                existing_items_dict[url] = new_item # Add
                added_count += 1

        # Convert the dictionary back to a list for the final payload
        final_items = list(existing_items_dict.values())

        # Create the final payload structure
        payload = {
            "data": {
                "schema": {
                    "searchableFields": ["Title", "URL", "Question", "Navigation"],
                    "metadataFields": ["Category", "Type"]
                },
                "name": table_name,
                "items": final_items
            }
        }

        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logger.info(f"Incrementally updated table payload for '{category}' in file: {filename} (Updated: {updated_count}, Added: {added_count}, Total: {len(final_items)})")
            modified_table_files_this_run.add(os.path.basename(filename)) # Track modified file
        except Exception as e:
            logger.error(f"Error writing table payload file {filename}: {str(e)}")

    else:
        # --- Full Save Logic (for all categories) --- 
        logger.info("Saving final table payloads for all categories.")
        for category, links in categorized_links.items():
            if not isinstance(links, list):
                logger.error(f"Data for category '{category}' is not a list ({type(links)}). Skipping this category.")
                continue
                
            table_name = f"{category.lower()}_table"
            filename = f"{output_dir}/{table_name}.json"
            
            final_items = []
            for link_data in links:
                if not isinstance(link_data, dict):
                    logger.warning(f"Skipping non-dictionary item during final save for category '{category}': {link_data}")
                    continue
                
                final_items.append({
                    "Title": link_data.get("Title", ""), 
                    "URL": link_data.get("URL", ""),
                    "Category": category,
                    "Question": link_data.get("Question", "Default question | Default question in English"),
                    "Navigation": link_data.get("Navigation", ""),
                    "Type": "Data"  # Add static Type field
                })
            
            payload = {
                "data": {
                    "schema": {
                        "searchableFields": ["Title", "URL", "Question", "Navigation"],
                        "metadataFields": ["Category", "Type"]
                    },
                    "name": table_name,
                    "items": final_items
                }
            }

            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                logger.info(f"Saved final table payload for '{category}' in file: {filename} (Total items: {len(final_items)})")
                modified_table_files_this_run.add(os.path.basename(filename)) # Track modified file
            except Exception as e:
                logger.error(f"Error writing final table payload file {filename}: {str(e)}")

def load_payloads_from_files():
    """Loads existing categorized link data from payload JSON files."""
    payloads_dir = "payloads"
    loaded_data = {}
    logger.info(f"Loading existing payloads from directory: {payloads_dir}")
    
    if not os.path.exists(payloads_dir):
        logger.warning(f"Payloads directory '{payloads_dir}' does not exist. Starting with empty data.")
        return loaded_data
        
    for filename in os.listdir(payloads_dir):
        # Only process the category table payloads, not the QA ones
        # Updated suffix check
        if filename.endswith('_table.json'):
            file_path = os.path.join(payloads_dir, filename)
            logger.debug(f"Attempting to load file: {filename}")
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    payload = json.load(f)
                    
                    # Extract data safely
                    data_section = payload.get('data', {})
                    items = data_section.get('items', [])
                    category = None
                    
                    # Try to determine category from filename or metadata
                    # Assuming filename format like "category_table.json"
                    category_from_filename = filename.replace('_table.json', '').capitalize() # Simple extraction
                    
                    # Check if items exist and have a Category field
                    if items and isinstance(items, list) and items[0].get('Category'):
                        category = items[0]['Category']
                    elif category_from_filename:
                         category = category_from_filename
                    else:
                        logger.warning(f"Could not determine category for file {filename}. Skipping.")
                        continue

                    if category:
                         # Ensure items is a list
                        if not isinstance(items, list):
                            logger.warning(f"Items in {filename} is not a list. Skipping.")
                            continue
                            
                        if category not in loaded_data:
                            loaded_data[category] = []
                            
                        # Set of existing URLs for the current category being loaded into
                        # This set is used to deduplicate items that *do* have a URL.
                        existing_urls_in_category_so_far = {item_data.get('URL') for item_data in loaded_data[category] if item_data.get('URL')}
                        
                        items_added_from_this_file_count = 0
                        for item_from_file in items: # 'items' are from the current file being loaded
                             if not isinstance(item_from_file, dict):
                                 logger.warning(f"Item in {filename} is not a dictionary. Skipping item: {item_from_file}")
                                 continue
                            
                             item_url = item_from_file.get('URL')
                             
                             if item_url: # Item has a URL
                                 if item_url not in existing_urls_in_category_so_far:
                                     loaded_data[category].append(item_from_file)
                                     # Add to set to check against subsequent items from other files for this category
                                     existing_urls_in_category_so_far.add(item_url) 
                                     items_added_from_this_file_count += 1
                                 # else: duplicate URL for this category, already loaded from a previous file, skip silently
                             else: # Item does NOT have a URL (e.g., from _questions_table.json or _contacts_table.json)
                                 # Add the item. The warning is removed.
                                 # These items are generally unique by content from their source generation.
                                 loaded_data[category].append(item_from_file)
                                 items_added_from_this_file_count += 1
                        
                        if items_added_from_this_file_count > 0:
                            logger.info(f"Added {items_added_from_this_file_count} items for category '{category}' from {filename} to loaded data.")
                        elif items: 
                             logger.info(f"No new items added for category '{category}' from {filename} to loaded data (all items were duplicates or invalid).")
                        # Removed the previous warning for missing URL and skipping item.
                        
            except json.JSONDecodeError as e:
                logger.error(f"Error decoding JSON from file {filename}: {str(e)}")
            except Exception as e:
                logger.error(f"Unexpected error loading file {filename}: {str(e)}")
                
    logger.info(f"Finished loading existing payloads. Found data for {len(loaded_data)} categories.")
    return loaded_data

def remove_accents(input_str):
    nfkd_form = unicodedata.normalize('NFKD', input_str)
    return ''.join([c for c in nfkd_form if not unicodedata.combining(c)])

def truncate_content(content, max_tokens=199000):
    """
    Truncate the content to a maximum number of tokens (approximated by characters).
    
    Args:
    content (str): The content to truncate
    max_tokens (int): Maximum number of tokens (approximated as characters)
    
    Returns:
    str: Truncated content
    """
    # A simple approximation: 1 token ~= 4 characters
    max_chars = max_tokens * 4
    if len(content) > max_chars:
        return content[:max_chars] + "..."
    return content

def save_payload_to_file(url, content, section):
    """Saves the provided content (list of QA pairs) to a JSON file,
    always overwriting the existing file.

    Args:
        url (str): The URL the content is associated with (used for filename and data).
        content (list): The list of QA pair dictionaries to save.
        section (str): The category/section (used for filename and metadata).
    """
    # Ensure the output directory exists
    output_dir = "payloads"
    os.makedirs(output_dir, exist_ok=True)
    
    # Generate a stable URL-based identifier for consistent filenames
    from urllib.parse import urlparse
    
    parsed_url = urlparse(url)
    url_path = parsed_url.path.strip('/')
    
    # If path is empty (homepage), use 'home'
    if not url_path:
        url_path = 'home'
    
    # Create a stable filename based on the URL path
    url_based_name = url_path.replace('/', '_')

    # Sanitize the final base name
    url_based_name = remove_accents(url_based_name)
    url_based_name = re.sub(r"[<>:\"/\\|?*]", "_", url_based_name)
    url_based_name = re.sub(r"\s+", "_", url_based_name)
    url_based_name = re.sub(r"_+", "_", url_based_name)
    url_based_name = url_based_name.strip("_").lower()  # Ensure lowercase for consistency
    url_based_name = url_based_name[:MAX_FILENAME_LENGTH] # Use MAX_FILENAME_LENGTH constant
    
    # QA payload filename format: {section}_{url_based_name}.json
    filename = f"payloads/{section.lower()}_{url_based_name}.json"
    
    # Ensure category and URL are added to each item in the *incoming* content list
    for item in content: # 'content' is the list of QA pairs
        if isinstance(item, dict):
            item["Category"] = section
            item["Type"] = "Data" # Add static Type field
            item["URL"] = url     # Add the source URL to each Q/A item
        else:
            logger.warning(f"Skipping non-dictionary item during category/URL assignment: {item}")

    # --- Always Overwrite Logic --- 
    logger.info(f"Performing full save (overwrite) to: {filename}")
    final_items = content # Use the provided content directly
    
    # Construct the final payload structure
    payload = {
        "data": {
            "schema": {
                "searchableFields": ["Question", "Answer"],
                "metadataFields": ["Category", "Type", "URL"] # Added "URL"
            },
            "name": f"{section.lower()}_{url_based_name}", # Schema name consistency
            "items": final_items 
        }
    }
    
    # --- Final Validation and Saving --- 
    # Dodatečná validace struktury payloadu
    if not isinstance(payload["data"]["items"], list):
        logger.error(f"Critical Error: Payload items is not a list before saving {filename}. Aborting save.")
        return None # Indicate save failure
        
    valid_items_count = 0
    validated_items = []
    for item in payload["data"]["items"]:
        if not isinstance(item, dict):
            logger.warning(f"Skipping non-dictionary item during final validation: {item}")
            continue
        # item["Type"] = "Data" # Already added
        # item["URL"] = url     # Already added to 'content' which became 'final_items'
        valid = True
        # Check all keys including the new "URL" in metadataFields
        for key in payload["data"]["schema"]["searchableFields"] + payload["data"]["schema"]["metadataFields"]:
            if key not in item:
                logger.warning(f"Missing required key '{key}' in item for file {filename}: {item}. Skipping item.")
                valid = False
                break
        if valid:
            validated_items.append(item)
            valid_items_count += 1
            
    payload["data"]["items"] = validated_items # Use only validated items
    
    if not validated_items and bool(content):
        logger.warning(f"No valid Q/A items found after validation for {filename}. Skipping file save as all originally provided items were invalid.")
        return None 

    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Successfully saved/overwritten payload for URL '{url}' ({len(payload['data']['items'])} items) to file: {filename}")
        print(f"Payload saved/overwritten: {filename}")
        return filename 
    except Exception as e:
        logger.error(f"Error writing payload file {filename}: {str(e)}")
        return None 

def upload_to_voiceflow(filename):
    logger.info(f"Nahrávání souboru '{filename}' do Voiceflow")
    url = "https://api.voiceflow.com/v1/knowledge-base/docs/upload/table?overwrite=true"
    headers = {
        "Authorization": VOICEFLOW_API_KEY,
        "accept": "application/json",
        "content-type": "application/json"
    }
    
    with open(filename, "r", encoding="utf-8") as f:
        payload = json.load(f)
    
    response = requests.post(url, headers=headers, json=payload)
    
    if response.status_code == 200:
        logger.info(f"Úspěšně nahráno {len(payload['data']['items'])} položek pro soubor '{filename}'")
    else:
        logger.error(f"Chyba při nahrávání souboru '{filename}': {response.text}")

def compile_search_queries_file():
    """
    Compiles all search queries from JSON payloads into a single TXT file.
    """
    output_file = "compiled_search_queries.txt"
    payloads_dir = "payloads"
    
    logger.info(f"Starting compilation of search queries into {output_file}")
    
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            example_counter = 1  # Initialize counter
            
            # Process all JSON payload files in the payloads directory
            for filename in os.listdir(payloads_dir):
                if filename.endswith('_payload.json'):
                    file_path = os.path.join(payloads_dir, filename)
                    # Enhanced log message
                    logger.info(f"Compiling queries from file: {filename}")
                    
                    try:
                        with open(file_path, 'r', encoding='utf-8') as json_file:
                            payload = json.load(json_file)
                            items = payload['data']['items']
                            
                            for item in items:
                                title = item.get('Title', '')
                                questions = item.get('Question', '')
                                
                                if title and questions:
                                    # Format the entry with counter and "Response:" line
                                    entry = (
                                        f"\n## Example {example_counter} - Original query about: '{title}'\n"
                                        "* Response:\n"
                                        "{\n"
                                        f'    "WebSearchQuery": "{title}",\n'
                                        f'    "UserReply": "{questions}"\n'
                                        "}\n"
                                    )
                                    f.write(entry)
                                    example_counter += 1  # Increment counter
                    
                    except Exception as e:
                        logger.error(f"Error processing file {filename}: {str(e)}")
                        continue
        
        logger.info(f"Successfully compiled {example_counter-1} search queries into {output_file}")
        
    except Exception as e:
        logger.error(f"Error creating compiled search queries file: {str(e)}")

def get_last_run_timestamp():
    """
    Get the timestamp of the last script run from the file.
    Returns the timestamp as a datetime object or None if the file doesn't exist.
    """
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
    """
    Save the current timestamp as the last run timestamp.
    """
    try:
        with open(LAST_RUN_FILE, 'w') as f:
            # Use timezone(timedelta(hours=2)) for UTC+02:00
            f.write(datetime.now().astimezone(timezone(timedelta(hours=2))).isoformat())
        logger.info(f"Saved current UTC+02:00 timestamp to {LAST_RUN_FILE}")
    except Exception as e:
        logger.error(f"Error saving last run timestamp: {str(e)}")

def parse_lastmod_date(date_str):
    """
    Parse a lastmod date string from the sitemap into a datetime object.
    Handles various date formats that might appear in sitemaps.
    
    Args:
        date_str (str): The date string to parse
        
    Returns:
        datetime: The parsed datetime object or None if parsing fails
    """
    formats = [
        # Standard ISO format with timezone
        lambda s: datetime.fromisoformat(s.replace('Z', '+00:00')),
        # Standard ISO format without timezone
        lambda s: datetime.fromisoformat(s),
        # Format: 2025-04-30T23:45Z
        lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ"),
        # Format: 2025-04-30T23:45:00Z
        lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ"),
        # Format: 2025-04-30 23:45:00
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S"),
        # Format: 2025-04-30
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
    """
    Fetch the XML sitemap from khk.cz/sitemap.xml and parse it.
    Returns a dictionary mapping URLs to their last modified dates.
    """
    logger.info(f"Fetching XML sitemap from {XML_SITEMAP_URL}")
    url_last_modified = {}
    
    try:
        # First try the main sitemap URL
        response = requests_retry_session().get(XML_SITEMAP_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.debug(f"Received response from {XML_SITEMAP_URL}, status code: {response.status_code}")
        logger.debug(f"Response content preview: {response.text[:500]}")
        
        # Based on the screenshots, the structure might be very simple - try direct parsing
        if "page=1" in response.text or "page=2" in response.text:
            logger.info("Detected simple sitemap index format")
            
            # Try to find URLs in the format seen in the screenshot
            sitemap_page_urls = []
            matches = re.findall(r'https://www\.khk\.cz/sitemap\.xml\?page=\d+', response.text)
            if matches:
                sitemap_page_urls = matches
                logger.info(f"Found {len(sitemap_page_urls)} sitemap page URLs: {sitemap_page_urls}")
            
            # Process each sitemap page
            for page_url in sitemap_page_urls:
                logger.info(f"Fetching sitemap page: {page_url}")
                try:
                    page_response = requests_retry_session().get(page_url, timeout=REQUEST_TIMEOUT)
                    page_response.raise_for_status()
                    logger.debug(f"Response content preview for {page_url}: {page_response.text[:500]}")
                    
                    # Extract URLs and last modified dates using regex
                    url_matches = re.findall(r'<url>.*?<loc>(.*?)</loc>.*?<lastmod>(.*?)</lastmod>.*?</url>', page_response.text, re.DOTALL)
                    
                    for url, lastmod in url_matches:
                        last_modified = parse_lastmod_date(lastmod)
                        if last_modified:
                            url_last_modified[url] = last_modified
                            logger.debug(f"Found URL: {url}, last modified: {last_modified}")
                    
                    logger.info(f"Extracted {len(url_matches)} URLs from {page_url}")
                    
                except Exception as e:
                    logger.error(f"Error processing sitemap page {page_url}: {str(e)}")
            
            if url_last_modified:
                logger.info(f"Successfully extracted {len(url_last_modified)} URLs with last modified dates")
                return url_last_modified
        
        # If simple parsing didn't work, try using BeautifulSoup with different parsers
        for parser in ["lxml-xml", "html.parser", "lxml"]:
            try:
                logger.info(f"Trying to parse with {parser}")
                soup = BeautifulSoup(response.text, parser)
                
                # Try to find sitemap entries (index format)
                sitemaps = soup.find_all("sitemap")
                if sitemaps:
                    logger.info(f"Found {len(sitemaps)} sitemap entries with parser {parser}")
                    sitemap_urls = []
                    
                    for sitemap in sitemaps:
                        loc = sitemap.find("loc")
                        if loc and loc.text:
                            sitemap_urls.append(loc.text)
                    
                    # Process each sitemap
                    for sitemap_url in sitemap_urls:
                        logger.info(f"Fetching sitemap: {sitemap_url}")
                        sitemap_response = requests_retry_session().get(sitemap_url, timeout=REQUEST_TIMEOUT)
                        sitemap_response.raise_for_status()
                        
                        sitemap_soup = BeautifulSoup(sitemap_response.text, parser)
                        
                        # Extract URLs and last modified dates
                        for url_entry in sitemap_soup.find_all("url"):
                            loc = url_entry.find("loc")
                            lastmod = url_entry.find("lastmod")
                            
                            if loc and loc.text:
                                url = loc.text
                                last_modified = None
                                
                                if lastmod and lastmod.text:
                                    last_modified = parse_lastmod_date(lastmod.text)
                                
                                if last_modified:
                                    url_last_modified[url] = last_modified
                    
                    logger.info(f"Extracted {len(url_last_modified)} URLs with their last modified dates")
                    return url_last_modified
                
                # Try to find URL entries directly (non-index format)
                url_entries = soup.find_all("url")
                if url_entries:
                    logger.info(f"Found {len(url_entries)} URL entries with parser {parser}")
                    
                    for url_entry in url_entries:
                        loc = url_entry.find("loc")
                        lastmod = url_entry.find("lastmod")
                        
                        if loc and loc.text:
                            url = loc.text
                            last_modified = None
                            
                            if lastmod and lastmod.text:
                                last_modified = parse_lastmod_date(lastmod.text)
                            
                            if last_modified:
                                url_last_modified[url] = last_modified
                    
                    logger.info(f"Extracted {len(url_last_modified)} URLs with their last modified dates")
                    return url_last_modified
            
            except Exception as e:
                logger.error(f"Error parsing with {parser}: {str(e)}")
        
        # If no parsing method worked, try the direct page URLs seen in the screenshot
        logger.info("Trying direct page URLs from screenshot")
        direct_page_urls = [
            f"{XML_SITEMAP_URL}?page=1",
            f"{XML_SITEMAP_URL}?page=2"
        ]
        
        for page_url in direct_page_urls:
            try:
                logger.info(f"Fetching sitemap page: {page_url}")
                page_response = requests_retry_session().get(page_url, timeout=REQUEST_TIMEOUT)
                page_response.raise_for_status()
                
                # Try all parsers for each page
                for parser in ["lxml-xml", "html.parser", "lxml"]:
                    try:
                        logger.info(f"Parsing {page_url} with {parser}")
                        page_soup = BeautifulSoup(page_response.text, parser)
                        
                        url_entries = page_soup.find_all("url")
                        if not url_entries:
                            # Try different structure
                            url_entries = page_soup.select("table tr")
                            if url_entries:
                                logger.info(f"Found {len(url_entries)} table rows, parsing as table")
                                for row in url_entries[1:]:  # Skip header row
                                    cells = row.find_all("td")
                                    if len(cells) >= 2:
                                        url_cell = cells[0].find("a")
                                        if url_cell and url_cell.has_attr("href"):
                                            url = url_cell["href"]
                                            lastmod_text = cells[1].text.strip()
                                            last_modified = parse_lastmod_date(lastmod_text)
                                            if last_modified:
                                                url_last_modified[url] = last_modified
                                                logger.debug(f"Found URL from table: {url}, last modified: {last_modified}")
                        else:
                            logger.info(f"Found {len(url_entries)} URL entries in {page_url}")
                            for url_entry in url_entries:
                                loc = url_entry.find("loc")
                                lastmod = url_entry.find("lastmod")
                                
                                if loc and loc.text:
                                    url = loc.text
                                    last_modified = None
                                    
                                    if lastmod and lastmod.text:
                                        last_modified = parse_lastmod_date(lastmod.text)
                                    
                                    if last_modified:
                                        url_last_modified[url] = last_modified
                                        logger.debug(f"Found URL: {url}, last modified: {last_modified}")
                    
                    except Exception as e:
                        logger.error(f"Error parsing {page_url} with {parser}: {str(e)}")
            
            except Exception as e:
                logger.error(f"Error fetching sitemap page {page_url}: {str(e)}")
        
        if url_last_modified:
            logger.info(f"Successfully extracted {len(url_last_modified)} URLs with last modified dates")
            return url_last_modified
        else:
            logger.warning("Failed to parse sitemap with any method")
            return {}
                
    except Exception as e:
        logger.error(f"Error fetching XML sitemap: {str(e)}")
        return {}

def find_url_last_modified(url, url_last_modified_map):
    """
    Find the last modified date for a given URL by matching against the sitemap map.
    Logs the matching process.
    
    Args:
        url (str): The URL to find in the sitemap.
        url_last_modified_map (dict): Dictionary mapping sitemap URLs to their last modified dates.
        
    Returns:
        datetime: The last modified datetime object if found, otherwise None.
    """
    logger.debug(f"Attempting to find last modified date for URL: {url}")
    last_modified = None
    matched_sitemap_url = None
    
    # Normalize URL by removing trailing slashes for matching
    normalized_url = url.rstrip('/')
    
    # 1. Try exact match
    if url in url_last_modified_map:
        last_modified = url_last_modified_map[url]
        matched_sitemap_url = url
        logger.debug(f"Exact match found: {url}")
    
    # 2. Try normalized URL match
    elif normalized_url in url_last_modified_map:
        last_modified = url_last_modified_map[normalized_url]
        matched_sitemap_url = normalized_url
        logger.debug(f"Normalized match found: {normalized_url}")
        
    # 3. Try substring matching (more robust but potentially less precise)
    else:
        matching_urls = [sitemap_url for sitemap_url in url_last_modified_map.keys() 
                         if normalized_url in sitemap_url or sitemap_url in normalized_url]
        
        if matching_urls:
            # Use the first matching URL
            matched_sitemap_url = matching_urls[0]
            last_modified = url_last_modified_map.get(matched_sitemap_url)
            logger.debug(f"Substring match found: {matched_sitemap_url} for {url}")
        else:
            logger.debug(f"No match found for URL {url} in sitemap map.")

    # Log the final matching result to console
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
    """
    Decide whether a URL should be processed further based on
    modification time and script settings. Logs the decision process.
    
    Args:
        url (str): The URL being processed.
        last_modified (datetime): The last modified datetime object (can be None).
        last_run_timestamp (datetime): Timestamp of the last script run (can be None).
        
    Returns:
        bool: True if URL processing should proceed, False otherwise.
    """
    logger.debug(f"Checking if URL should be processed: {url}")
    
    # Check if URL is blacklisted
    if url in BLACKLISTED_URLS:
        logger.info(f"URL {url} is blacklisted. Skipping processing.")
        print(f"\n=== URL PROCESSING STATUS ===")
        print(f"URL: {url}")
        print(f"Status: Blacklisted")
        print(f"Processing: SKIPPED")
        print(f"=============================\n")
        return False

    # If checking is disabled, always process
    if not CHECK_LAST_MODIFIED:
        logger.info(f"CHECK_LAST_MODIFIED is False. Proceeding with processing for {url}")
        print(f"\n=== URL PROCESSING STATUS ===")
        print(f"URL: {url}")
        print(f"CHECK_LAST_MODIFIED is disabled")
        print(f"Processing: WILL PROCEED")
        print("=============================\n")
        return True
    
    # If it's the first run, always process
    if not last_run_timestamp:
        logger.info(f"No last run timestamp available. Proceeding with processing for {url}")
        print(f"\n=== URL PROCESSING STATUS ===")
        print(f"URL: {url}")
        print(f"Last Run Timestamp: None (first run)")
        print(f"Processing: WILL PROCEED")
        print("=============================\n")
        return True
    
    # IMPORTANT CHANGE HERE:
    # If no last modified date is found in the sitemap, we'll SKIP the URL
    if last_modified is None:
        logger.info(f"No last modified date found for URL {url} in sitemap. Skipping processing.")
        print(f"\n=== URL PROCESSING STATUS ===")
        print(f"URL: {url}")
        print(f"Last Modified: Unknown (not found in sitemap)")
        print(f"Last Run: {last_run_timestamp}")
        print(f"Status: No last modified date found, skipping processing")
        print(f"Processing: SKIPPED")
        print("=============================\n")
        return False

    # Convert last_modified to UTC for safe comparison
    if last_modified.tzinfo is None:
        logger.warning(f"Last modified date for {url} ({last_modified}) is timezone-naive. Assuming UTC.")
        last_modified_utc = last_modified.replace(tzinfo=timezone.utc)
    else:
        last_modified_utc = last_modified.astimezone(timezone.utc)
        
    is_modified = last_modified_utc > last_run_timestamp
    
    # Log processing status to console
    print(f"\n=== URL PROCESSING STATUS ===")
    print(f"URL: {url}")
    print(f"Last Modified: {last_modified}")
    print(f"Last Run: {last_run_timestamp}")
    print(f"Is Modified Since Last Run: {'YES' if is_modified else 'NO'}")
    print(f"Processing: {'WILL PROCEED' if is_modified else 'SKIPPED'}")
    print("=============================\n")
    
    if is_modified:
        logger.info(f"URL {url} has been modified since last run ({last_modified_utc} > {last_run_timestamp}). Proceeding with processing.")
    else:
        logger.info(f"URL {url} has NOT been modified since last run ({last_modified_utc} <= {last_run_timestamp}). Skipping processing.")
        
    return is_modified

def main(skip_scraping, compile_only=False, process_missing=False, custom_urls=None):
    if compile_only:
        logger.info("Spouštím pouze kompilaci vyhledávacích dotazů")
        if COMPILE_SEARCH_QUERIES:
            compile_and_upload_question_tables() # Replaced call
        else:
            logger.warning("Kompilace vyhledávacích dotazů je vypnuta (COMPILE_SEARCH_QUERIES = False)")
        return

    start_time = datetime.now()
    logger.info(f"Začátek zpracování: {start_time}")

    if UPLOAD_IMMEDIATELY:
        logger.info("UPLOAD_IMMEDIATELY je True - přeskakuji veškeré zpracování a nahrávám existující payloady")
        payload_dir = "payloads"
        if not os.path.exists(payload_dir):
            logger.error(f"Adresář {payload_dir} neexistuje!")
            return
            
        # Get all files in the payload_dir
        all_files_in_payload_dir = []
        for entry in os.listdir(payload_dir):
            full_path = os.path.join(payload_dir, entry)
            if os.path.isfile(full_path):
                all_files_in_payload_dir.append(entry) # Add basename

        if not all_files_in_payload_dir:
            logger.warning(f"V adresáři {payload_dir} nebyly nalezeny žádné soubory k nahrání.") # Changed from error to warning
            return
            
        logger.info(f"Nalezeno {len(all_files_in_payload_dir)} souborů v adresáři '{payload_dir}' k nahrání")
        for file_basename in all_files_in_payload_dir: # Iterate over basenames
            file_path = os.path.join(payload_dir, file_basename) # Construct full path
            logger.info(f"Nahrávám soubor: {file_basename}")
            try:
                upload_to_voiceflow(file_path)
                time.sleep(VOICEFLOW_UPLOAD_DELAY) # Added delay
            except Exception as e:
                logger.error(f"Chyba při nahrávání souboru {file_basename}: {str(e)}")
        
        logger.info("Nahrávání dokončeno")
        return

    # Get the last run timestamp
    last_run_timestamp = get_last_run_timestamp()
    if last_run_timestamp:
        logger.info(f"Last run timestamp: {last_run_timestamp.isoformat()}")
    else:
        logger.info("No last run timestamp found, will process all URLs")

    # Initialize categorized_links dictionary
    categorized_links = {}

    if skip_scraping:
        logger.info("Přeskočit scraping, načítám payloady ze souborů")
        # Load existing data if skipping scraping
        categorized_links = load_payloads_from_files()
        # If skipping scraping, we assume we just want to upload the loaded files
        # The upload logic below will handle this.
    else:
        # Load existing payloads before scraping
        logger.info("Loading existing categorized links before scraping...")
        categorized_links = load_payloads_from_files()
        
        # Use either custom_urls passed to the function or the global CUSTOM_URLS
        urls_to_process = custom_urls if custom_urls is not None else CUSTOM_URLS
        
        # Check if custom URLs are provided
        if urls_to_process:
            logger.info(f"Found {len(urls_to_process)} custom URLs to process instead of sitemap")
            
            # Fetch XML sitemap data for last modified dates (still useful for custom URLs)
            url_last_modified_map = fetch_xml_sitemap()
            logger.info(f"Fetched last modified dates for {len(url_last_modified_map)} URLs")
            
            # Process custom URLs instead of scraping sitemap
            categorized_links = process_custom_urls(
                urls_to_process,
                categorized_links,
                url_last_modified_map,
                last_run_timestamp
            )
            
            # Save final results
            logger.info("Saving final categorized links to payload files...")
            save_payloads_to_files(categorized_links)
        else:
            # Standard sitemap processing
            logger.info(f"No custom URLs provided, proceeding with sitemap scraping from: {SITEMAP_URL}")

            try:
                # Use the specific Jina HTML function for sitemap
                html_content, _ = get_html_content_via_jina(SITEMAP_URL)
                if not html_content:
                    logger.error(f"Failed to get HTML sitemap content from {SITEMAP_URL}. Aborting.")
                    return

                main_menu = parse_menu(html_content)

                if not main_menu:
                    logger.error("Nepodařilo se najít hlavní menu na stránce.")
                    return
                
                # Fetch XML sitemap data
                url_last_modified_map = fetch_xml_sitemap()
                logger.info(f"Fetched last modified dates for {len(url_last_modified_map)} URLs")
                
                # Pass the loaded categorized_links dictionary to extract_links
                # It will be updated in place (or returned if you prefer immutable)
                categorized_links = extract_links(
                    main_menu, 
                    categorized_links=categorized_links,
                    url_last_modified_map=url_last_modified_map, 
                    last_run_timestamp=last_run_timestamp,
                    process_missing=process_missing
                )
                
                # Ukládání finálních payloadů do souborů PO SKONČENÍ extrakce
                logger.info("Saving final categorized links to payload files...")
                save_payloads_to_files(categorized_links)
            
            except Exception as e:
                logger.error(f"Došlo k chybě při zpracování: {str(e)}", exc_info=True)
                # Optionally save whatever was processed so far
                logger.warning("Attempting to save any processed data before exiting due to error...")
                save_payloads_to_files(categorized_links)
                return

    # Upload logic remains largely the same, but targets the files saved by save_payloads_to_files
    logger.info("Nahrávání dat do Voiceflow z finálních payload souborů")
    payload_dir = "payloads"
    if not os.path.exists(payload_dir):
        logger.error(f"Payload directory '{payload_dir}' does not exist. Cannot upload.")
    else:
        for filename in os.listdir(payload_dir):
            if filename.endswith('_table.json'): # Correct suffix check
                file_path = os.path.join(payload_dir, filename)
                logger.info(f"Uploading file: {filename}")
                try:
                    upload_to_voiceflow(file_path)
                    time.sleep(VOICEFLOW_UPLOAD_DELAY) # Added delay
                except Exception as e:
                    logger.error(f"Error uploading file {filename}: {str(e)}")

    # Add compilation of search queries if enabled
    if COMPILE_SEARCH_QUERIES:
        logger.info("Compiling search queries and uploading category question tables...") # Updated log
        compile_and_upload_question_tables() # Replaced call
    
    # Save the current timestamp as the last run timestamp
    save_last_run_timestamp()
    logger.info("Saved current timestamp as the last run timestamp")
    
    end_time = datetime.now()
    logger.info(f"Konec zpracování: {end_time}")
    logger.info(f"Celková doba zpracování: {end_time - start_time}")

def is_url_already_processed(url, category, title):
    """
    Check if a URL has already been processed by looking for its corresponding files.
    
    Args:
        url (str): The URL to check.
        category (str): The category of the URL.
        title (str): The title used in filename generation (not used anymore).
        
    Returns:
        bool: True if the URL has already been processed, False otherwise.
    """
    # Use the same URL-based naming logic as save_payload_to_file
    from urllib.parse import urlparse
    
    parsed_url = urlparse(url)
    url_path = parsed_url.path.strip('/')
    
    if not url_path:
        url_path = 'home'
    
    url_based_name = url_path.replace('/', '_')
    url_based_name = remove_accents(url_based_name)
    url_based_name = re.sub(r"[<>:\"/\\|?*]", "_", url_based_name)
    url_based_name = re.sub(r"\s+", "_", url_based_name)
    url_based_name = re.sub(r"_+", "_", url_based_name)
    url_based_name = url_based_name.strip("_").lower()  # Ensure lowercase for consistency
    url_based_name = url_based_name[:MAX_FILENAME_LENGTH]
    
    # Check for the Q/A extraction file
    qa_filename = f"payloads/{url_based_name}.json"
    
    return os.path.exists(qa_filename)

def process_custom_urls(custom_urls, categorized_links, url_last_modified_map, last_run_timestamp):
    """
    Process a list of custom URLs instead of scraping the sitemap.
    Maintains all the same functionality as the sitemap processing.
    
    Args:
        custom_urls (list): List of URLs to process
        categorized_links (dict): Dictionary holding the categorized links (used for initial loading and final state)
        url_last_modified_map (dict): Dictionary mapping URLs to their last modified dates
        last_run_timestamp (datetime): The timestamp of the last script run
        
    Returns:
        dict: The updated categorized_links dictionary
    """
    logger.info(f"Processing {len(custom_urls)} custom URLs instead of scraping sitemap")
    
    processed_categorized_links = {} # Store processed links for incremental saving

    for url in custom_urls:
        logger.info(f"\n=== Starting processing for custom URL: {url} ===")
        
        try:
            # 1. Find last modified date from sitemap.xml (if available)
            last_modified = find_url_last_modified(url, url_last_modified_map)
            
            # 2. Check if the URL should be processed based on modification date
            if not should_process_url(url, last_modified, last_run_timestamp):
                logger.info(f"Skipping all further processing for URL {url} as it hasn't changed.")
                continue
            
            # 3. Get metadata
            metadata = {"title": url.split("/")[-1], "url": url}
            
            # 4. Create path for categorization
            url_parts = url.replace(BASE_URL, "").strip("/").split("/")
            path = url_parts if url_parts and url_parts[0] else ["Home"]
            
            # 5. Categorize URL - Updated to use utility function
            category = categorize_link(path, CATEGORIES, LLM_PROVIDERS, LLM_SEQUENCE, MAX_RETRIES, INITIAL_RETRY_DELAY, API_CALL_DELAY)
            logger.info(f"Assigned category: {category} based on path")
            
            # 6. Generate RAG question - Updated to use utility function
            rag_question = generate_rag_question(path, LLM_PROVIDERS, LLM_SEQUENCE, MAX_RETRIES, INITIAL_RETRY_DELAY, API_CALL_DELAY)
            logger.info(f"Generated RAG question: {rag_question}")
            
            # Create readable title from URL
            title = url.split("/")[-1].replace("-", " ").replace("_", " ").capitalize()
            if not title:
                title = url.split("/")[-2] if len(url.split("/")) > 2 else "Homepage"
                
            # Create navigation path
            navigation = " > ".join([part.replace("-", " ").replace("_", " ").capitalize() for part in path])
                    
            link_data = {
                "Title": title,
                "URL": url,
                "Category": category,
                "Question": rag_question,
                "Navigation": navigation
            }
            
            # --- Update the main categorized_links dictionary --- 
            if category not in categorized_links:
                categorized_links[category] = []
                
            existing_entry_index = -1
            for i, entry in enumerate(categorized_links[category]):
                if entry.get("URL") == url:
                    existing_entry_index = i
                    break
            
            if existing_entry_index != -1:
                logger.info(f"Updating entry for URL: {url} in main dictionary (Category: {category})")
                categorized_links[category][existing_entry_index] = link_data
            else:
                logger.info(f"Adding new entry for URL: {url} to main dictionary (Category: {category})")
                categorized_links[category].append(link_data)
            # ------------------------------------------------------

            # --- Store processed link for incremental save --- 
            if category not in processed_categorized_links:
                processed_categorized_links[category] = []
            processed_categorized_links[category].append(link_data)
            # -------------------------------------------------

            # --- Save ONLY the currently processed link data incrementally --- 
            save_payloads_to_files({category: [link_data]}, category_to_save=category)
            # -------------------------------------------------------------------

            # 8. Q/A Processing - only if enabled
            if ENABLE_QA_PROCESSING:
                # ... (rest of Q/A processing logic remains the same) ...
                parsed_url = urlparse(url)
                domain = parsed_url.netloc.lower()
                is_sitemap = "mapa-stranek" in url.lower() or "sitemap" in url.lower()
                is_main_domain = domain == BASE_NETLOC or domain == NON_WWW_BASE_NETLOC
                
                if is_sitemap:
                    logger.info("Přeskakuji Q/A extrakci pro mapu stránek")
                elif not is_main_domain:
                    logger.info(f"Přeskakuji Q/A extrakci pro subdoménu nebo jinou doménu: {domain}")
                else:
                    logger.info("Spouštím Q/A extrakci (URL processing check passed)...")
                    # Corrected call: remove the None argument
                    process_url_content(url, category, metadata)
            else:
                logger.info("Q/A zpracování je vypnuto")
                
            logger.info(f"=== Dokončeno zpracování URL: {url} ===\n")
            
        except Exception as e:
            logger.error(f"Chyba při zpracování URL {url}: {str(e)}")
            logger.error("Pokračuji na další URL...")
    
    # Return the fully updated categorized_links dictionary for potential final save if needed
    return categorized_links

def sub_chunk_if_needed(chunk, identifier, max_limit):
    """Helper function to sub-chunk oversized text chunks."""
    if len(chunk) <= max_limit:
        return [chunk]

    logger.warning(f"Chunk starting with '{identifier[:50].replace(chr(10), ' ')}...' ({len(chunk)} chars) exceeds limit {max_limit}. Attempting sub-chunking.")
    sub_chunks = []

    # 1. Try splitting by Markdown Table Separators (|---|)
    # We look for the separator line itself to split *between* tables ideally
    table_separator_pattern = r'\n(\|\s*[:-]?---\s*)+\|?\n'
    # Find all start indices of the separator lines
    indices = [m.start() for m in re.finditer(table_separator_pattern, chunk)]

    if indices:
        logger.info(f"Attempting to sub-chunk '{identifier[:50].replace(chr(10), ' ')}...' based on table separators.")
        start_index = 0
        for match_start_index in indices:
            # Try to get the content *before* the separator
            sub = chunk[start_index:match_start_index].strip()
            if sub:
                # Recursively check/split this part if it's still too big
                sub_chunks.extend(sub_chunk_if_needed(sub, f"Table part before sep @{match_start_index}", max_limit))
            # Move start_index past the separator found at match_start_index
            # Find the end of the separator match
            sep_match = re.search(table_separator_pattern, chunk[match_start_index:])
            if sep_match:
                start_index = match_start_index + sep_match.end()
            else: # Should not happen, but break defensively
                start_index = match_start_index + 3 # Move past minimum separator length
                logger.warning("Separator end not found precisely, moving past conservatively.")

        # Add the last part after the final separator
        last_part = chunk[start_index:].strip()
        if last_part:
            sub_chunks.extend(sub_chunk_if_needed(last_part, f"Final table part after sep @{start_index}", max_limit))

        # If splitting by tables resulted in more than one chunk OR the single resulting chunk is smaller
        if len(sub_chunks) > 1 or (len(sub_chunks) == 1 and len(sub_chunks[0]) < len(chunk)):
             # Check if any sub-chunk is STILL too large
             if not any(len(sc) > max_limit for sc in sub_chunks):
                 logger.info(f"Successfully sub-chunked by tables into {len(sub_chunks)} parts below limit.")
                 return sub_chunks
             else:
                 logger.warning(f"Sub-chunking by tables for '{identifier[:50].replace(chr(10), ' ')}...' resulted in some parts still exceeding limit. Falling back to paragraph split.")
                 # Clear sub_chunks to proceed to paragraph splitting
                 sub_chunks = []
        else:
             logger.info(f"Splitting by tables for '{identifier[:50].replace(chr(10), ' ')}...' did not result in smaller/multiple chunks. Proceeding to paragraph split.")
             sub_chunks = [] # Ensure fallback happens

    # 2. Fallback: Split by Paragraphs if table splitting didn't work or yielded oversized chunks
    if not sub_chunks: # Proceed only if table splitting failed or wasn't applicable/effective
        logger.info(f"Attempting to sub-chunk '{identifier[:50].replace(chr(10), ' ')}...' based on paragraphs.")
        paragraphs = re.split(r'\n\s*\n', chunk)
        current_sub_chunk = ""
        temp_paragraph_chunks = []

        for para in paragraphs:
            para_stripped = para.strip()
            if not para_stripped:
                continue

            # If adding the next paragraph exceeds the limit...
            if current_sub_chunk and len(current_sub_chunk) + len(para_stripped) + 2 > max_limit:
                temp_paragraph_chunks.append(current_sub_chunk)
                current_sub_chunk = para_stripped
            # If a single paragraph itself exceeds the limit...
            elif not current_sub_chunk and len(para_stripped) > max_limit:
                # If there was a previous chunk, add it first
                if current_sub_chunk:
                     temp_paragraph_chunks.append(current_sub_chunk)
                # Add the oversized paragraph as its own chunk
                logger.warning(f"Single paragraph within '{identifier[:50].replace(chr(10), ' ')}...' exceeds limit ({len(para_stripped)} chars). Adding as its own chunk.")
                temp_paragraph_chunks.append(para_stripped)
                current_sub_chunk = "" # Reset
            # Otherwise, append the paragraph to the current sub-chunk
            else:
                if current_sub_chunk:
                    current_sub_chunk += "\n\n" + para_stripped
                else:
                    current_sub_chunk = para_stripped

        if current_sub_chunk:
            temp_paragraph_chunks.append(current_sub_chunk)

        # Check if paragraph splitting actually reduced the size or created chunks
        if temp_paragraph_chunks and (len(temp_paragraph_chunks) > 1 or len(temp_paragraph_chunks[0]) < len(chunk)):
             # Check if any sub-chunk is STILL too large
             if not any(len(sc) > max_limit for sc in temp_paragraph_chunks):
                  logger.info(f"Successfully sub-chunked by paragraphs into {len(temp_paragraph_chunks)} parts below limit.")
                  return temp_paragraph_chunks
             else:
                  logger.warning(f"Sub-chunking by paragraphs for '{identifier[:50].replace(chr(10), ' ')}...' resulted in some parts still exceeding limit. Returning these parts.")
                  return temp_paragraph_chunks # Return the paragraph chunks even if some are large
        else:
             # If splitting by paragraph didn't help
             logger.error(f"Failed to sub-chunk '{identifier[:50].replace(chr(10), ' ')}...' effectively using tables or paragraphs. Returning original oversized chunk.")
             return [chunk] # Return the original chunk as a last resort

    return sub_chunks # Should only be reached if table splitting worked and was sufficient

def chunk_content(content):
    """
    Split content into chunks based on Markdown headers (H1-H6),
    and further sub-chunk if any chunk exceeds MAX_CHUNK_CHAR_LIMIT,
    prepending the original header to sub-chunks.
    """
    # Use the global constant
    global MAX_CHUNK_CHAR_LIMIT

    processed_chunks = []
    if not content or not content.strip():
        logger.warning("Empty or None content received, skipping chunking")
        return []

    logger.info(f"Content length before chunking: {len(content)} characters")

    # Pattern to find headers (H1-H6) at the start of a line
    header_pattern = re.compile(r'^(#{1,6}\s+[^\n]+)\n?', re.MULTILINE)
    matches = list(header_pattern.finditer(content))

    primary_chunks = []
    last_end = 0

    # Add content before the first header if it exists
    if matches and matches[0].start() > 0:
        chunk = content[0:matches[0].start()].strip()
        if chunk:
            primary_chunks.append(("", chunk)) # No header associated
    elif not matches: # No headers found at all
         chunk = content.strip()
         if chunk:
             primary_chunks.append(("", chunk)) # No header associated

    # Process content associated with each header
    for i, match in enumerate(matches):
        start = match.start()
        header_text = match.group(1).strip() # Store the header text itself

        # Determine the end of the current chunk's content
        content_start = match.end()
        content_end = matches[i+1].start() if i + 1 < len(matches) else len(content)

        chunk_content_associated_with_header = content[content_start:content_end].strip()

        # Store header and its content together
        primary_chunks.append((header_text, chunk_content_associated_with_header))

    # Now, process primary_chunks, applying sub-chunking and prepending header if needed
    final_chunks = []
    for header, chunk_text in primary_chunks:
        # Reconstruct the full chunk text including header for size check
        full_chunk_text = (header + "\n\n" + chunk_text).strip() if header else chunk_text
        identifier = header if header else "Initial/Full content"

        if len(full_chunk_text) > MAX_CHUNK_CHAR_LIMIT:
            # Pass the full chunk text to the sub-chunker
            sub_chunks = sub_chunk_if_needed(full_chunk_text, identifier, MAX_CHUNK_CHAR_LIMIT)
            # --- MODIFICATION START ---
            # Prepend original header to each sub-chunk if a header existed
            for sub_chunk in sub_chunks:
                if header: # Only prepend if there was an original header
                    final_chunks.append((header + "\n\n" + sub_chunk).strip())
                else: # If the original oversized chunk had no header, add sub-chunk as is
                    final_chunks.append(sub_chunk)
            # --- MODIFICATION END ---
        elif full_chunk_text: # Add if not empty and within limit
            final_chunks.append(full_chunk_text)

    # Log final chunk details
    logger.info(f"Final number of chunks after potential sub-chunking: {len(final_chunks)}")
    for i, chunk in enumerate(final_chunks):
        logger.info(f"--- Final Chunk {i+1}/{len(final_chunks)} --- ({len(chunk)} characters) ---")
        preview_start = chunk[:200].replace('\n', ' ')
        preview_end = chunk[-100:].replace('\n', ' ')
        logger.info(f"Chunk {i+1} Preview: {preview_start}... ...{preview_end}")

    return final_chunks

def preprocess_markdown_content(content):
    """Clean and simplify markdown content before sending to Claude"""
    # Basic cleanup for markdown - more might be needed depending on source
    
    # Replace common HTML special characters that might remain
    html_special_chars = {
        '&#160;': ' ',  # non-breaking space
        '&nbsp;': ' ',  # non-breaking space
        '&lt;': '<',    # less than
        '&gt;': '>',    # greater than
        '&amp;': '&',   # ampersand
        '&quot;': '"',  # quotation mark
        '&apos;': "'",  # apostrophe
        '&#8217;': "'", # right single quotation mark
        '&#8220;': '"', # left double quotation mark
        '&#8221;': '"', # right double quotation mark
        '&#8211;': '-', # en dash
        '&#8212;': '--' # em dash
    }
    
    for char, replacement in html_special_chars.items():
        content = content.replace(char, replacement)
    
    # Remove any remaining HTML entity references (like &#xxxx;)
    content = re.sub(r'&#\d+;', '', content)
    
    # Normalize whitespace - replace multiple spaces/newlines with single ones
    # Be careful not to destroy markdown structure too much
    content = re.sub(r'[ \t]+', ' ', content) # Replace multiple spaces/tabs with single space
    content = re.sub(r'\n{3,}', '\n\n', content) # Replace 3+ newlines with 2 (preserve paragraphs)
    
    # Remove leading/trailing whitespace from each line
    lines = [line.strip() for line in content.split('\n')]
    content = '\n'.join(lines)
    
    # Remove lines that are purely whitespace
    content = re.sub(r'^\s*$\n', '', content, flags=re.MULTILINE)
    
    return content.strip()

def save_contacts_payload(url, contact_items, category):
    """
    Saves extracted contact items to a JSON file for Voiceflow.
    Filename: {category}_{url_based_name}_contacts_table.json
    """
    output_dir = "payloads"
    os.makedirs(output_dir, exist_ok=True)

    parsed_url = urlparse(url)
    url_path = parsed_url.path.strip('/')
    if not url_path:
        url_path = 'home'
    
    url_based_name = url_path.replace('/', '_')
    url_based_name = remove_accents(url_based_name)
    url_based_name = re.sub(r"[<>:\"/\\|?*]", "_", url_based_name)
    url_based_name = re.sub(r"\s+", "_", url_based_name)
    url_based_name = re.sub(r"_+", "_", url_based_name)
    url_based_name = url_based_name.strip("_").lower()
    url_based_name = url_based_name[:MAX_FILENAME_LENGTH]

    # Use the original category for filename uniqueness, but data category will be 'Kontakt'
    filename_category_prefix = category.lower()
    table_name = f"{filename_category_prefix}_{url_based_name}_contacts_table"
    filename = f"{output_dir}/{table_name}.json"

    # Ensure all required fields for the schema are present in items, adding them as empty strings if missing.
    expected_fields = ["FirstName", "LastName", "FullName", "Title", "Role", "Department", 
                       "Subdepartment", "Email", "PhoneNumber", "ProfileURL", "Office", "OfficeURL", "Origin"]

    processed_items = []
    for item in contact_items:
        if not isinstance(item, dict):
            logger.warning(f"Skipping non-dictionary item in contact_items for {url}: {item}")
            continue
        
        new_item = {}
        for field in expected_fields:
            new_item[field] = item.get(field, "") # Default to empty string if missing
        new_item["Category"] = "Kontakt" # Override category to 'Kontakt' for all contact items
        new_item["Type"] = "Data"  # Add static Type field
        processed_items.append(new_item)

    payload = {
        "data": {
            "schema": {
                "searchableFields": ["FirstName", "LastName", "FullName", "Title", "Role", "Department", "Subdepartment", "Email", "PhoneNumber", "Office", "OfficeURL", "Origin"],
                "metadataFields": ["Category", "FirstName", "LastName", "PhoneNumber", "Email", "Type", "Origin"]
            },
            "name": table_name,
            "items": processed_items
        }
    }

    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved contacts payload for URL '{url}' ({len(processed_items)} items) to file: {filename}")
        return filename
    except Exception as e:
        logger.error(f"Error writing contacts payload file {filename}: {str(e)}")
        return None

def detect_markdown_tables(content):
    """
    Detects markdown tables in content and returns them with their positions.
    
    Args:
        content (str): The markdown content to analyze
        
    Returns:
        list: List of tuples containing (table_content, start_pos, end_pos, table_index)
    """
    # Enhanced regex pattern to detect markdown tables
    table_pattern = re.compile(
        r'(\|[^\n]*\|[ \t]*\n(?:\|[ \t]*[-:]+[ \t]*)+\|[ \t]*\n(?:\|[^\n]*\|[ \t]*\n)*)',
        re.MULTILINE
    )
    
    tables = []
    for match_index, match in enumerate(table_pattern.finditer(content)):
        table_content = match.group(1).strip()
        start_pos = match.start()
        end_pos = match.end()
        tables.append((table_content, start_pos, end_pos, match_index + 1))
    
    logger.info(f"Detected {len(tables)} markdown tables in content")
    return tables

def extract_table_rows(table_content):
    """
    Extracts individual rows from a markdown table.
    
    Args:
        table_content (str): The markdown table content
        
    Returns:
        tuple: (headers, data_rows) where headers is a list and data_rows is a list of row strings
    """
    lines = table_content.strip().split('\n')
    if len(lines) < 3:  # Need at least header, separator, and one data row
        logger.warning("Table has insufficient lines for proper parsing")
        return [], []
    
    # Extract headers from first line
    header_line = lines[0]
    headers = []
    if header_line.startswith('|') and header_line.endswith('|'):
        headers = [h.strip() for h in header_line[1:-1].split('|')]
    
    # Find data rows (skip header and separator lines)
    data_rows = []
    for i, line in enumerate(lines):
        if i == 0:  # Skip header
            continue
        if re.match(r'^\|\s*[-:]+\s*(\|\s*[-:]+\s*)*\|?\s*$', line):  # Skip separator
            continue
        if line.strip().startswith('|') and line.strip().endswith('|'):
            data_rows.append(line.strip())
    
    logger.info(f"Extracted {len(headers)} headers and {len(data_rows)} data rows from table")
    return headers, data_rows

def create_row_context(headers, row, parent_context, table_index, row_index, total_rows):
    """
    Creates contextual information for a table row to be used in extractions.
    
    Args:
        headers (list): Table column headers
        row (str): The raw markdown row string
        parent_context (dict): Parent page context (title, category, url, etc.)
        table_index (int): Index of the table in the page
        row_index (int): Index of the row in the table
        total_rows (int): Total number of rows in the table
        
    Returns:
        tuple: (formatted_row_content, context_description)
    """
    # Clean row and extract cell values
    if row.startswith('|') and row.endswith('|'):
        cells = [cell.strip() for cell in row[1:-1].split('|')]
    else:
        cells = [cell.strip() for cell in row.split('|')]
    
    # Create structured row representation
    formatted_content = f"Table {table_index} - Row {row_index + 1}/{total_rows}:\n"
    
    # Add headers if available
    if headers:
        formatted_content += "Headers: " + " | ".join(headers) + "\n"
        formatted_content += "Values: " + " | ".join(cells) + "\n\n"
        
        # Create key-value pairs
        formatted_content += "Row Details:\n"
        for header, cell in zip(headers, cells):
            if cell:  # Only include non-empty cells
                formatted_content += f"- {header}: {cell}\n"
    else:
        formatted_content += "Row Data: " + " | ".join(cells) + "\n"
    
    # Create context description
    context_description = (
        f"Table {table_index}, Row {row_index + 1} from page '{parent_context.get('title', 'Unknown Page')}' "
        f"(Category: {parent_context.get('category', 'Unknown')}, URL: {parent_context.get('url', 'Unknown')})"
    )
    
    return formatted_content, context_description

def process_url_content(url, category, metadata):
    """Processes the markdown content of a single URL to extract Q/A pairs
    and potentially contact information and resolution information.
    Enhanced to detect tables and process each row individually.

    Args:
        url (str): The URL to process.
        category (str): The assigned category.
        metadata (dict): Metadata associated with the URL (e.g., title).
    """
    accumulated_pairs_this_run = [] # Accumulate Q/A pairs for this run
    accumulated_contacts_this_run = [] # Initialize accumulated contacts for this URL run
    accumulated_resolutions_this_run = [] # Initialize accumulated resolutions for this URL run
    
    final_qa_filename = None # Store the name of the file to be uploaded
    final_contacts_filename = None # Store the name of the latest successfully saved contacts file
    final_resolutions_filename = None # Store the name of the latest successfully saved resolutions file

    # Flags to track if files have been initialized for the current URL
    contacts_file_initialized = False
    resolutions_file_initialized = False

    # Initialize default date/year variables for resolution extraction (extracted only when needed)
    default_rok = None
    default_datum_konani = None
    default_values_extracted = False  # Flag to ensure we only extract once per URL

    try:
        # --- Step 1: Clear/Create Q/A file at the start of processing this URL ---
        logger.info(f"Initializing/Clearing Q/A file for URL: {url}")
        initial_qa_filename = save_payload_to_file(url, [], category)
        if not initial_qa_filename:
            logger.error(f"Failed to initialize/clear Q/A file for {url}. Aborting Q/A extraction for this URL.")
            return
        logger.info(f"Successfully initialized/cleared Q/A file: {initial_qa_filename}")
        final_qa_filename = initial_qa_filename
        # Note: Contact and Resolution files are now initialized conditionally later.
        # ----------------------------------------------------------------------

        # --- Step 2: Get Content ---
        try:
            # Specify markdown format and the selectors needed for Q/A
            qa_content, fetched_metadata = get_content(
                url,
                content_format="markdown"
                # target_selector is omitted, so it defaults;
                # but for markdown, get_content will use MARKDOWN_CONTENT_SELECTORS
            )
            if not qa_content:
                logger.error(f"Failed to retrieve markdown content for URL {url} from all providers.")
                raise ValueError("No markdown content returned from get_content")

            if fetched_metadata and fetched_metadata.get('title'):
                 metadata['title'] = fetched_metadata['title'] # Update title

        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout during Q/A content retrieval for URL {url}: {str(e)}")
            print(f"\n=== Q/A EXTRACTION ERROR ===\nURL: {url}\nError: Timeout during content retrieval\nQ/A Extraction: SKIPPED\n============================\n")
            return # Skip Q/A extraction for this URL
        except Exception as e:
            logger.error(f"Error during Q/A content retrieval for URL {url}: {str(e)}")
            print(f"\n=== Q/A EXTRACTION ERROR ===\nURL: {url}\nError: {str(e)}\nQ/A Extraction: SKIPPED\n============================\n")
            return # Skip Q/A extraction for this URL
        # ---------------------------

        qa_content = preprocess_markdown_content(qa_content)
        
        # --- NEW: Table Detection and Row Extraction ---
        detected_tables = detect_markdown_tables(qa_content)
        
        # Create parent context for table row processing
        parent_context = {
            'title': metadata.get('title', 'Unknown Page'),
            'category': category,
            'url': url
        }
        
        # Prepare content for processing: regular chunks + table rows
        content_to_process_list = []
        
        if ENABLE_CONTENT_CHUNKING:
            content_chunks = chunk_content(qa_content)
            logger.info(f"Content chunking enabled. Split content into {len(content_chunks)} chunks for processing.")
            # Removed pre_table_analysis_result from tuple element
            content_to_process_list = [(chunk, f" (part {i+1}/{len(content_chunks)})", "chunk") for i, chunk in enumerate(content_chunks)]
        else:
            logger.info("Content chunking disabled. Processing entire content as a single unit.")
            # Removed pre_table_analysis_result from tuple element
            content_to_process_list = [(qa_content, " (entire content)", "chunk")]

        # REMOVED: All logic related to pre-table context analysis, including:
        # - table_context_cache initialization
        # - Loop calling analyze_pre_table_context for each table
        # - Caching of pre_table_analysis_result
        # - All console output and logging related to pre-table context

        # Add table rows as processing units (without pre-table context analysis)
        for table_content, start_pos, end_pos, table_index in detected_tables:
            headers, data_rows = extract_table_rows(table_content)
            
            if data_rows:
                logger.info(f"Adding Table {table_index} with {len(data_rows)} rows to processing list.")
                # Removed pre_table_analysis_result from tuple element
                for row_index, row in enumerate(data_rows):
                    formatted_row_content, context_description = create_row_context(
                        headers, row, parent_context, table_index, row_index, len(data_rows)
                    )
                    enhanced_content = f"""PARENT CONTEXT:
Page: {parent_context['title']}
Category: {parent_context['category']}
URL: {parent_context['url']}

{formatted_row_content}"""
                    processing_info = f" (table {table_index} row {row_index + 1}/{len(data_rows)})"
                    # Removed pre_table_analysis_result from tuple element
                    content_to_process_list.append((enhanced_content, processing_info, "table_row"))

        logger.info(f"Total processing units: {len(content_to_process_list)}")

        # --- Step 3: Process Content (Chunks + Table Rows) Incrementally ---
        for i, processing_unit in enumerate(content_to_process_list):
            # Unpack processing unit (now always 3 elements)
            content_item, part_info, content_type = processing_unit
            # REMOVED: pre_table_analysis_result_for_unit from unpacking, as it's no longer in the tuple
                
            processing_unit_description = f"Processing unit {i+1}/{len(content_to_process_list)}{part_info} ({len(content_item)} characters, type: {content_type})"
            logger.info(processing_unit_description)
            
            # Q/A EXTRACTION - Skip for table rows, only process for regular content chunks
            item_qa_pairs = []
            contains_contacts = False
            contains_resolutions = False
            
            if content_type == "chunk":
                # Process Q/A extraction for regular content chunks (including entire table content)
                logger.info(f"Processing Q/A extraction for content chunk {i+1}{part_info}")
                # Updated to use utility function with required parameters
                item_qa_pairs, contains_contacts, contains_resolutions = convert_to_qa(
                    content_item, 
                    metadata.get('title', 'Unknown Page') + part_info, 
                    category,
                    LLM_PROVIDERS,
                    LLM_SEQUENCE,
                    MAX_RETRIES,
                    INITIAL_RETRY_DELAY,
                    API_CALL_DELAY
                )
            elif content_type == "table_row":
                # Skip Q/A extraction for table rows, but still assess for contacts and resolutions
                logger.info(f"Skipping Q/A extraction for table row {i+1}{part_info} (table rows only processed for contacts/resolutions)")
                
                # Use LLM-based assessment for table rows
                logger.info(f"Using LLM assessment for table row {i+1}{part_info}")
                contains_contacts, contains_resolutions = assess_content_type(
                    content_item,
                    metadata.get('title', 'Unknown Page') + part_info,
                    LLM_PROVIDERS,
                    LLM_SEQUENCE,
                    MAX_RETRIES,
                    INITIAL_RETRY_DELAY,
                    API_CALL_DELAY
                )
                
                logger.info(f"LLM assessment for table row {i+1}{part_info}: contacts={contains_contacts}, resolutions={contains_resolutions}")

            # If conversion succeeded and returned pairs for this item (only for chunks)
            if item_qa_pairs:
                success_message = f"Successfully extracted {len(item_qa_pairs)} Q/A pairs from {processing_unit_description}"
                logger.info(success_message)
                accumulated_pairs_this_run.extend(item_qa_pairs) # Add pairs from this item

                # Deduplicate the *current* accumulated list
                seen_qa = set()
                deduplicated_items = []
                for item_dedup in reversed(accumulated_pairs_this_run): # Renamed 'item' to 'item_dedup' to avoid conflict
                     qa_tuple = (item_dedup.get('Question'), item_dedup.get('Answer'))
                     if qa_tuple not in seen_qa:
                         seen_qa.add(qa_tuple)
                         deduplicated_items.append(item_dedup)
                accumulated_pairs_this_run = list(reversed(deduplicated_items))
                
                total_items_message = f"Total accumulated items after processing unit {i+1} and deduplicating: {len(accumulated_pairs_this_run)}"
                logger.info(total_items_message)

                # Save the *current accumulated* list, overwriting the previous state of the file
                incremental_filename = save_payload_to_file(url, accumulated_pairs_this_run, category)

                # Update the final filename if save was successful
                if incremental_filename:
                    final_qa_filename = incremental_filename
                else:
                    # Log error if saving failed, but continue processing other items
                    logger.error(f"Failed to save incremental payload after processing unit {i+1} for URL {url}. Upload might use older file state if no further saves succeed.")
            elif content_type == "chunk":
                # Log failure for this specific chunk (not for table rows since we skip Q/A for them)
                failure_message = f"Failed to extract Q/A pairs from processing unit {i+1}{part_info} or no pairs found."
                logger.warning(failure_message)

            # --- Contact Extraction and Accumulation (Modified Step) ---
            if contains_contacts: # LLM assessed this chunk might have contacts OR it's a table row
                logger.info(f"Contact assessment positive for processing unit {i+1}{part_info}. Attempting contact extraction from this unit.")
                
                # --- Conditional First-Time Initialization for Contacts File ---
                if not contacts_file_initialized:
                    logger.info(f"First contact assessment for URL {url}. Initializing/Clearing Contacts file.")
                    # Pass the original category for filename consistency
                    initial_contacts_filename_on_demand = save_contacts_payload(url, [], category)
                    if not initial_contacts_filename_on_demand:
                        logger.error(f"Failed to initialize/clear Contacts file for {url} on demand. Contact extraction might be skipped.")
                        # If init fails, we can't proceed with saving contacts for this URL
                    else:
                        logger.info(f"Successfully initialized/cleared Contacts file on demand: {initial_contacts_filename_on_demand}")
                        final_contacts_filename = initial_contacts_filename_on_demand
                        contacts_file_initialized = True
                # -------------------------------------------------------------

                if contacts_file_initialized: # Proceed only if file was successfully initialized
                    chunk_contact_items = extract_contact_details_llm(
                        content_item, 
                        metadata.get('title', 'Unknown Page') + part_info, 
                        category, 
                        url,
                        LLM_PROVIDERS,
                        LLM_SEQUENCE,
                        MAX_RETRIES,
                        INITIAL_RETRY_DELAY,
                        API_CALL_DELAY
                    )
                    
                    if chunk_contact_items:
                        logger.info(f"Extracted {len(chunk_contact_items)} new contact items from processing unit {i+1}{part_info}.")
                        accumulated_contacts_this_run.extend(chunk_contact_items)

                        # Improved contact deduplication logic
                        seen_contacts_by_email = {}
                        seen_contacts_by_name_phone = {}
                        deduplicated_contacts_list = []
                        
                        for contact_to_dedup in reversed(accumulated_contacts_this_run):
                            email = contact_to_dedup.get('Email', '').strip()
                            full_name = contact_to_dedup.get('FullName', '').strip()
                            phone = contact_to_dedup.get('PhoneNumber', '').strip()
                            
                            # Primary deduplication by email (most reliable)
                            if email and email != '':
                                if email in seen_contacts_by_email:
                                    # Keep the contact with more complete information
                                    existing_contact = seen_contacts_by_email[email]
                                    current_score = sum(1 for field in ['FirstName', 'LastName', 'Role', 'Department', 'PhoneNumber'] 
                                                      if contact_to_dedup.get(field, '').strip())
                                    existing_score = sum(1 for field in ['FirstName', 'LastName', 'Role', 'Department', 'PhoneNumber'] 
                                                       if existing_contact.get(field, '').strip())
                                    
                                    if current_score > existing_score:
                                        # Replace with more complete contact
                                        seen_contacts_by_email[email] = contact_to_dedup
                                        # Remove the old one from the list and add the new one
                                        deduplicated_contacts_list = [c for c in deduplicated_contacts_list if c.get('Email', '') != email]
                                        deduplicated_contacts_list.append(contact_to_dedup)
                                    # else: keep the existing one, skip current
                                else:
                                    seen_contacts_by_email[email] = contact_to_dedup
                                    deduplicated_contacts_list.append(contact_to_dedup)
                            else:
                                # Secondary deduplication by name + phone for contacts without email
                                name_phone_key = (full_name.lower(), phone)
                                if name_phone_key not in seen_contacts_by_name_phone and full_name:
                                    seen_contacts_by_name_phone[name_phone_key] = contact_to_dedup
                                    deduplicated_contacts_list.append(contact_to_dedup)
                                # else: skip duplicate
                        
                        accumulated_contacts_this_run = list(reversed(deduplicated_contacts_list))
                        logger.info(f"Total accumulated contact items after processing unit {i+1} and deduplicating: {len(accumulated_contacts_this_run)}")
                        
                        incremental_contacts_filename = save_contacts_payload(url, accumulated_contacts_this_run, category)
                        if incremental_contacts_filename:
                            final_contacts_filename = incremental_contacts_filename 
                            logger.info(f"Incrementally saved {len(accumulated_contacts_this_run)} contacts to {incremental_contacts_filename}")
                        else:
                            logger.error(f"Failed to save incremental contacts payload for URL {url} after processing unit {i+1}. File for upload might be outdated or missing.")
                    else: 
                        logger.info(f"Contact extraction from unit {i+1}{part_info} yielded no new contact items.")
                else:
                    logger.warning(f"Skipping contact processing for unit {i+1}{part_info} as Contacts file was not initialized (likely due to an earlier error).")
            
            elif not contacts_file_initialized: # Only log if no contacts found AND file wasn't even initialized yet
                 logger.info(f"Contact assessment negative for processing unit {i+1}{part_info} and no contacts found yet for {url}. Skipping contact extraction for this unit.")


            # --- Resolution Extraction and Accumulation (Modified Step) ---
            if contains_resolutions: # LLM assessed this chunk might have resolutions OR it's a table row
                logger.info(f"Resolution assessment positive for processing unit {i+1}{part_info}. Attempting resolution extraction from this unit.")

                # --- Conditional First-Time Initialization for Resolutions File ---
                if not resolutions_file_initialized:
                    logger.info(f"First resolution assessment for URL {url}. Initializing/Clearing Resolutions file.")
                    initial_resolutions_filename_on_demand = save_resolutions_payload(url, [], category)
                    if not initial_resolutions_filename_on_demand:
                        logger.error(f"Failed to initialize/clear Resolutions file for {url} on demand. Resolution extraction might be skipped.")
                    else:
                        logger.info(f"Successfully initialized/cleared Resolutions file on demand: {initial_resolutions_filename_on_demand}")
                        final_resolutions_filename = initial_resolutions_filename_on_demand
                        resolutions_file_initialized = True
                # --------------------------------------------------------------
                
                if resolutions_file_initialized: # Proceed only if file was successfully initialized
                    if not default_values_extracted:
                        logger.info(f"First resolution extraction for URL {url}. Extracting default date/year values from content.")
                        default_rok, default_datum_konani = extract_default_date_year_from_text(
                            qa_content,
                            metadata.get('title', 'Unknown Page'),
                            LLM_PROVIDERS,
                            LLM_SEQUENCE,
                            MAX_RETRIES,
                            INITIAL_RETRY_DELAY,
                            API_CALL_DELAY
                        )
                        default_values_extracted = True
                        
                        if default_rok or default_datum_konani:
                            logger.info(f"Successfully extracted default values for resolution processing: Rok='{default_rok}', DatumKonani='{default_datum_konani}'")
                        else:
                            logger.info(f"No default date/year values found for resolution processing in URL {url}")
                    
                    chunk_resolution_items = extract_resolution_details_llm(
                        content_item, 
                        metadata.get('title', 'Unknown Page') + part_info, 
                        category, 
                        url,
                        LLM_PROVIDERS,
                        LLM_SEQUENCE,
                        MAX_RETRIES,
                        INITIAL_RETRY_DELAY,
                        API_CALL_DELAY,
                        default_rok,
                        default_datum_konani
                    )
                    
                    if chunk_resolution_items:
                        logger.info(f"Extracted {len(chunk_resolution_items)} new resolution items from processing unit {i+1}{part_info}.")
                        accumulated_resolutions_this_run.extend(chunk_resolution_items)

                        seen_resolutions_tuples = set()
                        deduplicated_resolution_list = []
                        unique_resolution_keys_for_dedup = ("Popis", "CisloUsneseni", "SubCategory") 
                        for resolution_to_dedup in reversed(accumulated_resolutions_this_run):
                            resolution_tuple_for_dedup = tuple(resolution_to_dedup.get(key, "") for key in unique_resolution_keys_for_dedup)
                            if resolution_tuple_for_dedup not in seen_resolutions_tuples:
                                seen_resolutions_tuples.add(resolution_tuple_for_dedup)
                                deduplicated_resolution_list.append(resolution_to_dedup)
                        accumulated_resolutions_this_run = list(reversed(deduplicated_resolution_list))
                        logger.info(f"Total accumulated resolution items after processing unit {i+1} and deduplicating: {len(accumulated_resolutions_this_run)}")
                        
                        incremental_resolutions_filename = save_resolutions_payload(url, accumulated_resolutions_this_run, category)
                        if incremental_resolutions_filename:
                            final_resolutions_filename = incremental_resolutions_filename
                            logger.info(f"Incrementally saved {len(accumulated_resolutions_this_run)} resolutions to {incremental_resolutions_filename}")
                        else:
                            logger.error(f"Failed to save incremental resolutions payload for URL {url} after processing unit {i+1}. File for upload might be outdated or missing.")
                    else: 
                        logger.info(f"Resolution extraction from unit {i+1}{part_info} yielded no new resolution items.")
                else:
                    logger.warning(f"Skipping resolution processing for unit {i+1}{part_info} as Resolutions file was not initialized (likely due to an earlier error).")

            elif not resolutions_file_initialized: # Only log if no resolutions found AND file wasn't even initialized yet
                logger.info(f"Resolution assessment negative for processing unit {i+1}{part_info} and no resolutions found yet for {url}. Skipping resolution extraction for this unit.")

        # --- End Content Processing Loop ---

        # --- Step 4: Handle Fallback (only if NO pairs were ever accumulated for Q/A) ---
        if not accumulated_pairs_this_run:
            logger.warning(f"No QA pairs were accumulated from any processing unit for {url}. Creating fallback QA pair.")
            title = metadata.get('title', 'Unknown page')
            fallback_qa_pairs = [{
                "Question": f"Co najdu na stránce {title}? | What can I find on the {title} page? | Jaké informace obsahuje stránka {title}?",
                "Answer": f"Na stránce najdete informace o {title}. Pro podrobnosti navštivte přímo [webovou stránku]({url}).",
                "Category": category
            }]

            # Save the fallback payload (this will overwrite the initial empty file or last saved state)
            fallback_filename = save_payload_to_file(url, fallback_qa_pairs, category)

            # Update the final filename if fallback save was successful
            if fallback_filename:
                final_qa_filename = fallback_filename
            else:
                 logger.error(f"Failed to save fallback payload for URL {url}. No file will be uploaded.")
                 final_qa_filename = None # Ensure no upload happens if fallback save fails

        # --- Step 5: Final Upload and Save for all data types (Q/A, Contacts, Resolutions) ---
        logger.info(f"All processing units handled for URL {url}. Starting final save and upload process.")
        
        # Upload Q/A file if available
        if final_qa_filename and os.path.exists(final_qa_filename):
            logger.info(f"Uploading final Q/A file: {final_qa_filename}")
            upload_to_voiceflow(final_qa_filename)
            time.sleep(VOICEFLOW_UPLOAD_DELAY)
        else:
            if accumulated_pairs_this_run or (not accumulated_pairs_this_run and "fallback_qa_pairs" in locals()):
                 logger.error(f"Q/A data might exist for URL {url}, but file '{final_qa_filename}' is not available for upload (it might be None or not exist).")
            else:
                 logger.info(f"No Q/A data/file to upload for URL {url}.")

        # Save and upload contacts if any were accumulated
        if accumulated_contacts_this_run:
            # The file at final_contacts_filename already contains all accumulated and deduplicated contacts.
            if final_contacts_filename and os.path.exists(final_contacts_filename):
                logger.info(f"Uploading final contacts file: {final_contacts_filename} containing {len(accumulated_contacts_this_run)} items.")
                upload_to_voiceflow(final_contacts_filename)
                time.sleep(VOICEFLOW_UPLOAD_DELAY)
            else:
                logger.error(f"Contact items were accumulated for URL {url} ({len(accumulated_contacts_this_run)} items), but the file '{final_contacts_filename}' is not available for upload. Last incremental save might have failed or file does not exist.")
        else:
            logger.info(f"No contact items accumulated to upload for URL {url}")

        # Save and upload resolutions if any were accumulated
        if accumulated_resolutions_this_run:
            # The file at final_resolutions_filename already contains all accumulated and deduplicated resolutions.
            if final_resolutions_filename and os.path.exists(final_resolutions_filename):
                logger.info(f"Uploading final resolutions file: {final_resolutions_filename} containing {len(accumulated_resolutions_this_run)} items.")
                upload_to_voiceflow(final_resolutions_filename)
                time.sleep(VOICEFLOW_UPLOAD_DELAY)
            else:
                logger.error(f"Resolution items were accumulated for URL {url} ({len(accumulated_resolutions_this_run)} items), but the file '{final_resolutions_filename}' is not available for upload. Last incremental save might have failed or file does not exist.")
        else:
            logger.info(f"No resolution items accumulated to upload for URL {url}")

        logger.info(f"Completed all processing and uploads for URL {url}")
        # --- End Final Upload ---

    except Exception as e:
        # Catch any unexpected errors during the entire process for this URL
        logger.error(f"Chyba při zpracování obsahu URL {url}: {str(e)}", exc_info=True)
        print(f"\n=== Q/A EXTRACTION ERROR ===\nURL: {url}\nError: {str(e)}\nQ/A Extraction: FAILED (Overall processing)\n============================\n")

def compile_and_upload_question_tables():
    """
    Compiles "Question" fields from all JSON payload files, groups them by "Category",
    creates new category-specific table JSONs, and uploads them to Voiceflow.
    This replaces the old compile_search_queries_file functionality.
    """
    logger.info("Starting compilation and upload of category-specific question tables.")
    payloads_dir = "payloads"
    category_questions_map = {}

    if not os.path.exists(payloads_dir):
        logger.error(f"Payloads directory '{payloads_dir}' does not exist. Cannot compile question tables.")
        return

    logger.info(f"Iterating through JSON files in '{payloads_dir}' to extract questions.")
    for filename in os.listdir(payloads_dir):
        if filename.endswith('.json'):
            file_path = os.path.join(payloads_dir, filename)
            logger.debug(f"Processing file: {filename}")
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    payload_content = json.load(f)
                
                items = payload_content.get('data', {}).get('items', [])
                
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            question = item.get("Question")
                            category = item.get("Category")

                            if question and isinstance(question, str) and question.strip() and \
                               category and isinstance(category, str) and category.strip():
                                
                                if category not in category_questions_map:
                                    category_questions_map[category] = []
                                
                                # Avoid duplicate questions within the same category
                                is_duplicate = False
                                for existing_q_item in category_questions_map[category]:
                                    if existing_q_item["Question"] == question:
                                        is_duplicate = True
                                        break
                                if not is_duplicate:
                                    category_questions_map[category].append({
                                        "Question": question,
                                        "Category": category,  # Store category with each question item
                                        "Type": "Question"  # Revert to "Question"
                                    })
                            else:
                                logger.debug(f"Skipping item in {filename} due to missing/invalid Question or Category: {item}")
                        else:
                            logger.debug(f"Skipping non-dictionary item in {filename}: {item}")
                else:
                    logger.debug(f"No 'items' list found or 'items' is not a list in {filename}.")
            except json.JSONDecodeError:
                logger.error(f"Error decoding JSON from file {file_path}. Skipping.")
            except Exception as e:
                logger.error(f"Unexpected error processing file {file_path}: {str(e)}. Skipping.")

    if not category_questions_map:
        logger.info("No questions found to compile into category tables.")
        return

    logger.info(f"Found questions for {len(category_questions_map)} categories. Preparing and uploading tables...")
    for category, questions_list in category_questions_map.items():
        if not questions_list:
            logger.info(f"No questions to upload for category '{category}'. Skipping.")
            continue

        table_name = f"{category.lower().replace(' ', '_')}_questions_table"
        output_filename = os.path.join(payloads_dir, f"{table_name}.json")

        logger.info(f"Creating table for category '{category}' with {len(questions_list)} questions. Table name: {table_name}")

        new_payload = {
            "data": {
                "schema": {
                    "searchableFields": ["Question"],
                    "metadataFields": ["Category", "Type"] # Add Type to metadataFields
                },
                "name": table_name,
                "items": questions_list  # questions_list already contains {"Question": ..., "Category": ..., "Type": ...}
            }
        }

        try:
            with open(output_filename, 'w', encoding='utf-8') as f:
                json.dump(new_payload, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved question table for category '{category}' to {output_filename}")

            logger.info(f"Uploading {output_filename} to Voiceflow...")
            upload_to_voiceflow(output_filename)
            logger.info(f"Successfully uploaded {output_filename}. Waiting for {VOICEFLOW_UPLOAD_DELAY} seconds.")
            time.sleep(VOICEFLOW_UPLOAD_DELAY)

        except Exception as e:
            logger.error(f"Error saving or uploading question table for category '{category}' (file: {output_filename}): {str(e)}")
            
    logger.info("Finished compiling and uploading category-specific question tables.")

def add_url_to_existing_qa_files():
    payloads_dir = "payloads"
    if not os.path.exists(payloads_dir):
        logger.error(f"Payloads directory '{payloads_dir}' does not exist. Cannot update files.")
        return

    updated_files_count = 0
    logger.info(f"Scanning '{payloads_dir}' for Q/A files to update with URL field...")
    logger.warning("URL reconstruction is best-effort. It assumes filenames follow the '{category.lower()}_{original_url_path_with_slashes_replaced_by_underscores}.json' pattern.")

    for filename in os.listdir(payloads_dir):
        if not filename.endswith(".json"):
            continue

        if "_table" in filename: # Skip all table files
            logger.debug(f"Skipping table file: {filename}")
            continue
        
        file_path = os.path.join(payloads_dir, filename)
        logger.info(f"Processing Q/A file for URL update: {file_path}")

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                payload_data = json.load(f)

            if not isinstance(payload_data, dict) or 'data' not in payload_data or 'items' not in payload_data['data']:
                logger.warning(f"Skipping {filename}: Invalid payload structure (missing data/items).")
                continue

            base_name = filename[:-5]  # Remove .json
            reconstructed_url = f"URL_COULD_NOT_BE_RECONSTRUCTED_FROM_FILENAME:{filename}" # Default
            
            found_category_match = False
            for cat_from_list in CATEGORIES:
                category_prefix = cat_from_list.lower() + "_"
                if base_name.startswith(category_prefix):
                    # The part after the category prefix is the original url_based_name
                    url_based_name_from_file = base_name[len(category_prefix):]
                    
                    if url_based_name_from_file == "home":
                        reconstructed_url = BASE_URL
                    else:
                        # Convert underscores in this part back to slashes to get the original path
                        original_path_segment = url_based_name_from_file.replace('_', '/')
                        reconstructed_url = urljoin(BASE_URL, original_path_segment)
                    found_category_match = True
                    break 
            
            if not found_category_match:
                # Handle cases where the filename might not have a known category prefix
                # e.g., a file named "home.json" directly for the homepage's Q/A
                if base_name == "home":
                    reconstructed_url = BASE_URL
                else:
                    # If no category matched, and it's not "home.json", we might assume the whole base_name
                    # was a url_based_name (e.g. from a single custom URL process without a clear category prefix in filename)
                    # This part is more heuristic.
                    logger.warning(f"No known category prefix found in '{filename}'. Attempting to reconstruct URL assuming entire base_name ('{base_name}') is the path slug (underscores converted to slashes).")
                    original_path_segment = base_name.replace('_', '/')
                    reconstructed_url = urljoin(BASE_URL, original_path_segment)


            logger.debug(f"File: {filename}, Base name: {base_name}, Reconstructed URL: {reconstructed_url}")
            
            data_section = payload_data['data']
            schema = data_section.get('schema', {})
            if not schema: # Should not happen if previous check passed, but defensive
                logger.warning(f"Skipping {filename}: Schema not found in data section.")
                continue
            
            metadata_fields = schema.get('metadataFields', [])
            schema_changed = False
            if "URL" not in metadata_fields:
                metadata_fields.append("URL")
                schema['metadataFields'] = list(set(metadata_fields)) # Ensure uniqueness and preserve order (though set doesn't)
                schema_changed = True
                logger.info(f"Added 'URL' to metadataFields in schema for {filename}")

            items = data_section.get('items', [])
            if not isinstance(items, list): # Should not happen, but defensive
                logger.warning(f"Skipping {filename}: 'items' is not a list or not found in data section.")
                continue

            modified_item_count = 0
            for item in items:
                if isinstance(item, dict):
                    # Add or update the URL field
                    if item.get("URL") != reconstructed_url:
                        item["URL"] = reconstructed_url
                        modified_item_count += 1
            
            if modified_item_count > 0 or schema_changed:
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(payload_data, f, ensure_ascii=False, indent=2)
                logger.info(f"Updated {filename}: {modified_item_count} items' URL field set/updated. Schema updated: {schema_changed}.")
                updated_files_count += 1
            else:
                logger.info(f"No changes needed for {filename} (URL field likely already present and matches reconstructed, and schema correct).")

        except json.JSONDecodeError:
            logger.error(f"Error decoding JSON from {filename}. Skipping.")
        except Exception as e:
            logger.error(f"Unexpected error processing file {filename}: {str(e)}")
    
    logger.info(f"URL field update process for existing Q/A files finished. {updated_files_count} files were modified.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape and upload data to Voiceflow")
    parser.add_argument("--skip-scraping", type=int, choices=[0, 1], default=0,
                        help="Přeskočit scraping a nahrát existující payloady (0: ne, 1: ano)")
    parser.add_argument("--compile-only", action="store_true",
                        help="Pouze zkompilovat vyhledávací dotazy z existujících payloadů")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug mode with verbose output")
    parser.add_argument("--process-missing", action="store_true",
                        help="Process only URLs that don't have corresponding files from previous runs")
    parser.add_argument("--custom-urls", nargs='+', type=str,
                        help="Process only these specific URLs instead of scraping the sitemap")
    parser.add_argument("--update-qa-url-field", action="store_true",
                        help="Add/Update URL field in existing Q/A payload files. This is a best-effort update based on filenames.")
    parser.add_argument("--blacklist-urls", nargs='+', type=str,
                        help="Specify URLs to be completely skipped during processing.")
    args = parser.parse_args()
    
    # Set debug level if requested
    if args.debug:
        logger.setLevel(logging.DEBUG)
        console_handler.setLevel(logging.DEBUG)
        file_handler.setLevel(logging.DEBUG)
        logger.info("Debug mode enabled")

    if args.update_qa_url_field:
        logger.info("Starting update of existing Q/A files to add URL field.")
        add_url_to_existing_qa_files()
        logger.info("Finished updating Q/A files. Exiting.")
        # Typically, this would be a standalone operation, so we might exit.
        # If you want other operations to run after this, remove the exit/return.
        exit() # or return, if main() is called from elsewhere and needs to signal completion
    
    # Populate global BLACKLISTED_URLS if provided
    if args.blacklist_urls:
        BLACKLISTED_URLS = args.blacklist_urls # Removed global declaration
        logger.info(f"Blacklisted URLs loaded from command-line arguments: {BLACKLISTED_URLS}")
    elif BLACKLISTED_URLS: # Check if the list was pre-filled in the script
        logger.info(f"Using pre-defined blacklisted URLs from script: {BLACKLISTED_URLS}")
    else:
        logger.info("No URLs blacklisted (neither by command-line nor pre-defined in script).")

    # Pass custom URLs directly to main function if provided via command line
    main(skip_scraping=args.skip_scraping, 
         compile_only=args.compile_only, 
         process_missing=args.process_missing,
         custom_urls=args.custom_urls)