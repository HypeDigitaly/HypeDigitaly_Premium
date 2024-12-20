import requests
import json
import logging
import os
import time
from bs4 import BeautifulSoup
from datetime import datetime
from logging.handlers import RotatingFileHandler
import anthropic

# Constants for script name and log directory
SCRIPT_NAME = "scrape_event_calendar"
LOG_DIR = f"{SCRIPT_NAME}_logs"
LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")

# Create log directory if it doesn't exist
os.makedirs(LOG_DIR, exist_ok=True)

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Add handler for rotating file if not already added
if not any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)

# Add handler for console output if not already added
if not any(isinstance(handler, logging.StreamHandler) for handler in logger.handlers):
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(console_handler)

# Clear log file at the start of the script
with open(LOG_FILE, 'w'):
    pass

BASE_URL = "https://www.teplice.cz"
RSS_URL = "https://www.teplice.cz/rss/?22"
# OUTPUT_FILE NEMÁŠ AKTUÁLNĚ VÝZNAM, JELIKOŽ SE NÁZVY SOUBORŮ TVOŘÍ DYNAMICKY PODLE ROKŮ (SOUVISÍ S "dateThreshold")
# OUTPUT_FILE = "payloads/tiskove_informace_payload.json"
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"
dateThreshold = "20230101"  # Set the date threshold here

# Přidáno: Konstanta pro zpoždění mezi API voláními (v sekundách)
API_CALL_DELAY = 30

CLAUDE_API_KEY = "REMOVED-ANTHROPIC-KEY"

def get_categories_from_claude(title, description):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    system_prompt = """Jsi expertní systém pro kategorizaci událostí z městského kalendáře akcí. Tvým úkolem je analyzovat název a popis každé události a přiřadit jí relevantní kategorie z předdefinovaného seznamu.

## Dostupné kategorie:
- Administrativa_Uredni_Zalezitosti
- Charakteristika_Mesta
- Doprava
- Dotace
- Finance_Hospodareni
- Kontakt
- Krizove_Situace
- Kultura_Pamatkova_Pece
- Rozvoj_Projekty
- Socialni_Pece
- Strategicke_Dokumenty
- Ukrajina
- Uzemni_Planovani_Stavebni_Rad
- Verejne_Zakazky
- Vzdelavani
- Zdravotnictvi
- Zivotni_Prostredi_Zemedelstvi"""

    user_prompt = f"""Analyzuj následující událost z městského kalendáře a vrať relevantní kategorie jako JSON pole. Nepoužívej kategorii "Media_Komunikace", ta bude přidána automaticky.

## Název události: {title}
## Popis události: {description}

# TVŮJ JEDINÝ ÚKOL: Vrať pouze JSON pole kategorií, např: ["Kultura_Pamatkova_Pece", "Rozvoj_Projekty"]"""

    try:
        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=150,
            temperature=0.2,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        categories = json.loads(message.content[0].text)
        # Add "Media_Komunikace" to the beginning of the categories list
        categories.insert(0, "Media_Komunikace")
        logger.info(f"Claude categories for '{title}': {categories}")
        return categories
    except Exception as e:
        logger.error(f"Error getting categories from Claude: {str(e)}")
        return ["Media_Komunikace"]  # Fallback kategorie

def fetch_rss_feed(url):
    logger.info(f"Fetching RSS feed from URL: {url}")
    response = requests.get(url)
    response.raise_for_status()
    # Přidáno: Debug výpis odpovědi
    logger.debug(f"Response content: {response.content[:500]}")  # První část odpovědi pro kontrolu
    return response.content

def parse_rss_feed(content):
    # Explicitně specifikujeme XML parser
    soup = BeautifulSoup(content, 'xml')
    # Debug výpis pro kontrolu struktury
    logger.debug(f"Parsed XML structure: {soup.prettify()[:500]}")
    items = soup.find_all('event')
    logger.info(f"Found {len(items)} events")
    return items

