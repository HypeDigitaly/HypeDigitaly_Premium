import requests
import json
import logging
import os
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

#######################
### SCRIPT CONFIG ####
#######################

SCRIPT_NAME = "scrape_terminy"
LOG_DIR = f"{SCRIPT_NAME}_logs"
LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")

# Replace single FILTER_YEAR with a mapping
FILTER_YEARS = {
    1: 2025,  # RK from 2024
    2: 2024,  #
    3: 2024,  #
    4: 2024   #
}

#######################
### API KEYS & AUTH ###
#######################

VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"

########################
### URLs & ENDPOINTS ###
########################

BASE_URL = "https://samosprava.kr-vysocina.cz/api/v1"
TERMINY_LIST_URL = f"{BASE_URL}/terminy"

####################################
### PROCESSING PARAMETERS ##########
####################################

API_CALL_DELAY = 30  # Delay between API calls in seconds

########################################
### FILE & DIRECTORY CONFIGURATION  ####
########################################

PAYLOAD_DIR = "payloads"
os.makedirs(PAYLOAD_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

##########################
### LOGGING SETUP #######
##########################

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if not any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)

if not any(isinstance(handler, logging.StreamHandler) for handler in logger.handlers):
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(console_handler)

# Clear log file at the start of the script
with open(LOG_FILE, 'w'):
    pass

# Add organ type mapping
ORGAN_TYPE_MAPPING = {
    1: "RK",
    2: "ZK",
    3: "KOMISE",
    4: "VYBOR"
}

def convert_date_to_int(date_str):
    """Convert YYYY-MM-DD date string to YYYYMMDD integer"""
    try:
        if date_str:
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            return int(date_obj.strftime('%Y%m%d'))
        return None
    except ValueError as e:
        logger.warning(f"Could not parse date '{date_str}': {e}")
        return None

def fetch_meeting_data(meeting_id):
    """Fetch meeting data from the API"""
    url = f"{BASE_URL}/terminy/{meeting_id}"  # Fixed URL path
    logger.info(f"Fetching meeting data from: {url}")
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Error fetching meeting data: {str(e)}")
        return None

def fetch_terminy_list():
    """Fetch list of all terminy from the API and filter by year"""
    logger.info(f"Fetching terminy list from: {TERMINY_LIST_URL}")
    
    try:
        response = requests.get(TERMINY_LIST_URL)
        response.raise_for_status()
        data = response.json()
        
        # Filter records based on organ type and corresponding year
        filtered_data = [
            item for item in data 
            if item.get('typ_organu_id') in FILTER_YEARS 
            and item.get('rok') >= FILTER_YEARS[item.get('typ_organu_id')]
        ]
        
        logger.info(f"Filtered {len(data)} records to {len(filtered_data)} records based on organ-specific year filters")
        
        return filtered_data
    except requests.RequestException as e:
        logger.error(f"Error fetching terminy list: {str(e)}")
        return None

