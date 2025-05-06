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
FIRECRAWL_API_KEY = "REMOVED-FIRECRAWL-KEY" # Placeholder - Replace with your actual key

# ============================================================================
# LLM PROVIDER CONFIGURATION
# ============================================================================
# Define available LLM providers with their configurations
LLM_PROVIDERS = {
    "1": {
        "name": "anthropic",
        "api_key": CLAUDE_API_KEY,
        "model": "claude-3-7-sonnet-20250219", # Default Anthropic model (Haiku)
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
#!=================================!

# ============================================================================
# URL CONFIGURATION
# ============================================================================
BASE_URL = "https://www.khk.cz"
SITEMAP_URL = "https://khk.cz/mapa-webu"
XML_SITEMAP_URL = "https://www.khk.cz/sitemap.xml"

# ============================================================================
# CUSTOM URLS LIST
# ============================================================================
# When this list is not empty, the script will only process these URLs instead of scraping the sitemap
# Example: ["https://www.khk.cz/page1", "https://www.khk.cz/page2"]
CUSTOM_URLS = [
    "https://www.khk.cz/kraj/rada/komise-rady"
]

# ============================================================================
# API CALL SETTINGS
# ============================================================================
API_CALL_DELAY = 5  # Fixed delay between API calls in seconds
MAX_RETRIES = 3  # Maximum number of retry attempts
INITIAL_RETRY_DELAY = 5  # Initial retry delay in seconds

# ============================================================================
# PROCESSING FLAGS
# ============================================================================
ENABLE_QA_PROCESSING = True  # Enable Q/A processing
UPLOAD_IMMEDIATELY = False  # Skip processing and only upload existing payloads
COMPILE_SEARCH_QUERIES = True  # Enable compilation of search queries into TXT file
CHECK_LAST_MODIFIED = True  # Check last modified date from sitemap.xml before Q/A extraction

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
# MAX_CHUNK_SIZE = 4000  # Deprecated: Chunking is now based on headers only

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
LAST_RUN_FILE = "last_run_timestamp.txt"  # File to store the last run timestamp

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
# UNIFIED LLM API CALLER
# ============================================================================
def call_llm_api(messages, system_prompt=None, max_tokens=1024, temperature=0.7, max_retries=MAX_RETRIES, initial_retry_delay=INITIAL_RETRY_DELAY, api_call_delay=API_CALL_DELAY, tools=None, tool_choice=None): # Added tools and tool_choice
    """
    Calls LLM provider APIs based on the LLM_SEQUENCE, with fallback.

    Args:
        messages (list): List of message dictionaries (e.g., [{'role': 'user', 'content': '...'}]).
                         For Groq/OpenAI, if system_prompt is used, it should be the first message.
        system_prompt (str, optional): System prompt text. Used directly by Anthropic,
                                       prepended to messages for Groq/OpenAI.
        max_tokens (int): Maximum tokens to generate.
        temperature (float): Sampling temperature.
        max_retries (int): Maximum retry attempts for EACH provider.
        initial_retry_delay (int): Initial delay before retrying for EACH provider.
        api_call_delay (int): Fixed delay after a successful API call.
        tools (list, optional): A list of tools the model can use (Anthropic specific).
        tool_choice (dict, optional): Controls how the model uses tools (Anthropic specific).

    Returns:
        str or dict: The content of the LLM's response (string), or the tool input dictionary if a tool was used by Anthropic. Returns None if all providers fail.
    """
    sequence_ids = [id.strip() for id in LLM_SEQUENCE.split(',') if id.strip()]
    if not sequence_ids:
        logger.error("LLM_SEQUENCE je prázdná nebo neplatná.")
        return None

    original_messages = messages[:] # Copy original messages for Groq prepending

    for provider_id in sequence_ids:
        provider_config = LLM_PROVIDERS.get(provider_id)
        if not provider_config:
            logger.warning(f"Provider ID '{provider_id}' ze sekvence nebyl nalezen v LLM_PROVIDERS. Přeskakuji.")
            continue

        provider_name = provider_config["name"]
        api_key = provider_config["api_key"]
        model = provider_config["model"]
        api_url = provider_config.get("api_url") # Get the API URL
        current_messages = original_messages[:] # Use a copy for this provider
        current_system_prompt = system_prompt

        logger.info(f"Pokus o volání LLM API s providerem ID: {provider_id} (Název: {provider_name}, Model: {model})")
        # Log the target URL for clarity
        if api_url:
            logger.info(f"Cílová URL: {api_url} (Použito přímo pro Groq, implicitně pro Anthropic SDK)")
        else:
            logger.warning(f"API URL není definována pro providera ID: {provider_id}")
            # Decide if we should skip or proceed; skipping for now if URL is crucial and missing
            if provider_name == "groq": # Groq requires explicit URL
                 logger.error(f"Groq (ID: {provider_id}) vyžaduje 'api_url' v konfiguraci. Přeskakuji.")
                 continue

        # Prepare provider-specific parameters
        client = None
        if provider_name == "anthropic":
            if not api_key:
                logger.error(f"Chybí CLAUDE_API_KEY pro providera {provider_id}. Přeskakuji.")
                continue
            client = anthropic.Anthropic(api_key=api_key)
        elif provider_name == "groq":
            if not api_key:
                logger.error(f"Chybí GROQ_API_KEY pro providera {provider_id}. Přeskakuji.")
                continue
            # Groq uses OpenAI's message format, prepend system prompt if provided
            if current_system_prompt:
                current_messages = [{"role": "system", "content": current_system_prompt}] + current_messages
                current_system_prompt = None # System prompt is now part of messages
        else:
            logger.error(f"Neznámý typ providera '{provider_name}' pro ID {provider_id}. Přeskakuji.")
            continue

        # Inner retry loop for the current provider
        for attempt in range(max_retries):
            try:
                if provider_name == "anthropic":
                    api_params = {
                        "model": model,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "messages": current_messages
                    }
                    if current_system_prompt: # Anthropic specific parameter
                        api_params["system"] = current_system_prompt
                    # Add tools and tool_choice if provided for Anthropic
                    if tools:
                        api_params["tools"] = tools
                    if tool_choice:
                        api_params["tool_choice"] = tool_choice

                    message = client.messages.create(**api_params)
                    logger.debug(f"Anthropic API Response object (ID: {provider_id}): {message}") # Log the full response object

                    # Check for tool use in the response
                    response_text = None
                    tool_used = False
                    if message.content and isinstance(message.content, list):
                        for content_block in message.content:
                             if content_block.type == "tool_use":
                                 logger.info(f"Anthropic model (ID: {provider_id}) used tool: {content_block.name}")
                                 response_text = content_block.input # Return the dictionary input to the tool
                                 tool_used = True
                                 break # Assume only one tool use for now
                    
                    if not tool_used:
                        # Default text extraction if no tool was used or content is different
                        if message.content and isinstance(message.content, list) and message.content[0].type == "text":
                             response_text = message.content[0].text.strip()
                        else:
                             logger.error(f"Anthropic API (ID: {provider_id}) response format unexpected or text content missing.")
                             # Consider raising an error or returning None? Let's log and continue retrying for now.
                             raise ValueError("Unexpected Anthropic response format or missing text content")

                elif provider_name == "groq":
                    headers = {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    }
                    payload = {
                        "model": model,
                        "messages": current_messages, # Use potentially modified messages
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    }
                    
                    # Note: Groq API (OpenAI compatible) might support tools differently or not at all.
                    # This implementation currently focuses on Anthropic tool use. Groq calls remain unchanged.
                    if tools or tool_choice:
                        logger.warning(f"Parametry 'tools' nebo 'tool_choice' byly poskytnuty, ale Groq (ID: {provider_id}) je nemusí podporovat v tomto formátu. Ignoruji.")

                    # Use the configured API URL for Groq
                    if not api_url:
                         logger.error(f"Chybí api_url pro Groq (ID: {provider_id}) v konfiguraci.")
                         # Raise an error or break? Let's break the inner loop for this attempt.
                         break # Stop retrying this provider if URL is missing
                         
                    response = requests_retry_session().post(api_url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
                    response.raise_for_status()
                    
                    response_json = response.json()
                    logger.debug(f"Groq API Response JSON (ID: {provider_id}): {json.dumps(response_json, indent=2)}")

                    if response_json.get("choices") and len(response_json["choices"]) > 0:
                        response_text = response_json["choices"][0]["message"]["content"].strip()
                    else:
                        logger.error(f"Groq API (ID: {provider_id}) nevrátil platné 'choices': {response_json}")
                        raise ValueError(f"Groq API (ID: {provider_id}) did not return valid choices")
                
                # Success! Apply delay and return.
                time.sleep(api_call_delay)
                logger.info(f"LLM API volání úspěšné (Provider ID: {provider_id}, Název: {provider_name})")
                return response_text

            except anthropic.APIError as e: 
                logger.warning(f"Chyba Anthropic API (ID: {provider_id}, pokus {attempt + 1}/{max_retries}): {type(e).__name__} - {str(e)}")
                # Specific handling for potential tool use errors (though usually caught by general APIError)
                if "tool_choice" in str(e):
                     logger.error(f"Chyba související s tool_choice u Anthropic (ID: {provider_id}): {str(e)}. Zkontrolujte definici nástroje a tool_choice.")
                     # Break inner loop for tool configuration errors
                     break

                if attempt == max_retries - 1:
                    logger.error(f"Nepodařilo se zavolat Anthropic API (ID: {provider_id}) po {max_retries} pokusech. Přecházím na dalšího providera (pokud existuje).")
                    break # Break inner loop to try next provider
                delay = initial_retry_delay * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"Čekání {delay:.2f} sekund před dalším pokusem s providerem {provider_id}.")
                time.sleep(delay)
                
            except requests.exceptions.RequestException as e:
                status_code = e.response.status_code if e.response is not None else "N/A"
                logger.warning(f"Chyba Groq API (ID: {provider_id}, HTTP {status_code}, pokus {attempt + 1}/{max_retries}): {str(e)}")
                should_retry = False
                if e.response is not None and (e.response.status_code == 429 or e.response.status_code >= 500):
                    should_retry = True
                elif e.response is not None:
                     logger.error(f"Groq API (ID: {provider_id}) vrátilo neočekávaný HTTP status {status_code}, neprovádí se retry pro tohoto providera.")
                     # No retry for this provider, break inner loop
                else: # Network error or other non-HTTP error
                    logger.error(f"Chyba sítě nebo jiná chyba při volání Groq API (ID: {provider_id}): {str(e)}")
                    # Potentially retry network errors
                    should_retry = True
                         
                if should_retry and attempt < max_retries - 1:
                    delay = initial_retry_delay * (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(f"Čekání {delay:.2f} sekund před dalším pokusem s providerem {provider_id}.")
                    time.sleep(delay)
                else: # Max retries reached for this provider OR non-retryable HTTP error
                    logger.error(f"Nepodařilo se zavolat Groq API (ID: {provider_id}) po {max_retries} pokusech nebo došlo k neopravitelné chybě. Přecházím na dalšího providera (pokud existuje).")
                    break # Break inner loop to try next provider
                    
            except Exception as e:
                logger.error(f"Neočekávaná chyba při volání LLM API (ID: {provider_id}, Název: {provider_name}, pokus {attempt + 1}/{max_retries}): {type(e).__name__} - {str(e)}")
                if attempt == max_retries - 1:
                    logger.error(f"Nepodařilo se provést volání LLM API (ID: {provider_id}) po {max_retries} pokusech kvůli neočekávané chybě. Přecházím na dalšího providera (pokud existuje).")
                    break # Break inner loop to try next provider
                delay = initial_retry_delay * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"Čekání {delay:.2f} sekund před dalším pokusem s providerem {provider_id}.")
                time.sleep(delay)
        
        # If the inner loop finished without returning (i.e., all retries failed for this provider), 
        # the outer loop will continue to the next provider_id.

    # If the outer loop finishes, all providers in the sequence failed.
    logger.error(f"Všichni LLM provideři v sekvenci ({LLM_SEQUENCE}) selhali.")
    return None

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
                        headers["X-Target-Selector"] = ""
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
                    # Adapt target_selector to Firecrawl's includeTags
                    # Split selector string into a list of tags
                    include_tags = [tag.strip() for tag in target_selector.split(',') if tag.strip()]
                    # Payload matching the user's simple curl example
                    payload = {
                        "url": url,
                        "formats": ["markdown"],
                        "includeTags": include_tags
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
def get_html_content_via_jina(url, target_selector=".sitemap"):
    return get_content(url, content_format="html", target_selector=target_selector)

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

def categorize_link(path):
    """Categorizes a link path using the configured LLM provider."""
    path_string = ' > '.join(path)
    
    prompt = f"""Dána je následující cesta menu z webových stránek Královéhradeckého kraje:

{path_string}

Zařaďte prosím tuto cestu do JEDNÉ z následujících kategorií:

{', '.join(CATEGORIES)}

DŮLEŽITÉ INSTRUKCE:
1. MUSÍTE odpovědět POUZE názvem JEDNÉ JEDINÉ kategorie ze seznamu výše - odpověď "Nezařazeno" NENÍ povolena.
2. I když si nejste jisti, vyberte kategorii, která se nejvíce blíží obsahu nebo tématu cesty.
3. Neodpovídejte žádným jiným textem, pouze názvem kategorie ze seznamu.
4. V případě nejistoty použijte následující prioritizaci:
   a) Nejdříve hledejte přímou tematickou shodu
   b) Pokud není nalezena, hledejte související témata
   c) Pokud stále není jasné, použijte nejobecnější související kategorii
5. Použijte následující vodítka pro kategorizaci:
   - Kontakt:
     * lidé, osoby, krajský/městský úřad, organizační struktura
     * kontaktní informace, komise, výbory, zastupitelstvo, radní, zaměstnanci
     * všechna sportoviště, aquacentra, sportovní arény, sportovní zařízení
     * všechny portály (mapové, eGovernment, informační, atd.)
     * platformy a systémy pro komunikaci s úřadem
   - Administrativa_Uredni_Zalezitosti:
     * úřední dokumenty, postupy, vyhlášky
     * veškeré vyřizování a zařizování úředních záležitostí
     * formuláře a žádosti
     * hlášení závad a problémů
     * životní situace

Vezměte v úvahu celou absolutní cestu v daném stromě k URL odkazu pro co nejpřesnější zařazení/zvolení dané kategorie ze vstupního seznamu. MUSÍTE vybrat jednu kategorii, i když si nejste zcela jisti - vyberte tu nejvíce odpovídající.
"""

    messages = [{"role": "user", "content": prompt}]
    
    # Note: system_prompt is not used here, messages contain the full instruction
    category = call_llm_api(messages=messages, max_tokens=50, temperature=0)

    if category is None: # Handle API call failure
        logger.error(f"Nepodařilo se získat kategorii pro cestu: {path_string} po všech pokusech.")
        return "Nezařazeno"
        
    category = category.strip()
    
    if category not in CATEGORIES:
        # Basic check if the response contains one of the categories maybe with extra text
        found_category = None
        for valid_cat in CATEGORIES:
            if valid_cat in category:
                found_category = valid_cat
                logger.warning(f"LLM vrátil kategorii s extra textem: '{category}'. Extrahovaná platná kategorie: '{found_category}'.")
                break
        if found_category:
            category = found_category
        else:
            logger.warning(f"LLM vrátil neočekávanou nebo neplatnou kategorii: '{category}'. Použije se 'Nezařazeno'.")
            return "Nezařazeno"
            
    return category

def generate_rag_question(path):
    """Generates RAG questions using the configured LLM provider."""
    path_string = ' > '.join(path)
    
    prompt = f"""Based on the following absolute path from a website sitemap:

{path_string}

Create an open-ended, RAG-optimized question in both Czech and English that would help users find this specific page. The question should:
1. Be natural and conversational
2. Include key details from the path
3. Be suitable for semantic search
4. Help users find the exact page they're looking for
5. Include both Czech and English versions separated by " | "

Example format:
For path: "Dotace > Kotlíková dotace > Aktuality"
Output: "Kde najdu aktuality, články a důležité informace ke kotlíkovým dotacím? | Where do I find the latest articles, updates and important information regarding boiler subsidies?"

Return ONLY the question pair without any additional text or formatting."""

    messages = [{"role": "user", "content": prompt}]
    
    question = call_llm_api(messages=messages, max_tokens=200, temperature=0)
    
    if question is None:
        logger.error(f"Nepodařilo se vygenerovat RAG otázku pro cestu: {path_string} po všech pokusech.")
        # Provide a generic fallback to avoid breaking downstream processes
        fallback_title = path[-1] if path else "the page"
        return f"Kde najdu informace o {fallback_title}? | Where can I find information about {fallback_title}?"

    return question.strip()

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
                    # 3. Categorize URL
                    category = categorize_link(current_path)
                    logger.info(f"Assigned category: {category} based on path")

                    # 4. Generate RAG question
                    rag_question = generate_rag_question(current_path)
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
                        is_main_domain = domain == "www.khk.cz" or domain == "khk.cz"
                        
                        if is_sitemap:
                            logger.info("Přeskakuji Q/A extrakci pro mapu stránek")
                        elif not is_main_domain:
                            logger.info(f"Přeskakuji Q/A extrakci pro subdoménu nebo jinou doménu: {domain}")
                        else:
                            # Direct Q/A content processing without redundant HTML call
                            logger.info("Spouštím Q/A extrakci (URL processing check passed)...")
                            process_url_content(absolute_url, None, category, metadata)
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
                "Navigation": link_data.get("Navigation", "")
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
                    "metadataFields": ["Category"]
                },
                "name": table_name,
                "items": final_items
            }
        }

        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logger.info(f"Incrementally updated table payload for '{category}' in file: {filename} (Updated: {updated_count}, Added: {added_count}, Total: {len(final_items)})")
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
                    "Navigation": link_data.get("Navigation", "")
                })
            
            payload = {
                "data": {
                    "schema": {
                        "searchableFields": ["Title", "URL", "Question", "Navigation"],
                        "metadataFields": ["Category"]
                    },
                    "name": table_name,
                    "items": final_items
                }
            }

            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                logger.info(f"Saved final table payload for '{category}' in file: {filename} (Total items: {len(final_items)})")
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
                            
                        # Append items, checking for duplicates based on URL
                        existing_urls = {item.get('URL', '') for item in loaded_data[category] if item.get('URL')}
                        new_items_count = 0
                        for item in items:
                             if isinstance(item, dict) and item.get('URL') and item['URL'] not in existing_urls:
                                 loaded_data[category].append(item)
                                 existing_urls.add(item['URL'])
                                 new_items_count += 1
                             elif not item.get('URL'):
                                 logger.warning(f"Item in {filename} is missing 'URL'. Skipping item: {item}")
                        
                        logger.info(f"Loaded {new_items_count} new items for category '{category}' from {filename}")
                        
            except json.JSONDecodeError as e:
                logger.error(f"Error decoding JSON from file {filename}: {str(e)}")
            except Exception as e:
                logger.error(f"Unexpected error loading file {filename}: {str(e)}")
                
    logger.info(f"Finished loading existing payloads. Found data for {len(loaded_data)} categories.")
    return loaded_data

