import requests
import json
import logging
import os
import time
from bs4 import BeautifulSoup
from datetime import datetime
from logging.handlers import RotatingFileHandler
import anthropic

# Add these constants at the top with other constants
CLAUDE_API_KEY = "REMOVED-ANTHROPIC-KEY"

# Constants for script name and log directory
SCRIPT_NAME = "scrape_news"
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
RSS_URL = "https://www.teplice.cz/rss/?21"
# OUTPUT_FILE NEMÁ AKTUÁLNĚ VÝZNAM, JELIKOŽ SE NÁZVY SOUBORŮ TVOŘÍ DYNAMICKY PODLE ROKŮ (SOUVISÍ S "dateThreshold")
# OUTPUT_FILE = "payloads/tiskove_informace_payload.json"
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"
dateThreshold = "20230101"  # Set the date threshold here

# Přidáno: Konstanta pro zpoždění mezi API voláními (v sekundách)
API_CALL_DELAY = 30

def fetch_rss_feed(url):
    logger.info(f"Fetching RSS feed from URL: {url}")
    response = requests.get(url)
    response.raise_for_status()
    return response.content

def parse_rss_feed(content):
    soup = BeautifulSoup(content, 'xml')
    items = soup.find_all('item')
    return items

def extract_item_data(item):
    title = item.title.get_text(strip=True)
    link = item.link.get_text(strip=True)
    description = item.description.get_text(strip=True)
    pub_date = item.pubDate.get_text(strip=True)
    
    wh_icon = item.find('wh_icon')
    if wh_icon:
        image_url = BASE_URL + wh_icon['src']
    else:
        logger.warning(f"Image not found for item with title: {title}")
        image_url = None
    
    # Převod pubDate na formát YYYYMMDD jako celé číslo
    date_obj = datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %Z')
    formatted_date = int(date_obj.strftime('%Y%m%d'))
    
    # Log all processed URLs
    logger.info(f"Processing item with URL: {link}")
    
    # Kontrola, zda je datum větší nebo rovno dateThreshold
    if formatted_date < int(dateThreshold):
        logger.info(f"Přeskakuji položku s datem {formatted_date} (před prahem {dateThreshold})")
        return None
    
    categories = get_categories_from_claude(title, description)
    
    return {
        "Title": title,
        "URL": link,
        "Description": description,
        "ImageURL": image_url,
        "Category": categories,
        "Date": formatted_date  # Již je celé číslo
    }

def create_initial_payload(year):
    return {
        "data": {
            "schema": {
                "searchableFields": ["Title", "URL", "Description", "ImageURL", "Date"],
                "metadataFields": ["Category", "Date"]
            },
            "name": f"tiskove_informace_table_{year}",
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

def get_categories_from_claude(title, description):
    # Sloučení title a description do jednoho argumentu
    content = f"{title}\n{description}"
    
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    system_prompt = """Jsi expertní systém pro kategorizaci aktuality města Teplice. Tvým úkolem je analyzovat titulek dané aktuality pod názvem: "obsah zprávy" a přiřadit jí relevantní kategorie přesně podle vzorových příkladů níže.

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
- Zivotni_Prostredi_Zemedelstvi

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

"obsah zprávy": "MHD Teplice/Uzavírky/Opravy komunikací"
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

# Pravidla kategorizace:
1. Pečlivě analyzuj celý "obsah zprávy"
2. Použij vzorové příklady jako inspiraci pro pochopení kontextu jednotlivých kategorií
3. Využij vlastní inteligenci k identifikaci relevantních témat a přiřazení odpovídajících kategorií
4. Vrať JSON pole obsahující všechny relevantní kategorie
5. Používej přesně uvedené názvy kategorií včetně podtržítek
6. Pokud obsah opravdu nesouvisí s žádnou kategorií, vrať prázdné pole []

# DŮLEŽITÉ: Příklady výše slouží jako vodítko pro pochopení kategorií, ale nejsou vyčerpávající. Při kategorizaci kombinuj znalosti z příkladů s vlastní analýzou obsahu."""

    user_prompt = f"""Analyzuj následující aktualitu s následujícím názvem. Použij příklady ze system promptu jako inspiraci, ale především využij vlastní inteligenci pro určení nejvhodnějších kategorií. Vrať odpovídající kategorie jako JSON pole. Nepoužívej kategorii "Media_Komunikace", ta bude přidána automaticky.

"obsah zprávy": {content}

# TVŮJ JEDINÝ ÚKOL: Na základě pečlivé analýzy obsahu vrať JSON pole nejvhodnějších kategorií, např: ["Kultura_Pamatkova_Pece", "Rozvoj_Projekty"]"""

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
    """Initialize empty payload structures for all possible years"""
    payloads = {}
    # Initialize with current year and previous year to start
    current_year = datetime.now().year
    years = [str(current_year), str(current_year - 1)]
    
    for year in years:
        payloads[year] = create_initial_payload(year)
        # Create the file immediately
        output_file = f"payloads/table_tiskove_informace_{year}.json"
        save_payload_to_file(payloads[year], output_file)
        logger.info(f"Created initial payload structure for year {year}")
    
    return payloads

def main():
    try:
        # Initialize payload structure at the start
        payloads_by_year = create_payloads_structure()
        
        rss_content = fetch_rss_feed(RSS_URL)
        rss_items = parse_rss_feed(rss_content)
        
        total_items = 0
        filtered_items = 0
        
        for item in rss_items:
            total_items += 1
            item_data = extract_item_data(item)
            if item_data:
                filtered_items += 1
                year = str(item_data['Date'])[:4]
                
                # If we encounter a new year, initialize its structure
                if year not in payloads_by_year:
                    payloads_by_year[year] = create_initial_payload(year)
                    output_file = f"payloads/table_tiskove_informace_{year}.json"
                    save_payload_to_file(payloads_by_year[year], output_file)
                    logger.info(f"Created new payload structure for year {year}")
                
                # Add item to the payload
                append_item_to_payload(item_data, payloads_by_year[year])
                
                # Save the updated payload immediately
                output_file = f"payloads/table_tiskove_informace_{year}.json"
                save_payload_to_file(payloads_by_year[year], output_file)
                logger.info(f"Added and saved item: {item_data['Title']} to year {year}")
        
        # Final upload to Voiceflow
        for year, payload in payloads_by_year.items():
            output_file = f"payloads/table_tiskove_informace_{year}.json"
            upload_to_voiceflow(output_file)
        
        logger.info(f"Zpracováno celkem {total_items} položek, z toho {filtered_items} prošlo filtrem data.")
        
    except Exception as e:
        logger.error(f"Došlo k chybě: {str(e)}", exc_info=True)

if __name__ == "__main__":
    main()
