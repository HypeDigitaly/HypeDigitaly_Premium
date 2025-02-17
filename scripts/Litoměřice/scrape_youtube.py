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


#######################
### FEATURE FLAGS ####
#######################

ENABLE_CATEGORIZATION = True  # Set to False to disable Claude API categorization


#######################
### API KEYS & AUTH ###
#######################

CLAUDE_API_KEY = "REMOVED-ANTHROPIC-KEY"
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"


########################
### URLs & ENDPOINTS ###
########################

BASE_URL = "https://www.youtube.com"
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id=UCPJ6gHLJYsKdCVFtlYSLwkQ"


####################################
### DATE & PROCESSING PARAMETERS ###
####################################

##PROCESS_MAX_DATE = "20241220" 
PROCESS_MAX_DATE = None  # You can set this to None or remove this line entirely
PROCESS_MAX_DATE = datetime.now().strftime('%Y%m%d') if PROCESS_MAX_DATE is None else PROCESS_MAX_DATE
dateThreshold = "20240101"  # Lower date threshold for processing (YYYYMMDD)
API_CALL_DELAY = 30  # Delay between API calls in seconds


########################################
### FILE & DIRECTORY CONFIGURATION  ####
########################################

SCRIPT_NAME = "scrape_youtube"
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
        with open(LAST_PROCESSED_DATE_FILE, 'r') as f:  # Use standardized file name
            return f.read().strip()
    except FileNotFoundError:
        logger.info("No previous YouTube last processed date found")
        return None
    except Exception as e:
        logger.error(f"Error loading YouTube last processed date: {str(e)}")
        return None

def save_last_processed_date(date_str):
    """Save the last processed date to file"""
    try:
        with open(LAST_PROCESSED_DATE_FILE, 'w') as f:  # Use standardized file name
            f.write(date_str)
        logger.info(f"Saved YouTube last processed date: {date_str}")
    except Exception as e:
        logger.error(f"Error saving YouTube last processed date: {str(e)}")

def fetch_rss_feed(url):
    logger.info(f"Fetching RSS feed from URL: {url}")
    response = requests.get(url)
    response.raise_for_status()
    return response.content

def parse_date(date_str):
    """Parse date from YouTube's ISO 8601 format"""
    try:
        # Parse ISO 8601 format (e.g. '2024-12-31T18:09:36+00:00')
        return datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%S%z')
    except Exception as e:
        logger.error(f"Error parsing date '{date_str}': {str(e)}")
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
        try:
            # Parse the stored date which might be in either format
            try:
                last_processed_dt = datetime.strptime(last_processed_date, '%Y-%m-%dT%H:%M:%S%z')
            except ValueError:
                last_processed_dt = datetime.strptime(last_processed_date, '%a, %d %b %Y %H:%M:%S %z')
            logger.info(f"Found last processed date: {last_processed_dt.strftime('%Y-%m-%d')}")
        except Exception as e:
            logger.warning(f"Could not parse last processed date: {str(e)}")
            last_processed_dt = None
    else:
        last_processed_dt = None
        logger.info("No last processed date found - will process all items within threshold range")
    
    # Parse with XML namespace awareness
    soup = BeautifulSoup(content, 'xml')
    entries = soup.find_all('entry')  # YouTube feed uses 'entry' instead of 'item'
    
    logger.info(f"Found {len(entries)} total entries in YouTube RSS feed")
    
    filtered_entries = []
    for entry in entries:
        pub_date = entry.published.text.strip()  # YouTube uses 'published' tag
        pub_date_dt = parse_date(pub_date)
        
        if not pub_date_dt:
            continue
            
        # Skip if we've already processed this entry
        if last_processed_dt and pub_date_dt <= last_processed_dt:
            logger.debug(f"Skipping already processed video from {pub_date_dt.strftime('%Y-%m-%d')}")
            continue
            
        # Convert date to integer format for threshold comparison
        date_int = int(pub_date_dt.strftime('%Y%m%d'))
        if date_int >= int(dateThreshold):
            filtered_entries.append(entry)
            logger.info(f"Including new video with date {pub_date_dt.strftime('%Y-%m-%d')}")
        else:
            logger.debug(f"Skipping video with date {pub_date_dt.strftime('%Y-%m-%d')} - before threshold")
    
    logger.info(f"Filtered to {len(filtered_entries)} new videos within date range")
    
    # Save the newest processed date if we found any entries
    if filtered_entries:
        newest_date = filtered_entries[0].published.text.strip()
        save_last_processed_date(newest_date)
    
    return filtered_entries, last_processed_date

