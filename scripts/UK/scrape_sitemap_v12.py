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

# Constants for script name and log directory
SCRIPT_NAME = "scrape_sitemap"
LOG_DIR = f"{SCRIPT_NAME}_logs"
LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")

# Create log directory if it doesn't exist
os.makedirs(LOG_DIR, exist_ok=True)

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Add handler for rotating file
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

# Add handler for console output
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(console_handler)

# API klíče a konstanty
CLAUDE_API_KEY = "REMOVED-ANTHROPIC-KEY"
JINA_AI_API_KEY = "REMOVED-JINA-KEY"
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"
BASE_URL = "https://www.kr-ustecky.cz"

# New variables for API call management
API_CALL_DELAY = 5  # Fixed delay between API calls in seconds
MAX_RETRIES = 3  # Maximum number of retry attempts
INITIAL_RETRY_DELAY = 5  # Initial retry delay in seconds

# Seznam kategorií
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

def get_html_content(url):
    logger.info(f"Získávání HTML obsahu z URL: {url}")
    api_url = f'https://r.jina.ai/{url}'
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {JINA_AI_API_KEY}",
        "X-Return-Format": "html"
    }
    
    try:
        response = requests_retry_session().get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        logger.debug(f"API Response: {json.dumps(data, indent=2, ensure_ascii=False)}")
        
        if data['status'] == 20000 and 'data' in data:
            html_content = data['data'].get('html', '')
            if html_content:
                title = BeautifulSoup(html_content, 'html.parser').title.string if html_content else ''
                metadata = {
                    'title': title,
                    'url': data['data'].get('url', url)
                }
                return html_content, metadata
            else:
                logger.error("HTML obsah nebyl nalezen v odpovědi API")
                raise ValueError("HTML obsah chybí v odpovědi API")
        else:
            error_message = data.get('status', 'Neznámá chyba')
            logger.error(f"Chyba při získávání obsahu: {error_message}")
            raise ValueError(f"Chyba API: {error_message}")
    except requests.RequestException as e:
        logger.error(f"Chyba při volání Jina AI API: {str(e)}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Chyba při parsování JSON odpovědi: {str(e)}")
        logger.debug(f"Raw API Response: {response.text}")
        raise ValueError("Neplatná JSON odpověď od API")

def parse_menu(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    main_menu = soup.select_one('#osnova > div.odkazy.text-to-speech > ul')
    
    if not main_menu:
        print("Varování: Hlavní menu nebylo nalezeno pomocí selektoru '#osnova > div.odkazy.text-to-speech > ul'.")
        # Pokud není nalezeno hlavní menu, zkusíme alternativní metody
        main_menu = soup.find('ul', class_='ui')
        if main_menu:
            print("Nalezen <ul> element s třídou 'ui' jako záložní řešení.")
        else:
            main_menu = soup.find('ul')
            if main_menu:
                print("Nalezen první <ul> element jako záložní řešení.")
            else:
                print("Žádný vhodný <ul> element nebyl nalezen.")
    
    return main_menu

def categorize_link_claude(path):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    path_string = ' > '.join(path)
    
    prompt = f"""Dána je následující cesta menu z webových stránek Ústeckého kraje:

{path_string}

Zařaďte prosím tuto cestu do JEDNÉ z následujících kategorií:

{', '.join(CATEGORIES)}

DŮLEŽITÉ INSTRUKCE:
1. Odpovězte POUZE názvem JEDNÉ JEDINÉ nejvhodnější kategorie ze seznamu výše.
2. Pokud žádná z kategorií dobře neodpovídá, odpovězte "Nezařazeno".
3. Neodpovídejte žádným jiným textem, pouze názvem kategorie nebo "Nezařazeno".
4. Cokoliv se týká lidí, osob, krajského úřadu, organizační struktury nebo kontaktních informací, zařaďte do kategorie "Kontakt" (např. Komise, Výbory, Zastupitelstvo, Radní, zaměstnanci, atd.).

Vezměte v úvahu celou absolutní cestu v daném stromě k URL odkazu pro co nejpřesnější zařazení/zvolení dané kategorie ze vstupního seznamu.
"""

    for attempt in range(MAX_RETRIES):
        try:
            message = client.messages.create(
                model="claude-3-5-sonnet-20240620",
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

def extract_links(menu_item, path=[], categorized_links={}):
    if menu_item.name == 'li':
        link = menu_item.find('a')
        if link:
            current_path = path + [link.text.strip()]
            absolute_path = ' > '.join(current_path)
            absolute_url = urljoin(BASE_URL, link['href'])
            logger.info(f"Zpracovávání odkazu: {absolute_path}")
            category = categorize_link_claude(current_path)
            logger.info(f"Odkaz zařazen do kategorie: {category}")
            
            if category not in categorized_links:
                categorized_links[category] = []
            categorized_links[category].append({
                "Title": link.text.strip(),
                "URL": absolute_url,
                "Category": category
            })
            
            save_payloads_to_files(categorized_links)
        
        sub_menu = menu_item.find('ul')
        if sub_menu:
            extract_links(sub_menu, path + [link.text.strip() if link else ''], categorized_links)
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
        
        # Přidání Category do každé položky
        updated_links = [{"Title": link["Title"], "URL": link["URL"], "Category": category} for link in links]
        
        payload = {
            "data": {
                "schema": {
                    "searchableFields": ["Title", "URL"],
                    "metadataFields": ["Category"]
                },
                "name": table_name,
                "items": updated_links
            }
        }
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Aktualizován payload pro tabulku '{table_name}' v souboru: {filename}")

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
    
    schema = {
        "searchableFields": ["Question", "Answer"]
    }
    
    payload = {
        "data": {
            "schema": schema,
            "name": f"{section.lower()}_{title}",
            "tags": [section],
            "items": content
        }
    }
    
    # Dodatečná validace struktury payloadu
    if not isinstance(payload["data"]["items"], list):
        raise ValueError("Content must be a list")
    for item in payload["data"]["items"]:
        if not isinstance(item, dict):
            raise ValueError("Each item must be a dictionary")
        for key in schema["searchableFields"]:
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

def main(skip_scraping):
    start_time = datetime.now()
    logger.info(f"Začátek zpracování: {start_time}")

    if skip_scraping:
        logger.info("Přeskakuji scraping, načítám payloady ze souborů")
        payloads = load_payloads_from_files()
    else:
        url = 'https://www.kr-ustecky.cz/mapa-stranek'
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
    
    end_time = datetime.now()
    logger.info(f"Konec zpracování: {end_time}")
    logger.info(f"Celková doba zpracování: {end_time - start_time}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape and upload data to Voiceflow")
    parser.add_argument("--skip-scraping", type=int, choices=[0, 1], default=0,
                        help="Přeskočit scraping a nahrát existující payloady (0: ne, 1: ano)")
    args = parser.parse_args()
    
    main(skip_scraping=args.skip_scraping)
