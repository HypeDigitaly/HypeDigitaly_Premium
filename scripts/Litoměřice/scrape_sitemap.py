import requests
from bs4 import BeautifulSoup
import logging
import json
import anthropic
import time
import os
from urllib.parse import urljoin, urlparse
import argparse
from datetime import datetime, timedelta
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
# DATE FILTERING
# ============================================================================
FILTER_YEAR = 2025  # Set the year for date filtering - URLs with dates >= this year will be included

# ============================================================================
# API KEYS
# ============================================================================
CLAUDE_API_KEY = "REMOVED-ANTHROPIC-KEY"
JINA_AI_API_KEY = "REMOVED-JINA-KEY"
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"

# ============================================================================
# URL CONFIGURATION
# ============================================================================
BASE_URL = "https://www.litomerice.cz"
SITEMAP_URL = "https://www.litomerice.cz/component/osmap/?view=xml&id=1&format=xml"

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

# ============================================================================
# HTTP REQUEST SETTINGS
# ============================================================================
REQUEST_TIMEOUT = 30
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
    "Kontakt",
    "Administrativa_Uredni_Zalezitosti",  # This now serves as the parent category
    "Media_Komunikace",
    "Kultura_Pamatkova_Pece",
    "Dotace",
    "Charakteristika_Mesta",
    "Strategicke_Dokumenty",
    "Krizove_Situace",
    "Rozvoj_Projekty",
    "Finance_Hospodareni",
    "Socialni_Pece",
    "Ukrajina",
    "Uzemni_Planovani_Stavebni_Rad",
    "Vzdelavani",
    "Zdravotnictvi",
    "Zivotni_Prostredi_Zemedelstvi"
]

# Add this constant after CATEGORIES definition
ADMINISTRATIVE_SUBCATEGORIES = [
    "Komise_Rady_Mesta",
    "Usneseni_Rady_Mesta",
    "Redakcni_Rada",
    "Usneseni_Zastupitelstvo",
    "Zapisy_Vyboru",
    "Vyrocni_Zpravy",
    "Verejnopravni_Smlouvy",
    "Uredni_Deska",
    "Dokumenty_Mestskeho_Uradu",
    "Vyhlasky"
]

# Define main section paths that should always be kept
MAIN_SECTION_PATHS = [
    '/komise-rady-mesta',
    '/usneseni-rady',
    '/redakcni-rada',
    '/aktuality',
    '/usneseni-zastupitelstva',
    '/podklady-zm',
    '/zapisy-vyboru',
    '/vyrocni-zpravy-zkon106',
    '/dotace',
    '/verejnospravnismlouvy',
    '/rozpocet-a-hospodareni-mesta',
    '/uredni-deska-online',
    '/vyhlasky-mesta',
    '/dokumenty-meu',
    '/kalendar-akci',
    '/vylety',
    '/restaurace',
    '/doprava',
    '/sport',
    '/kultura',
    '/zakladni-kontakty',
    '/vedenimesta',
    '/uzemni-plany',
    '/ztraty-a-nalezy'
    '/mapa-mesta'
]

# Define paths under which content should be date filtered
DATE_FILTERED_PATHS = [path + '/' for path in MAIN_SECTION_PATHS]

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
class URLAndAPIFilter(logging.Filter):
    def filter(self, record):
        # Only allow logs related to URL processing and API calls
        return any([
            "URL:" in record.getMessage(),
            "Claude API" in record.getMessage(),
            "Status code:" in record.getMessage(),
            "API response:" in record.getMessage(),
            "Kategorizace URL" in record.getMessage(),
            "Cesta:" in record.getMessage(),
            "Přiřazena kategorie:" in record.getMessage()
        ])

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(formatter)
file_handler.addFilter(URLAndAPIFilter())
file_handler.setLevel(logging.INFO)
logger.addHandler(file_handler)

# Configure console handler with same filter
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
console_handler.addFilter(URLAndAPIFilter())
console_handler.setLevel(logging.INFO)
logger.addHandler(console_handler)

def get_html_content(url, for_qa=False):
    logger.info(f"Získávání HTML obsahu z URL: {url}")
    logger.info(f"Režim Q/A: {for_qa}")
    api_url = f'https://r.jina.ai/{url}'
    
    if for_qa:
        headers = {
            'Accept': 'application/json',
            'Authorization': f'Bearer {JINA_AI_API_KEY}',
            'X-Return-Format': 'markdown',
            'X-Target-Selector': '#sp-main-body',
            'X-Wait-For-Selector': '#sp-main-body'
        }
    else:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {JINA_AI_API_KEY}",
            "X-Return-Format": "html"
        }
    
    logger.debug(f"Volání Jina.AI API s hlavičkami: {headers}")
    
    try:
        response = requests_retry_session().get(api_url, headers=headers, timeout=30)
        logger.debug(f"Status code: {response.status_code}")
        logger.debug(f"Response headers: {response.headers}")
        
        try:
            response_text = response.text
            logger.debug(f"Raw response: {response_text[:500]}...")
            data = response.json()
            logger.debug(f"Parsed JSON response: {json.dumps(data, indent=2, ensure_ascii=False)}")
            
            if data['status'] == 20000 and 'data' in data:
                if for_qa:
                    content = data['data'].get('content', '')
                else:
                    content = data['data'].get('html', '')
                
                if content:
                    logger.debug(f"Získaný obsah ({len(content)} znaků): {content[:200]}...")
                    if not for_qa:
                        title = BeautifulSoup(content, 'html.parser').title.string if content else ''
                        metadata = {
                            'title': title,
                            'url': data['data'].get('url', url)
                        }
                        return content, metadata
                    return content, {'url': url}
                else:
                    logger.error("Obsah nebyl nalezen v odpovědi API")
                    logger.debug(f"Data struktura: {data}")
                    raise ValueError("Obsah chybí v odpovědi API")
            else:
                error_message = data.get('status', 'Neznámá chyba')
                logger.error(f"Chyba při získávání obsahu: {error_message}")
                logger.debug(f"Kompletní response data: {data}")
                raise ValueError(f"Chyba API: {error_message}")
                
        except json.JSONDecodeError as e:
            logger.error(f"Chyba při parsování JSON: {str(e)}")
            logger.error(f"Kompletní response text: {response_text}")
            raise
            
    except requests.RequestException as e:
        logger.error(f"Chyba při volání Jina AI API: {str(e)}")
        logger.error(f"Request URL: {api_url}")
        logger.error(f"Request headers: {headers}")
        raise

