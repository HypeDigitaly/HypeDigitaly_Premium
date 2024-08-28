import requests
import logging
import json
import anthropic
import time
import os
from bs4 import BeautifulSoup
from datetime import datetime, timezone, date
import re
import unicodedata
import random
from logging.handlers import RotatingFileHandler
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import xml.etree.ElementTree as ET

# API klíče a konstanty
JINA_AI_API_KEY = "[]"
CLAUDE_API_KEY = "[]"
VOICEFLOW_API_KEY = "[]"

# Zpoždění mezi scrapováním jednotlivých stránek (v sekundách)
SCRAPING_DELAY = 5

# Seznam URL ke zpracování
CUSTOM_URLS = []

# Přepínač pro kontrolu data modifikace
CHECK_MODIFIED_DATE = False

# Definice sekcí a jejich sitemap
SECTIONS = {
    #"Services": ["https://icuk.cz/pro-firmy-sitemap.xml", "https://icuk.cz/pro-region-sitemap.xml", "https://icuk.cz/pro-skoly-sitemap.xml"],
    #"References": ["https://icuk.cz/reference-sitemap.xml"],
    #"SuccessStories": ["https://icuk.cz/success-story-sitemap.xml"],
    "Events": ["https://icuk.cz/udalost-sitemap.xml"],
    #"Podcasts": ["https://icuk.cz/podcast-sitemap.xml"],
    #"Articles": ["https://icuk.cz/post-sitemap.xml"]
}

# Speciální URL
SPECIAL_URLS = {
    #"https://icuk.cz/o-nas/": "Contact",
    #"https://icuk.cz/dokumenty-ke-stazeni/": "Documents"
}

# Nové proměnné pro určení počátečního bodu
START_SITEMAP_INDEX = 0  # Index sitemapy, od které se má začít
START_URL_INDEX = 0  # Index URL v rámci sitemapy, od kterého se má začít

def date_to_number(date_string):
    try:
        date_obj = datetime.strptime(date_string, "%Y-%m-%d").date()
        return date_obj.toordinal()
    except ValueError:
        logger.error(f"Invalid date format: {date_string}")
        return None

def setup_logging():
    log_file = 'logs/scraper.log'
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s - %(levelname)s - %(message)s',
                        handlers=[
                            logging.FileHandler(log_file, mode='w', encoding='utf-8'),
                            logging.StreamHandler()
                        ])
    logger = logging.getLogger(__name__)
    logger.info("Logging initialized. Log file cleared and ready for new run.")
    return logger

logger = setup_logging()

def create_directories():
    directories = ['logs', 'payloads']
    for directory in directories:
        if not os.path.exists(directory):
            os.makedirs(directory)
            logger.info(f"Vytvořena složka: {directory}")
        else:
            logger.info(f"Složka {directory} již existuje")

def call_anthropic_api_with_retry(client, system_prompt, user_prompt, max_retries=3, base_delay=1):
    for attempt in range(max_retries):
        try:
            return client.messages.create(
                model="claude-3-5-sonnet-20240620",
                max_tokens=4000,
                temperature=0,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": user_prompt}
                ]
            )
        except anthropic.APIError as e:
            logger.error(f"Anthropic API error (attempt {attempt + 1}/{max_retries}): {str(e)}")
            if attempt == max_retries - 1:
                raise
            time.sleep(base_delay * (2 ** attempt))

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

def get_html_content(url, return_format='markdown'):
    logger.info(f"Získávání obsahu z URL: {url}")
    api_url = f'https://r.jina.ai/{url}'
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {JINA_AI_API_KEY}",
        "X-Return-Format": return_format
    }
    
    try:
        response = requests_retry_session().get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        logger.debug(f"API Response: {json.dumps(data, indent=2, ensure_ascii=False)}")
        
        if data['status'] == 20000 and 'data' in data:
            if return_format == 'html':
                content = data['data'].get('html', '')
                title = BeautifulSoup(content, 'html.parser').title.string if content else ''
            elif return_format == 'markdown':
                content = data['data'].get('content', '')
                title = data['data'].get('title', '')
            else:
                raise ValueError(f"Nepodporovaný formát: {return_format}")

            metadata = {
                'title': title,
                'url': data['data'].get('url', url)
            }
            return content, metadata
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

