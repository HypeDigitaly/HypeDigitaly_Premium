import json
import time
import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import datetime
import logging
import argparse
import anthropic
from logging.handlers import RotatingFileHandler
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import random

# Configuration
JSON_FILE_PATH = 'Contacts_URL_List.txt'
JINA_AI_API_KEY = "REMOVED-JINA-KEY"
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"
START_INDEX = 0
UPPER_THRESHOLD = None
RETRY_ATTEMPTS = 3
OUTPUT_DIRECTORY = 'payloads'
BASE_URL = "https://www.kr-ustecky.cz"

# Constants for script name and log directory
SCRIPT_NAME = "scrape_contacts"
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

# New variables for API call management
MAX_RETRIES = 3  # Maximum number of retry attempts
INITIAL_RETRY_DELAY = 5  # Initial retry delay in seconds

# Přejmenujeme proměnnou a nastavíme ji na 10 sekund
JINA_AI_TIMEOUT = 10

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

def load_urls_from_file(file_path):
    logger.info(f"Loading URLs from file: {file_path}")
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            url_data = json.load(file)
        logger.info(f"Loaded {len(url_data)} URL entries")
        return url_data
    except Exception as e:
        logger.error(f"Error loading URLs: {str(e)}")
        raise

def get_html_content(url):
    logger.info(f"Getting HTML content from URL: {url}")
    api_url = f'https://r.jina.ai/{url}'
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {JINA_AI_API_KEY}",
        "X-Return-Format": "html",
        "X-Timeout": str(JINA_AI_TIMEOUT),
        "X-Wait-For-Selector": "#utvar"
    }
    
    for attempt in range(MAX_RETRIES):
        try:
            response = requests_retry_session().get(api_url, headers=headers, timeout=JINA_AI_TIMEOUT)
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
                    logger.error("HTML content not found in API response")
                    raise ValueError("HTML content missing in API response")
            else:
                error_message = data.get('status', 'Unknown error')
                logger.error(f"Error getting content: {error_message}")
                raise ValueError(f"API Error: {error_message}")
        
        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            if attempt == MAX_RETRIES - 1:
                logger.error(f"Failed to get HTML content after {MAX_RETRIES} attempts: {str(e)}")
                raise
            
            delay = INITIAL_RETRY_DELAY * (2 ** attempt) + random.uniform(0, 1)
            logger.warning(f"Error getting HTML content, attempt {attempt + 1}/{MAX_RETRIES}. Retrying in {delay:.2f} seconds...")
            time.sleep(delay)
    
    # Použijeme JINA_AI_TIMEOUT místo API_CALL_DELAY
    time.sleep(JINA_AI_TIMEOUT)

def sanitize_filename(filename):
    # Remove invalid characters for filenames across operating systems
    return re.sub(r'[\\/*?:"<>|]', "_", filename)

def convert_phone_number(phone_str):
    """
    Převede telefonní číslo z formátu "+420 475 657 981" na celé číslo 475657981.
    Odstraní +420 a všechny mezery.
    Pokud telefon není platný, vrací N/A.
    """
    if phone_str == "N/A" or not phone_str:
        return "N/A"
    
    # Odstranění předvolby +420 (pokud existuje)
    phone_str = re.sub(r'^\+420\s*', '', phone_str)
    
    # Odstranění všech bílých znaků a spojovníků
    phone_str = re.sub(r'[\s\-]', '', phone_str)
    
    # Pokus o převod na celé číslo
    try:
        phone_int = int(phone_str)
        return phone_int
    except ValueError:
        # Pokud převod selže, vrátíme původní hodnotu
        return "N/A"

def extract_contacts(soup, url, title):
    items = []
    main_content = soup.find('div', class_='obsah')
    if not main_content:
        return items

    current_department = ""
    current_subdepartment = ""
    origin = determine_origin(url, title)

    # Extract the department name from the title
    department_match = re.search(r'^(.*?)(?::\s*Ústecký kraj)?$', title)
    if department_match:
        current_department = department_match.group(1).strip()

    for element in main_content.find_all(['li', 'strong']):
        if element.name == 'strong':
            # Update current_department with the specific commission/committee name
            strong_text = element.get_text(strip=True)
            if "komise" in strong_text.lower() or "výbor" in strong_text.lower():
                current_department = strong_text
                current_subdepartment = ""  # Reset subdepartment only when department changes
            elif "oddělení" in strong_text.lower():
                current_subdepartment = strong_text
        elif element.name == 'li' and element.get('class') == ['o']:
            contact_info = extract_contact_info(element, BASE_URL, current_department, current_subdepartment, origin)
            if contact_info:
                items.append(contact_info)

    return items