def requests_retry_session(
    retries=3,
    backoff_factor=0.3,
    status_forcelist=(500, 502, 503, 504, 524),
    session=None,
):
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

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

def save_payload_to_file(url, content, section, metadata):
    """Saves the provided content (list of QA pairs) to a JSON file,
    always overwriting the existing file.

    Args:
        url (str): The URL the content is associated with (used for filename).
        content (list): The list of QA pair dictionaries to save.
        section (str): The category/section (used for filename and metadata).
        metadata (dict): Metadata (currently unused in this simplified version).
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
    
    # Ensure category is added to each item in the *incoming* content list
    for item in content:
        if isinstance(item, dict):
            item["Category"] = section
        else:
            logger.warning(f"Skipping non-dictionary item during category assignment: {item}")

    # --- Always Overwrite Logic --- 
    logger.info(f"Performing full save (overwrite) to: {filename}")
    final_items = content # Use the provided content directly
    
    # Construct the final payload structure
    payload = {
        "data": {
            "schema": {
                "searchableFields": ["Question", "Answer"],
                "metadataFields": ["Category"]
            },
            "name": f"{section.lower()}_{url_based_name}", # Schema name consistency
            "items": final_items # Use the validated items
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
        valid = True
        for key in payload["data"]["schema"]["searchableFields"] + payload["data"]["schema"]["metadataFields"]:
            if key not in item:
                logger.warning(f"Missing required key '{key}' in item: {item}. Skipping item.")
                valid = False
                break
        if valid:
            validated_items.append(item)
            valid_items_count += 1
            
    payload["data"]["items"] = validated_items # Use only validated items
    
    # Determine if we should skip saving:
    # Skip ONLY if the final list is empty AND the original input content was NOT empty.
    # This means validation failed on all items that were actually passed in.
    # We allow saving if the final list is empty BECAUSE the original input was empty (initialization).
    if not validated_items and bool(content):
        logger.warning(f"No valid Q/A items found after validation for {filename}. Skipping file save as all originally provided items were invalid.")
        return None # Indicate save failure due to validation removing all items

    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Successfully saved/overwritten payload for URL '{url}' ({len(payload['data']['items'])} items) to file: {filename}")
        print(f"Payload saved/overwritten: {filename}")
        return filename # Return filename on success
    except Exception as e:
        logger.error(f"Error writing payload file {filename}: {str(e)}")
        return None # Indicate save failure

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

def convert_to_qa(content, title, category):
    """Converts content to Q/A pairs using the configured LLM provider, forcing structured JSON output for Anthropic."""
    
    # Define the tool schema for structured Q/A extraction
    qa_tool = [
        {
            "name": "extract_qa_pairs",
            "description": "Extracts Question/Answer pairs from the provided text and formats them as a valid JSON object according to the specified schema.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "qa_pairs": {
                        "type": "array",
                        "description": "An array of Question/Answer objects extracted from the text. This is of utmost importance and must be ALWAYS present no matter what. Presence of this array is life or death scenario.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "Question": {"type": "string", "description": "The question formulated from the text content, including multiple variations separated by ' | '."},
                                "Answer": {"type": "string", "description": "The answer extracted verbatim from the text, including any relevant Markdown formatting for links and images."}, 
                                "Category": {"type": "string", "description": "The category assigned to this Q/A pair."}
                            },
                            "required": ["Question", "Answer", "Category"]
                        }
                    }
                },
                "required": ["qa_pairs"]
            }
        }
    ]
    
    # Force the model to use the defined tool
    tool_choice = {"type": "tool", "name": "extract_qa_pairs"}
    
    # Revised system prompt focusing on extraction principles
    # Improved System Prompt
    system_prompt = f"""# Tvá role: Jste ultra-precizní asistent pro extrakci informací specializovaný na obsah webových stránek. Vaším úkolem je analyzovat poskytnutý text (fragment webové stránky) a extrahovat z něj POUZE informace **přímo související s hlavním tématem stránky: '{title}'**. Výstup MUSÍ být ve formě párů Otázka/Odpověď pomocí poskytnutého nástroje `extract_qa_pairs`. Ignorujte obecné navigační prvky, patičky, záhlaví a nesouvisející postranní panely.

