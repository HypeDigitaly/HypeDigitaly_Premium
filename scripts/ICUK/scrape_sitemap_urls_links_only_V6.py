import requests
import xml.etree.ElementTree as ET
import logging
import json
import anthropic
import time
import os
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin

# Konstanty pro názvy souborů a adresářů
SCRIPT_NAME = "scrape_sitemap_urls_links_only"
LOG_DIR = "scrape_sitemap_urls_links_only_logs"  # Nová konstanta pro adresář s logy
LAST_RUN_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_last_run_time.txt")
LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")

# Vytvoření adresáře pro logy, pokud neexistuje
os.makedirs(LOG_DIR, exist_ok=True)

# Nastavení loggeru
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler(LOG_FILE, encoding='utf-8'),
                        logging.StreamHandler()
                    ])
logger = logging.getLogger(__name__)

# API klíče a konstanty
CLAUDE_API_KEY = "REMOVED-ANTHROPIC-KEY"
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"
BASE_URL = "https://icuk.cz/sitemap.xml"

# TRUE = NAHRAVAT POUZE AKTUALIZACE, NIKOLIV VSE OD ZACATKU
CHECK_MODIFIED_DATE = False

# Seznam kategorií
CATEGORIES = [
    "Services",
    "References",
    "SuccessStories",
    "Events",
    "Podcasts",
    "Articles",
    "Documents",
    "Contact"
]

def get_last_run_time():
    if os.path.exists(LAST_RUN_FILE):
        with open(LAST_RUN_FILE, 'r') as f:
            return datetime.fromisoformat(f.read().strip())
    return datetime.min.replace(tzinfo=timezone.utc)

def save_current_run_time():
    with open(LAST_RUN_FILE, 'w') as f:
        f.write(datetime.now(timezone.utc).isoformat())

def is_url_modified(lastmod):
    if not CHECK_MODIFIED_DATE:
        return True
    
    if not lastmod:
        return True
    
    last_run_time = get_last_run_time()
    
    try:
        lastmod_date = datetime.fromisoformat(lastmod.rstrip('Z')).replace(tzinfo=timezone.utc)
        return lastmod_date > last_run_time
    except ValueError:
        logger.error(f"Neplatný formát data poslední modifikace: {lastmod}")
        return True

def initialize_payloads():
    payloads = {}
    for category in CATEGORIES:
        if category != "Events":  # Přeskočit vytvoření payloadu pro Events
            table_name = f"{category.lower()}_table"
            payloads[category] = {
                "data": {
                    "schema": {
                        "searchableFields": ["Title", "URL"],
                        "metadataFields": ["Category"]
                    },
                    "name": table_name,
                    "items": []
                }
            }
    return payloads

def get_sitemap_content(url):
    logger.info(f"Získávání obsahu sitemapy z URL: {url}")
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        logger.error(f"Chyba při získávání sitemapy: {str(e)}")
        raise

def parse_sitemap(content):
    root = ET.fromstring(content)
    namespace = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    return [
        {
            'url': elem.find('sm:loc', namespace).text,
            'lastmod': elem.find('sm:lastmod', namespace).text if elem.find('sm:lastmod', namespace) is not None else None
        }
        for elem in root.findall('sm:url', namespace) + root.findall('sm:sitemap', namespace)
    ]

def categorize_url(url):
    url_lower = url.lower()
    
    # Expanded keyword matching on the entire URL
    if any(keyword in url_lower for keyword in ['podcast', 'audio']):
        return "Podcasts"
    elif any(keyword in url_lower for keyword in ['pro-region', 'pro-firmy', 'pro-skoly', 'sluzby', 'services']):
        return "Services"
    elif any(keyword in url_lower for keyword in ['reference', 'klienti', 'clients']):
        return "References"
    elif any(keyword in url_lower for keyword in ['success-story', 'uspech', 'case-study']):
        return "SuccessStories"
    elif any(keyword in url_lower for keyword in ['udalost', 'akce', 'event']):
        return "Events"
    elif any(keyword in url_lower for keyword in ['post', 'clanek', 'article', 'blog']):
        return "Articles"
    elif any(keyword in url_lower for keyword in ['kontakt', 'contact', 'about-us', 'o-nas']):
        return "Contact"
    elif any(keyword in url_lower for keyword in ['dokument', 'document', 'pdf', 'download', 'stahnout']):
        return "Documents"
    else:
        # If no clear category, use Claude API
        return categorize_url_claude(url)

def categorize_url_claude(url):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    prompt = f"""Analyze the following complete URL from the icuk.cz website:

{url}

Based on the entire URL structure and any keywords present anywhere in the URL, categorize this URL into ONE of the following categories:

{', '.join(CATEGORIES)}

Consider these guidelines:
- URLs containing 'podcast' or audio-related terms should be "Podcasts"
- URLs about services, regions, companies, or schools should be "Services"
- URLs with 'reference' or client-related terms should be "References"
- URLs about success stories or case studies should be "SuccessStories"
- URLs related to events or activities should be "Events"
- URLs containing blog posts or articles should be "Articles"
- URLs with downloadable files or resources should be "Documents"
- URLs with contact information or about the company should be "Contact"

Look at the entire URL, including the domain, path, and any query parameters. If you're unsure, choose the most likely category based on the complete URL structure.

RESPOND ONLY with the category name, nothing else.
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
    
    if category not in CATEGORIES:
        logger.warning(f"Claude vrátil neočekávanou kategorii: {category}. Použije se 'Uncategorized'.")
        return "Uncategorized"
    
    return category

def get_title_from_url(url):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    prompt = f"""URL: {url}

