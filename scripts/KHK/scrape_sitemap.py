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
def call_llm_api(messages, system_prompt=None, max_tokens=1024, temperature=0.7, max_retries=MAX_RETRIES, initial_retry_delay=INITIAL_RETRY_DELAY, api_call_delay=API_CALL_DELAY):
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

    Returns:
        str: The content of the LLM's response, or None if all providers in sequence fail.
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
                    
                    message = client.messages.create(**api_params)
                    response_text = message.content[0].text.strip()
                    
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
# HTML CONTENT & PARSING
# ============================================================================

def get_html_content(url, for_qa=False):
    logger.info(f"Získávání HTML obsahu z URL: {url}")
    logger.info(f"Režim Q/A: {for_qa}")
    api_url = f"https://r.jina.ai/{url}"
    
    if for_qa:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {JINA_AI_API_KEY}",
            "X-Return-Format": "markdown",
            "X-Engine": "browser",
            "X-Target-Selector": "#block-khk-content, #block-khk-sectionmenu"
        }
    else:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {JINA_AI_API_KEY}",
            "X-Engine": "browser",
            "X-Return-Format": "html",
            "X-Target-Selector": ".sitemap"
        }
    
    logger.debug(f"Volání Jina.AI API s hlavičkami: {headers}")
    
    # Add specific retry logic for timeout errors
    for retry_attempt in range(REQUEST_RETRY_COUNT + 1):  # +1 for the initial attempt
        try:
            if retry_attempt > 0:
                logger.warning(f"Retry attempt {retry_attempt}/{REQUEST_RETRY_COUNT} for URL: {url}")
                # Add exponential backoff between retries
                backoff_time = REQUEST_BACKOFF_FACTOR * (2 ** (retry_attempt - 1))
                logger.info(f"Waiting {backoff_time:.2f} seconds before retry...")
                time.sleep(backoff_time)
            
            response = requests_retry_session().get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
            logger.debug(f"Status code: {response.status_code}")
            logger.debug(f"Response headers: {response.headers}")
            
            try:
                response_text = response.text
                logger.debug(f"Raw response: {response_text[:500]}...")
                # Log full response to console for debugging
                print(f"\n=== JINA AI API RESPONSE ===")
                print(f"URL: {url}")
                print(f"Status Code: {response.status_code}")
                print(f"Response Preview: {response_text[:300]}...")
                print("===========================\n")
                
                data = response.json()
                logger.debug(f"Parsed JSON response: {json.dumps(data, indent=2, ensure_ascii=False)}")
                logger.info(f"Jina AI API Response: {json.dumps(data, indent=2, ensure_ascii=False)[:500]}...")
                
                if data["status"] == 20000 and "data" in data:
                    if for_qa:
                        content = data["data"].get("content", "")
                        # Log markdown content preview to console for Q/A
                        print(f"\n=== Q/A MARKDOWN CONTENT PREVIEW FOR {url} ===")
                        print(f"{content[:500]}...\n")
                        logger.info(f"Q/A Markdown content preview: {content[:500]}...")
                    else:
                        content = data["data"].get("html", "")
                    
                    if content:
                        logger.debug(f"Získaný obsah ({len(content)} znaků): {content[:200]}...")
                        if not for_qa:
                            # Safely extract title with better error handling
                            title = ""
                            try:
                                soup = BeautifulSoup(content, "html.parser")
                                title_element = soup.title
                                if title_element and title_element.string:
                                    title = title_element.string
                                else:
                                    logger.warning(f"No title element found in HTML content for URL: {url}")
                            except Exception as e:
                                logger.warning(f"Error extracting title from HTML: {str(e)}")
                                
                            metadata = {
                                "title": title,
                                "url": data["data"].get("url", url)
                            }
                            return content, metadata
                        return content, {"url": url}
                    else:
                        logger.error("Obsah nebyl nalezen v odpovědi API")
                        logger.debug(f"Data struktura: {data}")
                        if retry_attempt < REQUEST_RETRY_COUNT:
                            logger.warning("Retrying due to missing content...")
                            continue
                        raise ValueError("Obsah chybí v odpovědi API")
                else:
                    error_message = data.get("status", "Neznámá chyba")
                    logger.error(f"Chyba při získávání obsahu: {error_message}")
                    logger.debug(f"Kompletní response data: {data}")
                    if retry_attempt < REQUEST_RETRY_COUNT:
                        logger.warning(f"Retrying due to API error: {error_message}...")
                        continue
                    raise ValueError(f"Chyba API: {error_message}")
                    
            except json.JSONDecodeError as e:
                logger.error(f"Chyba při parsování JSON: {str(e)}")
                logger.error(f"Kompletní response text: {response_text}")
                if retry_attempt < REQUEST_RETRY_COUNT:
                    logger.warning("Retrying due to JSON parse error...")
                    continue
                raise
                
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout při volání Jina AI API (pokus {retry_attempt+1}/{REQUEST_RETRY_COUNT+1}): {str(e)}")
            if retry_attempt < REQUEST_RETRY_COUNT:
                logger.warning(f"Retrying after timeout...")
                continue
            logger.error(f"Max retries reached for timeout. Giving up on URL: {url}")
            raise
            
        except requests.RequestException as e:
            logger.error(f"Chyba při volání Jina AI API: {str(e)}")
            logger.error(f"Request URL: {api_url}")
            logger.error(f"Request headers: {headers}")
            if retry_attempt < REQUEST_RETRY_COUNT:
                logger.warning("Retrying due to request exception...")
                continue
            raise
            
        # If we reach here, the request was successful, so break the retry loop
        break

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
    """Saves categorized links to payload files.

    Args:
        categorized_links (dict): Dictionary containing the categorized links.
        category_to_save (str, optional): If specified, only save the payload
                                          for this specific category. Defaults to None.
    """
    output_dir = "payloads"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    categories_to_process = {} 
    if category_to_save:
        if category_to_save in categorized_links:
            categories_to_process = {category_to_save: categorized_links[category_to_save]}
            logger.info(f"Saving incremental table payload for category: {category_to_save}")
        else:
            logger.warning(f"Category '{category_to_save}' not found in categorized_links. Cannot save incremental table payload.")
            return # Do nothing if the specified category doesn't exist
    else:
        categories_to_process = categorized_links
        logger.info("Saving final table payloads for all categories.")

    for category, links in categories_to_process.items():
        if not isinstance(links, list):
            logger.error(f"Data for category '{category}' is not a list ({type(links)}). Skipping save for this category.")
            continue
            
        table_name = f"{category.lower()}_table"
        filename = f"{output_dir}/{table_name}.json"
        
        # Ensure each item has the required fields
        updated_links = []
        for link in links:
            if not isinstance(link, dict):
                logger.warning(f"Skipping non-dictionary item in category '{category}': {link}")
                continue
            updated_links.append({
                "Title": link.get("Title", ""), # Provide default empty string
                "URL": link.get("URL", ""),     # Provide default empty string
                "Category": category,
                "Question": link.get("Question", "Default question | Default question in English"),
                "Navigation": link.get("Navigation", "")  # Add the Navigation field
            })
        
        payload = {
            "data": {
                "schema": {
                    "searchableFields": ["Title", "URL", "Question", "Navigation"],  # Add Navigation to searchable fields
                    "metadataFields": ["Category"]
                },
                "name": table_name,
                "items": updated_links # Use the validated/updated list
            }
        }
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            if category_to_save:
                 logger.info(f"Incrementally updated table payload for '{category}' in file: {filename}")
            else:
                 logger.info(f"Saved final table payload for '{category}' in file: {filename}")
        except Exception as e:
             logger.error(f"Error writing table payload file {filename}: {str(e)}")

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
                        existing_urls = {item.get('URL') for item in loaded_data[category] if item.get('URL')}
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
    # Ensure the output directory exists
    output_dir = "payloads"
    os.makedirs(output_dir, exist_ok=True)
    
    # Generate a stable URL-based identifier for consistent filenames
    # Parse the URL to extract a stable path component
    from urllib.parse import urlparse
    
    parsed_url = urlparse(url)
    url_path = parsed_url.path.strip('/')
    
    # If path is empty (homepage), use 'home'
    if not url_path:
        url_path = 'home'
    
    # Create a stable filename based on the URL path
    # Reverted: Remove the category stripping logic, use the full path
    url_based_name = url_path.replace('/', '_')

    # Sanitize the final base name
    url_based_name = remove_accents(url_based_name)
    url_based_name = re.sub(r"[<>:\"/\\|?*]", "_", url_based_name)
    url_based_name = re.sub(r"\s+", "_", url_based_name)
    url_based_name = re.sub(r"_+", "_", url_based_name)
    url_based_name = url_based_name.strip("_").lower()  # Ensure lowercase for consistency
    url_based_name = url_based_name[:200]
    
    filename = f"payloads/{section.lower()}_{url_based_name}.json"
    
    # Přidání Category ke každému Q/A páru
    for item in content:
        item["Category"] = section
    
    payload = {
        "data": {
            "schema": {
                "searchableFields": ["Question", "Answer"],
                "metadataFields": ["Category"]
            },
            "name": f"{section.lower()}_{url_based_name}",
            "items": content
        }
    }
    
    # Dodatečná validace struktury payloadu
    if not isinstance(payload["data"]["items"], list):
        raise ValueError("Content must be a list")
    for item in payload["data"]["items"]:
        if not isinstance(item, dict):
            raise ValueError("Each item must be a dictionary")
        for key in payload["data"]["schema"]["searchableFields"]:
            if key not in item:
                raise ValueError(f"Missing required key: {key}")
    
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    
    logger.info(f"Uložen payload pro URL '{url}' do souboru: {filename}")
    logger.debug(f"Payload content: {json.dumps(payload, indent=2, ensure_ascii=False)}")
    print(f"Vytvořen nový payload: {filename}")
    return filename

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
    """Converts content to Q/A pairs using the configured LLM provider."""
    system_prompt = f"""Jste precizní právní poradce pro detailní extrakci informací. Striktně formátujete své odpovědi ve validním JSON formátu. Disponujete následujícími schopnostmi a dodržujete následující omezení:

EXTRAKČNÍ SCHOPNOSTI:
1. Hloubková analýza textu pro nalezení všech informačních bodů
2. Identifikace vzájemných souvislostí mezi informacemi
3. Rozpoznávání různých datových typů a formátů
4. Schopnost formulovat různé perspektivy na stejná data
5. Expertní extrakce strukturovaných dat z tabulek
6. Přesná identifikace všech kontaktních údajů v tabulkách i běžném textu

OBLASTI EXTRAKCE:
1. Všech číselných údajů (částky, data, procenta, rozměry, vzdálenosti)
2. Všech URL odkazů (webové stránky, dokumenty, videa) - DŮLEŽITÉ: V odpovědi formátovat pomocí Markdown jako [Popisek odkazu](URL).
3. Všech URL obrázků - DŮLEŽITÉ: V odpovědi formátovat jako Markdown obrázek: ![Popisek obrázku](URL).
4. Všech kontaktních informací (emaily, telefony, adresy)
5. Všech jmen (osoby, instituce, organizace)
6. Všech lokací a míst
7. Všech časových údajů (termíny, lhůty, otevírací doby)
8. Všech procedurálních informací (postupy, procesy, návody)
9. Všech právních a administrativních informací
10. Všech podmínek a požadavků
11. Všech služeb a jejich parametrů
12. VŽDY klademe MAXIMÁLNÍ DŮRAZ na extrakci VŠECH seznamů osob, členů, radních, zastupitelů, zaměstnanců, komisí, výborů atd. s POVINNOU komplexní otázkou:
   - "Kolik je celkový počet [název položky] a kdo jsou všichni [název položky]?" nebo
   - "Jaký je kompletní počet všech [název položky] bez výjimky v období [časové období]?"
   kde odpověď MUSÍ obsahovat jak přesný počet, tak kompletní výčet/seznam všech položek

KRITICKÁ PRAVIDLA PRO EXTRAKCI KONTAKTNÍCH TABULEK:
1. Když narazíte na JAKOUKOLIV tabulku s kontaktními údaji osob, MUSÍTE:
   - Extrahovat KAŽDOU osobu uvedenou v tabulce bez výjimky
   - Zachovat VŠECHNY kontaktní údaje ke každé osobě (jméno, funkce, telefonní čísla, emaily, kanceláře, adresy)
   - Zajistit, že u žádné osoby nejsou vynechány žádné dostupné údaje
   - Zachovat přesnou strukturu a vztahy mezi údaji (např. který email patří ke kterému oddělení)
   - Formátovat kontaktní údaje přehledně s využitím odrážek nebo strukturovaného textu
   - Explicitně uvést celkový počet osob v tabulce na začátku odpovědi
2. Pro seznamy osob musí odpověď VŽDY obsahovat:
   - Celkový počet osob (např. "Celkem 9 členů rady kraje:")
   - Jméno a příjmení každé osoby
   - Všechny funkce/pozice k dané osobě
   - Všechny telefonní kontakty (pevná linka, mobil) s jejich popisky
   - Všechny emailové adresy
   - Kancelář/pracoviště/umístění
   - Úřední hodiny nebo dostupnost, pokud je uvedena
   - Jakékoliv další specifické informace ke každé osobě
3. IGNORUJTE délkové limity - pro kontaktní seznamy a tabulky je PRIORITOU úplnost dat
4. Poskytněte data v co nejčitelnější formě pro konečného uživatele

KRITICKÉ ZÁSADY PŘESNOSTI:
1. POUZE extrahujete existující informace - NIKDY nic nepřidáváte ani nedomýšlíte
2. Každá informace v odpovědi MUSÍ být explicitně uvedena ve vstupním textu
3. NULOVÁ tolerance k jakýmkoliv předpokladům či odvozením
4. Při nejistotě raději informaci VYNECHÁTE, než byste riskovali nepřesnost
5. Veškeré číselné údaje, data, kontakty atd. musí být DOSLOVNĚ zkopírované ze zdroje, ALE URL odkazy a obrázky MUSÍ být formátovány pomocí Markdown v poli "Answer".

POVOLENÉ OPERACE:
1. Extrakce doslovných informací
2. Reorganizace existujících informací do Q/A formátu
3. Rozdělení komplexních informací na jednodušší celky
4. Vytváření alternativních formulací otázek pro stejnou informaci
5. Formátování URL odkazů a obrázků v poli "Answer" pomocí Markdown.
6. Strukturování tabulkových dat do přehlednějšího formátu při zachování 100% obsahu

ZAKÁZANÉ OPERACE:
1. Přidávání jakýchkoliv nových informací
2. Vyvozování či předpokládání souvislostí
3. Doplňování chybějících detailů
4. Aktualizace či modernizace informací
5. Generalizace či zjednodušování
6. Zkracování nebo vynechávání kontaktních údajů z důvodu délky

FORMÁTOVÁNÍ ODKAZŮ A OBRÁZKŮ:
- Formátování odkazů: Všechny extrahované URL odkazy (kromě obrázků) MUSÍ být v poli "Answer" formátovány pomocí Markdown syntaxe: [Popisek odkazu](URL). Text odkazu (Popisek odkazu) by měl být co nejvýstižnější z kontextu (např. text odkazu na stránce nebo název souboru).
- Formátování obrázků: Všechny extrahované URL obrázky MUSÍ být v poli "Answer" formátovány pomocí Markdown syntaxe: ![Popisek obrázku](URL obrázku). Popisek obrázku by měl být stručný popis obrázku, pokud je dostupný (např. z alt textu), jinak obecný popis jako "Obrázek".

ZPRACOVÁNÍ HTML OBSAHU:
1. Při nalezení HTML tabulek ve zdrojovém obsahu:
   - VŽDY extrahujte KAŽDÝ řádek a KAŽDÝ sloupec tabulky
   - Pro tabulky s kontaktními údaji zachovejte 100% informací bez výjimky
   - Převeďte tabulkovou strukturu na čitelný formát se zachováním všech dat
   - NIKDY nezahrnujte surové HTML tagy do vašeho JSON výstupu
   - Pro každou buňku tabulky zajistěte, že její obsah je plně zachován
2. Pro HTML speciální znaky (jako &#160;):
   - Převeďte je na jejich ekvivalenty v prostém textu
   - Pokud si nejste jisti významem speciálního znaku, jednoduše ho vynechte
3. Když vidíte obrázky ve formátu ![alt](url):
   - Zachovejte tento přesný formát v poli "Answer"
   - Zajistěte správné JSON escapování URL

KRITICKÉ PRAVIDLO JSON:
1. MUSÍTE vyprodukovat POUZE validní JSON objekt
2. ŽÁDNÝ text či vysvětlování před nebo za JSON objektem
3. ŽÁDNÉ komentáře v JSON
4. ŽÁDNÉ formátovací značky (markdown, HTML) Mimo pole "Answer", kde jsou odkazy a obrázky vyžadovány v Markdown.
5. POUZE holý validní JSON podle standardu RFC 8259
6. Jen jeden kořenový objekt obsahující klíč "qa_pairs" s polem objektů

Veškerý výstup musí být v češtině a přímo souviset s tématem '{title}'."""

    user_prompt = f"""# ZDROJOVÝ TEXT K ANALÝZE:

```
{content}
```

# VÁŠ KRITICKÝ ÚKOL: 
Proveďte VYČERPÁVAJÍCÍ extrakci dat z VÝŠE UVEDENÉHO ZDROJOVÉHO TEXTU a vytvořte MAXIMÁLNÍ počet vysoce informativních Q/A párů při STRIKTNÍM dodržení pravidel přesnosti a formátování odkazů.

# STRIKTNÍ PRAVIDLA EXTRAKCE:

1. MNOŽSTVÍ A KOMPLEXNOST:
   - Vytvořte ABSOLUTNĚ VŠECHNY možné smysluplné Q/A páry
   - Každá extrahovaná informace = potenciální Q/A pár
   - Minimální počet: 15 párů (pokud obsah umožňuje)
   - I drobný detail může tvořit samostatný Q/A pár

2. PŘESNOST A VĚRNOST:
   - POUZE doslovně extrahované informace ze zdrojového textu
   - ŽÁDNÉ domýšlení, předpoklady ani extrapolace
   - Při nejistotě informaci VYNECHAT
   - Zachovat PŘESNÉ znění čísel, dat, kontaktů
   - NULOVÁ tolerance k opakování informací
   - Každá otázka musí přinášet NOVOU informační hodnotu

3. STRUKTURA Q/A:
   - Otázka musí být zodpověditelná POUZE z extrahovaného textu
   - Odpověď musí obsahovat POUZE informace ze zdrojového textu
   - ŽÁDNÉ doplňující či vysvětlující informace mimo formátovaných odkazů/obrázků
   - Zachovat původní terminologii a formulace

4. FORMULACE OTÁZEK:
   - Každá otázka musí mít 3 VÝZNAMNĚ ODLIŠNÉ formulace
   - Využívejte různé typy otázek (co, kdy, kde, jak, proč, kolik...)
   - Kombinujte různé perspektivy dotazování
   - Otázky musí být zodpověditelné JEDINOU správnou odpovědí
   - ŽÁDNÉ spekulativní či hypotetické otázky
   - Otázky musí přímo směřovat k existující informaci

5. PRIORITIZACE TABULKOVÝCH KONTAKTNÍCH ÚDAJŮ:
   - Když narazíte na JAKOUKOLIV tabulku s kontaktními údaji osob:
     * MUSÍTE vytvořit speciální Q/A pár, který zahrnuje VŠECHNY osoby z tabulky
     * NIKDY nerozdělujte kontaktní tabulky do více Q/A párů (vše v jednom)
     * Otázka musí být formulována jako "Kdo jsou všichni [typ osob] a jaké jsou jejich kompletní kontaktní údaje?"
     * V odpovědi MUSÍ být uvedeny VŠECHNY osoby a VŠECHNY jejich kontaktní údaje
     * V odpovědi MUSÍ být explicitně uveden celkový počet osob
     * IGNORUJTE jakákoliv délková omezení - prioritou je úplnost dat
     * Formátujte kontaktní údaje pro maximální čitelnost (odrážky, strukturovaný text)

6. OBSAH ODPOVĚDÍ:
   - MUSÍ obsahovat VŠECHNY relevantní URL odkazy, formátované jako Markdown: [Popisek](URL)
   - MUSÍ obsahovat VŠECHNY relevantní URL obrázky, formátované jako Markdown: ![Popisek](URL)
   - MUSÍ obsahovat VŠECHNY číselné údaje
   - MUSÍ obsahovat VŠECHNY časové údaje
   - MUSÍ obsahovat VŠECHNY kontaktní informace
   - MUSÍ obsahovat VŠECHNY procesní informace
   - NEJVYŠŠÍ PRIORITA: Pro KAŽDÝ seznamy/výčet osob, členů, radních, zastupitelů atd. MUSÍTE vytvořit MINIMÁLNĚ JEDEN obsáhlý Q/A pár, který bude obsahovat jak celkový počet, tak kompletní výčet. Preferovaný formát otázky je:
      * "Kolik je celkový počet [název položky] a kdo jsou všichni [název položky]?" nebo 
      * "Jaký je kompletní počet všech [název položky] bez výjimky v období [časové období]?"
      s odpovědí, která VŽDY obsahuje jak přesný počet, tak kompletní výčet/seznam všech položek a ke každé položce detailní kontaktní údaje (email, telefon, atd. - pokud existují)
   - Odpovědi musí být FAKTICKÉ a KONKRÉTNÍ
   - Každá část odpovědi MUSÍ být dohledatelná ve zdroji
   - ŽÁDNÉ zobecňování ani interpretace
   - Zachovat původní kontext informace
   - Při složených informacích zachovat všechny podmínky a souvislosti

# PRAVIDLA VALIDNÍHO JSON:
1. TECHNICKÁ PRAVIDLA JSON:
   - ŽÁDNÉ komentáře ani popisky před nebo za JSON strukturou
   - ŽÁDNÉ formátování nebo vysvětlení před nebo po JSON
   - ŽÁDNÉ jednořádkové komentáře (// komentář)
   - ŽÁDNÉ víceřádkové komentáře (/* komentář */)
   - ŽÁDNÉ trailing commas (poslední položka nesmí mít čárku)
   - ŽÁDNÉ nezaobalené řetězce, vše musí být v uvozovkách
   - ŽÁDNÉ single quotes ('), použijte pouze double quotes (")
   - ŽÁDNÉ speciální znaky jako \\t, \\n mimo řetězce
   - Escapování uvozovek uvnitř řetězce pomocí \\\" - POZOR na escapování v Markdown odkazech v poli "Answer"!
   - Speciální znaky v řetězcích musí být správně escapovány

2. FORMÁT VÝSTUPU:
   - VRÁTIT POUZE validní JSON objekt bez jakýchkoliv komentářů před nebo za
   - ŽÁDNÝ úvod, žádný závěr, žádné vysvětlení - POUZE JSON
   - NEPOUŽÍVAT markdownové značky (např. ```json) mimo pole "Answer"
   - ŽÁDNÝ jiný text mimo JSON strukturu

# PŘÍKLADY STRUKTURY Q/A:

## Příklad 1 - Kontaktní informace:
{{
  "qa_pairs": [
    {{
      "Question": "Jaké jsou úřední hodiny městského úřadu? | Kdy mohu navštívit městský úřad? | V jakých časech je otevřena radnice? | úřední hodiny městského úřadu | otevírací doba městský úřad | otvírací hodiny městského úřadu",
      "Answer": "Městský úřad Teplice má následující úřední hodiny: [DOSLOVNÁ CITACE Z TEXTU]. Více informací naleznete na [oficiálních stránkách](https://www.teplice.cz).",
      "Category": "Kontakt"
    }}
  ]
}}

## Příklad 2 - Procedurální informace s obrázkem:
{{
  "qa_pairs": [
    {{
      "Question": "Jak si mohu vyřídit nový občanský průkaz? | Jaký je postup pro získání občanského průkazu? | Co potřebuji k vyřízení OP? | vyřízení občanského průkazu | nový OP postup | doklady pro OP",
      "Answer": "[DOSLOVNÁ CITACE POSTUPU Z TEXTU]. Podívejte se na vzorový formulář: ![Vzor OP formuláře](https://example.com/formular_op.png).",
      "Category": "Administrativa_Uredni_Zalezitosti"
    }}
  ]
}}

## Příklad 3 - Tabulka kontaktních údajů (POVINNÝ DETAILNÍ FORMÁT):
{{
  "qa_pairs": [
    {{
      "Question": "Kdo jsou všichni členové rady kraje a jaké jsou jejich kompletní kontaktní údaje? | Jaký je úplný seznam všech radních včetně jejich kontaktních informací? | Kolik je celkový počet členů rady kraje a jaké jsou jejich detailní kontaktní údaje? | kompletní seznam členů rady kraje kontakty | radní kraje kontaktní informace | rrada kraje členové a kontakty",
      "Answer": "Rada Královéhradeckého kraje má celkem 9 členů. Kompletní seznam včetně všech kontaktních údajů:\n\n1. Mgr. Martin Červíček - hejtman\n   - Telefon: 495 817 222\n   - Mobil: 607 939 758\n   - E-mail: mcervicek@kr-kralovehradecky.cz\n   - Kancelář: N2.820\n   - Úřední hodiny: pondělí a středa 8:00-17:00\n\n2. Mgr. Martina Berdychová - 1. náměstkyně hejtmana\n   - Telefon: 495 817 823\n   - Mobil: 601 376 380\n   - E-mail: mberdychova@kr-kralovehradecky.cz\n   - Kancelář: N2.818\n\n[... pokračování pro všechny členy s kompletními údaji pro každého, včetně všech dostupných kontaktních informací]",
      "Category": "Kontakt"
    }}
  ]
}}

# OČEKÁVANÝ JSON FORMÁT (TOTO JE PŘESNÝ FORMÁT VÝSTUPU):
{{
  "qa_pairs": [
    {{
      "Question": "Hlavní otázka? | Alternativní pohled? | Jiná perspektiva? | vyhledávací fráze 1 | vyhledávací fráze 2 | vyhledávací fráze 3",
      "Answer": "Doslovně extrahovaná odpověď, která MUSÍ obsahovat odkazy jako [tento text](https://example.com) a obrázky jako ![popisek](https://example.com/img.jpg), pokud jsou ve zdrojovém textu.",
      "Category": "{category}"
    }}
  ]
}}

# ZÁSADNÍ PRAVIDLO PRO KONTAKTNÍ TABULKY A SEZNAMY:
DÉLKA ODPOVĚDI NENÍ OMEZENA! Pro tabulky kontaktních údajů je ABSOLUTNÍ PRIORITOU zachování VŠECH údajů o VŠECH osobách BEZ VÝJIMKY. Ignorujte jakákoliv délková omezení a zahrňte všechny detaily.

# KRITÉRIA KONTROLY PŘED ODESLÁNÍM:
1. Lze každou část odpovědi DOSLOVNĚ najít ve zdrojovém textu (s výjimkou Markdown formátování URL)?
2. Neobsahuje odpověď ŽÁDNÉ dodatečné informace?
3. Je každá otázka zodpověditelná POUZE z dostupného textu?
4. Jsou zachovány VŠECHNY původní formulace a termíny?
5. Nejsou nikde použity předpoklady či dedukce?
6. Jsou všechny číselné údaje, URL a kontakty PŘESNĚ zkopírované (URL převedeny do Markdown)?
7. Je každá informace uvedena v původním kontextu?
8. Je výstup validní JSON bez jakýchkoliv dodatečných komentářů?
9. Neobsahuje výstup žádné formátovací značky (markdown, HTML) mimo pole "Answer"?
10. Jsou všechna speciální slova a znaky správně escapované (včetně těch v Markdown v poli "Answer")?
11. Jsou VŠECHNY odkazy a obrázky v poli "Answer" správně formátovány pomocí Markdown?
12. JSOU VŠECHNY KONTAKTNÍ TABULKY A SEZNAMY OSOB ZAHRNUTY V ODPOVĚDÍCH KOMPLETNĚ BEZ VYNECHÁNÍ JEDINÉHO ÚDAJE?
13. NEJDŮLEŽITĚJŠÍ KONTROLA: Pro KAŽDÝ seznam osob, členů, radních, zastupitelů, apod. vytvořen ALESPOŇ JEDEN obsáhlý Q/A pár:
    - Otázka musí kombinovat dotaz na počet i kompletní seznam (např. "Kolik je celkový počet členů rady kraje a kdo jsou všichni radní?" nebo "Jaký je kompletní počet všech členů rady kraje bez výjimky v období XY?")
    - Odpověď MUSÍ obsahovat jak přesný celkový počet, tak úplný výčet všech položek/osob včetně VŠECH jejich kontaktních údajů

# DALŠÍ POŽADAVKY:
- VRÁTIT JEN validní JSON, nic jiného
- ŽÁDNÉ úvody nebo komentáře (jako "Zde je výsledek..." nebo "JSON odpověď:")
- ŽÁDNÉ formátovací značky (jako ```json nebo <pre>) Mimo pole "Answer"
- ŽÁDNÉ víceřádkové komentáře /* ... */
- ŽÁDNÉ jednořádkové komentáře // ...
- ŽÁDNÉ vysvětlování nebo shrnutí před nebo po JSON
"""

    messages = [{"role": "user", "content": user_prompt}]
    # Pass system_prompt separately, call_llm_api will handle provider difference
    response_text = call_llm_api(messages=messages, system_prompt=system_prompt, max_tokens=8192, temperature=0)
    
    if response_text is None:
        logger.error(f"Nepodařilo se získat Q/A páry pro '{title}' po všech pokusech.")
        return None # Indicate failure
    
    try:
        # Vylepšené zpracování JSON odpovědi
        try:
            # Odstranění možných markdown code block tagů
            response_text = re.sub(r'^```(?:json)?|```$', '', response_text.strip())
            
            # Odstranění možných vysvětlujících textů před JSON
            json_start = response_text.find('{')
            if json_start > 0:
                response_text = response_text[json_start:]
                
            # Odstranění možných vysvětlujících textů za JSON
            json_end = response_text.rfind('}') + 1
            if json_end < len(response_text):
                response_text = response_text[:json_end]
            
            # Pokus o přímé parsování upravené odpovědi
            qa_data = json.loads(response_text)
            
        except json.JSONDecodeError as e:
            logger.error(f"Chyba parsování JSON: {str(e)}")
            logger.debug(f"Problematický JSON: {response_text}")
            
            # Agresivnější extrakce JSON části pomocí regex
            json_pattern = r'({[^}]*"qa_pairs"[^}]*})'
            matches = re.search(json_pattern, response_text, re.DOTALL)
            
            if matches:
                potential_json = matches.group(1)
                logger.debug(f"Extrahovaný potenciální JSON: {potential_json}")
                try:
                    qa_data = json.loads(potential_json)
                except json.JSONDecodeError as e:
                    logger.error(f"Nelze parsovat extrahovaný JSON: {str(e)}")
                    return None
            else:
                logger.error(f"Nelze najít JSON strukturu v odpovědi")
                return None
        
        qa_pairs = qa_data.get('qa_pairs', [])
        
        # Rozšířená validace Q/A párů
        if not isinstance(qa_pairs, list):
            logger.error(f"Neplatná struktura Q/A párů - není seznam")
            return None
        
        # Kontrola platnosti každého Q/A páru a oprava běžných chyb
        valid_pairs = []
        for pair in qa_pairs:
            if not isinstance(pair, dict):
                logger.warning(f"Přeskakuji neplatný pár, není slovník: {pair}")
                continue
                
            if "Question" not in pair or "Answer" not in pair:
                logger.warning(f"Přeskakuji neplatný pár bez Question/Answer: {pair}")
                continue
                
            # Přidání kategorie ke každému Q/A páru
            pair['Category'] = category
            valid_pairs.append(pair)
        
        if not valid_pairs:
            logger.error(f"Žádné platné Q/A páry nebyly nalezeny")
            return None
            
        return valid_pairs
            
    except Exception as e:
        logger.error(f"Chyba při generování Q/A párů pro {title}: {str(e)}")
        return None