def create_unified_payload(data, organ_type, is_detail=False, meeting_id=None):
    """Create unified payload structure for both general and detail records"""
    items = []
    
    if is_detail and "materialy" in data:
        # Process detailed record
        for material in data["materialy"]:
            usneseni_with_dates = []
            if material.get("usneseni"):
                for u in material["usneseni"]:
                    if isinstance(u, dict):
                        usneseni_with_dates.append({
                            **u,
                            "datumJednani": convert_date_to_int(u.get("datumJednani")),
                            "datumZverejneni": convert_date_to_int(u.get("datumZverejneni"))
                        })

            prilohy_with_dates = []
            if material.get("prilohy"):
                for p in material["prilohy"]:
                    if isinstance(p, dict):
                        prilohy_with_dates.append({
                            **p,
                            "casZverejneni": convert_date_to_int(p.get("casZverejneni"))
                        })

            # Construct materialUrl if cisloJednaci exists
            material_url = ""
            if material.get("cisloJednaci"):
                material_url = f"https://samosprava.kr-vysocina.cz/material/{material.get('cisloJednaci')}"
            
            item = {
                "Title": material.get("nazev", ""),
                "Description": material.get("popisProblemu", ""),
                "URL": material.get("materialPdf", ""),
                "Category": ["Administrativa_Uredni_Zalezitosti"],
                "SubCategory": [organ_type],
                "id": meeting_id if meeting_id is not None else data.get("id"),
                "rok": data.get("rok"),
                "cislo": data.get("cislo"),
                "typ_organu_id": data.get("typ_organu_id"),
                "pocetMaterialu": data.get("pocetMaterialu"),
                "cisloJednaci": material.get("cisloJednaci"),
                "materialUrl": material_url,
                "neverejnost": material.get("neverejnost"),
                "duvodNeverejnosti": material.get("duvodNeverejnosti"),
                "zpracovatelskyOdbor": material.get("zpracovatelskyOdbor"),
                "zpracovatele": material.get("zpracovatele", []),
                "predkladatele": material.get("predkladatele", []),
                "navrhReseni": material.get("navrhReseni"),
                "pocetUsneseni": material.get("pocetUsneseni"),
                "pocetPriloh": material.get("pocetPriloh"),
                "terminKonani": convert_date_to_int(data.get("terminKonani")),
                "casZverejneniPozvanky": convert_date_to_int(data.get("casZverejneniPozvanky")),
                "casZverejneniMaterialu": convert_date_to_int(data.get("casZverejneniMaterialu")),
                "casZverejneniZapisu": convert_date_to_int(data.get("casZverejneniZapisu")),
                "usneseni": usneseni_with_dates,
                "prilohy": prilohy_with_dates
            }
            items.append(item)
    else:
        # Process general record
        item = {
            "Title": data.get("nazev", ""),
            "Description": "",
            "URL": "",
            "Category": ["Administrativa_Uredni_Zalezitosti"],
            "SubCategory": [organ_type],
            "id": data.get("id"),
            "rok": data.get("rok"),
            "cislo": data.get("cislo"),
            "typ_organu_id": data.get("typ_organu_id"),
            "pocetMaterialu": data.get("pocetMaterialu"),
            "cisloJednaci": None,
            "materialUrl": "",
            "neverejnost": None,
            "duvodNeverejnosti": None,
            "zpracovatelskyOdbor": None,
            "zpracovatele": [],
            "predkladatele": [],
            "navrhReseni": None,
            "pocetUsneseni": None,
            "pocetPriloh": None,
            "terminKonani": convert_date_to_int(data.get("terminKonani")),
            "casZverejneniPozvanky": convert_date_to_int(data.get("casZverejneniPozvanky")),
            "casZverejneniMaterialu": convert_date_to_int(data.get("casZverejneniMaterialu")),
            "casZverejneniZapisu": convert_date_to_int(data.get("casZverejneniZapisu")),
            "usneseni": [],
            "prilohy": []
        }
        items.append(item)

    return {
        "data": {
            "schema": {
                "searchableFields": [
                    "Title", "Description", "URL", "cisloJednaci",
                    "navrhReseni", "zpracovatelskyOdbor", "duvodNeverejnosti"
                ],
                "metadataFields": [
                    "Category", "SubCategory", "id", "rok", "cislo",
                    "typ_organu_id", "pocetMaterialu", "materialUrl",
                    "neverejnost", "zpracovatele", "predkladatele",
                    "pocetUsneseni", "pocetPriloh", "terminKonani",
                    "casZverejneniPozvanky", "casZverejneniMaterialu",
                    "casZverejneniZapisu", "usneseni", "prilohy"
                ]
            },
            "name": f"usneseni_{organ_type.lower()}_table",
            "items": items
        }
    }

