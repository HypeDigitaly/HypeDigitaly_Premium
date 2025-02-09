import requests
import json
import logging
import os
import time
from bs4 import BeautifulSoup
from datetime import datetime
from logging.handlers import RotatingFileHandler
import anthropic
import feedparser
import pytz
import traceback


#######################
### FEATURE FLAGS ####
#######################

ENABLE_CATEGORIZATION = False  # Set to False to disable Claude API categorization


#######################
### API KEYS & AUTH ###
#######################

CLAUDE_API_KEY = "REMOVED-ANTHROPIC-KEY"
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"


########################
### URLs & ENDPOINTS ###
########################

BASE_URL = "https://www.knihovnalitomerice.cz"
RSS_URL = "https://www.knihovnalitomerice.cz/knihovna-events-export.php"


####################################
### DATE & PROCESSING PARAMETERS ###
####################################

PROCESS_MAX_DATE = "20251231"  # Changed to end of 2025
dateThreshold = "20240101"  # Lower date threshold for processing (YYYYMMDD)
API_CALL_DELAY = 30  # Delay between API calls in seconds


########################################
### FILE & DIRECTORY CONFIGURATION  ####
########################################

SCRIPT_NAME = "scrape_knihovna"
LOG_DIR = f"{SCRIPT_NAME}_logs"
LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")
LAST_PROCESSED_DATE_FILE = f"{SCRIPT_NAME}_last_processed_date.txt"

# Create required directories
os.makedirs(LOG_DIR, exist_ok=True)


##########################
### LOGGING SETUP #######
##########################

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


################################################################################
### MAIN SCRIPT FUNCTIONS BELOW ##############################################
################################################################################