ÚKOL:
Vytvořte gramaticky správný titulek stránky v češtině s diakritikou, založený na struktuře dané URL.

PRAVIDLA:
1. Použijte pouze informace z URL cesty (vynechejte protokol a doménu).
2. Zachovejte přesný počet a pořadí slov z URL.
3. Nahraďte pomlčky mezerami.
4. Doplňte českou diakritiku tam, kde chybí, aby byl text gramaticky správný.
5. Každé slovo začínejte velkým písmenem, kromě předložek a spojek uprostřed věty.
6. Neměňte ani nepřidávejte žádná slova navíc.
7. Délka titulku musí být přesně stejná jako počet slov v URL cestě.
8. Zajistěte, aby výsledný text byl gramaticky správný a dával smysl v češtině.

PŘÍKLADY:
Input: https://icuk.cz/pro-firmy/startup-go/
Output: Pro Firmy Startup Go

Input: https://icuk.cz/pro-region/smart-akcelerator-iii/
Output: Pro Region Smart Akcelerátor III

Input: https://icuk.cz/pro-firmy/socialni-podnikani-spoint/
Output: Pro Firmy Sociální Podnikání SPoint

Input: https://icuk.cz/mesto-most-otevrelo-novy-cowork/
Output: Město Most Otevřelo Nový Cowork

Input: https://icuk.cz/obhajili-jsme-statut-referencniho-mista-v-rscn/
Output: Obhájili Jsme Statut Referenčního Místa v RSCN

Input: https://icuk.cz/goodaccess-bodovala-v-lize-mistru-pro-startupy/
Output: GoodAccess Bodovala v Lize Mistrů Pro Startupy

Input: https://icuk.cz/udalost/time-management-pro-podnikatele/
Output: Událost Time Management Pro Podnikatele

Input: https://icuk.cz/udalost/11-rocnik-startup-go-grill/
Output: Událost 11. Ročník Startup Go Grill

FORMÁT ODPOVĚDI:
Odpovězte pouze výsledným titulkem bez jakýchkoliv dodatečných informací nebo vysvětlení.

VÝSTUP:"""

    message = client.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=50,
        temperature=0,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    title = message.content[0].text.strip()
    return title

def process_sitemap(url, payloads, processed_urls=None):
    if processed_urls is None:
        processed_urls = set()

    if url in processed_urls:
        return

    processed_urls.add(url)
    content = get_sitemap_content(url)
    urls = parse_sitemap(content)

    for item in urls:
        child_url = item['url']
        lastmod = item['lastmod']
        
        if child_url.endswith('.xml'):
            process_sitemap(child_url, payloads, processed_urls)
        elif is_url_modified(lastmod):
            category = categorize_url(child_url)
            if category != "Events":  # Přeskočit zpracování pro Events
                title = get_title_from_url(child_url)
                payloads[category]["data"]["items"].append({
                    "Title": title,
                    "URL": child_url,
                    "Category": category
                })
                log_processed_url(child_url, category, title)
                save_payloads_to_files(payloads)  # Aktualizovat payloady po každé URL
            logger.info(f"Zpracována URL: {child_url}")
        else:
            logger.info(f"Přeskočena URL (nebyla modifikována): {child_url}")

def log_processed_url(url, category, title):
    log_file = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_url_list.log")
    with open(log_file, "a", encoding="utf-8") as f:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{timestamp} - URL: {url}, Category: {category}, Title: {title}\n")

def save_payloads_to_files(payloads):
    output_dir = "payloads"
    os.makedirs(output_dir, exist_ok=True)
    
    for category, payload in payloads.items():
        if category != "Events":  # Přeskočit ukládání pro Events
            table_name = f"{category.lower()}_table"
            filename = os.path.join(output_dir, f"{table_name}_payload.json")
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logger.info(f"Aktualizován payload pro tabulku '{table_name}' v souboru: {filename}")

def upload_to_voiceflow(payloads):
    logger.info("Nahrávání dat do Voiceflow")
    url = 'https://api.voiceflow.com/v1/knowledge-base/docs/upload/table?overwrite=true'
    headers = {
        'Authorization': VOICEFLOW_API_KEY,
        'accept': 'application/json',
        'content-type': 'application/json'
    }
    
    for category, payload in payloads.items():
        if category != "Events" and payload["data"]["items"]:  # Přeskočit nahrávání pro Events
            table_name = payload["data"]["name"]
            response = requests.post(url, headers=headers, json=payload)
            
            if response.status_code == 200:
                logger.info(f"Úspěšně nahráno {len(payload['data']['items'])} položek pro tabulku '{table_name}'")
            else:
                logger.error(f"Chyba při nahrávání tabulky '{table_name}': {response.text}")

def main():
    logger.info(f"Začátek zpracování sitemapy: {BASE_URL}")
    logger.info(f"Poslední běh skriptu: {get_last_run_time()}")
    
    try:
        payloads = initialize_payloads()
        process_sitemap(BASE_URL, payloads)
        
        logger.info("Zpracování sitemapy dokončeno. Nahrávání dat do Voiceflow.")
        upload_to_voiceflow(payloads)
    
        save_current_run_time()
        logger.info(f"Aktuální běh skriptu dokončen: {datetime.now(timezone.utc)}")
    except Exception as e:
        logger.error(f"Došlo k chybě při zpracování: {str(e)}", exc_info=True)
        return

    logger.info("Zpracování a nahrávání dokončeno")

if __name__ == "__main__":
    main()