def extract_item_data(item):
    """Extract data from YouTube RSS feed entry"""
    title = item.title.get_text(strip=True)
    link = item.find('link', rel='alternate')['href']
    
    # Get media group info
    media_group = item.find('media:group')
    
    # Get thumbnail URL from media:thumbnail
    thumbnail = media_group.find('media:thumbnail')
    image_url = thumbnail['url'] if thumbnail else None
    
    # Get description from media:description
    description = media_group.find('media:description').get_text(strip=True)
    
    # Get published date
    pub_date = item.published.get_text(strip=True)
    
    date_obj = parse_date(pub_date)
    if not date_obj:
        logger.error(f"Could not parse date for item: {title}")
        return None
        
    formatted_date = int(date_obj.strftime('%Y%m%d'))
    
    logger.info(f"Processing YouTube video with URL: {link}")
    
    if formatted_date < int(dateThreshold):
        logger.info(f"Skipping video with date {formatted_date} (before threshold {dateThreshold})")
        return None
    
    if ENABLE_CATEGORIZATION:
        categories = get_categories_from_claude(title, description)
    else:
        categories = ["Media_Komunikace"]
    
    return {
        "Title": title,
        "URL": link,
        "Description": description,
        "ImageURL": image_url,
        "Category": categories,
        "Date": formatted_date
    }

def create_initial_payload(filename_without_extension):
    """Create initial payload structure with standardized table naming"""
    return {
        "data": {
            "schema": {
                "searchableFields": ["Title", "URL", "Description", "ImageURL", "Date"],
                "metadataFields": ["Category", "Date"]
            },
            "name": f"table_youtube_videa_{filename_without_extension}",  # Standardized naming
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
        # Initialize payload structure
        payloads_by_year = {}
        
        rss_content = fetch_rss_feed(RSS_URL)
        filtered_entries, last_processed_date = parse_rss_feed(rss_content)
        
        if not filtered_entries:
            logger.info("No new videos to process")
            return
            
        newest_date = filtered_entries[0].published.text.strip()
        newest_date_formatted = format_date_for_filename(newest_date)
        
        last_processed_formatted = format_date_for_filename(last_processed_date) if last_processed_date else None
        if not last_processed_formatted:
            logger.warning("No last processed date found, using dateThreshold")
            last_processed_formatted = dateThreshold
        
        current_year = str(datetime.now().year)
        
        # Process entries and organize by year
        for entry in filtered_entries:
            item_data = extract_item_data(entry)
            if item_data:
                year = str(item_data['Date'])[:4]
                
                if year not in payloads_by_year:
                    if year == current_year:
                        # For current year, include date range in filename
                        filename_without_ext = f"{year}_{last_processed_formatted}_{newest_date_formatted}"
                    else:
                        # For past years, just use the year
                        filename_without_ext = year
                    payloads_by_year[year] = create_initial_payload(filename_without_ext)
                
                append_item_to_payload(item_data, payloads_by_year[year])
        
        # Save and upload files
        for year, payload in payloads_by_year.items():
            if payload['data']['items']:
                output_file = f"payloads/{payload['data']['name']}.json"
                save_payload_to_file(payload, output_file)
                upload_to_voiceflow(output_file)
                logger.info(f"Saved and uploaded file: {output_file} with {len(payload['data']['items'])} videos")
        
        logger.info(f"Processed total {len(filtered_entries)} new YouTube videos")
        
    except Exception as e:
        logger.error(f"Error occurred: {str(e)}", exc_info=True)

if __name__ == "__main__":
    main()
