import requests
import json
import logging
import os
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
import re

#######################
### API KEYS & AUTH ###
#######################

# Replace with your actual Voiceflow API Key
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"

########################
### URLs & ENDPOINTS ###
########################

DATA_SOURCE_URL = "https://portalno.kr-vysocina.cz/szp_ai.php"

###########################
### PROCESSING PARAMETERS ###
###########################

API_CALL_DELAY = 2  # Delay in seconds after an API call to avoid rate-limiting

#############################
### FEATURE FLAGS         ###
#############################
ENABLE_VOICEFLOW_UPLOAD = True # Set to False to disable the Voiceflow upload and only generate the JSON file

########################################
### FILE & DIRECTORY CONFIGURATION  ####
########################################

SCRIPT_NAME = "scrape_vysocinapecuje_vyhledavani"
LOG_DIR = f"{SCRIPT_NAME}_logs"
LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")
OUTPUT_DIR = "payloads"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "vysocina_pecuje_knowledge_base.json")

# Create required directories
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

##########################
### LOGGING SETUP #######
##########################

# Configure logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.propagate = False  # Prevent duplicate logs to the root logger

# Clear existing handlers to avoid multiple outputs
if logger.hasHandlers():
    logger.handlers.clear()

# File handler for detailed logs
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

# Console handler for high-level feedback
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(console_handler)

# Clear log file at the start of each run
with open(LOG_FILE, 'w', encoding='utf-8'):
    pass

################################################################################
### MAIN SCRIPT FUNCTIONS BELOW ##############################################
################################################################################

def fetch_json_data(url):
    """Fetches and decodes JSON data from the specified URL."""
    logger.info(f"Fetching JSON data from: {url}")
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'cs,en-US;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Referer': 'https://portalno.kr-vysocina.cz/',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'Cache-Control': 'no-cache',
        }
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching data: {e}")
        return None
    except json.JSONDecodeError:
        logger.error(f"Error decoding JSON. Response text starts with: '{response.text[:200]}'")
        return None