def parse_sitemap(xml_content):
    """
    Parse XML sitemap content and extract URLs.
    
    Args:
        xml_content (str): XML content of the sitemap
        
    Returns:
        BeautifulSoup object: Parsed sitemap XML
    """
    try:
        # Parse XML content using BeautifulSoup with 'xml' parser
        soup = BeautifulSoup(xml_content, 'xml')
        
        if not soup.find('url'):  # Check if sitemap contains any URLs
            logger.warning("Sitemap neobsahuje žádné URL elementy.")
            return None
            
        return soup
        
    except Exception as e:
        logger.error(f"Chyba při parsování XML sitemapy: {str(e)}")
        return None

def categorize_link_claude(path):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    path_string = ' > '.join(path)
    
    prompt = f"""Jste precizní právní poradce pro detailní kategorizaci informací. Analyzujte tuto cestu z menu webových stránek města a přiřaďte NEJVHODNĚJŠÍ kategorii. Toto je NAPROSTO KRITICKÉ:

# Cesta ke kategorizaci (DŮLEŽITÉ: PRO SPRÁVNÉ PŘIŘAZENÍ JEDNÉ KONKRÉTNÍ KATEGORIE ANALYZUJTE DETAILNĚ CELISTVÝ POPISEK NÁSLEDUJÍCÍ ABSOLUTNÍ CESTY): {path_string}

# !!! ULTIMÁTNÍ PRAVIDLO - OTÁZKA ŽIVOTA A SMRTI !!!
MUSÍTE vybrat PRÁVĚ JEDNU kategorii. Není ABSOLUTNĚ přípustné nevybrat žádnou nebo váhat.
Je to Vaše KRITICKÁ povinnost vybrat tu NEJVHODNĚJŠÍ kategorii, i kdyby to mělo být Vaše poslední rozhodnutí.
Neexistuje možnost "nevím" nebo "nejsem si jistý", nebo "nezařazeno" - MUSÍTE se rozhodnout!
Lepší JAKÁKOLIV kategorie než ŽÁDNÁ kategorie!

# DOSTUPNÉ KATEGORIE PRO PŘIŘAZENÍ (VYBÍRAT POUZE A JEN Z NÍŽE ZMÍNĚNÝCH KATEGORIÍ):
{', '.join(CATEGORIES)}

# KRITICKÁ PRAVIDLA:
1. MUSÍTE přiřadit přesně JEDNU kategorii ze seznamu výše - NENÍ CESTY ZPĚT
2. "Nezařazeno" je ABSOLUTNĚ ZAKÁZÁNO - neexistuje situace, kdy by nešlo přiřadit kategorii
3. I když je volba obtížná, MUSÍTE vybrat tu nejvíce relevantní kategorii
4. Pokud si nejste jisti, použijte tato záložní pravidla:
   - Pro služby/podniky/restaurace -> "Kultura_Pamatkova_Pece"
   - Pro dopravu/infrastrukturu -> "Rozvoj_Projekty"
   - Pro sportovní/rekreační zařízení -> "Kultura_Pamatkova_Pece"
   - Pro turistické atrakce/městská zařízení -> "Charakteristika_Mesta"
   - Pro obecné informace o městě -> "Kontakt"
   - Pro cokoliv, co souvisí s úřadem -> "Administrativa_Uredni_Zalezitosti"
   - Pro jakékoliv dokumenty města -> "Dokumenty_Mestskeho_Uradu"

# PAMATUJTE: Nepřiřazení kategorie NENÍ MOŽNOST. Raději přiřaďte kategorii, která není 100% přesná, než žádnou. Toto je Váš JEDINÝ a NEJDŮLEŽITĚJŠÍ úkol. Selhat není možné.

# POKYNY PRO KATEGORIZACI:
- Kontakt: Kontaktní informace, úřední hodiny, přímé komunikační kanály
- Administrativa_Uredni_Zalezitosti: Administrativní postupy a úřední záležitosti
- Komise_Rady_Mesta: Informace o komisích a radě města
- Usneseni_Rady_Mesta: Usnesení z jednání rady města
- Redakcni_Rada: Informace o redakční radě
- Usneseni_Zastupitelstvo: Usnesení ze zasedání zastupitelstva
- Zapisy_Vyboru: Zápisy z jednání výborů
- Vyrocni_Zpravy: Výroční zprávy města
- Verejnopravni_Smlouvy: Veřejnoprávní smlouvy
- Uredni_Deska: Informace z úřední desky
- Dokumenty_Mestskeho_Uradu: Dokumenty městského úřadu
- Vyhlasky: Městské vyhlášky a nařízení
- Media_Komunikace: Zprávy, oznámení, mediální vztahy
- Kultura_Pamatkova_Pece: Kulturní akce, památky, restaurace, sportovní zařízení, rekreační oblasti, místní podniky
- Dotace: Informace o dotacích
- Charakteristika_Mesta: Obecné informace o městě, turistické info
- Strategicke_Dokumenty: Strategické dokumenty města
- Krizove_Situace: Informace o krizových situacích
- Rozvoj_Projekty: Infrastruktura, doprava, rozvojové projekty
- Finance_Hospodareni: Finance a hospodaření města
- Socialni_Pece: Sociální péče a služby
- Ukrajina: Informace týkající se Ukrajiny
- Uzemni_Planovani_Stavebni_Rad: Územní plánování a stavební řád
- Vzdelavani: Vzdělávání, školy
- Zdravotnictvi: Zdravotnická zařízení, nemocnice
- Zivotni_Prostredi_Zemedelstvi: Životní prostředí a zemědělství

# FORMÁT ODPOVĚDI: Vraťte POUZE název kategorie, nic jiného."""

    for attempt in range(MAX_RETRIES):
        try:
            message = client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=50,
                temperature=0,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            
            category = message.content[0].text.strip()
            
            # Validate returned category
            if category not in CATEGORIES:
                logger.warning(f"Neplatná kategorie: {category}. Použití záložní kategorizace.")
                # Apply fallback categorization
                if any(kw in path_string.lower() for kw in ['restaurace', 'bistro', 'jidlo', 'sport', 'bazen', 'koupaliste', 'kurt']):
                    return ["Kultura_Pamatkova_Pece"]
                elif any(kw in path_string.lower() for kw in ['doprav', 'mhd']):
                    return ["Rozvoj_Projekty"]
                elif any(kw in path_string.lower() for kw in ['nemocnice', 'lekar', 'zdravi']):
                    return ["Zdravotnictvi"]
                else:
                    return ["Charakteristika_Mesta"]
            
            logger.info(f"Přiřazena kategorie: {category}")
            time.sleep(API_CALL_DELAY)
            return [category]
            
        except (InternalServerError, RateLimitError) as e:
            if attempt == MAX_RETRIES - 1:
                logger.error(f"Nepodařilo se kategorizovat po {MAX_RETRIES} pokusech: {str(e)}")
                return ["Charakteristika_Mesta"]  # Default fallback
            
            delay = INITIAL_RETRY_DELAY * (2 ** attempt) + random.uniform(0, 1)
            logger.warning(f"Chyba kategorizace, pokus {attempt + 1}/{MAX_RETRIES}. Čekání {delay:.2f} sekund.")
            time.sleep(delay)
        except Exception as e:
            logger.error(f"Neočekávaná chyba při kategorizaci: {str(e)}")
            return ["Charakteristika_Mesta"]  # Default fallback

