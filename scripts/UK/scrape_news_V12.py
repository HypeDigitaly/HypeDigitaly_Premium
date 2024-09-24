import requests
import json
import logging
import os
from bs4 import BeautifulSoup
from datetime import datetime
from logging.handlers import RotatingFileHandler

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

BASE_URL = "https://www.kr-ustecky.cz"
RSS_URL = "https://www.kr-ustecky.cz/rss/?21"
# OUTPUT_FILE NEMÁŠ AKTUÁLNĚ VÝZNAM, JELIKOŽ SE NÁZVY SOUBORŮ TVOŘÍ DYNAMICKY PODLE ROKŮ (SOUVISÍ S "dateThreshold")
# OUTPUT_FILE = "payloads/tiskove_informace_payload.json"
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"
dateThreshold = "20230101"  # Set the date threshold here

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
    
    wh_image = item.find('wh_image')
    if wh_image:
        image_url = BASE_URL + wh_image['src']
    else:
        logger.warning(f"Image not found for item with title: {title}")
        image_url = None  # nebo nastavte na nějakou výchozí hodnotu
    
    # Convert pubDate to YYYYMMDD format
    date_obj = datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %Z')
    formatted_date = date_obj.strftime('%Y%m%d')
    
    # Check if the date is greater than or equal to the dateThreshold
    if formatted_date < dateThreshold:
        logger.info(f"Skipping item with date {formatted_date} (before threshold {dateThreshold})")
        return None
    
    return {
        "Title": title,
        "URL": link,
        "Description": description,
        "ImageURL": image_url,
        "Category": "Media_Komunikace",
        "Date": formatted_date
    }

def create_initial_payload(year):
    return {
        "data": {
            "schema": {
                "searchableFields": ["Title", "URL", "Description", "ImageURL"],
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

def main():
    try:
        rss_content = fetch_rss_feed(RSS_URL)
        rss_items = parse_rss_feed(rss_content)
        
        payloads_by_year = {}
        
        for item in rss_items:
            item_data = extract_item_data(item)
            if item_data:
                year = item_data['Date'][:4]
                if year not in payloads_by_year:
                    payloads_by_year[year] = create_initial_payload(year)
                append_item_to_payload(item_data, payloads_by_year[year])
        
        for year, payload in payloads_by_year.items():
            output_file = f"payloads/table_tiskove_informace_{year}.json"
            save_payload_to_file(payload, output_file)
            upload_to_voiceflow(output_file)
        
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}", exc_info=True)

if __name__ == "__main__":
    main()