def transform_data_for_voiceflow(records):
    """Transforms raw records into the structured format for Voiceflow, separating searchable and metadata fields."""
    logger.info(f"Transforming {len(records)} records for Voiceflow upload...")
    items = []
    for record in records:
        # Extract URLs from description and other fields
        popis_text = record.get('popis', '')
        
        # Find all URLs in the description using regex
        url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+[^\s<>"{}|\\^`\[\].,;!?]'
        urls_in_popis = re.findall(url_pattern, popis_text)
        
        # Remove URLs from the description text for clean content
        clean_popis = re.sub(url_pattern, '', popis_text).strip()
        # Clean up extra whitespace
        clean_popis = re.sub(r'\s+', ' ', clean_popis)
        
        # Determine the source URL - prioritize URLs found in popis, then check other fields
        source_url = ''
        if urls_in_popis:
            source_url = urls_in_popis[0]  # Take the first URL found
        else:
            # Fallback to other potential URL fields
            source_url = record.get('www', record.get('url', record.get('web', record.get('website', ''))))

        # Extract unique municipalities and districts
        mista_pusobnosti = record.get('misto_pusobnosti', [])
        obce = sorted(list(set(m.get('obec', '') for m in mista_pusobnosti if m.get('obec'))))
        okresy = sorted(list(set(m.get('okres', '') for m in mista_pusobnosti if m.get('okres'))))

        # --- Searchable Content ---
        # All relevant text is combined into a single 'content' field for effective semantic search.
        searchable_content = [
            f"Název organizace: {record.get('org_nazev', '')}",
            f"Popis služby: {clean_popis}" if clean_popis else "",
            f"Druh služby: {record.get('druh_sluzby_nazev', '')}",
            f"Cílová skupina: {', '.join(record.get('cilova_skupina', []))}" if record.get('cilova_skupina') else "",
            f"Životní situace: {', '.join(record.get('zivotni_situace', []))}" if record.get('zivotni_situace') else "",
            f"Klíčová slova: {', '.join(record.get('klicova_slova', []))}" if record.get('klicova_slova') else ""
        ]

        # Filter out empty content parts
        searchable_content = [part for part in searchable_content if part.strip()]

        # Create flat item structure (not nested)
        item = {
            # Searchable field
            "content": "\n".join(searchable_content),
            # Metadata fields
            "ID_organizace": record.get('ID_organizace'),
            "nazev_organizace": record.get('org_nazev'),
            "druh_sluzby": record.get('druh_sluzby_nazev'),
            "cilova_skupina": record.get('cilova_skupina', []),
            "zivotni_situace": record.get('zivotni_situace', []),
            "klicova_slova": record.get('klicova_slova', []),
            "obce_pusobnosti": obce,
            "okresy_pusobnosti": okresy,
            "typ_pece_pobytova": record.get('pobytova') == '1',
            "typ_pece_ambulantni": record.get('ambulantni') == '1',
            "typ_pece_terenni": record.get('terenni') == '1',
            "source_url": source_url
        }

        items.append(item)
    logger.info("Data transformation complete.")
    return items

def create_voiceflow_payload(items):
    """Creates the final payload with the correct schema for Voiceflow."""
    return {
        "data": {
            "schema": {
                "searchableFields": ["content"],
                "metadataFields": [
                    "ID_organizace", "nazev_organizace", "druh_sluzby",
                    "cilova_skupina", "zivotni_situace", "klicova_slova",
                    "obce_pusobnosti", "okresy_pusobnosti",
                    "typ_pece_pobytova", "typ_pece_ambulantni", "typ_pece_terenni",
                    "source_url"
                ]
            },
            "name": "VysocinaPecuje_Vyhledavani",
            "items": items
        }
    }

def save_payload_to_file(payload, filename):
    """Saves the payload to a local JSON file."""
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Payload containing {len(payload['data']['items'])} items saved to: {filename}")
    except IOError as e:
        logger.error(f"Failed to save payload to {filename}: {e}")

def create_unique_value_files(transformed_items, output_dir):
    """Creates separate .txt files for each metadataField with all unique values.
    Uses sets to automatically ensure NO DUPLICATES - each value appears only once
    to serve as lookup combobox values from the original data source."""
    metadata_fields = [
        "ID_organizace", "nazev_organizace", "druh_sluzby",
        "cilova_skupina", "zivotni_situace", "klicova_slova",
        "obce_pusobnosti", "okresy_pusobnosti",
        "typ_pece_pobytova", "typ_pece_ambulantni", "typ_pece_terenni",
        "source_url"
    ]
    # Using sets ensures absolute uniqueness - no duplicate values will be stored
    unique_values = {field: set() for field in metadata_fields}
    for item in transformed_items:
        for field in metadata_fields:
            value = item.get(field)
            if value is not None:
                if isinstance(value, list):
                    # For list fields, collect unique values from all list items across all records
                    for sub_value in value:
                        if sub_value:  # Skip empty values
                            unique_values[field].add(sub_value)
                elif isinstance(value, bool):
                    # Convert boolean to string for consistent storage
                    unique_values[field].add(str(value))
                else:
                    # For single values, add if not empty
                    if value:
                        unique_values[field].add(value)
    # Write each unique set of values to separate file
    for field, values in unique_values.items():
        filename = f"vysocinaPecuje_{field}.txt"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            for value in sorted(values):  # Sort for consistent ordering
                f.write(f"{value}\n")
        logger.info(f"Created unique values file: {filepath} (contains {len(values)} unique values)")

def upload_to_voiceflow(filename):
    """Uploads the JSON file to Voiceflow's knowledge base."""
    if not os.path.exists(filename):
        logger.error(f"Cannot upload. File not found: {filename}")
        return

    logger.info(f"Uploading '{filename}' to Voiceflow. This will overwrite the existing table.")
    # The 'overwrite=true' parameter ensures the knowledge base is fully refreshed with the new data.
    url = 'https://api.voiceflow.com/v1/knowledge-base/docs/upload/table?overwrite=true'
    headers = {
        'Authorization': VOICEFLOW_API_KEY,
        'accept': 'application/json',
        'content-type': 'application/json'
    }
    
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        
        # Voiceflow API can return 200 or 202 for successful uploads
        if response.status_code in [200, 202]:
            logger.info(f"Successfully started upload for '{filename}'. Voiceflow is processing the file.")
        else:
            logger.error(f"Error uploading file. Status: {response.status_code}, Response: {response.text}")
            
    except requests.exceptions.RequestException as e:
        logger.error(f"An exception occurred during upload: {e}")

    logger.info(f"Waiting {API_CALL_DELAY} seconds before exiting.")
    time.sleep(API_CALL_DELAY)

def main():
    """Main execution function."""
    logger.info("--- Starting Vysočina Pečuje Scraper ---")
    try:
        # 1. Fetch
        raw_data = fetch_json_data(DATA_SOURCE_URL)
        if not raw_data:
            logger.critical("No data fetched from the source. Aborting.")
            return

        # 2. Transform
        transformed_items = transform_data_for_voiceflow(raw_data)
        if not transformed_items:
            logger.warning("Data was fetched, but no items were transformed. Check the source data format.")
            return

        # Create unique value files
        create_unique_value_files(transformed_items, OUTPUT_DIR)

        # 3. Create Payload
        voiceflow_payload = create_voiceflow_payload(transformed_items)

        # 4. Save to File
        save_payload_to_file(voiceflow_payload, OUTPUT_FILE)

        # 5. Upload to Voiceflow (if enabled)
        if ENABLE_VOICEFLOW_UPLOAD:
            upload_to_voiceflow(OUTPUT_FILE)
        else:
            logger.info("Voiceflow upload is disabled. Skipping upload step.")

        logger.info("--- Script finished successfully ---")

    except Exception as e:
        logger.critical(f"A critical error occurred in main process: {e}", exc_info=True)

if __name__ == "__main__":
    main()