def generate_rag_question(path):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    path_string = ' > '.join(path)
    
    prompt = f"""Based on the following absolute path from a website sitemap:

{path_string}

Create an open-ended, RAG-optimized question in both Czech and English that would help users find this specific page. The question should:
1. Be natural and conversational
2. Include key details from the path
3. Be suitable for semantic search
4. Help users find the exact page they're looking for
5. Include both Czech and English versions separated by " | "

Important abbreviations to expand in questions:
- "RM" = "Rada města" (City Council)
- "ZM" = "Zastupitelstvo města" (City Assembly)

Example format:
For path: "Dotace > Kotlíková dotace > Aktuality"
Output: "Kde najdu aktuality, články a důležité informace ke kotlíkovým dotacím? | Where do I find the latest articles, updates and important information regarding boiler subsidies?"

For path with abbreviation: "RM > Usnesení"
Output: "Kde najdu usnesení Rady města? | Where can I find City Council resolutions?"

Return ONLY the question pair without any additional text or formatting."""

    for attempt in range(MAX_RETRIES):
        try:
            message = client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=200,
                temperature=0,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            
            question = message.content[0].text.strip()
            time.sleep(API_CALL_DELAY)
            return question
            
        except (InternalServerError, RateLimitError) as e:
            if attempt == MAX_RETRIES - 1:
                logger.error(f"Failed to generate RAG question after {MAX_RETRIES} attempts: {str(e)}")
                return "Default question | Default question in English"
            
            delay = INITIAL_RETRY_DELAY * (2 ** attempt) + random.uniform(0, 1)
            logger.warning(f"Error generating RAG question, attempt {attempt + 1}/{MAX_RETRIES}. Waiting {delay:.2f} seconds before retry.")
            time.sleep(delay)

def extract_links(menu_item, path=[], categorized_links={}):
    if menu_item.name == 'li':
        link = menu_item.find('a')
        if link:
            current_path = path + [link.text.strip()]
            absolute_path = ' > '.join(current_path)
            absolute_url = urljoin(BASE_URL, link['href'])
            
            logger.info(f"=== Starting processing URL: {absolute_url} ===")
            logger.info(f"Path: {absolute_path}")
            
            try:
                # 1. Categorize URL
                categories = categorize_link_claude(current_path)
                logger.info(f"Assigned categories: {categories} based on path")
                
                # 2. Generate RAG question
                rag_question = generate_rag_question(current_path)
                logger.info(f"Generated RAG question: {rag_question}")
                
                # 3. Get page content
                html_content, metadata = get_html_content(absolute_url)
                
                # 4. Add to categorized_links with new fields
                for category in categories:
                    if category not in categorized_links:
                        categorized_links[category] = []
                    categorized_links[category].append({
                        "Title": link.text.strip(),
                        "URL": absolute_url,
                        "Category": category,
                        "Question": rag_question,
                        "Navigation": absolute_path
                    })
                
                # 5. Save updated CATEGORIES payload
                save_payloads_to_files(categorized_links)
                
                # 6. Q/A Processing - now controlled by ENABLE_QA_PROCESSING flag
                if ENABLE_QA_PROCESSING:
                    # Přeskočení Q/A extrakce pro mapu stránek
                    is_sitemap = 'mapa-stranek' in absolute_url.lower() or 'sitemap' in absolute_url.lower()
                    if not is_sitemap:
                        logger.info("Spouštím Q/A extrakci...")
                        process_url_content(absolute_url, html_content, category, metadata)
                    else:
                        logger.info("Přeskakuji Q/A extrakci pro mapu stránek")
                else:
                    logger.info("Q/A zpracování je vypnuto")
                
                logger.info(f"=== Dokončeno zpracování URL: {absolute_url} ===")
                
            except Exception as e:
                logger.error(f"Error processing URL {absolute_url}: {str(e)}")
                logger.error("Continuing to next URL...")
        
        # Continue processing submenu
        sub_menu = menu_item.find('ul')
        if sub_menu:
            extract_links(sub_menu, path + [link.text.strip() if link else ''], categorized_links)
    
    elif menu_item.name == 'ul':
        for item in menu_item.find_all('li', recursive=False):
            extract_links(item, path, categorized_links)
    
    return categorized_links