def save_payload_to_file(payload, meeting_id):
    """Save payload to JSON file"""
    filename = os.path.join(PAYLOAD_DIR, f"{SCRIPT_NAME}_{meeting_id}_payload.json")
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info(f"Payload saved to: {filename}")
    return filename

def upload_to_voiceflow(filename):
    """Upload payload to Voiceflow"""
    logger.info(f"Uploading file to Voiceflow: {filename}")
    
    url = 'https://api.voiceflow.com/v1/knowledge-base/docs/upload/table?overwrite=true'
    headers = {
        'Authorization': VOICEFLOW_API_KEY,
        'accept': 'application/json',
        'content-type': 'application/json'
    }
    
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()  # This will raise an exception for error status codes
        
        if response.status_code == 200:
            logger.info(f"Successfully uploaded {filename} to Voiceflow")
        else:
            logger.error(f"Error uploading to Voiceflow: {response.text}")
            return False
        
        # Add delay after API call
        logger.info(f"Waiting {API_CALL_DELAY} seconds before next API call...")
        time.sleep(API_CALL_DELAY)
        return True
        
    except requests.RequestException as e:
        logger.error(f"Error uploading to Voiceflow: {str(e)}")
        return False

def main():
    try:
        terminy_list = fetch_terminy_list()
        if not terminy_list:
            logger.error("Failed to fetch terminy list")
            return
            
        # Group terminy by organ type
        terminy_by_type = {}
        for termin in terminy_list:
            organ_type_id = termin.get('typ_organu_id')
            if organ_type_id in ORGAN_TYPE_MAPPING:
                if organ_type_id not in terminy_by_type:
                    terminy_by_type[organ_type_id] = []
                terminy_by_type[organ_type_id].append(termin)
        
        # Process each organ type
        for organ_type_id, terminy in terminy_by_type.items():
            organ_code = ORGAN_TYPE_MAPPING[organ_type_id]
            all_items = []
            
            # Process only detailed material records (skip empty parent records)
            for termin in terminy:
                meeting_id = termin.get('id')
                meeting_data = fetch_meeting_data(meeting_id)
                if meeting_data:
                    # Check if meeting has materials
                    if meeting_data.get("materialy") and len(meeting_data.get("materialy", [])) > 0:
                        # Only add detailed material records - they contain the parent id for traceability
                        detail_payload = create_unified_payload(meeting_data, organ_code, is_detail=True, meeting_id=meeting_id)
                        all_items.extend(detail_payload["data"]["items"])
                    else:
                        # Meeting has no materials - create a header record for it
                        header_payload = create_unified_payload(meeting_data, organ_code, is_detail=False, meeting_id=meeting_id)
                        all_items.extend(header_payload["data"]["items"])
            
            # Create combined payload with schema
            combined_payload = {
                "data": {
                    "schema": {
                        "searchableFields": [
                            "Title", "Description", "URL", "cisloJednaci",
                            "navrhReseni", "zpracovatelskyOdbor", "duvodNeverejnosti"
                        ],
                        "metadataFields": [
                            "Category", "SubCategory", "id", "rok", "cislo",
                            "typ_organu_id", "pocetMaterialu", "materialUrl",
                            "neverejnost", "zpracovatele", "predkladatele",
                            "pocetUsneseni", "pocetPriloh", "terminKonani",
                            "casZverejneniPozvanky", "casZverejneniMaterialu",
                            "casZverejneniZapisu", "usneseni", "prilohy"
                        ]
                    },
                    "name": f"usneseni_{organ_code.lower()}_table",
                    "items": all_items
                }
            }
            
            # Save and upload combined payload
            filename = save_payload_to_file(combined_payload, f"combined_{organ_code}")
            if upload_to_voiceflow(filename):
                logger.info(f"Successfully uploaded combined data for {organ_code}")
            else:
                logger.error(f"Failed to upload combined data for {organ_code}")
            
            time.sleep(API_CALL_DELAY)
            
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}", exc_info=True)

if __name__ == "__main__":
    main() 