def load_last_processed_date():
    """Load the last processed date from file if it exists"""
    try:
        with open(LAST_PROCESSED_DATE_FILE, 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.info("No previous processed date found")
        return None

def save_last_processed_date(timestamp):
    """Save the last processed date to file"""
    with open(LAST_PROCESSED_DATE_FILE, 'w') as f:
        f.write(str(timestamp))
    logger.info(f"Saved last processed timestamp: {timestamp} ({datetime.fromtimestamp(int(timestamp)).strftime('%Y-%m-%d %H:%M:%S')})")

def fetch_rss_feed(url):
    logger.info(f"Fetching RSS feed from URL: {url}")
    response = requests.get(url)
    response.raise_for_status()
    return response.content

def parse_date(date_str):
    """Parse date string in format 'Tue, 21 Jan 2025 15:00:18 +0100' to datetime object"""
    try:
        # First try parsing with timezone offset
        return datetime.strptime(date_str, '%a, %d %b %Y %H:%M:%S %z')
    except ValueError:
        try:
            # Fallback to GMT/UTC format if the first attempt fails
            return datetime.strptime(date_str, '%a, %d %b %Y %H:%M:%S %Z')
        except ValueError as e:
            logger.error(f"Error parsing date '{date_str}': {e}")
            return None

def format_date_for_filename(date_str):
    """Convert date string to YYYYMMDD format for filenames"""
    date_obj = parse_date(date_str)
    if date_obj:
        return date_obj.strftime('%Y%m%d')
    return None

def parse_rss_feed(content):
    # Load the last processed date
    last_processed_date = load_last_processed_date()
    if last_processed_date:
        last_processed_dt = datetime.fromtimestamp(int(last_processed_date))
        logger.info(f"Found last processed date: {last_processed_dt}")
    else:
        last_processed_dt = None
        logger.info("No last processed date found - will process all items within threshold range")
    
    # Convert threshold dates to Unix timestamps
    max_date_dt = datetime.strptime(PROCESS_MAX_DATE, '%Y%m%d').replace(hour=23, minute=59, second=59)
    min_date_dt = datetime.strptime(dateThreshold, '%Y%m%d').replace(hour=0, minute=0, second=0)
    
    # Convert to Unix timestamps
    max_timestamp = int(max_date_dt.timestamp())
    min_timestamp = int(min_date_dt.timestamp())
    
    logger.info(f"Date thresholds: min={datetime.fromtimestamp(min_timestamp)} ({min_timestamp}), max={datetime.fromtimestamp(max_timestamp)} ({max_timestamp})")
    
    # Parse XML with correct root element
    soup = BeautifulSoup(content, 'lxml-xml')
    items = soup.find('data').find_all('item')  # Changed to find items under 'data' element
    
    logger.info(f"Found {len(items)} total items in RSS feed")
    
    filtered_items = []
    for item in items:
        try:
            start_timestamp = int(item.find('start').text.strip())
            title = item.find('title').text.strip()
            
            logger.info(f"Processing item: '{title}' with timestamp {start_timestamp} ({datetime.fromtimestamp(start_timestamp)})")
            
            # Skip if we've already processed this item in a previous run
            if last_processed_dt and start_timestamp <= int(last_processed_dt.timestamp()):
                logger.debug(f"Skipping already processed item: {title}")
                continue
                
            # Check if date is within our desired range
            if min_timestamp <= start_timestamp <= max_timestamp:
                filtered_items.append(item)
                logger.info(f"Including item: '{title}' with date {datetime.fromtimestamp(start_timestamp)}")
            else:
                logger.info(f"Skipping item: '{title}' - outside range (timestamp={start_timestamp}, min={min_timestamp}, max={max_timestamp})")
        except Exception as e:
            logger.error(f"Error processing item: {str(e)}")
            continue
    
    logger.info(f"Filtered to {len(filtered_items)} new items within date range")
    
    # Sort filtered_items by date (newest first)
    filtered_items.sort(key=lambda x: int(x.find('start').text.strip()), reverse=True)
    
    # Save the newest processed date (first item after sorting)
    if filtered_items:
        newest_timestamp = int(filtered_items[0].find('start').text.strip())
        save_last_processed_date(str(newest_timestamp))
    
    return filtered_items, last_processed_date

def extract_item_data(item):
    title = item.find('title').get_text(strip=True)
    link = item.find('url').get_text(strip=True)
    description = BeautifulSoup(item.find('description').text, 'html.parser').get_text(strip=True)
    image_url = item.find('image').get_text(strip=True) if item.find('image') else None
    
    # Convert Unix timestamp to datetime
    start_time = int(item.find('start').text.strip())
    date_obj = datetime.fromtimestamp(start_time)
    formatted_date = int(date_obj.strftime('%Y%m%d'))
    
    if formatted_date < int(dateThreshold):
        logger.info(f"Skipping item with date {formatted_date} (before threshold {dateThreshold})")
        return None
    
    # Set fixed categories for all items
    categories = ["Media_Komunikace", "Kultura_Pamatkova_Pece"]
    
    return {
        "Title": title,
        "URL": link,
        "Description": description,
        "ImageURL": image_url,
        "Category": categories,
        "Date": formatted_date
    }

def create_initial_payload(filename_without_extension):
    return {
        "data": {
            "schema": {
                "searchableFields": ["Title", "URL", "Description", "ImageURL", "Date"],
                "metadataFields": ["Category", "Date"]
            },
            "name": f"table_knihovna_{filename_without_extension}",  # Changed prefix
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
    url = 'https://api.voiceflow.com/v1/knowledge-base/docs/upload/table?overwrite=false'
    headers = {
        'Authorization': VOICEFLOW_API_KEY,
        'accept': 'application/json',
        'content-type': 'application/json'
    }
    
    with open(filename, 'r', encoding='utf-8') as f:
        payload = json.load(f)
    
    response = requests.post(url, headers=headers, json=payload)
    
    if response.status_code == 200:
        logger.info(f"Úspěně nahráno {len(payload['data']['items'])} položek pro soubor '{filename}'")
    else:
        logger.error(f"Chyba při nahrávání souboru '{filename}': {response.text}")
    
    # Log stavu nahrávání
    logger.info(f"Stav nahrávání pro '{filename}': {response.status_code} - {response.reason}")
    
    # Přidáno: Zpoždění po každém API volání
    logger.info(f"Čekání {API_CALL_DELAY} sekund před dalším API voláním...")
    time.sleep(API_CALL_DELAY)

def get_categories_from_claude(title, description):
    # Sloučení title a description do jednoho argumentu
    content = f"{title}\n{description}"
    
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    system_prompt = """Jsi precizní právní expert pro kategorizaci zpráv města Vysočina. Tvým KRITICKÝM úkolem je analyzovat obsah zprávy a přiřadit jí nejvhodnější kategorie. Toto je NAPROSTO ZÁSADNÍ pro správné fungování informačního systému.

## DOSTUPNÉ KATEGORIE (VYBÍRAT VÝHRADNĚ Z TOHOTO SEZNAMU):
- Administrativa_Uredni_Zalezitosti: Administrativní postupy, úřední záležitosti, dokumenty
- Charakteristika_Mesta: Obecné informace o městě, turistické informace
- Doprava: MHD, uzavírky, opravy silnic, dopravní situace
- Dotace: Všechny typy dotací a podpor
- Finance_Hospodareni: Rozpočet, účetnictví, hospodaření města
- Kontakt: Kontaktní údaje, úřední hodiny, komunikační kanály
- Krizove_Situace: Mimořádné události, bezpečnost, povodně
- Kultura_Pamatkova_Pece: Kulturní akce, památky, volnočasové aktivity
- Rozvoj_Projekty: Rozvojové projekty, inovace, infrastruktura
- Socialni_Pece: Sociální služby, péče o občany
- Strategicke_Dokumenty: Strategické plány a dokumenty města
- Ukrajina: Záležitosti spojené s ukrajinskou krizí
- Uzemni_Planovani_Stavebni_Rad: Územní plánování, stavební řízení
- Verejne_Zakazky: Veřejné zakázky, výběrová řízení
- Vzdelavani: Školství, vzdělávací programy
- Zdravotnictvi: Zdravotnická zařízení, zdravotní péče
- Zivotni_Prostredi_Zemedelstvi: Ekologie, ochrana přírody, zemědělství

## KRITICKÁ PRAVIDLA KATEGORIZACE:
1. MUSÍŠ přiřadit alespoň jednu kategorii - není přípustné vrátit prázdné pole
2. Maximální počet kategorií pro jednu zprávu je 3
3. Kategorie musí být PŘESNĚ zapsány včetně podtržítek
4. Používej vlastní úsudek, ale drž se příkladů níže jako vodítka
5. Při nejistotě použij tyto záložní pravidla:
   - Pro služby/podniky/restaurace -> "Kultura_Pamatkova_Pece"
   - Pro dopravu/infrastrukturu -> "Doprava"
   - Pro sportovní/rekreační zařízení -> "Kultura_Pamatkova_Pece"
   - Pro turistické atrakce/městská zařízení -> "Charakteristika_Mesta"
   - Pro obecné informace o městě -> "Kontakt"
   - Pro cokoliv, co souvisí s úřadem -> "Administrativa_Uredni_Zalezitosti"
   - Pro jakékoliv dokumenty města -> "Dokumenty_Mestskeho_Uradu"

## DŮLEŽITÉ: Níže jsou vzorové příklady, které ukazují, jak správně kategorizovat aktuality.
Když "obsah zprávy" obsahuje:

"obsah zprávy": "Kontakt na vedoucího/starostu/místostarostu"
Správné kategorie: ["Kontakt"]

"obsah zprávy": "Odbory úřadu/Počet odborů/Zaměstnanci odborů"
Správné kategorie: ["Kontakt"]

"obsah zprávy": "Úřední hodiny/Otvírací doba/Podatelna/Pokladna"
Správné kategorie: ["Kontakt", "Administrativa_Uredni_Zalezitosti"]

"obsah zprávy": "IČO/DIČ/Datová schránka/Bankovní spojení"
Správné kategorie: ["Kontakt", "Administrativa_Uredni_Zalezitosti"]

"obsah zprávy": "Zastupitelstvo/Rada města/Zápisy/Usnesení"
Správné kategorie: ["Kontakt", "Administrativa_Uredni_Zalezitosti"]

"obsah zprávy": "Jak nahlásit změnu trvalého bydliště/Jak podat stížnost"
Správné kategorie: ["Administrativa_Uredni_Zalezitosti"]

"obsah zprávy": "Volná pracovní místa/Výběrová řízení"
Správné kategorie: ["Administrativa_Uredni_Zalezitosti"]

"obsah zprávy": "Rozpočet/Účetní uzávěrka/Výroční zpráva"
Správné kategorie: ["Finance_Hospodareni"]

"obsah zprávy": "Platby/Poplatky/Exekuce/Insolvence"
Správné kategorie: ["Finance_Hospodareni"]

"obsah zprávy": "Dotace pro podnikatele/sport/kulturu"
Správné kategorie: ["Dotace"]

"obsah zprávy": "Dotace na obnovu památek/kapliček"
Správné kategorie: ["Dotace", "Kultura_Pamatkova_Pece"]

"obsah zprávy": "Dotace na zateplení/ekologické projekty"
Správné kategorie: ["Dotace", "Zivotni_Prostredi_Zemedelstvi"]

"obsah zprávy": "Podpora pro lékaře/zdravotní služby"
Správné kategorie: ["Zdravotnictvi", "Dotace"]

"obsah zprávy": "Sociální služby/Pěstounská péče/Rodinná péče"
Správné kategorie: ["Socialni_Pece"]

"obsah zprávy": "Územní plán/Stavební povolení/Stavební řád"
Správné kategorie: ["Uzemni_Planovani_Stavebni_Rad"]

"obsah zprávy": "MHD Litoměřice/Uzavírky/Opravy komunikací"
Správné kategorie: ["Doprava"]

"obsah zprávy": "Kvalita ovzduší/Chráněné druhy/NATURA"
Správné kategorie: ["Zivotni_Prostredi_Zemedelstvi"]

"obsah zprávy": "Krizový plán/Bezpečnostní rada/Povodně"
Správné kategorie: ["Krizove_Situace", "Strategicke_Dokumenty"]

"obsah zprávy": "Kulturní akce/Kalendář akcí/Památky města"
Správné kategorie: ["Kultura_Pamatkova_Pece"]

"obsah zprávy": "Strategické projekty/Inovace/Průmyslová zóna"
Správné kategorie: ["Rozvoj_Projekty", "Strategicke_Dokumenty"]

"obsah zprávy": "Seznam škol/Školská zařízení/Vzdělávací programy"
Správné kategorie: ["Vzdelavani"]

"obsah zprávy": "Ubytování uprchlíků/Náhrady za ubytování/Pomoc Ukrajincům"
Správné kategorie: ["Ukrajina", "Socialni_Pece"]

"obsah zprávy": "Aktuální veřejné zakázky/Výběrová řízení města"
Správné kategorie: ["Verejne_Zakazky"]

## ULTIMÁTNÍ PRAVIDLA:
1. Pečlivě analyzuj celý "obsah zprávy"
2. Použij vzorové příklady jako inspiraci pro pochopení kontextu jednotlivých kategorií
3. Využij vlastní inteligenci k identifikaci relevantních témat
4. Vrať JSON pole obsahující 1-3 nejvhodnější kategorie
5. Používej přesně uvedené názvy kategorií včetně podtržítek
6. Nepřiřazení kategorie NENÍ MOŽNOST - musíš vybrat alespoň jednu
7. Příklady výše slouží jako vodítko, ale nejsou vyčerpávající
8. Při kategorizaci kombinuj znalosti z příkladů s vlastní analýzou obsahu

## PAMATUJ:
- Nepřiřazení kategorie NENÍ MOŽNOST
- Raději přiřaď méně přesnou kategorii než žádnou
- Každá zpráva MUSÍ být kategorizována
- Maximálně 3 kategorie na jednu zprávu"""

    user_prompt = f"""KRITICKÝ ÚKOL: Analyzuj následující zprávu a přiřaď jí nejvhodnější kategorie.

OBSAH ZPRÁVY:
{content}

TVŮJ JEDINÝ ÚKOL:
Na základě pečlivé analýzy obsahu vrať JSON pole obsahující 1-3 nejvhodnější kategorie ze seznamu povolených kategorií.
Nepoužívej kategorii "Media_Komunikace" - ta bude přidána automaticky.

Příklad odpovědi: ["Kultura_Pamatkova_Pece", "Rozvoj_Projekty"]

ODPOVĚZ POUZE JSON POLEM KATEGORIÍ, NIC JINÉHO."""

    # Dokončení implementace
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

def create_payloads_structure():
    """Initialize empty payload structures only for years matching dateThreshold"""
    payloads = {}
    threshold_year = str(dateThreshold)[:4]
    current_year = datetime.now().year
    
    years = [str(year) for year in range(int(threshold_year), current_year + 1)]
    
    # Initialize a dict to store the last processed date for each year
    last_dates = {year: None for year in years}
    
    for year in years:
        payloads[year] = create_initial_payload(year)
        logger.info(f"Created initial payload structure for year {year}")
    
    return payloads, last_dates

def parse_litomerice_news():
    # Load the last processed date
    last_processed_date = load_last_processed_date()
    if last_processed_date:
        last_processed_dt = datetime.strptime(last_processed_date, '%a, %d %b %Y %H:%M:%S %z')
        logger.info(f"Found last processed date: {last_processed_dt}")
    else:
        last_processed_dt = None
        logger.info("No last processed date found - will process all items within threshold range")
    
    # Parse the RSS feed
    feed = feedparser.parse('https://www.litomerice.cz/aktuality?format=feed&type=rss')
    
    # Create timezone object for Czech Republic
    tz = pytz.timezone('Europe/Prague')
    
    news_items = []
    for entry in feed.entries:
        # Parse the publication date
        pub_date = datetime.strptime(entry.published, '%a, %d %b %Y %H:%M:%S %z')
        
        # Skip if we've already processed this item
        if last_processed_dt and pub_date <= last_processed_dt:
            logger.debug(f"Skipping already processed item from {pub_date}")
            continue
            
        # Check if date is within our desired range
        date_str = pub_date.strftime('%Y%m%d')
        if int(date_str) < int(dateThreshold):
            logger.debug(f"Skipping item with date {date_str} - before threshold")
            continue
            
        # Create news item dictionary
        news_item = {
            'title': entry.title,
            'link': entry.link,
            'description': entry.description,
            'pub_date': pub_date,
            'author': entry.author if hasattr(entry, 'author') else None,
            'date': int(date_str)
        }
        
        news_items.append(news_item)
    
    # Save the newest processed date if we found any items
    if news_items:
        newest_date = max(item['pub_date'] for item in news_items)
        save_last_processed_date(newest_date.strftime('%a, %d %b %Y %H:%M:%S %z'))
    
    logger.info(f"Found {len(news_items)} new items to process")
    return news_items

def main():
    try:
        # Fetch and parse RSS feed
        rss_content = fetch_rss_feed(RSS_URL)
        rss_items, last_processed_date = parse_rss_feed(rss_content)
        
        if not rss_items:
            logger.info("No new items to process")
            return
        
        # Get the date of the newest item for the filename
        newest_timestamp = int(rss_items[0].find('start').text.strip())
        newest_date = datetime.fromtimestamp(newest_timestamp).strftime('%Y%m%d')
        
        # Create payloads directory if it doesn't exist
        output_dir = "payloads"  # Changed from "output" to "payloads"
        os.makedirs(output_dir, exist_ok=True)
        
        # Initialize payload
        payload = create_initial_payload(newest_date)
        
        # Process each item
        for item in rss_items:
            item_data = extract_item_data(item)
            if item_data:
                append_item_to_payload(item_data, payload)
        
        # Save to file
        output_filename = os.path.join(output_dir, f"table_knihovna_{newest_date}.json")
        save_payload_to_file(payload, output_filename)
        
        # Upload to Voiceflow if there are items
        if payload['data']['items']:
            upload_to_voiceflow(output_filename)
        
    except Exception as e:
        logger.error(f"Error occurred: {str(e)}")
        logger.error(traceback.format_exc())
        raise

if __name__ == "__main__":
    main()