def extract_item_data(item):
    try:
        # Debug výpis pro kontrolu struktury jednotlivého eventu
        logger.debug(f"Processing event: {item}")
        
        # Bezpečnější přístup k elementům
        name_elem = item.find('name')
        if not name_elem:
            logger.error("Name element not found")
            return None
        title = name_elem.string.strip() if name_elem.string else ""

        # Bezpečné získání URL
        details_elem = item.find('details')
        url_elem = details_elem.find('url') if details_elem else None
        link = url_elem.string.strip() if url_elem and url_elem.string else ""

        # Bezpečné získání popisu
        desc_elem = item.find('description')
        description = desc_elem.string.strip() if desc_elem and desc_elem.string else ""

        # Bezpečné získání data
        dates_elem = item.find('dates')
        date_elem = dates_elem.find('date') if dates_elem else None
        start_date_elem = date_elem.find('start_date') if date_elem else None
        
        if not start_date_elem or not start_date_elem.string:
            logger.error("Start date not found")
            return None
            
        start_date = start_date_elem.string.strip()
        
        # Convert date to required format
        try:
            date_obj = datetime.strptime(start_date, '%Y-%m-%d')
            formatted_date = int(date_obj.strftime('%Y%m%d'))
        except ValueError as e:
            logger.error(f"Error parsing date {start_date}: {e}")
            return None

        # Bezpečné získání URL obrázku
        image_url = None
        photos_elem = item.find('photos')
        if photos_elem:
            photo_elem = photos_elem.find('photo')
            if photo_elem:
                photo_url_elem = photo_elem.find('photo_url')
                if photo_url_elem and photo_url_elem.string:
                    image_url = photo_url_elem.string.strip()

        # Log processed URLs
        logger.info(f"Processing item: {title} with URL: {link}")
        
        # Check if date is after threshold
        if formatted_date < int(dateThreshold):
            logger.info(f"Přeskakuji položku s datem {formatted_date} (před prahem {dateThreshold})")
            return None
        
        # Get categories from Claude before creating the return dictionary
        categories = get_categories_from_claude(title, description)
        
        return {
            "Title": title,
            "URL": link,
            "Description": description,
            "ImageURL": image_url,
            "Category": categories,  # Changed from static "Media_Komunikace" to dynamic categories
            "Date": formatted_date
        }
    except Exception as e:
        logger.error(f"Chyba při zpracování položky: {str(e)}")
        logger.debug(f"Problematický item: {item}")
        return None

def create_initial_payload(year):
    return {
        "data": {
            "schema": {
                "searchableFields": ["Title", "URL", "Description", "ImageURL", "Date"],
                "metadataFields": ["Category", "Date"]
            },
            "name": f"kalendar_akci_table_{year}",
            "items": []
        }
    }

def save_payload_to_file(payload, filename):
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info(f"Payload saved to file: {filename}")

def append_item_to_payload(item, payload):
    payload['data']['items'].append(item)

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
    
    # Log stavu nahrávání
    logger.info(f"Stav nahrávání pro '{filename}': {response.status_code} - {response.reason}")
    
    # Přidáno: Zpoždění po každém API volání
    logger.info(f"Čekání {API_CALL_DELAY} sekund před dalším API voláním...")
    time.sleep(API_CALL_DELAY)

def main():
    try:
        rss_content = fetch_rss_feed(RSS_URL)
        rss_items = parse_rss_feed(rss_content)
        
        payloads_by_year = {}
        total_items = 0
        filtered_items = 0
        
        for item in rss_items:
            total_items += 1
            item_data = extract_item_data(item)
            if item_data:
                filtered_items += 1
                year = str(item_data['Date'])[:4]  # Převedeme int na string před získáním roku
                if year not in payloads_by_year:
                    payloads_by_year[year] = create_initial_payload(year)
                append_item_to_payload(item_data, payloads_by_year[year])
        
        for year, payload in payloads_by_year.items():
            output_file = f"payloads/table_kalendar_akci_{year}.json"
            save_payload_to_file(payload, output_file)
            upload_to_voiceflow(output_file)
        
        logger.info(f"Zpracováno celkem {total_items} položek, z toho {filtered_items} prošlo filtrem data.")
        
    except Exception as e:
        logger.error(f"Došlo k chybě: {str(e)}", exc_info=True)

if __name__ == "__main__":
    main()
