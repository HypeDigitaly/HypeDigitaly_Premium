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

# Nastavení loggeru
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# API klíče a konstanty
CLAUDE_API_KEY = "[INSERT CLAUDE API KEY]"
FIRECRAWL_API_KEY = "[INSERT FIRECRAWL API KEY]"
VOICEFLOW_API_KEY = "[INSERT VF API KEY]"
BASE_URL = "https://www.kr-ustecky.cz"

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
    api_url = "https://api.firecrawl.dev/v0/scrape"
    payload = {
        "url": url,
        "pageOptions": {
            "includeHtml": True,
            "includeRawHtml": True,
            "replaceAllPathsWithAbsolutePaths": True
        }
    }
    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(api_url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        if data.get('success'):
            html_content = data.get('data', {}).get('html')
            if html_content:
                return html_content
            else:
                logger.error("HTML obsah nebyl nalezen v odpovědi API")
                raise ValueError("HTML obsah chybí v odpovědi API")
        else:
            error_message = data.get('data', {}).get('warning', 'Neznámá chyba')
            logger.error(f"Chyba při získávání obsahu: {error_message}")
            raise ValueError(f"Chyba API: {error_message}")
    except requests.RequestException as e:
        logger.error(f"Chyba při volání Firecrawl API: {str(e)}")
        raise

def parse_menu(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    main_menu = soup.find('ul', class_='ui')
    return main_menu

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
            categorized_links[category].append({"Title": link.text.strip(), "URL": absolute_url})
            
            save_payloads_to_files(categorized_links)
            time.sleep(5)  # 5 sekundová pauza mezi zpracováním odkazů
        
        sub_menu = menu_item.find('ul')
        if sub_menu:
            extract_links(sub_menu, path + [link.text.strip() if link else ''], categorized_links)
    elif menu_item.name == 'ul':
        for item in menu_item.find_all('li', recursive=False):
            extract_links(item, path, categorized_links)
    return categorized_links

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
4. Cokoliv se týká lidí, osob, krajského úřadu, organizační struktury nebo kontaktních informací, zařaďte do kategorie "Kontakt".

Vezměte v úvahu celou absolutní cestu v daném stromě k URL odkazu pro co nejpřesnější zařazení/zvolení dané kategorie ze vstupního seznamu.
"""

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
    
    return category

def save_payloads_to_files(categorized_links):
    output_dir = "payloads"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    for category, links in categorized_links.items():
        table_name = f"{category.lower()}_table"
        filename = f"{output_dir}/{table_name}_payload.json"
        payload = {
            "data": {
                "schema": {
                    "searchableFields": ["Title", "URL"]
                },
                "name": table_name,
                "tags": [category],
                "items": links
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

def clean_old_logs(log_file):
    """Vyčistí log soubor, pokud je starší než týden."""
    if not os.path.exists(log_file):
        return

    current_time = datetime.now()
    file_modification_time = datetime.fromtimestamp(os.path.getmtime(log_file))
    
    if current_time - file_modification_time > timedelta(days=7):
        try:
            os.remove(log_file)
            logger.info(f"Starý log soubor {log_file} byl odstraněn.")
        except Exception as e:
            logger.error(f"Chyba při odstraňování starého log souboru {log_file}: {str(e)}")

def upload_to_voiceflow(table_name, category, items):
    logger.info(f"Nahrávání tabulky '{table_name}' s kategorií '{category}' do Voiceflow")
    url = 'https://api.voiceflow.com/v1/knowledge-base/docs/upload/table?overwrite=true'
    headers = {
        'Authorization': VOICEFLOW_API_KEY,
        'accept': 'application/json',
        'content-type': 'application/json'
    }
    
    payload = {
        "data": {
            "schema": {
                "searchableFields": ["Title", "URL"]
            },
            "name": table_name,
            "tags": [category],
            "items": items
        }
    }
    
    log_filename = f"logs/{table_name}_log.txt"
    
    # Vyčistí starý log soubor, pokud existuje a je starší než týden
    clean_old_logs(log_filename)
    
    with open(log_filename, 'a', encoding='utf-8') as f:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"--- Log entry: {timestamp} ---\n")
        f.write("REQUEST:\n")
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n\n")
        
        response = requests.post(url, headers=headers, json=payload)
        
        f.write("RESPONSE:\n")
        f.write(f"Status Code: {response.status_code}\n")
        f.write(f"Response Body:\n{response.text}\n")
        f.write("--- End of log entry ---\n\n")
    
    if response.status_code == 200:
        logger.info(f"Úspěšně nahráno {len(items)} položek pro tabulku '{table_name}'")
    else:
        logger.error(f"Chyba při nahrávání tabulky '{table_name}': {response.text}")

def main(skip_scraping):
    if not os.path.exists('logs'):
        os.makedirs('logs')

    if skip_scraping:
        logger.info("Přeskakuji scraping, načítám payloady ze souborů")
        payloads = load_payloads_from_files()
    else:
        url = 'https://www.kr-ustecky.cz/mapa-stranek'
        logger.info(f"Začátek zpracování webu: {url}")
        
        try:
            html_content = get_html_content(url)
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
        upload_to_voiceflow(table_name, data['category'], data['items'])
    
    logger.info("Zpracování dokončeno")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape and upload data to Voiceflow")
    parser.add_argument("--skip-scraping", type=int, choices=[0, 1], default=0,
                        help="Přeskočit scraping a nahrát existující payloady (0: ne, 1: ano)")
    args = parser.parse_args()
    
    main(skip_scraping=args.skip_scraping)