def save_payloads_to_files(categorized_links):
    """Save categorized links to separate JSON files."""
    for category, links in categorized_links.items():
        payload = {
            "data": {
                "schema": {
                    "searchableFields": ["Title", "URL", "Question", "Navigation"],
                    "metadataFields": ["Category", "SubCategory"]  # Added SubCategory to metadata
                },
                "name": f"{category.lower()}_table",
                "items": links
            }
        }
        
        filename = f"payloads/{category.lower()}_table_payload.json"
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            
        logger.info(f"Updated payload for table '{category}' in file: {filename}")

def load_payloads_from_files():
    payloads_dir = "payloads"
    payloads = {}
    for filename in os.listdir(payloads_dir):
        if filename.endswith("_payload.json"):
            with open(os.path.join(payloads_dir, filename), 'r', encoding='utf-8') as f:
                try:
                    payload = json.load(f)
                    table_name = payload['data']['name']
                    tags = payload['data'].get('tags', [])
                    category = tags[0] if isinstance(tags, list) and tags else 'Unknown'
                    payloads[table_name] = {
                        'category': category,
                        'items': payload['data']['items']
                    }
                    logger.info(f"Načten payload pro tabulku '{table_name}' s kategorií '{category}'")
                except Exception as e:
                    logger.error(f"Chyba při načítání souboru {filename}: {str(e)}")
    return payloads

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

def save_payload_to_file(url, new_items, category, metadata):
    """Save payload to JSON file with item accumulation"""
    logger.info(f"Attempting to save payload for URL: {url}, Category: {category}")
    
    try:
        filename = f"payloads/{category.lower()}_table_payload.json"
        
        # Load existing payload if it exists
        existing_payload = {
            "data": {
                "schema": {
                    "searchableFields": ["Title", "URL"],
                    "metadataFields": ["Categories"]
                },
                "name": f"{category.lower()}_table",
                "items": []
            }
        }
        
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    existing_payload = json.load(f)
            except json.JSONDecodeError:
                logger.warning(f"Could not parse existing payload file {filename}, starting fresh")
        
        # Add new items to existing items, avoiding duplicates based on URL
        existing_urls = {item['URL'] for item in existing_payload['data']['items']}
        for item in new_items:
            if item['URL'] not in existing_urls:
                existing_payload['data']['items'].append(item)
                existing_urls.add(item['URL'])
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        
        # Save updated payload
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(existing_payload, f, ensure_ascii=False, indent=2)
            logger.info(f"Successfully saved payload to {filename} with {len(existing_payload['data']['items'])} total items")
            
        return filename
        
    except Exception as e:
        logger.error(f"Failed to save payload: {str(e)}", exc_info=True)
        raise

def upload_to_voiceflow(filename):
    logger.info(f"Nahrávání souboru '{filename}' do Voiceflow")
    url = 'https://api.voiceflow.com/v1/knowledge-base/docs/upload/table?overwrite=true&llmGeneratedQ=true'
    headers = {
        'Authorization': VOICEFLOW_API_KEY,
        'accept': 'application/json',
        'content-type': 'application/json'
    }
    
    with open(filename, 'r', encoding='utf-8') as f:
        payload = json.load(f)
    
    response = requests.post(url, headers=headers, json=payload)
    
    if response.status_code == 200:
        logger.info(f"Úspěšně nahráno {len(payload['data']['items'])} položek pro soubor '{filename}'")
    else:
        logger.error(f"Chyba při nahrávání souboru '{filename}': {response.text}")

