import requests
from bs4 import BeautifulSoup
import logging
import json
import anthropic
import time
import os
from urllib.parse import urljoin
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
# API KEYS
# ============================================================================
CLAUDE_API_KEY = "REMOVED-ANTHROPIC-KEY"
JINA_AI_API_KEY = "REMOVED-JINA-KEY"
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"

# ============================================================================
# URL CONFIGURATION
# ============================================================================
BASE_URL = "https://www.khk.cz"
SITEMAP_URL = "https://khk.cz/mapa-webu"

# ============================================================================
# API CALL SETTINGS
# ============================================================================
API_CALL_DELAY = 5  # Fixed delay between API calls in seconds
MAX_RETRIES = 3  # Maximum number of retry attempts
INITIAL_RETRY_DELAY = 5  # Initial retry delay in seconds

# ============================================================================
# PROCESSING FLAGS
# ============================================================================
ENABLE_QA_PROCESSING = False  # Enable Q/A processing
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
            "Path:" in record.getMessage(),
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
            'X-Target-Selector': '#sitemap',
            'X-Wait-For-Selector': '#sitemap'
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

def parse_menu(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    main_menu = soup.select_one('div.sitemap > ul')
    
    if not main_menu:
        logger.warning("Hlavní menu nebylo nalezeno pomocí selektoru 'div.sitemap > ul'.")
        # Optional: Add fallback logic if needed, though specific targeting is preferred
        # main_menu = soup.find('ul') # Example fallback
        # if not main_menu:
        #     logger.error("Nebylo nalezeno žádné <ul> menu na stránce.")
        # else:
        #     logger.info("Používá se první nalezený <ul> jako záložní řešení.")

    return main_menu

def categorize_link_claude(path):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    path_string = ' > '.join(path)
    
    prompt = f"""Dána je následující cesta menu z webových stránek města Teplice:

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

    for attempt in range(MAX_RETRIES):
        try:
            message = client.messages.create(
                model="claude-3-7-sonnet-20250219",
                max_tokens=50,
                temperature=0,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            
            category = message.content[0].text.strip()
            
            if category not in CATEGORIES and category != "Nezařazeno":
                logger.warning(f"Claude vrátil neočekávanou kategorii: {category}. Použije se 'Nezařazeno'.")
                return "Nezařazeno"
            
            # Apply fixed delay after successful API call
            time.sleep(API_CALL_DELAY)
            
            return category
        
        except (InternalServerError, RateLimitError) as e:
            if attempt == MAX_RETRIES - 1:
                logger.error(f"Nepodařilo se získat kategorii po {MAX_RETRIES} pokusech: {str(e)}")
                return "Nezařazeno"
            
            # Calculate exponential backoff delay
            delay = INITIAL_RETRY_DELAY * (2 ** attempt) + random.uniform(0, 1)
            logger.warning(f"Chyba při kategorizaci, pokus {attempt + 1}/{MAX_RETRIES}. Čekání {delay:.2f} sekund před dalším pokusem.")
            time.sleep(delay)

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

Example format:
For path: "Dotace > Kotlíková dotace > Aktuality"
Output: "Kde najdu aktuality, články a důležité informace ke kotlíkovým dotacím? | Where do I find the latest articles, updates and important information regarding boiler subsidies?"

Return ONLY the question pair without any additional text or formatting."""

    for attempt in range(MAX_RETRIES):
        try:
            message = client.messages.create(
                model="claude-3-7-sonnet-20250219",
                max_tokens=200,
                temperature=0,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            
            question = message.content[0].text.strip()
            
            # Apply fixed delay after successful API call
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
        # Updated selector to find the link within the new structure
        link_tag = menu_item.select_one('div.views-field span.field-content a')
        link_text_element = menu_item.select_one('div.views-field span.field-content') # Get the span to extract text, even if 'a' is missing

        # Use the text from the span directly if link_tag is found, otherwise try to get text from span
        link_text = link_tag.text.strip() if link_tag else (link_text_element.text.strip() if link_text_element else '')

        if link_text: # Process if we found text, even without a link
            current_path = path + [link_text]
            absolute_path = ' > '.join(current_path)

            if link_tag and link_tag.has_attr('href'): # Process link only if 'a' tag with href exists
                absolute_url = urljoin(BASE_URL, link_tag['href'])
                logger.info(f"\n=== Starting processing URL: {absolute_url} ===")
                logger.info(f"Path: {absolute_path}")

                try:
                    # 1. Categorize URL
                    category = categorize_link_claude(current_path)
                    logger.info(f"Assigned category: {category} based on path")

                    # 2. Generate RAG question
                    rag_question = generate_rag_question(current_path)
                    logger.info(f"Generated RAG question: {rag_question}")

                    # 3. Get page content
                    html_content, metadata = get_html_content(absolute_url)

                    # 4. Add to categorized_links for CATEGORIES payload
                    if category not in categorized_links:
                        categorized_links[category] = []
                    categorized_links[category].append({
                        "Title": link_text, # Use extracted link_text
                        "URL": absolute_url,
                        "Category": category,
                        "Question": rag_question,
                        "Navigation": absolute_path
                    })

                    # 5. Okamžité uložení aktualizovaného CATEGORIES payloadu
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

                    logger.info(f"=== Dokončeno zpracování URL: {absolute_url} ===\n")

                except Exception as e:
                    logger.error(f"Chyba při zpracování URL {absolute_url}: {str(e)}")
                    logger.error("Pokračuji na další URL...")
            else:
                # Log items that have text but no link (they might be headers for submenus)
                 logger.info(f"Skipping item with no link: {absolute_path}")


            # Pokračování v procházení submenu
            # Find 'ul' that is a direct child of the current 'li'
            sub_menu = menu_item.find('ul', recursive=False)
            if sub_menu:
                extract_links(sub_menu, current_path, categorized_links) # Pass the updated current_path

    elif menu_item.name == 'ul':
        for item in menu_item.find_all('li', recursive=False):
            extract_links(item, path, categorized_links)
    
    return categorized_links

def save_payloads_to_files(categorized_links):
    output_dir = "payloads"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    for category, links in categorized_links.items():
        table_name = f"{category.lower()}_table"
        filename = f"{output_dir}/{table_name}_payload.json"
        
        # Add Category, Question, and Navigation to each item
        updated_links = [{
            "Title": link["Title"],
            "URL": link["URL"],
            "Category": category,
            "Question": link.get("Question", "Default question | Default question in English"),
            "Navigation": link.get("Navigation", "")  # Add the Navigation field
        } for link in links]
        
        payload = {
            "data": {
                "schema": {
                    "searchableFields": ["Title", "URL", "Question", "Navigation"],  # Add Navigation to searchable fields
                    "metadataFields": ["Category"]
                },
                "name": table_name,
                "items": updated_links
            }
        }
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Updated payload for table '{table_name}' in file: {filename}")

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

def save_payload_to_file(url, content, section, metadata):
    title = metadata.get('title', '')
    if not title:
        title = url.replace('/', '_').replace(':', '_')
    
    title = remove_accents(title)
    title = re.sub(r'[<>:"/\\|?*]', '_', title)
    title = re.sub(r'\s+', '_', title)
    title = re.sub(r'_+', '_', title)
    title = title.strip('_')
    title = title[:200]
    
    filename = f"payloads/{section.lower()}_{title}_payload.json"
    
    # Přidání Category ke každému Q/A páru
    for item in content:
        item["Category"] = section
    
    payload = {
        "data": {
            "schema": {
                "searchableFields": ["Question", "Answer"],
                "metadataFields": ["Category"]
            },
            "name": f"{section.lower()}_{title}",
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
    
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    
    logger.info(f"Uložen payload pro URL '{url}' do souboru: {filename}")
    logger.debug(f"Payload content: {json.dumps(payload, indent=2, ensure_ascii=False)}")
    print(f"Vytvořen nový payload: {filename}")
    return filename

def upload_to_voiceflow(filename):
    logger.info(f"Nahrávání souboru '{filename}' do Voiceflow")
    url = 'https://api.voiceflow.com/v1/knowledge-base/docs/upload/table?overwrite=true'
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
            model="claude-3-7-sonnet-20250219",
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

def process_url_content(url, html_content, category, metadata):
    try:
        # Získání markdown obsahu pro Q/A
        qa_content, _ = get_html_content(url, for_qa=True)
        
        # Generování Q/A párů
        qa_pairs = convert_to_qa(qa_content, metadata.get('title', ''), category)
        
        if qa_pairs:
            # Vytvoření payloadu
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
            
            # Uložení payloadu do souboru
            filename = save_payload_to_file(url, qa_pairs, category, metadata)
            
            # Upload do Voiceflow
            upload_to_voiceflow(filename)
            
    except Exception as e:
        logger.error(f"Chyba při zpracování obsahu URL {url}: {str(e)}")

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
                    logger.info(f"Processing file: {filename}")
                    
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

def main(skip_scraping, compile_only=False):
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

    if skip_scraping:
        logger.info("Přeskočit scraping, načítám payloady ze souborů")
        payloads = load_payloads_from_files()
    else:
        url = 'https://khk.cz/mapa-webu'
        logger.info(f"Začátek zpracování webu: {url}")
        
        try:
            html_content, _ = get_html_content(url)
            main_menu = parse_menu(html_content)
            
            if not main_menu:
                logger.error("Nepodařilo se najít hlavní menu na stránce.")
                return
            
            categorized_links = extract_links(main_menu)
            
            # Ukládání payloadů do souborů
            save_payloads_to_files(categorized_links)
            
            # Příprava payloadů pro nahrání
            payloads = {f"{category.lower()}_table": {'category': category, 'items': links} for category, links in categorized_links.items()}
        
        except Exception as e:
            logger.error(f"Došlo k chybě při zpracování: {str(e)}", exc_info=True)
            return

    logger.info("Nahrávání dat do Voiceflow")
    for table_name, data in payloads.items():
        upload_to_voiceflow(f"payloads/{table_name}_payload.json")
    
    # Add compilation of search queries if enabled
    if COMPILE_SEARCH_QUERIES:
        compile_search_queries_file()
    
    end_time = datetime.now()
    logger.info(f"Konec zpracování: {end_time}")
    logger.info(f"Celková doba zpracování: {end_time - start_time}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape and upload data to Voiceflow")
    parser.add_argument("--skip-scraping", type=int, choices=[0, 1], default=0,
                        help="Přeskočit scraping a nahrát existující payloady (0: ne, 1: ano)")
    parser.add_argument("--compile-only", action="store_true",
                        help="Pouze zkompilovat vyhledávací dotazy z existujících payloadů")
    args = parser.parse_args()
    
    main(skip_scraping=args.skip_scraping, compile_only=args.compile_only)