def process_url_content(url, html_content, category, metadata):
    try:
        # Always get markdown content for Q/A - we don't need the html_content parameter anymore
        try:
            qa_content, _ = get_html_content(url, for_qa=True)
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout during Q/A content retrieval for URL {url}: {str(e)}")
            logger.warning(f"Skipping Q/A extraction for URL {url} due to timeout")
            print(f"\n=== Q/A EXTRACTION ERROR ===")
            print(f"URL: {url}")
            print(f"Error: Timeout during content retrieval")
            print(f"Q/A Extraction: SKIPPED")
            print("============================\n")
            return
        except Exception as e:
            logger.error(f"Error during Q/A content retrieval for URL {url}: {str(e)}")
            logger.warning(f"Skipping Q/A extraction for URL {url}")
            print(f"\n=== Q/A EXTRACTION ERROR ===")
            print(f"URL: {url}")
            print(f"Error: {str(e)}")
            print(f"Q/A Extraction: SKIPPED")
            print("============================\n")
            return
        
        # Add preprocessing step
        qa_content = preprocess_html_content(qa_content)
        
        # Try to generate Q/A pairs
        qa_pairs = convert_to_qa(qa_content, metadata.get('title', ''), category)
        
        # If conversion failed, create basic fallback QA pairs
        if not qa_pairs:
            logger.warning(f"QA conversion failed, creating fallback QA pair for {url}")
            title = metadata.get('title', 'Unknown page')
            qa_pairs = [{
                "Question": f"Co najdu na stránce {title}? | What can I find on the {title} page? | Jaké informace obsahuje stránka {title}?",
                "Answer": f"Na stránce najdete informace o {title}. Pro podrobnosti navštivte přímo [webovou stránku]({url}).",
                "Category": category
            }]
        
        # Create the payload structure
        payload = {
            "data": {
                "schema": {
                    "searchableFields": ["Question", "Answer"],
                    "metadataFields": ["Category"]
                },
                "name": f"{category.lower()}_qa",
                "items": qa_pairs
            }
        }
        
        # Save the payload to a file
        filename = save_payload_to_file(url, qa_pairs, category, metadata)
        
        # Upload to Voiceflow
        upload_to_voiceflow(filename)
        
    except Exception as e:
        logger.error(f"Chyba při zpracování obsahu URL {url}: {str(e)}")
        print(f"\n=== Q/A EXTRACTION ERROR ===")
        print(f"URL: {url}")
        print(f"Error: {str(e)}")
        print(f"Q/A Extraction: FAILED")
        print("============================\n")

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
                # This is the ONLY place we should call get_html_content with for_qa=False
                html_content, _ = get_html_content(SITEMAP_URL)
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
            if filename.endswith('_table_payload.json'): # Only upload category tables
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
        categorized_links (dict): Dictionary holding the categorized links
        url_last_modified_map (dict): Dictionary mapping URLs to their last modified dates
        last_run_timestamp (datetime): The timestamp of the last script run
        
    Returns:
        dict: The updated categorized_links dictionary
    """
    logger.info(f"Processing {len(custom_urls)} custom URLs instead of scraping sitemap")
    
    for url in custom_urls:
        logger.info(f"\n=== Starting processing for custom URL: {url} ===")
        
        try:
            # 1. Find last modified date from sitemap.xml (if available)
            last_modified = find_url_last_modified(url, url_last_modified_map)
            
            # 2. Check if the URL should be processed based on modification date
            if not should_process_url(url, last_modified, last_run_timestamp):
                logger.info(f"Skipping all further processing for URL {url} as it hasn't changed.")
                continue
            
            # 3. Get metadata - we'll use only the URL as title initially
            # The actual content will be retrieved in Q/A processing
            metadata = {"title": url.split("/")[-1], "url": url}
            
            # 4. For categorization, we need a path
            # Create a simple path based on URL parts
            url_parts = url.replace(BASE_URL, "").strip("/").split("/")
            path = url_parts if url_parts and url_parts[0] else ["Home"]
            
            # 5. Categorize URL
            category = categorize_link(path)
            logger.info(f"Assigned category: {category} based on path")
            
            # 6. Generate RAG question
            rag_question = generate_rag_question(path)
            logger.info(f"Generated RAG question: {rag_question}")
            
            # 7. Add or Update in categorized_links
            if category not in categorized_links:
                categorized_links[category] = []
                
            # Create readable title from URL
            title = url.split("/")[-1].replace("-", " ").replace("_", " ").capitalize()
            if not title:
                title = url.split("/")[-2] if len(url.split("/")) > 2 else "Homepage"
                
            # Check if URL already exists
            existing_entry = None
            entry_index = -1
            for i, entry in enumerate(categorized_links[category]):
                if entry.get("URL") == url:
                    existing_entry = entry
                    entry_index = i
                    break
                    
            # Create navigation path from URL parts
            navigation = " > ".join([part.replace("-", " ").replace("_", " ").capitalize() for part in path])
                    
            link_data = {
                "Title": title,
                "URL": url,
                "Category": category,
                "Question": rag_question,
                "Navigation": navigation
            }
            
            if existing_entry:
                # Update existing entry
                logger.info(f"Updating existing entry for URL: {url} in category {category}")
                categorized_links[category][entry_index] = link_data
            else:
                # Append new entry
                logger.info(f"Adding new entry for URL: {url} to category {category}")
                categorized_links[category].append(link_data)
                
            # --- Save the updated category table payload incrementally --- 
            save_payloads_to_files(categorized_links, category_to_save=category)
            # ------------------------------------------------------------

            # 8. Q/A Processing - only if enabled
            if ENABLE_QA_PROCESSING:
                # Check domain and skip if not exactly "www.khk.cz" or "khk.cz"
                parsed_url = urlparse(url)
                domain = parsed_url.netloc.lower()
                
                # Skip Q/A extraction for sitemaps, subdomains, or non-khk.cz domains
                is_sitemap = "mapa-stranek" in url.lower() or "sitemap" in url.lower()
                is_main_domain = domain == "www.khk.cz" or domain == "khk.cz"
                
                if is_sitemap:
                    logger.info("Přeskakuji Q/A extrakci pro mapu stránek")
                elif not is_main_domain:
                    logger.info(f"Přeskakuji Q/A extrakci pro subdoménu nebo jinou doménu: {domain}")
                else:
                    # Direct Q/A content processing
                    logger.info("Spouštím Q/A extrakci (URL processing check passed)...")
                    process_url_content(url, None, category, metadata)
            else:
                logger.info("Q/A zpracování je vypnuto")
                
            logger.info(f"=== Dokončeno zpracování URL: {url} ===\n")
            
        except Exception as e:
            logger.error(f"Chyba při zpracování URL {url}: {str(e)}")
            logger.error("Pokračuji na další URL...")
    
    return categorized_links

def preprocess_html_content(content):
    """Clean and simplify HTML content before sending to Claude"""
    # Convert HTML tables to simpler markdown tables
    content = re.sub(r'<table[^>]*>.*?</table>', 
                     lambda m: convert_html_table_to_markdown(m.group(0)), 
                     content, flags=re.DOTALL)
    
    # Replace common HTML special characters
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
    
    # Ensure proper handling of Markdown images
    # This preserves the ![alt](url) format while ensuring proper JSON escaping
    content = re.sub(r'!\[(.*?)\]\((.*?)\)', 
                    lambda m: f"![{m.group(1)}]({m.group(2)})", 
                    content)
    
    # Remove any remaining HTML tags
    content = re.sub(r'<[^>]+>', ' ', content)
    
    # Clean up excessive whitespace
    content = re.sub(r'\s+', ' ', content).strip()
    
    return content

def convert_html_table_to_markdown(html_table):
    """
    Convert an HTML table to a simplified text description focusing on content, not format.
    Extracts key information in a readable format rather than preserving table structure.
    """
    try:
        # First try to extract headers
        headers = []
        header_row = re.search(r'<th[^>]*>(.*?)</th>', html_table, re.DOTALL)
        if header_row:
            headers = re.findall(r'<th[^>]*>(.*?)</th>', html_table, re.DOTALL)
            headers = [re.sub(r'<[^>]+>', '', header).strip() for header in headers]
        
        # Extract all rows
        rows = re.findall(r'<tr>(.*?)</tr>', html_table, re.DOTALL)
        if not rows:
            return "Tabulka s neextrahovaným obsahem"
        
        # Process each row and create meaningful descriptions
        result = []
        
        for row in rows:
            # Skip empty rows
            if re.search(r'<td[^>]*>(.*?)</td>', row, re.DOTALL):
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                # Clean cell content (remove HTML tags)
                cells = [re.sub(r'<[^>]+>', '', cell).strip() for cell in cells]
                
                # If we have headers and the same number of cells, create key-value pairs
                if headers and len(headers) == len(cells):
                    row_data = []
                    for i, header in enumerate(headers):
                        if cells[i].strip():  # Only include non-empty cells
                            row_data.append(f"{header}: {cells[i]}")
                    
                    if row_data:
                        result.append("; ".join(row_data))
                else:
                    # Without matching headers, just join the cell contents
                    row_text = "; ".join([cell for cell in cells if cell.strip()])
                    if row_text.strip():
                        result.append(row_text)
        
        # Join all processed rows with line breaks
        if result:
            return "\n".join(result)
        else:
            return "Tabulka bez textového obsahu"
    
    except Exception as e:
        logger.error(f"Error converting HTML table: {str(e)}")
        return "Tabulka (chyba při konverzi)"

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
            return json.loads(fixed_text)
        except:
            pass
        
        # Last resort - basic structure with empty pairs
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