def convert_to_qa(content, title, category):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    system_prompt = f"""Jste precizní právní poradce pro detailní extrakci informací. Striktně formátujete své odpovědi ve validním JSON formátu. Disponujete následující,i schopnostmi a dodržujete následující omezení:

EXTRAKČNÍ SCHOPNOSTI:
1. Hloubková analýza textu pro nalezení všech informačních bodů
2. Identifikace vzájemných souvislostí mezi informacemi
3. Rozpoznávání různých datových typů a formátů
4. Schopnost formulovat různé perspektivy na stejná data

OBLASTI EXTRAKCE:
1. Všech číselných údajů (částky, data, procenta, rozměry, vzdálenosti)
2. Všech URL odkazů (webové stránky, dokumenty, obrázky, videa)
3. Všech kontaktních informací (emaily, telefony, adresy)
4. Všech jmen (osoby, instituce, organizace)
5. Všech lokací a míst
6. Všech časových údajů (termíny, lhůty, otevírací doby)
7. Všech procedurálních informací (postupy, procesy, návody)
8. Všech právních a administrativních informací
9. Všech podmínek a požadavků
10. Všech služeb a jejich parametrů

KRITICKÉ ZÁSADY PŘESNOSTI:
1. POUZE extrahujete existující informace - NIKDY nic nepřidáváte ani nedomýšlíte
2. Každá informace v odpovědi MUSÍ být explicitně uvedena ve vstupním textu
3. NULOVÁ tolerance k jakýmkoliv předpokladům či odvozením
4. Při nejistotě raději informaci VYNECHÁTE, než byste riskovali nepřesnost
5. Veškeré číselné údaje, data, odkazy musí být DOSLOVNĚ zkopírované ze zdroje

POVOLENÉ OPERACE:
1. Extrakce doslovných informací
2. Reorganizace existujících informací do Q/A formátu
3. Rozdělení komplexních informací na jednodušší celky
4. Vytváření alternativních formulací otázek pro stejnou informaci

ZAKÁZANÉ OPERACE:
1. Přidávání jakýchkoliv nových informací
2. Vyvozování či předpokládání souvislostí
3. Doplňování chybějících detailů
4. Aktualizace či modernizace informací
5. Generalizace či zjednodušování

Veškerý výstup musí být v češtině a přímo souviset s tématem '{title}'."""

    user_prompt = """# VÁŠ KRITICKÝ ÚKOL: 
Proveďte VYČERPÁVAJÍCÍ extrakci dat a vytvořte MAXIMÁLNÍ počet vysoce informativních Q/A párů při STRIKTNÍM dodržení pravidel přesnosti.

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
   - Zachovat PŘESNÉ znění čísel, dat, URL, kontaktů
   - NULOVÁ tolerance k opakování informací
   - Každá otázka musí přinášet NOVOU informační hodnotu

3. STRUKTURA Q/A:
   - Otázka musí být zodpověditelná POUZE z extrahovaného textu
   - Odpověď musí obsahovat POUZE informace ze zdrojového textu
   - ŽÁDNÉ doplňující či vysvětlující informace
   - Zachovat původní terminologii a formulace

4. FORMULACE OTÁZEK:
   - Každá otázka musí mít 3 VÝZNAMNĚ ODLIŠNÉ formulace
   - Využívejte různé typy otázek (co, kdy, kde, jak, proč, kolik...)
   - Kombinujte různé perspektivy dotazování
   - Otázky musí být zodpověditelné JEDINOU správnou odpovědí
   - ŽÁDNÉ spekulativní či hypotetické otázky
   - Otázky musí přímo směřovat k existující informaci

5. OBSAH ODPOVĚDÍ:
   - MUSÍ obsahovat VŠECHNY relevantní URL odkazy
   - MUSÍ zachovat PŘESNÝ formát URL
   - MUSÍ obsahovat VŠECHNY číselné údaje
   - MUSÍ obsahovat VŠECHNY časové údaje
   - MUSÍ obsahovat VŠECHNY kontaktní informace
   - MUSÍ obsahovat VŠECHNY procesní informace
   - Odpovědi musí být FAKTICKÉ a KONKRÉTNÍ
   - Každá část odpovědi MUSÍ být dohledatelná ve zdroji
   - ŽÁDNÉ zobecňování ani interpretace
   - Zachovat původní kontext informace
   - Při složených informacích zachovat všechny podmínky a souvislosti

# PŘÍKLADY STRUKTURY Q/A:

## Příklad 1 - Kontaktní informace:
{{
  "qa_pairs": [
    {{
      "Question": "Jaké jsou úřední hodiny městského úřadu? | Kdy mohu navštívit městský úřad? | V jakých časech je otevřena radnice? | úřední hodiny městského úřadu | otevírací doba městský úřad | otvírací hodiny městského úřadu",
      "Answer": "Městský úřad Teplice má následující úřední hodiny: [DOSLOVNÁ CITACE Z TEXTU]",
      "Category": "Kontakt"
    }}
  ]
}}

## Příklad 2 - Procedurální informace:
{{
  "qa_pairs": [
    {{
      "Question": "Jak si mohu vyřídit nový občanský průkaz? | Jaký je postup pro získání občanského průkazu? | Co potřebuji k vyřízení OP? | vyřízení občanského průkazu | nový OP postup | doklady pro OP",
      "Answer": "[DOSLOVNÁ CITACE POSTUPU Z TEXTU]",
      "Category": "Administrativa_Uredni_Zalezitosti"
    }}
  ]
}}

# OČEKÁVANÝ JSON FORMÁT:
{{
  "qa_pairs": [
    {{
      "Question": "Hlavní otázka? | Alternativní pohled? | Jiná perspektiva? | vyhledávací fráze 1 | vyhledávací fráze 2 | vyhledávací fráze 3",
      "Answer": "Doslovně extrahovaná odpověď ze zdrojového textu bez jakýchkoliv úprav či doplnění",
      "Category": "{}"
    }}
  ]
}}

# KRITÉRIA KONTROLY PŘED ODESLÁNÍM:
1. Lze každou část odpovědi DOSLOVNĚ najít ve zdrojovém textu?
2. Neobsahuje odpověď ŽÁDNÉ dodatečné informace?
3. Je každá otázka zodpověditelná POUZE z dostupného textu?
4. Jsou zachovány VŠECHNY původní formulace a termíny?
5. Nejsou nikde použity předpoklady či dedukce?
6. Jsou všechny číselné údaje, URL a kontakty PŘESNĚ zkopírované?
7. Je každá informace uvedena v původním kontextu?""".format(category)

    try:
        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=8192,
            temperature=0,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt}
            ]
        )
        
        response_text = message.content[0].text.strip()
        
        # Vylepšené zpracování JSON odpovědi
        try:
            # Pokus o přímé parsování celé odpovědi
            qa_data = json.loads(response_text)
        except json.JSONDecodeError:
            # Pokud selže, pokus o extrakci JSON části
            json_start = response_text.find('{')
            json_end = response_text.rfind('}') + 1
            if json_start != -1 and json_end != -1:
                json_str = response_text[json_start:json_end]
                try:
                    qa_data = json.loads(json_str)
                except json.JSONDecodeError as e:
                    logger.error(f"Nelze parsovat JSON z odpovědi pro {title}: {str(e)}")
                    return None
            else:
                logger.error(f"Nelze najít JSON v odpovědi pro {title}")
                return None
        
        qa_pairs = qa_data.get('qa_pairs', [])
        
        # Validace Q/A párů
        if not isinstance(qa_pairs, list):
            logger.error(f"Neplatná struktura Q/A párů pro {title}")
            return None
        
        # Přidání kategorie ke každému Q/A páru
        for pair in qa_pairs:
            pair['Category'] = category
        
        return qa_pairs
            
    except Exception as e:
        logger.error(f"Chyba při generování Q/A párů pro {title}: {str(e)}")
        return None