## HLAVNÍ CÍL: Vytvořit **VYČERPÁVAJÍCÍ** odpovědi ("Answer") obsahující **každý relevantní detail** z textu **k tématu '{title}'**. Odpovědi NESMÍ být stručné nebo sumarizované.

## KLÍČOVÉ ZAMĚŘENÍ:
*   **POUZE Téma '{title}':** Extrahujte informace **VÝHRADNĚ** se týkající **'{title}'**. Pokud text obsahuje sekce (např. telefonní seznam rozdělený podle oddělení), extrahujte informace pro každou tuto podsekci tématu.
*   **IGNORUJTE Boilerplate:** **NEEXTRAHUJTE** informace z:
    *   Hlavních navigačních menu (pokud nejsou specifické pro '{title}').
    *   Obecných záhlaví a patiček stránky.
    *   Postranních panelů s odkazy na nesouvisející portály nebo sekce.
    *   Cookie lišt a podobných obecných prvků webu.
*   **STRUKTURA (např. Telefonní seznam):** Pokud je hlavní téma strukturované (jako telefonní seznam podle oddělení), zachovejte tuto strukturu v odpovědích. Pro každé oddělení/sekci v rámci tématu '{title}' vytvořte Q/A páry obsahující **VŠECHNY** osoby a jejich **KOMPLETNÍ** kontaktní údaje (viz bod 6 níže).