def extract_contact_info(li_element, base_url, department, subdepartment, origin):
    name_element = li_element.find('strong')
    if not name_element:
        return None

    full_name = name_element.get_text(strip=True)
    title, first_name, last_name = split_name_with_title(full_name)
    profile_link = name_element.find('a', href=True)
    full_url = urljoin(base_url, profile_link['href']) if profile_link else None

    phone_element = li_element.find('span', class_='phone')
    phone = phone_element.find('a').get_text(strip=True) if phone_element else "N/A"

    role = ""
    person_type = li_element.find('span', class_='person-type')
    if person_type:
        role = person_type.get_text(strip=True).strip(', ')

    return {
        "FullName": full_name,
        "Title": title,
        "FirstName": first_name,
        "LastName": last_name,
        "Role": role,
        "Department": department,
        "Subdepartment": subdepartment,
        "PhoneNumber": phone,
        "URL": full_url if full_url else "N/A",
        "Origin": origin,
        "Category": "Kontakt",  # Added static field with value "Kontakt"
    }

def split_name_with_title(full_name):
    titles = ['Mgr.', 'Bc.', 'Ing.', 'PhDr.', 'JUDr.', 'RNDr.', 'MUDr.', 'PaedDr.', 'doc.', 'prof.', 'DiS.', 'MBA', 'CSc.', 'Ph.D.']
    
    title = ""
    name_parts = full_name.split()
    while name_parts and any(name_parts[0].rstrip('.') == t.rstrip('.') for t in titles):
        title += name_parts.pop(0) + " "
    title = title.strip()
    
    if len(name_parts) > 1:
        first_name = name_parts[0]
        last_name = ' '.join(name_parts[1:])
    else:
        first_name = ' '.join(name_parts)
        last_name = ""
    
    return title, first_name, last_name

def determine_origin(url, title):
    lower_title = title.lower()
    if "komise" in lower_title:
        return "Komise"
    elif "výbor" in lower_title:
        return "Vybor"
    elif "odbor" in lower_title:
        return "Odbor"
    elif "zastupitelstvo" in lower_title:
        return "Zastupitelstvo"
    elif "hejtman" in lower_title:
        return "Hejtman"
    elif "rada" in lower_title or "radní" in lower_title:
        return "Rada"
    else:
        return "UNKNOWN"

def upload_to_voiceflow(table_name, items):
    logger.info(f"Uploading table '{table_name}' to Voiceflow")
    url = 'https://api.voiceflow.com/v1/knowledge-base/docs/upload/table?overwrite=true'
    headers = {
        'Authorization': VOICEFLOW_API_KEY,
        'accept': 'application/json',
        'content-type': 'application/json'
    }
    
    payload = {
        "data": {
            "schema": {
                "searchableFields": ["FullName", "Title", "Role", "Department", "Subdepartment", "PhoneNumber", "URL", "Origin"],
                "metadataFields": ["FirstName", "LastName", "Title", "Department", "Subdepartment", "PhoneNumber", "Origin", "Category"]
            },
            "name": table_name,
            "items": items
        }
    }
    
    log_filename = os.path.join(LOG_DIR, f"{table_name}_upload_log.txt")
    
    with open(log_filename, 'a', encoding='utf-8') as f:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        logger.info(f"Successfully uploaded {len(items)} items for table '{table_name}'")
    else:
        logger.error(f"Error uploading table '{table_name}': {response.text}")