def save_qa_payload_for_url(url, qa_pairs, category, metadata):
    """Save QA payload to a separate JSON file for each URL"""
    logger.info(f"Saving QA payload for individual URL: {url}")
    
    try:
        # Create a clean filename from the URL
        url_slug = clean_title_from_url(url)
        filename = f"payloads/qa_{url_slug}.json"
        
        # Create payload structure
        payload = {
            "data": {
                "schema": {
                    "searchableFields": ["Question", "Answer"],
                    "metadataFields": ["Category", "URL"]
                },
                "name": f"qa_{url_slug}",
                "items": qa_pairs
            }
        }
        
        # Add URL to each QA pair for reference
        for pair in payload["data"]["items"]:
            pair["URL"] = url
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        
        # Save payload
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            logger.info(f"Successfully saved QA payload to {filename} with {len(qa_pairs)} items")
            
        return filename
        
    except Exception as e:
        logger.error(f"Failed to save QA payload for URL {url}: {str(e)}", exc_info=True)
        raise

def process_url_content(url, html_content, category, metadata):
    try:
        # Získání markdown obsahu pro Q/A
        qa_content, _ = get_html_content(url, for_qa=True)
        
        # Generování Q/A párů
        qa_pairs = convert_to_qa(qa_content, metadata.get('title', ''), category)
        
        if qa_pairs:
            # Save QA pairs in a separate file for this specific URL
            qa_filename = save_qa_payload_for_url(url, qa_pairs, category, metadata)
            
            # Upload the individual QA file to Voiceflow
            upload_to_voiceflow(qa_filename)
            
            logger.info(f"Successfully processed QA content for URL: {url}")
            
    except Exception as e:
        logger.error(f"Chyba při zpracování obsahu URL {url}: {str(e)}")

def clean_title_from_url(url, title=None):
    # Always extract from URL
    clean_url = re.sub(r'https?://[^/]+/', '', url)
    path_components = clean_url.split('/')
    title = next((comp for comp in reversed(path_components) if comp), '')
    
    # Remove any domains from title
    title = re.sub(r'www\.[^\s/]+\.[a-z]{2,}/?', '', title)
    
    # Replace dots with hyphens (except for file extensions)
    title = re.sub(r'\.(?!\w+$)', '-', title)
    
    # Clean up the title
    clean_title = title.strip()
    clean_title = remove_accents(clean_title)
    clean_title = re.sub(r'[<>:"/\\|?*]', '_', clean_title)
    clean_title = re.sub(r'\s+', '_', clean_title)
    clean_title = re.sub(r'_+', '_', clean_title)
    
    # Remove trailing hyphens and underscores
    clean_title = re.sub(r'[-_]+$', '', clean_title)
    
    # Ensure title doesn't exceed maximum length
    return clean_title[:MAX_FILENAME_LENGTH]

def extract_date_from_url(url):
    """
    Extract date from URL, handling European format (DD-MM-YYYY).
    Returns datetime object or None if no valid date found.
    """
    # Look for dates in formats: DD-MM-YYYY, D-MM-YYYY
    date_patterns = [
        r'(\d{1,2})-(\d{1,2})-(\d{4})',  # DD-MM-YYYY or D-MM-YYYY
        r'(\d{1,2})\.(\d{1,2})\.(\d{4})'  # DD.MM.YYYY or D.MM.YYYY
    ]
    
    for pattern in date_patterns:
        matches = re.findall(pattern, url)
        if matches:
            day, month, year = matches[0]  # Take the first match
            try:
                # Convert to integers, assuming DD-MM-YYYY format
                day = int(day)
                month = int(month)
                year = int(year)
                
                # Validate date components
                if 1 <= day <= 31 and 1 <= month <= 12 and year >= 2000:
                    return datetime(year, month, day)
            except ValueError as e:
                logger.warning(f"Neplatné datum v URL {url}: {str(e)}")
                continue
    
    return None

def should_apply_date_filter(url):
    """
    Determines if a URL should have date filtering applied.
    Returns False for main section pages, True for sub-pages.
    """
    parsed_url = urlparse(url)
    path = parsed_url.path.rstrip('/')  # Remove trailing slash for comparison
    
    # Check if this is a main section URL (exact match)
    if path in MAIN_SECTION_PATHS:
        return False
        
    # Check if this is a sub-page under a filtered section
    return any(path.startswith(filter_path) for filter_path in DATE_FILTERED_PATHS)

def get_last_processed_date():
    """Get the datetime of last script execution"""
    last_processed_file = os.path.join(LOG_DIR, "scrape_sitemap_last_processed_date.txt")
    try:
        with open(last_processed_file, 'r') as f:
            date_str = f.read().strip()
            return datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
    except (FileNotFoundError, ValueError):
        return None

def update_last_processed_date():
    """Update the last processed datetime"""
    last_processed_file = os.path.join(LOG_DIR, "scrape_sitemap_last_processed_date.txt")
    with open(last_processed_file, 'w') as f:
        f.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

def check_url_in_payloads(url):
    """
    Check if URL exists in any payload file.
    """
    payload_dir = "payloads"
    
    if not os.path.exists(payload_dir):
        logger.info(f"Adresář {payload_dir} neexistuje - URL {url} bude zpracována")
        return False
        
    for filename in os.listdir(payload_dir):
        if not filename.endswith('_payload.json'):
            continue
            
        try:
            with open(os.path.join(payload_dir, filename), 'r', encoding='utf-8') as f:
                payload = json.load(f)
                # Vylepšená kontrola existence URL
                if 'data' in payload and 'items' in payload['data']:
                    for item in payload['data']['items']:
                        if item.get('URL') == url:
                            logger.info(f"URL {url} již existuje v souboru {filename} - přeskakuji")
                            return True
        except Exception as e:
            logger.error(f"Chyba při kontrole souboru {filename}: {str(e)}")
            continue
    
    logger.info(f"URL {url} nenalezena v žádném payload souboru - bude zpracována")
    return False