## KROK 0: POVINNÉ PŘEDZPRACOVÁNÍ ZDROJOVÉHO TEXTU
PŘED extrakcí Q/A párů a PŘED aplikací jakýchkoli dalších pravidel níže, MUSÍTE nejprve upravit "ZDROJOVÝ TEXT K ANALÝZE" následovně:
- V celém textu nahraďte KAŽDÝ VÝSKYT jakéhokoli typu jednoduché nebo dvojité uvozovky (například: `\"", `"`, `"`, `"`, `'`, `'`, `'`, `'`) PŘESNĚ dvěma jednoduchými apostrofy (`''`).
- Toto pravidlo se vztahuje na VŠECHNY uvozovky, které najdete. Cílem je, aby výsledný text, který budete dále analyzovat pro Q/A extrakci, neobsahoval žádné původní uvozovky, ale pouze `''` na jejich místě.
- Tento krok je kriticky důležitý pro zajištění správného formátu JSON výstupu.

## PRINCIPY EXTRAKCE (pro relevantní obsah k '{title}' PO KROKU 0):
1.  **PŘESNOST & VERBATIM:** Extrahujte POUZE informace explicitně uvedené v relevantním textu (již upraveném dle Kroku 0). NIC si nedomýšlejte. Odpověď ("Answer") by měla být co nejvíce **verbatim** (doslovná kopie textu). **Nezkracujte ani nesumarizujte.**
2.  **MAXIMÁLNÍ ÚPLNOST (v rámci tématu):** Extrahujte **ABSOLUTNĚ VŠECHNY** smysluplné informace **k tématu '{title}'**. To zahrnuje (ale není omezeno na):
    *   **Všechny** kontaktní údaje (jména, příjmení, tituly, funkce, oddělení, emaily, VŠECHNY telefony, fax, adresy kanceláří, čísla dveří) **osob relevantních k '{title}'**.
    *   **Kompletní** seznamy osob (členové výborů, zaměstnanci oddělení atd.) **patřící k '{title}'**.
    *   **Všechny** číselné údaje, časové údaje, odkazy, názvy **související s '{title}'**.
    *   **Všechny** procedurální informace, podmínky, kritéria **týkající se '{title}'**.
3.  **KONKRÉTNOST:** Odpovědi musí být konkrétní a faktické, **přímo citující** zdrojový text.
4.  **KONTEXT:** Zachovejte původní kontext extrahovaných informací v rámci tématu '{title}'.
5.  **FORMÁTOVÁNÍ V ODPOVĚDI A ZAJIŠTĚNÍ VALIDNÍHO JSON VÝSTUPU:**
    *   **Markdown v "Answer":** V poli "Answer" formátujte odkazy pomocí Markdown: `[Popisek](URL)`.
    *   **Kritické Pravidlo (JSON Escaping pro "Question" a "Answer"):** Pole "Question" a "Answer" jsou textové řetězce (strings) uvnitř JSON struktury. Aby byl výsledný JSON vždy platný a správně interpretovatelný, MUSÍTE důsledně escapovat speciální znaky VŽDY, když se objeví uvnitř HODNOT těchto polí. Dodržujte striktně následující pravidla pro escapování:
        *   **Dvojité uvozovky (`"`)**: KAŽDÝ VÝSKYT znaku dvojité uvozovky (`"`) uvnitř textu, který vkládáte do pole "Question" nebo "Answer", MUSÍ být escapován jako `\\"`.
            *   **Velmi Důležitá Poznámka k "KROKU 0"**: Váš stávající "KROK 0" nařizuje nahrazení VŠECH typů uvozovek ve VSTUPNÍM textu za dva jednoduché apostrofy (`''`). Pokud je tento krok proveden na textu PŘED jeho extrakcí pomocí LLM, pak LLM neuvidí žádné původní dvojité uvozovky (`"`) ve vstupním textu. V důsledku toho LLM nemůže tyto původní dvojité uvozovky escapovat jako `\\"`. Pokud je vaším cílem mít ve výsledném JSONu `\\"` tam, kde byly původně dvojité uvozovky, pak "KROK 0" by měl být upraven tak, aby nenahrazoval dvojité uvozovky, které mají být součástí extrahovaného obsahu (nebo by měl být vynechán pro tyto případy). Pokud "KROK 0" zůstane v platnosti tak, jak je, pak se toto pravidlo (`" -> \\"`) uplatní pouze na dvojité uvozovky, které by LLM samo nějakým způsobem generovalo do obsahu polí "Question" nebo "Answer", což by nemělo být běžné, pokud se drží principu verbatim extrakce.
        *   **Zpětné lomítko (`\\`)**: KAŽDÝ VÝSKYT znaku zpětného lomítka (`\\`) MUSÍ být escapován jako `\\\\`.
        *   **Lomítko (`/`)**: KAŽDÝ VÝSKYT znaku lomítka (`/`) MUSÍ být escapován jako `\\/`.
        *   **Backspace**: Pokud se vyskytne, escapujte jako `\\b`.
        *   **Form feed (`\\f`)**: Pokud se vyskytne, escapujte jako `\\f`.
        *   **Newline (`\\n`)**: KAŽDÝ VÝSKYT znaku nového řádku MUSÍ být escapován jako `\\n`.
        *   **Carriage return (`\\r`)**: KAŽDÝ VÝSKYT znaku carriage return MUSÍ být escapován jako `\\r`.
        *   **Tab (`\\t`)**: KAŽDÝ VÝSKYT znaku tabulátoru MUSÍ být escapován jako `\\t`.
        *   **Ostatní kontrolní znaky (Unicode U+0000 až U+001F)**: Všechny tyto znaky MUSÍ být escapovány pomocí `\\uXXXX` notace (např. `\\u000B` pro vertikální tabulátor).
    *   **Obsah "Answer":** Pole "Answer" **MUSÍ** obsahovat **PLNÝ, NESKRÁCENÝ, VERBATIM** text extrahovaný ze zdroje **k tématu '{title}'** (přičemž na tento extrahovaný text byla aplikována výše uvedená pravidla JSON escapování). Toto platí obzvláště pro seznamy a detailní popisy. **ŽÁDNÉ SUMARIZACE!** Odpověď MUSÍ být v ČEŠTINĚ.
    *   **Struktura Výstupu:** Celý váš výstup MUSÍ být POUZE JEDEN validní JSON objekt, který přesně odpovídá schématu definovanému pro nástroj `extract_qa_pairs`. Žádný další text, poznámky, vysvětlení nebo formátování (např. Markdown bloky ```json ... ```) nesmí být přítomno mimo tento jediný JSON objekt.
6.  **KONTAKTY A SEZNAMY (v rámci tématu '{title}'):**
    *   Věnujte **NEJVYŠŠÍ POZORNOST** extrakci **KOMPLETNÍCH, NEZKRÁCENÝCH** seznamů osob (např. zaměstnanci odborů v telefonním seznamu) a **VŠECH** jejich kontaktních údajů.
    *   Vaše "Answer" **MUSÍ** obsahovat **ABSOLUTNĚ PLNÝ VÝČET VŠECH** jednotlivců v dané sekci tématu, spolu se **VŠEMI** jejich detaily (tituly, funkce, pracoviště/odbor, **KAŽDÉ** telefonní číslo, **KAŽDÝ** email, číslo kanceláře atd.), přesně jak je to v textu.
    *   **NEVYNECHÁVEJTE ŽÁDNÝ DETAIL U ŽÁDNÉ OSOBY!**
    *   Pro tyto seznamy **IGNORUJTE JAKÉKOLI VNÍMANÉ OMEZENÍ DÉLKY ODPOVĚDI**. Cílem je **100% ÚPLNOST** detailů pro každou osobu v seznamu relevantním k '{title}'.
    *   Ideálně vytvořte **JEDEN KOMPLEXNÍ Q/A pár** pro každou logickou podsekci tématu (např. pro každý odbor v telefonním seznamu: Otázka: "Kdo pracuje v [Název odboru] a jaké jsou jejich kompletní kontakty?" a Odpověď obsahující **celý, nezměněný výpis** všech osob a detailů z dané sekce textu). **NEVYTVÁŘEJTE** samostatné Q/A páry pro jednotlivé osoby v seznamu.
7.  **OTÁZKY:** Formulujte jasné otázky **specifické pro téma '{title}'**, které přímo vedou k extrahované **detailní** odpovědi. Zahrňte 3-5 různých formulací otázky oddělených ` | `. PRVNÍ formulace by měla být hlavní otázka v ČEŠTINĚ, následovaná dalšími českými variantami. Poté přidejte anglické překlady/varianty otázky, také oddělené ` | `. Příklad formátu pro telefonní seznam: `Jaké jsou kontakty na Odbor kancelář hejtmana? | Kdo pracuje v Kanceláři hejtmana a jak je kontaktovat? | Telefonní seznam Kanceláře hejtmana | What are the contacts for the Governor's Office Department? | Who works at the Governor's Office and what are their contact details?`

## POSTUP EXTRAKCE STEP-BY-STEP:
1.  Pečlivě analyzuj "ZDROJOVÝ TEXT K ANALÝZE" s cílem extrahovat **MAXIMUM** informativních Q/A párů **k tématu '{title}'**.
2.  Identifikuj **VŠECHNY** klíčové informace, fakta, detaily, kontakty, seznamy **relevantní k '{title}'**. **IGNORUJ** obsah nesouvisející s tímto tématem.
3.  Pro každou identifikovanou informaci (nebo ucelený blok informací jako sekce telefonního seznamu) vytvořte pár Otázka/Odpověď.
4.  Pokuste se generovat Q/A páry v pořadí, v jakém se informace objevují v textu.
5.  Dbejte na **ABSOLUTNÍ PŘESNOST A MAXIMÁLNÍ ÚPLNOST** extrakce v rámci tématu. **NENECHÁVEJTE ŽÁDNÝ RELEVANTNÍ DETAIL** pozadu v poli "Answer".
6.  V odpovědích správně formátujte odkazy pomocí Markdown.
7.  Vytvořte **VYČERPÁVAJÍCÍ** Q/A páry pro všechny seznamy osob a kontaktů **v rámci tématu '{title}'**.

## Cílové téma tohoto textového fragmentu je: '{title}'. Zaměřte se POUZE na extrakci informací k tomuto tématu."""

    # Revised user prompt focusing on the task and instructing tool use
    # Improved User Prompt
    user_prompt = f"""# ZDROJOVÝ TEXT K ANALÝZE:

```
{content}
```

# TVŮJ AKTUÁLNÍ ÚKOL: **NYNÍ POUŽIJ NÁSTROJ `extract_qa_pairs`** pro extrakci Q/A párů ze "ZDROJOVÝ TEXT K ANALÝZE".
**DŮRAZ:** Zaměř se na vytvoření **maximálně vyčerpávajících a detailních odpovědí ('Answer')**, jak je specifikováno v systémových instrukcích, zejména pro kontaktní informace a seznamy. **NEZKRACUJ** odpovědi.
**KLÍČOVÁ POŽADAVKA NA FORMÁT:** Ujistěte se, že celý JSON výstup je striktně validní. Všechny textové hodnoty v JSONu (zejména v polích "Question" a "Answer") MUSÍ mít korektně escapované všechny speciální znaky (jako jsou uvozovky, zpětná lomítka, nové řádky atd.) podle standardu JSON a dle detailních pravidel uvedených v systémových instrukcích. Výstup nesmí obsahovat žádný text mimo samotný JSON objekt.
"""

    messages = [{"role": "user", "content": user_prompt}]
    
    # Call the API, providing the tool definition and forcing its use for Anthropic
    # The response should now be the dictionary from the tool_use input if successful
    response_data = call_llm_api(
        messages=messages, 
        system_prompt=system_prompt, 
        max_tokens=8192, # Max tokens suitable for large extractions
        temperature=0,     # Low temperature for factual extraction
        tools=qa_tool,     # Provide the tool definition
        tool_choice=tool_choice # Force the model to use the tool
    )

    # Debug logging for problematic Claude QA responses
    log_full_response_for_debug = False
    problematic_qa_pairs_source_string = None # Stores the string that was problematic

    if response_data is None:
        logger.error(f"Nepodařilo se získat Q/A páry pro '{title}' (API volání selhalo nebo nevrátilo data). ")
        # No response_data to log in this case as it is None.
    elif isinstance(response_data, dict):
        if 'qa_pairs' in response_data:
            potential_pairs_val = response_data['qa_pairs']
            if isinstance(potential_pairs_val, str):
                problematic_qa_pairs_source_string = potential_pairs_val # Store the string for logging
                # Check if this string leads to extract_json_from_text returning a placeholder
                # for a genuinely unparseable string.
                parsed_val_for_check = extract_json_from_text(potential_pairs_val)
                # Condition: placeholder returned AND original string wasn't a trivial empty list/object.
                if parsed_val_for_check == {"qa_pairs": []} and \
                   potential_pairs_val.strip().lower() not in ('[]', '{"qa_pairs": []}', '{"qa_pairs":null}', '{"qa_pairs": ""}','{}'):
                    log_full_response_for_debug = True
                    logger.warning(f"QA EXTRACTION DEBUG TRIGGER: 'qa_pairs' was a string that extract_json_from_text likely converted to placeholder for '{title}'.")
            # If potential_pairs_val is not a string (e.g., already a list), it's less likely to be the source of the user's specific type of warning.
        else: # 'qa_pairs' key missing in the response dictionary
            log_full_response_for_debug = True
            logger.warning(f"QA EXTRACTION DEBUG TRIGGER: 'qa_pairs' key missing in response_data dictionary for '{title}'.")
    elif isinstance(response_data, str): # The entire response_data is a string, not a tool_use dictionary
        log_full_response_for_debug = True
        problematic_qa_pairs_source_string = response_data # The entire response is the "problematic string"
        logger.warning(f"QA EXTRACTION DEBUG TRIGGER: Entire response_data from LLM was a string for '{title}'.")
    else: # Unexpected type for response_data
        log_full_response_for_debug = True
        logger.warning(f"QA EXTRACTION DEBUG TRIGGER: Unexpected response_data type received from LLM for '{title}'. Type: {type(response_data)}")

    if log_full_response_for_debug:
        logger.info(f"======== QA EXTRACTION DEBUG: RAW LLM RESPONSE FOR '{title}' ========")
        try:
            if isinstance(response_data, dict):
                 logger.info(f"Full response_data (dict): {json.dumps(response_data, ensure_ascii=False, indent=2)}")
            elif isinstance(response_data, str): # Already captured as problematic_qa_pairs_source_string if it's the whole response
                 logger.info(f"Full response_data (string): {response_data}")
            else:
                 logger.info(f"Full response_data (type {type(response_data)}): {str(response_data)}")
            
            # If the problematic part was specifically the qa_pairs string inside a dict, and it wasn't the entire response
            if problematic_qa_pairs_source_string and problematic_qa_pairs_source_string is not response_data:
                logger.info(f"Problematic 'qa_pairs' source string content: {problematic_qa_pairs_source_string}")
            
            print(f"QA EXTRACTION DEBUG: Full LLM API response for '{title}' logged to detailed log due to a processing issue (see log for details).")
        except Exception as e:
            logger.error(f"QA EXTRACTION DEBUG: Error encountered while trying to log full response_data: {str(e)}")
            # Attempt to log a snippet if json.dumps or str() fails for some reason
            logger.info(f"Problematic response_data (snippet if full logging failed): {str(response_data)[:1000]}...")
        logger.info(f"======== END QA EXTRACTION DEBUG: RAW LLM RESPONSE FOR '{title}' ========")

    # --- Revised Handling of Tool Response ---
    qa_pairs = None # Initialize qa_pairs

    if isinstance(response_data, dict):
        if 'qa_pairs' in response_data:
            potential_pairs = response_data['qa_pairs']

            # <<< FIX: Check if potential_pairs is a string and try to parse it >>>
            if isinstance(potential_pairs, str):
                logger.info(f"Model returned qa_pairs as a string for '{title}'. Attempting robust parsing.")
                # Use the more robust JSON extraction function
                parsed_data = extract_json_from_text(potential_pairs)
                # Check if the robust parser returned the placeholder or actual data
                if parsed_data and isinstance(parsed_data, dict) and 'qa_pairs' in parsed_data and parsed_data['qa_pairs'] is not None:
                    # Check if the placeholder wasn't returned (meaning parsing was likely successful)
                    # We check against the specific placeholder structure returned by extract_json_from_text on complete failure
                    # Modify this check if the placeholder structure in extract_json_from_text changes
                    is_placeholder = (parsed_data == {"qa_pairs": []}) and '[TABLE DATA]' not in potential_pairs # Crude check if it's the placeholder
                    
                    potential_pairs = parsed_data['qa_pairs'] # Extract the list
                    if not is_placeholder:
                        logger.info(f"Successfully parsed string using extract_json_from_text for '{title}'.")
                    else:
                        logger.warning(f"extract_json_from_text returned placeholder for '{title}'. Original string likely unparseable.")
                        # Keep potential_pairs as [] from the placeholder
                else:
                    logger.error(f"Failed to parse JSON string robustly for 'qa_pairs' for '{title}'. Input string preview: {potential_pairs[:500]}...")
                    potential_pairs = None # Indicate parsing failure

            # <<< End of FIX >>>

            if isinstance(potential_pairs, list):
                # Now, validate the structure of items within the list BEFORE assigning category
                validated_internal_pairs = []
                valid_structure = True
                for item in potential_pairs:
                    if not isinstance(item, dict):
                        logger.error(f"Položka v 'qa_pairs' není slovník pro '{title}': {item}")
                        valid_structure = False
                        break
                    # Check for REQUIRED keys ('Question', 'Answer') based on the simplified schema
                    if "Question" not in item or not isinstance(item.get("Question"), str):
                        logger.error(f"Chybí klíč 'Question' nebo není string v položce pro '{title}': {item}")
                        valid_structure = False
                        break
                    if "Answer" not in item or not isinstance(item.get("Answer"), str):
                        logger.error(f"Chybí klíč 'Answer' nebo není string v položce pro '{title}': {item}")
                        valid_structure = False
                        break
                    # If structure is okay so far for this item
                    # Only keep the required keys the model was asked to generate
                    validated_internal_pairs.append({"Question": item["Question"], "Answer": item["Answer"]})

                if valid_structure:
                    qa_pairs = validated_internal_pairs # SUCCESS: Found list with correct internal structure
                    logger.info(f"Úspěšně extrahováno a validováno {len(qa_pairs)} Q/A párů pomocí nástroje (klíč 'qa_pairs') pro '{title}'.")
                else:
                    # List found, but internal items have wrong structure/missing keys
                    logger.error(f"Seznam 'qa_pairs' nalezen pro '{title}', ale vnitřní struktura položek neodpovídá schématu ['Question', 'Answer'].")
                    # qa_pairs remains None
            else:
                # Found 'qa_pairs' key, but the value is NOT a list
                logger.error(f"Klíč 'qa_pairs' nalezen v odpovědi nástroje pro '{title}', ale hodnota není seznam (typ: {type(potential_pairs)}). Data: {response_data}")
        else:
            # The dictionary exists, but doesn't contain the 'qa_pairs' key
            logger.error(f"Odpověď nástroje pro '{title}' je slovník, ale neobsahuje očekávaný klíč 'qa_pairs'. Klíče: {list(response_data.keys())}. Data: {response_data}")

    elif isinstance(response_data, str):
         # This case should ideally not happen with tool_choice force, but handle just in case
         logger.warning(f"Model vrátil textovou odpověď místo očekávaného JSON z nástroje pro '{title}'. Text: {response_data[:200]}...")
         # Attempt to parse the string as JSON as a fallback (though unlikely to be correct)
         try:
             qa_data = json.loads(response_data)
             # Check structure again after parsing
             if isinstance(qa_data, dict) and 'qa_pairs' in qa_data and isinstance(qa_data['qa_pairs'], list):
                 # Validate internal structure after parsing
                 potential_pairs = qa_data['qa_pairs']
                 validated_internal_pairs = []
                 valid_structure = True
                 for item in potential_pairs:
                     # Check for REQUIRED keys and types after parsing
                     if not isinstance(item, dict) or "Question" not in item or not isinstance(item.get("Question"), str) or "Answer" not in item or not isinstance(item.get("Answer"), str):
                         logger.error(f"Položka v parsovaných 'qa_pairs' neodpovídá schématu ['Question', 'Answer'] pro '{title}': {item}")
                         valid_structure = False
                         break
                     # Only keep the required keys
                     validated_internal_pairs.append({"Question": item["Question"], "Answer": item["Answer"]})

                 if valid_structure:
                     qa_pairs = validated_internal_pairs
                     logger.info(f"Úspěšně parsováno a validováno {len(qa_pairs)} Q/A párů z textové odpovědi pro '{title}'.")
                 else:
                      logger.error(f"Textovou odpověď se podařilo parsovat, ale vnitřní struktura 'qa_pairs' neodpovídá schématu ['Question', 'Answer']. Parsed data: {qa_data}")
             else:
                 logger.error(f"Textovou odpověď nelze parsovat do očekávané struktury {{'qa_pairs': [...]}}. Parsed data: {qa_data}")
         except json.JSONDecodeError:
             logger.error(f"Textovou odpověď nelze parsovat jako JSON.")
         # If parsing fails or structure is wrong, qa_pairs remains None

    else:
        # Response was not None, not dict, not str - very unexpected
        logger.error(f"Neočekávaný typ odpovědi ({type(response_data)}) z call_llm_api pro '{title}'. Data: {response_data}")

    # If we successfully extracted a list of pairs, add the category
    if qa_pairs is not None: # Check if qa_pairs is a list (could be empty)
        valid_pairs_with_category = []
        for pair in qa_pairs:
             # Add category to the validated pairs
             pair['Category'] = category # Add the external category
             valid_pairs_with_category.append(pair)
        
        # Return the list (could be empty if no pairs were found but structure was valid)
        # Log if we are returning an empty list due to no content, but valid structure
        if not valid_pairs_with_category and isinstance(response_data, dict) and 'qa_pairs' in response_data and response_data['qa_pairs'] == []:
             logger.info(f"Model vrátil validní prázdný seznam 'qa_pairs' pro '{title}', protože nebyly nalezeny žádné relevantní informace.")

        return valid_pairs_with_category
    else:
        # If qa_pairs is still None, it means extraction or validation failed. Trigger fallback.
        logger.warning(f"Nepodařilo se extrahovat platný seznam Q/A párů z odpovědi modelu/nástroje pro '{title}'. Spouští se fallback.")
        # Fallback logic (will be handled by the calling function `process_url_content`)
        return None

def process_url_content(url, category, metadata):
    """Processes the markdown content of a single URL to extract Q/A pairs.
    Clears the QA file at the start, saves incrementally (overwriting),
    and uploads the final file to Voiceflow only once at the end.

    Args:
        url (str): The URL to process.
        category (str): The assigned category.
        metadata (dict): Metadata associated with the URL (e.g., title).
    """
    accumulated_pairs_this_run = [] # Accumulate Q/A pairs for this run
    final_filename = None # Store the name of the file to be uploaded

    try:
        # --- Step 1: Clear/Create the file at the start of processing this URL ---
        logger.info(f"Initializing/Clearing Q/A file for URL: {url}")
        # Call save_payload_to_file with an empty list to ensure the file is overwritten/created fresh
        initial_filename = save_payload_to_file(url, [], category, metadata)
        if not initial_filename:
            # If we can't even create the empty file, log error and stop processing this URL
            logger.error(f"Failed to initialize/clear Q/A file for {url}. Aborting Q/A extraction for this URL.")
            return
        logger.info(f"Successfully initialized/cleared file: {initial_filename}")
        final_filename = initial_filename # Keep track of the latest valid filename
        # No initial upload needed for an empty file
        # ----------------------------------------------------------------------

        # --- Step 2: Get Content ---
        try:
            # Specify markdown format and the selectors needed for Q/A
            qa_content, fetched_metadata = get_content(
                url,
                content_format="markdown",
                target_selector="#block-khk-content, #block-khk-sectionmenu"
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
        content_chunks = chunk_content(qa_content)
        logger.info(f"Split content into {len(content_chunks)} chunks for processing")

        # --- Step 3: Process Chunks Incrementally ---
        for i, chunk in enumerate(content_chunks):
            logger.info(f"Processing chunk {i+1}/{len(content_chunks)} ({len(chunk)} characters)")
            chunk_qa_pairs = convert_to_qa(chunk, metadata.get('title', 'Unknown Page') + f" (part {i+1})", category)

            # If conversion succeeded and returned pairs for this chunk
            if chunk_qa_pairs:
                logger.info(f"Successfully extracted {len(chunk_qa_pairs)} Q/A pairs from chunk {i+1}")
                accumulated_pairs_this_run.extend(chunk_qa_pairs) # Add pairs from this chunk

                # Deduplicate the *current* accumulated list
                seen_qa = set()
                deduplicated_items = []
                for item in reversed(accumulated_pairs_this_run):
                     qa_tuple = (item.get('Question'), item.get('Answer'))
                     if qa_tuple not in seen_qa:
                         seen_qa.add(qa_tuple)
                         deduplicated_items.append(item)
                accumulated_pairs_this_run = list(reversed(deduplicated_items))
                logger.info(f"Total accumulated items after adding chunk {i+1} and deduplicating: {len(accumulated_pairs_this_run)}")

                # Save the *current accumulated* list, overwriting the previous state of the file
                incremental_filename = save_payload_to_file(url, accumulated_pairs_this_run, category, metadata)

                # Update the final filename if save was successful
                if incremental_filename:
                    final_filename = incremental_filename
                else:
                    # Log error if saving failed, but continue processing other chunks
                    logger.error(f"Failed to save incremental payload after chunk {i+1} for URL {url}. Upload might use older file state if no further saves succeed.")
            else:
                # Log failure for this specific chunk
                logger.warning(f"Failed to extract Q/A pairs from chunk {i+1} or no pairs found.")
        # --- End Chunk Loop ---

        # --- Step 4: Handle Fallback (only if NO pairs were ever accumulated) ---
        if not accumulated_pairs_this_run:
            logger.warning(f"No QA pairs were accumulated from any chunk for {url}. Creating fallback QA pair.")
            title = metadata.get('title', 'Unknown page')
            fallback_qa_pairs = [{
                "Question": f"Co najdu na stránce {title}? | What can I find on the {title} page? | Jaké informace obsahuje stránka {title}?",
                "Answer": f"Na stránce najdete informace o {title}. Pro podrobnosti navštivte přímo [webovou stránku]({url}).",
                "Category": category
            }]

            # Save the fallback payload (this will overwrite the initial empty file or last saved state)
            fallback_filename = save_payload_to_file(url, fallback_qa_pairs, category, metadata)

            # Update the final filename if fallback save was successful
            if fallback_filename:
                final_filename = fallback_filename
            else:
                 logger.error(f"Failed to save fallback payload for URL {url}. No file will be uploaded.")
                 final_filename = None # Ensure no upload happens if fallback save fails

        # --- Step 5: Final Upload (after loop and fallback check) ---
        if final_filename:
            logger.info(f"All chunks processed for URL {url}. Proceeding to upload final file: {final_filename}")
            upload_to_voiceflow(final_filename)
        else:
            logger.error(f"No final file available to upload for URL {url} (either initial save, incremental saves, or fallback save failed).")
        # --- End Upload ---

    except Exception as e:
        # Catch any unexpected errors during the entire process for this URL
        logger.error(f"Chyba při zpracování obsahu URL {url}: {str(e)}", exc_info=True)
        print(f"\n=== Q/A EXTRACTION ERROR ===\nURL: {url}\nError: {str(e)}\nQ/A Extraction: FAILED (Overall processing)\n============================\n")

def chunk_content(content):
    """
    Split content into chunks based solely on Markdown headers (# to ######).
    Chunks are not limited by size.

    Args:
        content (str): The full markdown content

    Returns:
        list: List of content chunks, split by headers. Returns a single chunk if no headers are found.
    """
    # Initialize empty list to hold chunks
    processed_chunks = []

    # Skip processing if content is empty or None
    if not content or not content.strip():
        logger.warning("Empty or None content received, skipping chunking")
        return []

    # Log the content size for context
    logger.info(f"Content length before chunking: {len(content)} characters")

    # --- Split by any Markdown Header (# to ######) ---
    # Use regex that captures the header line itself and splits before it
    header_split_pattern = r'(\n#{1,6}\s+[^\n]+\n)'
    # Ensure content has leading newline for pattern matching at the start
    if not content.startswith('\n'):
        content = '\n' + content
    header_split = re.split(header_split_pattern, content)

    if len(header_split) > 1: # Found Markdown headers
        num_headers = (len(header_split) - 1) // 2
        logger.info(f"Splitting content by {num_headers} Markdown headers ('#' to '######').")
        # The regex split results in [before_header, header1, after_header1_before_header2, header2, ...]
        # We need to combine header with the text that follows it

        # Handle text before the first header if it exists
        text_before_first_header = header_split[0].strip()
        if text_before_first_header:
            processed_chunks.append(text_before_first_header)

        # Combine headers with their following text
        for i in range(1, len(header_split), 2):
            header = header_split[i].strip() # Remove leading/trailing whitespace from header line
            following_text = header_split[i+1] if i+1 < len(header_split) else ""
            
            # Create chunk starting with the header
            current_chunk = header + "\n" + following_text.strip() # Add newline after header, trim following text
            
            # Add the combined chunk if it has content
            if current_chunk.strip():
                processed_chunks.append(current_chunk.strip())

    else:
        # --- No Headers Found ---
        logger.info("No Markdown headers found. Returning the entire content as a single chunk.")
        processed_chunks.append(content.strip()) # Return the whole content as one chunk

    # Final validation (remove any potentially empty chunks created during processing)
    validated_chunks = [chunk for chunk in processed_chunks if chunk]

    logger.info(f"Final number of chunks created: {len(validated_chunks)}")

    # Log each chunk for inspection
    for i, chunk in enumerate(validated_chunks):
        logger.info(f"--- Chunk {i+1}/{len(validated_chunks)} --- ({len(chunk)} characters) ---")
        # Log a preview of the chunk
        preview_start = chunk[:200].replace('\n', ' ')
        preview_end = chunk[-100:].replace('\n', ' ')
        logger.info(f"Chunk {i+1} Preview: {preview_start}... ...{preview_end}")

    return validated_chunks

def simple_chunk_by_size(text, max_size):
    """
    Simple and reliable fallback chunking method that splits text into chunks
    of maximum size, trying to break at paragraph or sentence boundaries.
    (This function is no longer used by chunk_content but kept for potential future use or reference)
    
    Args:
        text (str): Text to split
        max_size (int): Maximum size of each chunk

    Returns:
        list: List of text chunks
    """
    chunks = []
    
    # First try to split by paragraphs
    paragraphs = re.split(r'\n\s*\n', text)
    
    current_chunk = ""
    for para in paragraphs:
        if len(para.strip()) == 0:
            continue
            
        # If this paragraph alone exceeds max size, split it by sentences
        if len(para) > max_size:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""
                
            # Split paragraph by sentences and add them
            sentences = re.split(r'([.!?]\s+)', para)
            sentence_chunk = ""
            
            for i in range(0, len(sentences), 2):
                sentence = sentences[i]
                # Add the punctuation back if it exists
                if i+1 < len(sentences):
                    sentence += sentences[i+1]
                    
                if len(sentence_chunk) + len(sentence) > max_size:
                    if sentence_chunk:
                        chunks.append(sentence_chunk.strip())
                    # If a single sentence is too long, just force split it by size
                    if len(sentence) > max_size:
                        sentence_parts = [sentence[i:i+max_size] for i in range(0, len(sentence), max_size)]
                        chunks.extend([p.strip() for p in sentence_parts if p.strip()])
                        sentence_chunk = ""
                    else:
                        sentence_chunk = sentence
                else:
                    sentence_chunk += sentence
            
            # Add the final sentence chunk
            if sentence_chunk.strip():
                chunks.append(sentence_chunk.strip())
        
        # Normal case - add paragraph to current chunk if it fits
        elif len(current_chunk) + len(para) + 2 > max_size:  # +2 for the newlines
            chunks.append(current_chunk.strip())
            current_chunk = para
        else:
            if current_chunk:
                current_chunk += "\n\n" + para
            else:
                current_chunk = para
    
    # Add the final chunk
    if current_chunk.strip():
        chunks.append(current_chunk.strip())
    
    return chunks

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
            "https://www.khk.cz/sitemap.xml?page=1",
            "https://www.khk.cz/sitemap.xml?page=2"
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
            compile_search_queries_file()
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
            
        payload_files = [f for f in os.listdir(payload_dir) if f.endswith('_payload.json')]
        if not payload_files:
            logger.error(f"V adresáři {payload_dir} nebyly nalezeny žádné payload soubory!")
            return
            
        logger.info(f"Nalezeno {len(payload_files)} payload souborů k nahrání")
        for file in payload_files:
            file_path = os.path.join(payload_dir, file)
            logger.info(f"Nahrávám soubor: {file}")
            try:
                upload_to_voiceflow(file_path)
            except Exception as e:
                logger.error(f"Chyba při nahrávání souboru {file}: {str(e)}")
        
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
                except Exception as e:
                    logger.error(f"Error uploading file {filename}: {str(e)}")

    # Add compilation of search queries if enabled
    if COMPILE_SEARCH_QUERIES:
        logger.info("Compiling search queries...") # Added log
        compile_search_queries_file()
    
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
            
            # 5. Categorize URL
            category = categorize_link(path)
            logger.info(f"Assigned category: {category} based on path")
            
            # 6. Generate RAG question
            rag_question = generate_rag_question(path)
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
                is_main_domain = domain == "www.khk.cz" or domain == "khk.cz"
                
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

def extract_json_from_text(text):
    """Extract valid JSON from potentially malformed text"""
    # Try direct parsing first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Multiple fallback methods
        try:
            # Try to find JSON pattern starting with {"qa_pairs":
            pattern = r'(\{[\s\S]*"qa_pairs"[\s\S]*\})'
            match = re.search(pattern, text)
            if match:
                return json.loads(match.group(1))
        except:
            pass
        
        try:
            # Try to fix common JSON errors
            fixed_text = re.sub(r',\s*\}', '}', text)  # Remove trailing commas
            fixed_text = re.sub(r',\s*\]', ']', fixed_text)
            # Attempt to parse the fixed text
            # Added check to ensure it's a dictionary with 'qa_pairs' list
            parsed = json.loads(fixed_text)
            if isinstance(parsed, dict) and 'qa_pairs' in parsed and isinstance(parsed['qa_pairs'], list):
                return parsed 
        except json.JSONDecodeError:
            # If fixing common errors didn't work, proceed to last resort
            pass
        except Exception as e:
             logger.error(f"Unexpected error during JSON fixing/parsing: {str(e)}")
             # Proceed to last resort
             pass
        
        # Last resort - basic structure with empty pairs if all else fails
        logger.warning(f"Could not extract valid JSON structure from text. Returning empty list placeholder.")
        return {"qa_pairs": []}

def convert_to_qa_with_retry(content, title, category, max_retries=3):
    """Try multiple approaches to generate QA pairs with retries"""
    for attempt in range(max_retries):
        try:
            # First try with full content
            if attempt == 0:
                qa_pairs = convert_to_qa(content, title, category)
                if qa_pairs:
                    return qa_pairs
            
            # Second try with simplified content
            elif attempt == 1:
                # Simplify content by removing complex HTML
                simplified = re.sub(r'<table.*?</table>', '[TABLE DATA]', content, flags=re.DOTALL)
                qa_pairs = convert_to_qa(simplified, title, category)
                if qa_pairs:
                    return qa_pairs
            
            # Last try with minimal extraction approach
            else:
                # Extract just text and basic info
                text_only = re.sub(r'<[^>]+>', ' ', content)
                text_only = re.sub(r'\s+', ' ', text_only).strip()
                qa_pairs = convert_to_qa(text_only[:5000], title, category)
                if qa_pairs:
                    return qa_pairs
                
        except Exception as e:
            logger.error(f"Error in convert_to_qa attempt {attempt+1}: {str(e)}")
    
    # If all attempts fail, return None instead of using a fallback
    logger.error(f"All QA extraction attempts failed for {title}")
    return None

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
    args = parser.parse_args()
    
    # Set debug level if requested
    if args.debug:
        logger.setLevel(logging.DEBUG)
        console_handler.setLevel(logging.DEBUG)
        file_handler.setLevel(logging.DEBUG)
        logger.info("Debug mode enabled")
    
    # Pass custom URLs directly to main function if provided via command line
    main(skip_scraping=args.skip_scraping, 
         compile_only=args.compile_only, 
         process_missing=args.process_missing,
         custom_urls=args.custom_urls)
