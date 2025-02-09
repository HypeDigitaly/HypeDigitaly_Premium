import json
import time
import os
import requests
from bs4 import BeautifulSoup
import logging
from logging.handlers import RotatingFileHandler
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import random
import datetime

# Configuration
JINA_AI_API_KEY = "REMOVED-JINA-KEY"
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"
OUTPUT_DIRECTORY = 'payloads'
JINA_AI_TIMEOUT = 30
MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 5

# Setup logging
SCRIPT_NAME = "scrape_contacts_zastupitelstvo"
LOG_DIR = f"{SCRIPT_NAME}_logs"
LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logger.handlers.clear()

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(console_handler)

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

# Update script name and URL
SINGLE_URL = "https://www.litomerice.cz/zakladni-informace-zm"

def get_html_content(url):
    logger.info(f"Getting HTML content from URL: {url}")
    api_url = f'https://r.jina.ai/{url}'
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {JINA_AI_API_KEY}",
        "X-Return-Format": "html",
        "X-Timeout": str(JINA_AI_TIMEOUT)
    }
    
    session = requests_retry_session()
    
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(api_url, headers=headers, timeout=JINA_AI_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            
            if data['status'] == 20000 and 'data' in data:
                html_content = data['data'].get('html', '')
                if html_content:
                    return html_content, {'url': url}
            
            raise ValueError("Failed to get valid HTML content")
            
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                raise
            delay = INITIAL_RETRY_DELAY * (2 ** attempt) + random.uniform(0, 1)
            time.sleep(delay)

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

def extract_contacts(soup):
    items = []
    logger.info("Starting contact extraction")
    counter = 1  # Initialize counter for Department numbering
    current_party = ""  # Track current political party
    
    # Find the table
    table = soup.find('table', class_='table table-striped table-bordered')
    if not table:
        logger.error("Could not find the main table")
        return items
        
    tbody = table.find('tbody')
    if not tbody:
        logger.error("Could not find tbody in table")
        return items
    
    # Process all rows
    for row in tbody.find_all('tr'):
        # Check if this is a party header row
        party_header = row.find('td', colspan="2")
        if party_header and party_header.find('h4'):
            current_party = party_header.find('h4').text.strip()
            logger.debug(f"Found party header: {current_party}")
            continue
        
        # Process contact row
        cols = row.find_all('td')
        if len(cols) == 2:  # Regular contact row has 2 columns
            name = cols[0].text.strip()
            email_elem = cols[1].find('a')
            
            if email_elem and 'href' in email_elem.attrs:
                email = email_elem['href'].replace('mailto:', '')
                
                if name and email and '@litomerice.cz' in email:
                    title, first_name, last_name = split_name_with_title(name)
                    
                    items.append({
                        'FullName': f"{title} {first_name} {last_name}".strip(),
                        'Title': title,
                        'Role': 'Člen',
                        'Department': f'ZAS-{counter}',
                        'Subdepartment': current_party,
                        'PhoneNumber': None,
                        'Email': email,
                        'Origin': 'Zastupitelstvo',
                        'FirstName': first_name,
                        'LastName': last_name,
                        'Category': 'Kontakt'
                    })
                    logger.info(f"Added contact: {name} ({email}) from party {current_party}")
                    counter += 1
    
    logger.info(f"Finished extracting contacts. Found {len(items)} items")
    return items

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
                "searchableFields": [
                    "FullName", "Title", "Role", "Department", "Subdepartment", 
                    "PhoneNumber", "Origin"
                ],
                "metadataFields": [
                    "FirstName", "LastName", "Origin", "Category"
                ]
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

def process_single_url():
    if not os.path.exists(OUTPUT_DIRECTORY):
        os.makedirs(OUTPUT_DIRECTORY)
        logger.info(f"Created output directory: {OUTPUT_DIRECTORY}")

    try:
        logger.info(f"Fetching content from URL: {SINGLE_URL}")
        html_content, metadata = get_html_content(SINGLE_URL)
        
        if not html_content:
            logger.error("No HTML content received")
            return
            
        logger.info("Successfully retrieved HTML content")
        soup = BeautifulSoup(html_content, 'html.parser')
        items = extract_contacts(soup)

        if not items:
            logger.error("No contacts were extracted from the page")
            return

        file_path = os.path.join(OUTPUT_DIRECTORY, "contacts_zastupitelstvo.json")
        json_output = {
            "data": {
                "schema": {
                    "searchableFields": ["FullName", "Role", "Department", "Subdepartment", "PhoneNumber", "Email", "Origin"],
                    "metadataFields": ["FirstName", "LastName", "Department", "Subdepartment", "Origin", "Category"]
                },
                "name": "contacts_zastupitelstvo",
                "items": items
            }
        }

        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(json_output, f, ensure_ascii=False, indent=2)
            logger.info(f"Successfully saved {len(items)} contacts to {file_path}")

        # Upload to Voiceflow
        logger.info("Attempting to upload to Voiceflow")
        upload_to_voiceflow("contacts_zastupitelstvo", items)

    except Exception as e:
        logger.error(f"Error processing URL {SINGLE_URL}: {str(e)}", exc_info=True)

if __name__ == "__main__":
    try:
        logger.info("Script started")
        process_single_url()
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")
    finally:
        logger.info("Script completed")