def should_process_url(url, lastmod_date):
    """
    Determine if URL should be processed based on existence and last modification date
    """
    logger.info(f"Kontrola URL: {url}")
    
    # Nejdřív zkontrolujeme existenci v payloadech
    url_exists = check_url_in_payloads(url)
    if url_exists:
        last_processed = get_last_processed_date()
        if last_processed and lastmod_date and lastmod_date > last_processed:
            logger.info(f"URL {url} existuje, ale byla modifikována {lastmod_date} po posledním zpracování {last_processed} - zpracuji znovu")
            return True
        logger.info(f"URL {url} již existuje a nebyla změněna - přeskakuji")
        return False

    # Pokud URL neexistuje, aplikujeme datový filtr
    if not should_apply_date_filter(url):
        logger.info(f"URL {url} je hlavní sekce - zpracuji")
        return True
        
    if lastmod_date:
        should_process = lastmod_date.year >= FILTER_YEAR
        logger.info(f"URL {url} má datum {lastmod_date.year} - {'zpracuji' if should_process else 'přeskočím'}")
        return should_process
            
    logger.info(f"URL {url} nemá datum - přeskočím")
    return False

def get_administrative_subcategory(path, url):
    """Determine subcategory for administrative content using Claude."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    path_string = ' > '.join(path)
    
    prompt = f"""Analyze this webpage path and URL to assign the most appropriate administrative subcategory.

Path: {path_string}
URL: {url}

Available subcategories (MUST choose exactly one):
{', '.join(ADMINISTRATIVE_SUBCATEGORIES)}

Rules:
1. You MUST select exactly one subcategory from the list above
2. The subcategory should best match the content type and purpose
3. Return ONLY the subcategory name, nothing else

