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
SCRIPT_NAME = "scrape_contacts_vybory"
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

# Remove JSON_FILE_PATH constant and add direct URL
SINGLE_URL = "https://www.litomerice.cz/zakladni-informace-zm/vybory-pri-zm-2022-2026"

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
    committee_counter = 1  # Initialize counter
    
    # Find the article body that contains all committees
    article_body = soup.find('div', {'itemprop': 'articleBody'})
    if not article_body:
        logger.warning("Could not find the article body")
        return items
    
    # Find all h4 headings (committee names) and process each committee section
    committee_headings = article_body.find_all('h4')
    
    for heading in committee_headings:
        committee_name = f"VYB-{committee_counter}"  # Create new department code
        
        # Process the chairman
        predseda_p = heading.find_next('p')
        if predseda_p and 'Předseda:' in predseda_p.text:
            name = predseda_p.text.replace('Předseda:', '').strip()
            if '(' in name:
                name, party = name.split('(', 1)
                party = party.rstrip(')').strip()
            else:
                party = ''
                
            title, first_name, last_name = split_name_with_title(name)
            
            items.append({
                'FullName': f"{title} {first_name} {last_name}".strip(),
                'Title': title,
                'Role': 'Předseda',
                'Department': committee_name,
                'Subdepartment': party,  # Changed to party
                'PhoneNumber': None,
                'Email': None,
                'Origin': 'Vybory',
                'FirstName': first_name,
                'LastName': last_name,
                'Category': 'Kontakt'
            })
        
        # Process the members list
        members_list = predseda_p.find_next('ol') if predseda_p else None
        if members_list:
            for member in members_list.find_all('li'):
                name = member.text.strip()
                if '(' in name:
                    name, party = name.split('(', 1)
                    party = party.rstrip(')').strip()
                else:
                    party = ''
                    
                title, first_name, last_name = split_name_with_title(name)
                
                items.append({
                    'FullName': f"{title} {first_name} {last_name}".strip(),
                    'Title': title,
                    'Role': 'Člen',
                    'Department': committee_name,
                    'Subdepartment': party,  # Changed to party
                    'PhoneNumber': None,
                    'Email': None,
                    'Origin': 'Vybory',
                    'FirstName': first_name,
                    'LastName': last_name,
                    'Category': 'Kontakt'
                })
        
        committee_counter += 1  # Increment counter for next committee
    
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
                    "FullName", "Role", "Department", "Subdepartment", 
                    "PhoneNumber", "Email", "Origin"
                ],
                "metadataFields": [
                    "FirstName", "LastName", "Department", "Subdepartment", 
                    "Origin", "Category"
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

    try:
        html_content, metadata = get_html_content(SINGLE_URL)
        if html_content:
            soup = BeautifulSoup(html_content, 'html.parser')
            items = extract_contacts(soup)

            if items:
                file_path = os.path.join(OUTPUT_DIRECTORY, "contacts_vybory.json")
                json_output = {
                    "data": {
                        "schema": {
                            "searchableFields": [
                                "FullName", "Role", "Department", "Subdepartment",
                                "PhoneNumber", "Email", "Origin"
                            ],
                            "metadataFields": [
                                "FirstName", "LastName", "Department", "Subdepartment",
                                "Origin", "Category"
                            ]
                        },
                        "name": "contacts_vybory",
                        "items": items
                    }
                }

                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(json_output, f, ensure_ascii=False, indent=2)

                logger.info(f"Successfully processed Rada URL: {SINGLE_URL}")
                logger.info(f"Saved to file: {file_path}")
                
                # Upload to Voiceflow
                upload_to_voiceflow("contacts_vybory", items)

    except Exception as e:
        logger.error(f"Error processing URL {SINGLE_URL}: {str(e)}")

if __name__ == "__main__":
    try:
        logger.info("Script started")
        process_single_url()
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")
    finally:
        logger.info("Script completed")