def parse_contacts(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    contacts = []
    for contact_div in soup.find_all('div', class_='osoba-info'):
        name = contact_div.find('h3', class_='osoba-nazev').text.strip() if contact_div.find('h3', class_='osoba-nazev') else ''
        role = contact_div.find('span', class_='osoba-pozice').text.strip() if contact_div.find('span', class_='osoba-pozice') else ''
        email = contact_div.find('span', class_='osoba-email').text.strip() if contact_div.find('span', class_='osoba-email') else ''
        linkedin = contact_div.find('span', class_='osoba-linkedin').find('a')['href'] if contact_div.find('span', class_='osoba-linkedin') and contact_div.find('span', class_='osoba-linkedin').find('a') else ''
        
        name_parts = name.split()
        first_name = name_parts[0] if name_parts else ''
        last_name = ' '.join(name_parts[1:]) if len(name_parts) > 1 else ''
        
        contact = {
            "FirstName": first_name,
            "LastName": last_name,
            "FullName": name,
            "Role": role,
            "Email": email,
            "LinkedInProfileURL": linkedin
        }
        contacts.append(contact)
    return contacts

def parse_documents(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    documents = []
    document_table = soup.find('table', class_='dokument-table')
    if document_table:
        for row in document_table.find_all('tr'):
            name_td = row.find('td', class_='tddokumentname')
            category_td = row.find('td', class_='tdkategory')
            
            if name_td and category_td:
                name_link = name_td.find('a')
                if name_link:
                    name = name_link.text.strip()
                    url = name_link['href']
                    category = category_td.text.strip()
                    
                    document = {
                        "Name": name,
                        "Category": category,
                        "URL": url
                    }
                    documents.append(document)
    return documents

def update_events_file(event):
    filename = "payloads/all_events.json"
    
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            content = f.read()
            if content.strip():
                data = json.loads(content)
                events = data["data"]["items"]
            else:
                events = []
    except (FileNotFoundError, json.JSONDecodeError):
        events = []
    
    # Convert Speakers and Images to strings if they are lists
    if isinstance(event.get('Speakers', []), list):
        event['Speakers'] = " | ".join(event['Speakers'])
    if isinstance(event.get('Images', []), list):
        event['Images'] = " | ".join(event['Images'])
    
    # Convert the date to its number representation
    if 'Date' in event and event['Date']:
        if isinstance(event['Date'], str):
            event['Date'] = date_to_number(event['Date'])
    
    # Ensure all required fields are present
    required_fields = ["Name", "Date", "SourceURL", "RegistrationURL", "TimeFrom", "TimeTo", "Place", "Summary", "Speakers", "Images"]
    for field in required_fields:
        if field not in event:
            event[field] = ""
    
    # Check if the event already exists (based on Name and Date)
    event_exists = any(e['Name'] == event['Name'] and e['Date'] == event['Date'] for e in events)
    
    if not event_exists:
        events.append(event)
        
        data = {
            "data": {
                "schema": {
                    "searchableFields": required_fields,
                    "metadataFields": ["Date"]
                },
                "name": "all_events",
                "tags": ["Events"],
                "items": events
            }
        }
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Událost přidána do souboru: {filename}")
        logger.debug(f"Aktuální obsah souboru událostí: {json.dumps(data, indent=2, ensure_ascii=False)}")
        print(f"Aktualizován soubor událostí: {filename}")
    else:
        logger.info(f"Událost '{event['Name']}' na datum {event['Date']} již existuje, přeskakuji.")

def update_podcasts_file(podcast):
    filename = "payloads/all_podcasts.json"
    
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
            podcasts = data["data"]["items"]
    except (FileNotFoundError, json.JSONDecodeError):
        podcasts = []
    
    # Check if the podcast already exists (based on SourceURL)
    podcast_exists = any(p['SourceURL'] == podcast['SourceURL'] for p in podcasts)
    
    if not podcast_exists:
        podcasts.append(podcast)
        
        data = {
            "data": {
                "schema": {
                    "searchableFields": ["SourceURL", "YoutubeURL", "Summary"]
                },
                "name": "all_podcasts",
                "tags": ["Podcasts"],
                "items": podcasts
            }
        }
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Podcast přidán do souboru: {filename}")
        logger.debug(f"Aktuální obsah souboru podcastů: {json.dumps(data, indent=2, ensure_ascii=False)}")
        print(f"Aktualizován soubor podcastů: {filename}")
    else:
        logger.info(f"Podcast s URL '{podcast['SourceURL']}' již existuje, přeskakuji.")

    logger.info(f"Celkový počet podcastů v souboru: {len(podcasts)}")

def parse_sitemap(sitemap_url):
    logger.info(f"Parsování sitemapy: {sitemap_url}")
    try:
        response = requests_retry_session().get(sitemap_url, timeout=30)
        response.raise_for_status()
        logger.debug(f"Sitemap response: {response.text[:500]}...")
        root = ET.fromstring(response.content)
        
        urls = []
        for url in root.findall('.//{http://www.sitemaps.org/schemas/sitemap/0.9}url'):
            loc = url.find('{http://www.sitemaps.org/schemas/sitemap/0.9}loc').text
            lastmod = url.find('{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod')
            lastmod = lastmod.text if lastmod is not None else None
            
            urls.append({
                'url': loc,
                'lastmod': lastmod
            })
        
        logger.debug(f"Parsed URLs: {urls}")
        return urls
    except Exception as e:
        logger.error(f"Chyba při parsování sitemapy {sitemap_url}: {str(e)}")
        return []

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

def update_contacts_file(new_contacts):
    filename = "payloads/all_contacts.json"
    with open(filename, 'r+', encoding='utf-8') as f:
        data = json.load(f)
        data['data']['items'].extend(new_contacts)
        f.seek(0)
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.truncate()
    logger.info(f"Updated {filename} with {len(new_contacts)} new contacts")

def update_documents_file(new_documents):
    filename = "payloads/all_documents.json"
    with open(filename, 'r+', encoding='utf-8') as f:
        data = json.load(f)
        data['data']['items'].extend(new_documents)
        f.seek(0)
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.truncate()
    logger.info(f"Updated {filename} with {len(new_documents)} new documents")

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
        "searchableFields": ["Question", "Answer"] if section not in ["Contact", "Documents"] else list(content[0].keys())
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

def save_all_events_payload():
    filename = "payloads/all_events.json"
    
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        logger.warning(f"Soubor {filename} neexistuje. Vytváří se nový.")
        data = {
            "data": {
                "schema": {
                    "searchableFields": ["Name", "Date", "Summary", "TimeFrom", "TimeTo", "Place", "SourceURL", "RegistrationURL", "Speakers", "Images"],
                    "metadataFields": ["Date"]
                },
                "name": "all_events",
                "tags": ["Events"],
                "items": []
            }
        }
    
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"Uložen payload pro všechny události do souboru: {filename}")
    logger.debug(f"Payload content: {json.dumps(data, indent=2, ensure_ascii=False)}")
    print(f"Vytvořen payload pro všechny události: {filename}")
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
    
    logger.debug(f"Voiceflow Upload Request: {json.dumps(payload, indent=2)}")
    
    response = requests_retry_session().post(url, headers=headers, json=payload, timeout=30)
    
    logger.debug(f"Voiceflow Upload Response: {response.text}")
    
    if response.status_code == 200:
        logger.info(f"Úspěšně nahráno {len(payload['data']['items'])} položek pro soubor '{filename}'")
    else:
        logger.error(f"Chyba při nahrávání souboru '{filename}': {response.text}")

def is_url_modified(lastmod):
    if not CHECK_MODIFIED_DATE:
        return True
    
    if not lastmod:
        return True
    
    try:
        lastmod_date = datetime.strptime(lastmod, "%Y-%m-%dT%H:%M:%S%z")
        current_date = datetime.now(timezone.utc)
        return lastmod_date > current_date
    except ValueError:
        logger.error(f"Neplatný formát data poslední modifikace: {lastmod}")
        return True
    
def determine_section(url):
    if "pro-firmy" in url or "pro-region" in url or "pro-skoly" in url:
        return "Services"
    elif "reference" in url:
        return "References"
    elif "success-story" in url:
        return "SuccessStories"
    elif "udalost" in url:
        return "Events"
    elif "post" in url:
        return "Articles"
    elif "podcast" in url:
        return "Podcasts"
    else:
        return "Unknown"

def convert_to_event(markdown_content, title):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    system_prompt = "Jsi právní poradce, který se specializuje na extrakci a formátování odpovědí v JSON formátu. Tvým úkolem je analyzovat poskytnutý obsah o události a vytvořit z něj strukturovaný JSON objekt s informacemi o události."
    
    user_prompt = f"""STRIKTNÍ INSTRUKCE PRO AI ASISTENTA:

Vaším JEDINÝM úkolem je extrahovat specifické informace z poskytnutého markdown obsahu a vytvořit PŘESNĚ FORMÁTOVANÝ JSON objekt. Neposkytujte ŽÁDNÝ další text, vysvětlení nebo komentáře.

1. STRIKTNĚ DODRŽUJTE následující JSON strukturu:
{{
    "Name": "Název události v češtině",
    "Date": "Přesné datum konání události ve formátu YYYY-MM-DD",
    "RegistrationURL": "URL pro registraci na událost",
    "TimeFrom": "Čas začátku události",
    "TimeTo": "Čas konce události",
    "Place": "Místo konání události v češtině",
    "Summary": "Stručné shrnutí nebo popis události v češtině",
    "Speakers": "Seznam řečníků na události v češtině, oddělený | ",
    "Images": "URL adresy obrázků spojené s událostí, oddělené | ",
    "SourceURL": "URL stránky události"
}}

2. PRAVIDLA PRO ZPRACOVÁNÍ:
   - Pokud informace není k dispozici, použijte hodnotu null.
   - Pole "Date" je ABSOLUTNĚ KLÍČOVÉ. Pokud není explicitně uvedeno, MUSÍTE ho odvodit z kontextu.
   - Extrahujte VŠECHNY relevantní informace související s událostí "{title}".
   - Zajistěte, že výstup je 100% VALIDNÍ JSON objekt.
   - Pro pole "Speakers" a "Images" použijte string s oddělovačem |, ne JSON array.

3. FORMÁT VÝSTUPU:
   - Poskytněte POUZE holý JSON objekt.
   - NEPOUŽÍVEJTE žádné uvozovací znaky kolem celého JSON objektu.
   - NEPŘIDÁVEJTE žádný úvodní text, vysvětlení nebo závěrečné komentáře.

Zde je markdown obsah k zpracování:

{markdown_content}

PAMATUJTE: Váš výstup musí být ČISTÝ JSON OBJEKT bez jakéhokoli dalšího textu nebo formátování."""

    logger.debug(f"Claude API Event Prompt: {user_prompt[:500]}...")
    
    response = call_anthropic_api_with_retry(client, system_prompt, user_prompt)
    response_text = response.content[0].text.strip()
    logger.debug(f"Raw Claude API Event Response: {response_text}")
    
    event_json = extract_json_from_response(response_text)
    
    if event_json:
        logger.info(f"Úspěšně extrahován JSON: {json.dumps(event_json, indent=2, ensure_ascii=False)}")
        if "Date" not in event_json or not event_json["Date"]:
            logger.warning("Pole 'Date' chybí nebo je prázdné v extrahovaných datech události.")
            date_match = re.search(r'\d{4}-\d{2}-\d{2}', markdown_content)
            if date_match:
                event_json["Date"] = date_match.group(0)
                logger.info(f"Datum extrahováno z obsahu: {event_json['Date']}")
        
        # Convert the date to its number representation
        if event_json.get("Date"):
            event_json["Date"] = date_to_number(event_json["Date"])
        
        return event_json
    else:
        raise ValueError("Nepodařilo se extrahovat JSON strukturu z odpovědi")

def convert_to_podcast(markdown_content, title):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    system_prompt = "Jsi odborník na analýzu obsahu, specializující se na extrakci informací o podcastech a formátování odpovědí v JSON formátu."
    
    user_prompt = f"""STRIKTNÍ INSTRUKCE PRO AI ASISTENTA:

Vaším JEDINÝM úkolem je extrahovat specifické informace o podcastu z poskytnutého markdown obsahu a vytvořit PŘESNĚ FORMÁTOVANÝ JSON objekt. Neposkytujte ŽÁDNÝ další text, vysvětlení nebo komentáře.

1. STRIKTNĚ DODRŽUJTE následující JSON strukturu:
{{
    "SourceURL": "URL stránky podcastu",
    "YoutubeURL": "URL YouTube videa podcastu",
    "Summary": "Stručné shrnutí nebo popis podcastu v češtině"
}}

2. PRAVIDLA PRO ZPRACOVÁNÍ:
   - Pokud informace není k dispozici, použijte hodnotu null.
   - Extrahujte VŠECHNY relevantní informace související s podcastem "{title}".
   - Zajistěte, že výstup je 100% VALIDNÍ JSON objekt.

3. FORMÁT VÝSTUPU:
   - Poskytněte POUZE holý JSON objekt.
   - NEPOUŽÍVEJTE žádné uvozovací znaky kolem celého JSON objektu.
   - NEPŘIDÁVEJTE žádný úvodní text, vysvětlení nebo závěrečné komentáře.

Zde je markdown obsah k zpracování:

{markdown_content}

PAMATUJTE: Váš výstup musí být ČISTÝ JSON OBJEKT bez jakéhokoli dalšího textu nebo formátování."""

    logger.debug(f"Claude API Podcast Prompt: {user_prompt[:500]}...")
    
    response = call_anthropic_api_with_retry(client, system_prompt, user_prompt)
    response_text = response.content[0].text.strip()
    logger.debug(f"Raw Claude API Podcast Response: {response_text}")
    
    podcast_json = extract_json_from_response(response_text)
    
    if podcast_json:
        logger.info(f"Úspěšně extrahován JSON podcastu: {json.dumps(podcast_json, indent=2, ensure_ascii=False)}")
        return podcast_json
    else:
        raise ValueError("Nepodařilo se extrahovat JSON strukturu z odpovědi")

def extract_json_from_response(response_text):
    try:
        # Try to parse the entire response as JSON
        return json.loads(response_text)
    except json.JSONDecodeError:
        # If that fails, try to find a JSON object in the response
        match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                logger.error("Nepodařilo se extrahovat validní JSON z odpovědi")
                return None
        else:
            logger.error("V odpovědi nebyl nalezen žádný JSON objekt")
            return None

def process_url(url, section):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return_format = 'html' if url in SPECIAL_URLS else 'markdown'
            content, metadata = get_html_content(url, return_format)
            if not content:
                logger.warning(f"Prázdný obsah pro URL {url}, přeskakuji")
                return None

            title = metadata.get('title', '')

            if url in SPECIAL_URLS:
                if SPECIAL_URLS[url] == "Contact":
                    parsed_content = parse_contacts(content)
                    update_contacts_file(parsed_content)
                elif SPECIAL_URLS[url] == "Documents":
                    parsed_content = parse_documents(content)
                    update_documents_file(parsed_content)
                return parsed_content
            elif section == "Events":
                try:
                    parsed_content = convert_to_event(content, title)
                    if parsed_content:
                        parsed_content["SourceURL"] = url
                        logger.info(f"Úspěšně získán obsah události: {json.dumps(parsed_content, indent=2, ensure_ascii=False)}")
                        update_events_file(parsed_content)
                        return parsed_content
                except Exception as e:
                    logger.error(f"Chyba při konverzi události pro URL {url}: {str(e)}")
            elif section == "Podcasts":
                try:
                    parsed_content = convert_to_podcast(content, title)
                    if parsed_content:
                        parsed_content["SourceURL"] = url
                        logger.info(f"Úspěšně získán obsah podcastu: {json.dumps(parsed_content, indent=2, ensure_ascii=False)}")
                        update_podcasts_file(parsed_content)
                        return parsed_content
                except Exception as e:
                    logger.error(f"Chyba při konverzi podcastu pro URL {url}: {str(e)}")
            else:
                # For other sections, we'll keep the existing Q&A processing logic
                client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
                system_prompt = "Jste špičkový systém pro extrakci informací a tvorbu JSON. Vaším úkolem je analyzovat poskytnutý obsah a vytvořit strukturované páry otázek a odpovědí ve formátu JSON. Veškerý výstup musí být v češtině a vztahovat se k tématu '{title}'."
                user_prompt = f"""
# TVŮJ JEIDNÝ ÚKOL: Vytvořte JSON pole párů otázek a odpovědí z poskytnutého obsahu. Veškerý výstup musí být v češtině a musí se přímo týkat tématu "{title}".

# OČEKÁVÁNÝ JSON FORMÁT TVÉ ODPOVĚDI:
[
  {{
    "Question": "Otázka č. 1 ? | Alternativa 1? | Alternativa 2?",
    "Answer": "Vyčerpávající faktická odpověď."
  }}, 
  {{
    "Question": "Otázka č. 2 ? | Alternativa 1? | Alternativa 2?",
    "Answer": "Vyčerpávající faktická odpověď."
  }}, 
  {{
    "Question": "Otázka č. 3 ? | Alternativa 1? | Alternativa 2?",
    "Answer": "Vyčerpávající faktická odpověď."
  }}, 
  {{
    "Question": "Otázka č. 4 ? | Alternativa 1? | Alternativa 2?",
    "Answer": "Vyčerpávající faktická odpověď."
  }}, 
  {{
    "Question": "Otázka č. 5 ? | Alternativa 1? | Alternativa 2?",
    "Answer": "Vyčerpávající faktická odpověď."
  }}, 
  ....
]

## PRAVIDLA:
1. Vytvořte minimálně 5 párů Q&A, ideálně více, dokud se nevyčerpá všechen relevantní a faktický obsah.
2. Každá otázka musí mít 3 formulace oddělené |.
3. Zaměřte se na VYSOCE SPECIFICKÉ údaje: čísla, částky, data, názvy.
4. Otázky a odpovědi musí být KONKRÉTNÍ a PŘÍMO související s "{title}".
5. Extrahujte VŠECHNY relevantní klíčové informace k tématu.
6. KRITICKY DŮLEŽITÉ: Zahrňte všechny relevantní URL odkazy PŘÍMO v odpovědích, PŘESNĚ v tom formátu, v jakém jsou uvedeny ve zdrojovém Markdown obsahu. 
   - NEMĚŇTE absolutně nic na formátu nebo textu odkazů. 
   - NEKÓDUJTE ani NEDEKÓDUJTE URL adresy. 
   - NEZASAHUJTE do struktury URL adres jakýmkoliv způsobem.
   - Zachovejte PŘESNĚ stejný formát URL, včetně všech parametrů, speciálních znaků a encodování, jaký je ve zdrojovém obsahu.
7. Vynechte irelevantní informace (záhlaví, zápatí, GDPR, cookies, atd.) a další informace, které explicitně přímo nesouvisejí s "{title}".
8. Zajistěte 100% validní JSON.
9. Nepoužívejte vnořené struktury v odpovědích.

## FORMÁTOVÁNÍ:
- Poskytněte pouze holé JSON pole.
- Žádné uvozovky kolem celého pole.
- Žádný dodatečný text mimo JSON.
- Správně escapujte uvozovky.

# OBSAH KE ZPRACOVÁNÍ:
{truncate_content(content)}

# DŮLEŽITÉ: Výstup = čisté JSON pole v češtině. Extrahujte maximum SPECIFICKÝCH, FAKTICKÝCH Q&A párů (min. 5) pokrývajících všechny relevantní informace k "{title}"."""

                response = call_anthropic_api_with_retry(client, system_prompt, user_prompt)
                response_text = response.content[0].text.strip()
                
                try:
                    parsed_content = json.loads(response_text)
                    if parsed_content and isinstance(parsed_content, list) and len(parsed_content) >= 5:
                        payload_file = save_payload_to_file(url, parsed_content, section, metadata)
                        upload_to_voiceflow(payload_file)
                    else:
                        logger.warning(f"Neplatný nebo nedostatečný Q&A obsah pro URL {url}, přeskakuji")
                except json.JSONDecodeError:
                    logger.error(f"Chyba při parsování JSON odpovědi pro URL {url}")

            return None
        except Exception as e:
            logger.error(f"Chyba při zpracování URL {url} (pokus {attempt + 1}/{max_retries}): {str(e)}", exc_info=True)
            if attempt == max_retries - 1:
                logger.error(f"Všechny pokusy o zpracování URL {url} selhaly")
            else:
                time.sleep(60)  # Čekáme minutu před dalším pokusem
    
    logger.error(f"URL {url} byla přeskočena kvůli opakovaným chybám")
    return None

def initialize_json_files():
    files_to_initialize = {
        "payloads/all_events.json": {
            "schema": {
                "searchableFields": ["Name", "Date", "Summary", "TimeFrom", "TimeTo", "Place", "SourceURL", "RegistrationURL", "Speakers", "Images"],
                "metadataFields": ["Date"]
            },
            "name": "all_events",
            "tags": ["Events"]
        },
        "payloads/all_podcasts.json": {
            "schema": {
                "searchableFields": ["SourceURL", "YoutubeURL", "Summary"]
            },
            "name": "all_podcasts",
            "tags": ["Podcasts"]
        },
        "payloads/all_contacts.json": {
            "schema": {
                "searchableFields": ["FirstName", "LastName", "FullName", "Role", "Email", "LinkedInProfileURL"],
                "metadataFields": ["FirstName", "LastName", "Role"]
            },
            "name": "all_contacts",
            "tags": ["Contact"]
        },
        "payloads/all_documents.json": {
            "schema": {
                "searchableFields": ["Name", "Category", "URL"],
                "metadataFields": ["Category"]
            },
            "name": "all_documents",
            "tags": ["Documents"]
        }
    }

    for file_path, structure in files_to_initialize.items():
        initial_data = {
            "data": {
                "schema": structure["schema"],
                "name": structure["name"],
                "tags": structure["tags"],
                "items": []
            }
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(initial_data, f, ensure_ascii=False, indent=2)
        logger.info(f"Initialized/reset file: {file_path}")

def upload_all_to_voiceflow():
    events_file = save_all_events_payload()
    upload_to_voiceflow(events_file)
    
    podcasts_file = "payloads/all_podcasts.json"
    if os.path.exists(podcasts_file):
        upload_to_voiceflow(podcasts_file)
    else:
        logger.warning(f"Soubor s podcasty {podcasts_file} neexistuje, přeskakuji upload.")
    
    contacts_file = "payloads/all_contacts.json"
    if os.path.exists(contacts_file):
        upload_to_voiceflow(contacts_file)
    else:
        logger.warning(f"Soubor s kontakty {contacts_file} neexistuje, přeskakuji upload.")
    
    documents_file = "payloads/all_documents.json"
    if os.path.exists(documents_file):
        upload_to_voiceflow(documents_file)
    else:
        logger.warning(f"Soubor s dokumenty {documents_file} neexistuje, přeskakuji upload.")

    logger.info("Všechny soubory byly nahrány do Voiceflow.")

def main():
    create_directories()
    
    start_time = datetime.now()
    logger.info(f"Začátek zpracování: {start_time}")

    # Ensure the events and podcasts files exist and have the correct structure
    initialize_json_files()

    # Process special URLs first
    for url, section in SPECIAL_URLS.items():
        try:
            process_url(url, section)
        except Exception as e:
            logger.error(f"Chyba při zpracování speciální URL {url}: {str(e)}", exc_info=True)

    # Upload Special URLs data to Voiceflow
    logger.info("Nahrávání dat ze speciálních URL do Voiceflow...")
    upload_all_to_voiceflow()

    if CUSTOM_URLS:
        logger.info(f"Zpracování vlastních URL: {CUSTOM_URLS}")
        for i, url in enumerate(CUSTOM_URLS[START_URL_INDEX:], start=START_URL_INDEX):
            try:
                section = determine_section(url)
                process_url(url, section)
                if i < len(CUSTOM_URLS) - 1:  # Don't delay after the last URL
                    logger.info(f"Čekání {SCRAPING_DELAY} sekund před zpracováním další URL...")
                    time.sleep(SCRAPING_DELAY)
            except Exception as e:
                logger.error(f"Chyba při zpracování URL {url}: {str(e)}", exc_info=True)
        
        # Upload processed data to Voiceflow after processing custom URLs
        logger.info("Nahrávání zpracovaných dat z vlastních URL do Voiceflow...")
        upload_all_to_voiceflow()
    else:
        for section, sitemaps in list(SECTIONS.items())[START_SITEMAP_INDEX:]:
            for sitemap_url in sitemaps:
                try:
                    urls = parse_sitemap(sitemap_url)
                    start_index = START_URL_INDEX if section == list(SECTIONS.keys())[START_SITEMAP_INDEX] else 0
                    for i, url_data in enumerate(urls[start_index:], start=start_index):
                        url = url_data['url']
                        lastmod = url_data['lastmod']
                        logger.info(f"Zpracovávám URL {i} ze sitemapy {sitemap_url}: {url}")
                        if is_url_modified(lastmod):
                            process_url(url, section)
                            if i < len(urls) - 1:  # Don't delay after the last URL
                                logger.info(f"Čekání {SCRAPING_DELAY} sekund před zpracováním další URL...")
                                time.sleep(SCRAPING_DELAY)
                        else:
                            logger.info(f"Přeskakuji URL {url}, nebyla modifikována od posledního zpracování")
                except Exception as e:
                    logger.error(f"Chyba při zpracování sitemapy {sitemap_url}: {str(e)}", exc_info=True)
                
                # Upload to Voiceflow after processing each sitemap
                logger.info(f"Nahrávání zpracovaných dat ze sitemapy {sitemap_url} do Voiceflow...")
                if section == "Events":
                    events_file = save_all_events_payload()
                    upload_to_voiceflow(events_file)
                elif section == "Podcasts":
                    podcasts_file = "payloads/all_podcasts.json"
                    upload_to_voiceflow(podcasts_file)
                else:
                    # For other sections, we'll upload all data
                    upload_all_to_voiceflow()

    # Final upload of all processed data to Voiceflow
    logger.info("Závěrečné nahrávání všech zpracovaných dat do Voiceflow...")
    upload_all_to_voiceflow()

    end_time = datetime.now()
    logger.info(f"Konec zpracování: {end_time}")
    logger.info(f"Celková doba zpracování: {end_time - start_time}")

if __name__ == "__main__":
    main()