Key mappings:
- Commission/committee content -> Komise_Rady_Mesta
- City council resolutions -> Usneseni_Rady_Mesta
- Editorial board -> Redakcni_Rada
- Assembly resolutions -> Usneseni_Zastupitelstvo
- Committee records -> Zapisy_Vyboru
- Annual reports -> Vyrocni_Zpravy
- Public contracts -> Verejnopravni_Smlouvy
- Official notice board -> Uredni_Deska
- Municipal office documents -> Dokumenty_Mestskeho_Uradu
- Regulations/ordinances -> Vyhlasky"""

    try:
        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=50,
            temperature=0,
            messages=[{"role": "user", "content": prompt}]
        )
        
        subcategory = message.content[0].text.strip()
        if subcategory not in ADMINISTRATIVE_SUBCATEGORIES:
            logger.warning(f"Invalid subcategory returned: {subcategory}. Using default.")
            return "Dokumenty_Mestskeho_Uradu"
            
        return subcategory
        
    except Exception as e:
        logger.error(f"Error getting subcategory: {str(e)}")
        return "Dokumenty_Mestskeho_Uradu"

def extract_links_from_sitemap(sitemap_soup, categorized_links={}):
    """Process sitemap URLs"""
    if not sitemap_soup:
        logger.error("Neplatná sitemap struktura")
        return categorized_links

    processed_count = 0
    skipped_count = 0
    
    for url_element in sitemap_soup.find_all('url'):
        loc = url_element.find('loc')
        if not loc:
            continue
            
        absolute_url = loc.text.strip()
        lastmod_date = None
        
        # Get lastmod date if available
        lastmod = url_element.find('lastmod')
        if lastmod:
            try:
                lastmod_date = datetime.strptime(lastmod.text.strip()[:10], '%Y-%m-%d')
                logger.info(f"URL {absolute_url} má lastmod datum: {lastmod_date}")
            except (ValueError, IndexError):
                logger.warning(f"Nelze zpracovat lastmod datum pro {absolute_url}")
        
        # Single unified check for processing
        if should_process_url(absolute_url, lastmod_date):
            try:
                logger.info(f"=== Starting processing URL: {absolute_url} ===")
                
                # Get HTML content and metadata using existing function
                html_content, metadata = get_html_content(absolute_url)
                if not html_content:
                    logger.warning(f"Cannot get content for URL {absolute_url}")
                    continue

                # Extract path components for categorization and navigation
                path_components = urlparse(absolute_url).path.strip('/').split('/')
                current_path = [comp.replace('-', ' ').title() for comp in path_components if comp]
                absolute_path = ' > '.join(current_path)

                # Get category using path-based categorization
                categories = categorize_link_claude(current_path)
                if not categories:
                    logger.warning(f"Cannot determine category for URL {absolute_url}")
                    continue

                # Generate RAG question
                rag_question = generate_rag_question(current_path)
                logger.info(f"Generated RAG question: {rag_question}")

                # Get title from metadata or generate from URL
                title = metadata.get('title', '') or path_components[-1].replace('-', ' ').title()

                logger.info(f"Processing URL {absolute_url} with categories: {categories}")

                # Process QA content for this URL immediately if enabled
                if ENABLE_QA_PROCESSING:
                    # Use the first category for QA processing
                    primary_category = categories[0]
                    logger.info(f"Starting QA processing for URL {absolute_url} with category {primary_category}")
                    process_url_content(absolute_url, html_content, primary_category, metadata)

                # Add to categorized_links with new fields
                for category in categories:
                    if category not in categorized_links:
                        categorized_links[category] = []
                    
                    link_data = {
                        "Title": title,
                        "URL": absolute_url,
                        "Category": category,
                        "Question": rag_question,
                        "Navigation": absolute_path,
                        "SubCategory": None  # Initialize SubCategory field
                    }
                    
                    # Add subcategory only for administrative content
                    if category == "Administrativa_Uredni_Zalezitosti":
                        subcategory = get_administrative_subcategory(current_path, absolute_url)
                        link_data["SubCategory"] = subcategory
                        logger.info(f"Assigned administrative subcategory: {subcategory}")

                    categorized_links[category].append(link_data)
                    
                    # Save immediately after each URL is processed
                    try:
                        logger.info(f"Attempting to save payload for category: {category}")
                        save_payloads_to_files(categorized_links)
                        logger.info(f"Successfully saved payload")
                    except Exception as e:
                        logger.error(f"Failed to save payload for URL {absolute_url}: {str(e)}", exc_info=True)
                        continue

                processed_count += 1
                logger.info(f"=== URL {absolute_url} successfully processed ===")
                
            except Exception as e:
                logger.error(f"Error processing URL {absolute_url}: {str(e)}", exc_info=True)
                continue
        else:
            skipped_count += 1
            logger.info(f"Skipping URL: {absolute_url}")
            
    logger.info(f"Total URLs processed: {processed_count}")
    logger.info(f"Total URLs skipped: {skipped_count}")
    return categorized_links

def count_existing_urls():
    """Count URLs in existing payloads"""
    payload_dir = "payloads"
    if not os.path.exists(payload_dir):
        return 0
        
    url_count = 0
    for filename in os.listdir(payload_dir):
        if filename.endswith('_payload.json'):
            try:
                with open(os.path.join(payload_dir, filename), 'r', encoding='utf-8') as f:
                    payload = json.load(f)
                    if 'data' in payload and 'items' in payload['data']:
                        url_count += len(payload['data']['items'])
            except Exception as e:
                logger.error(f"Chyba při počítání URL v {filename}: {str(e)}")
    return url_count

def compile_search_queries_file():
    """Compiles all search queries from JSON payloads into a single TXT file."""
    output_file = "compiled_search_queries.txt"
    payloads_dir = "payloads"
    
    logger.info(f"Starting compilation of search queries into {output_file}")
    
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            example_counter = 1
            
            for filename in os.listdir(payloads_dir):
                if filename.endswith('_payload.json'):
                    file_path = os.path.join(payloads_dir, filename)
                    logger.info(f"Processing file: {filename}")
                    
                    try:
                        with open(file_path, 'r', encoding='utf-8') as json_file:
                            payload = json.load(json_file)
                            items = payload['data']['items']
                            
                            for item in items:
                                title = item.get('Title', '')
                                questions = item.get('Question', '')
                                
                                if title and questions:
                                    entry = (
                                        f"\n## Example {example_counter} - Original query about: '{title}'\n"
                                        "* Response:\n"
                                        "{\n"
                                        f'    "WebSearchQuery": "{title}",\n'
                                        f'    "UserReply": "{questions}"\n'
                                        "}\n"
                                    )
                                    f.write(entry)
                                    example_counter += 1
                    
                    except Exception as e:
                        logger.error(f"Error processing file {filename}: {str(e)}")
                        continue
        
        logger.info(f"Successfully compiled {example_counter-1} search queries into {output_file}")
        
    except Exception as e:
        logger.error(f"Error creating compiled search queries file: {str(e)}")

def main(skip_scraping, compile_only=False):
    if compile_only:
        logger.info("Running search queries compilation only")
        if COMPILE_SEARCH_QUERIES:
            compile_search_queries_file()
        else:
            logger.warning("Search queries compilation is disabled (COMPILE_SEARCH_QUERIES = False)")
        return

    start_time = datetime.now()
    logger.info(f"Začátek zpracování: {start_time}")
    
    # Přidáno počítání existujících URL
    existing_urls = count_existing_urls()
    logger.info(f"Počet existujících URL v payloadech: {existing_urls}")

    # Create payloads directory immediately
    payload_dir = "payloads"
    if not os.path.exists(payload_dir):
        os.makedirs(payload_dir)
        logger.info(f"Vytvořen adresář pro payloady: {payload_dir}")

    if UPLOAD_IMMEDIATELY:
        logger.info("UPLOAD_IMMEDIATELY je True - přeskakuji veškeré zpracování a nahrávám existující payloady")
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

    if skip_scraping:
        logger.info("Přeskočit scraping, načítám payloady ze souborů")
        payloads = load_payloads_from_files()
    else:
        try:
            # Initialize empty categorized_links
            categorized_links = {}
            
            # Get sitemap content and process URLs
            response = requests_retry_session().get(SITEMAP_URL, timeout=REQUEST_TIMEOUT)
            sitemap_content = response.text
            
            # Parse sitemap XML
            sitemap_soup = parse_sitemap(sitemap_content)
            if not sitemap_soup:
                logger.error("Nepodařilo se zpracovat sitemapu.")
                return
            
            # Process URLs from sitemap and save incrementally
            categorized_links = extract_links_from_sitemap(sitemap_soup, categorized_links)
            
            # Final save to ensure everything is written
            save_payloads_to_files(categorized_links)
            
            # Update last processed date after successful execution
            update_last_processed_date()
            
        except Exception as e:
            logger.error(f"Došlo k chybě při zpracování: {str(e)}", exc_info=True)
            return

    logger.info("Nahrávání dat do Voiceflow")
    for category in categorized_links.keys():
        filename = f"payloads/{category.lower()}_table_payload.json"
        if os.path.exists(filename):
            upload_to_voiceflow(filename)
    
    # Add compilation of search queries if enabled
    if COMPILE_SEARCH_QUERIES:
        compile_search_queries_file()

    end_time = datetime.now()
    logger.info(f"Konec zpracování: {end_time}")
    logger.info(f"Celková doba zpracování: {end_time - start_time}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape and upload data to Voiceflow")
    parser.add_argument("--skip-scraping", type=int, choices=[0, 1], default=0,
                        help="Skip scraping and upload existing payloads (0: no, 1: yes)")
    parser.add_argument("--compile-only", action="store_true",
                        help="Only compile search queries from existing payloads")
    args = parser.parse_args()
    
    main(skip_scraping=args.skip_scraping, compile_only=args.compile_only)