def process_urls(url_data, start_index=0, upper_threshold=None, upload_to_voiceflow_flag=False):
    count = 0
    end_index = upper_threshold if upper_threshold else len(url_data)

    logger.info(f"\nZpracovávám URL od indexu {start_index} do {end_index}")

    if not os.path.exists(OUTPUT_DIRECTORY):
        os.makedirs(OUTPUT_DIRECTORY)
        logger.info(f"Vytvořen adresář: {OUTPUT_DIRECTORY}")
    else:
        logger.info(f"Výstupní adresář již existuje: {OUTPUT_DIRECTORY}")

    for i, entry in enumerate(url_data[start_index:end_index], start=start_index):
        initial_url = entry['URL']
        logger.info(f"\n--- Zpracovávám URL {i+1}/{end_index}: {initial_url} ---")

        try:
            html_content, metadata = get_html_content(initial_url)
            
            if html_content:
                title = metadata.get('title', 'Untitled')
                url = metadata.get('url', initial_url)
                soup = BeautifulSoup(html_content, 'html.parser')
                items = extract_contacts(soup, url, title)

                if items:
                    # Převedení telefonních čísel na celá čísla
                    for item in items:
                        item["PhoneNumber"] = convert_phone_number(item["PhoneNumber"])
                    
                    # Remove 'Ústecký kraj' from the table name
                    table_name = re.sub(r'\s*Ústecký kraj\s*$', '', title).strip()
                    
                    json_output = {
                        "data": {
                            "schema": {
                                "searchableFields": [
                                    "FullName", "Role", "Department", "Subdepartment", "PhoneNumber", "URL", "Origin"
                                ],
                                "metadataFields": [
                                    "FirstName", "LastName", "Department", "Subdepartment", "PhoneNumber", "Origin", "Category"
                                ]
                            },
                            "name": sanitize_filename(table_name),
                            "items": items
                        }
                    }

                    sanitized_title = sanitize_filename(table_name)
                    filename = f"{sanitized_title}.json"
                    file_path = os.path.join(OUTPUT_DIRECTORY, filename)
                    
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(json_output, f, ensure_ascii=False, indent=2)

                    logger.info(f"Successfully processed URL: {url}")
                    logger.info(f"Saved to file: {file_path}")

                    if upload_to_voiceflow_flag:
                        upload_to_voiceflow(sanitized_title, items)
                else:
                    logger.info(f"No content to save for URL: {url}")
            else:
                logger.info(f"No HTML content retrieved for URL: {initial_url}")

        except Exception as e:
            logger.error(f"Error processing URL: {initial_url}")
            logger.error(f"Error details: {str(e)}")

        count += 1
        # Odstraníme podmínku pro čekání po každých 3 URL a použijeme vždy JINA_AI_TIMEOUT
        logger.info(f"Čekám {JINA_AI_TIMEOUT} sekund před zpracováním dalšího URL...")
        time.sleep(JINA_AI_TIMEOUT)

def upload_existing_files(directory):
    for filename in os.listdir(directory):
        if filename.endswith('.json'):
            file_path = os.path.join(directory, filename)
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                table_name = data['data']['name']
                items = data['data']['items']
                
                # Převedení telefonních čísel na celá čísla
                for item in items:
                    if "PhoneNumber" in item:
                        item["PhoneNumber"] = convert_phone_number(item["PhoneNumber"])
                
                upload_to_voiceflow(table_name, items)

# Main execution
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape and upload data to Voiceflow")
    parser.add_argument("--skip-scraping", type=int, choices=[0, 1], default=0,
                        help="Skip scraping and upload existing files (0: no, 1: yes)")
    parser.add_argument("--upload-to-voiceflow", type=int, choices=[0, 1], default=1,
                        help="Upload data to Voiceflow (0: no, 1: yes)")
    args = parser.parse_args()

    try:
        logger.info("Script started")
        
        if args.skip_scraping:
            logger.info("Skipping scraping, uploading existing files to Voiceflow")
            if args.upload_to_voiceflow:
                upload_existing_files(OUTPUT_DIRECTORY)
            else:
                logger.info("Voiceflow upload is disabled. Files will not be uploaded.")
        else:
            logger.info(f"Loading URLs from: {JSON_FILE_PATH}")
            url_data = load_urls_from_file(JSON_FILE_PATH)

            logger.info(f"\nProcessing URLs with the following configuration:")
            logger.info(f"Start Index: {START_INDEX}")
            logger.info(f"Upper Threshold: {UPPER_THRESHOLD}")
            logger.info(f"Output Directory: {OUTPUT_DIRECTORY}")
            logger.info(f"Upload to Voiceflow: {'Yes' if args.upload_to_voiceflow else 'No'}")

            process_urls(url_data, START_INDEX, UPPER_THRESHOLD, args.upload_to_voiceflow)

    except Exception as e:
        logger.error(f"An error occurred during script execution: {str(e)}")

    finally:
        logger.info("Script completed")