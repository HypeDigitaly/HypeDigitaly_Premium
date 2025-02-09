import requests
import json
import logging
import os
from bs4 import BeautifulSoup
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Any

# Constants
SCRIPT_NAME = "scrape_contacts_odbory"
LOG_DIR = f"{SCRIPT_NAME}_logs"
LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")
RSS_URL = "https://www.litomerice.cz/incity_zamestnanci_Litomerice.php"
PAYLOAD_DIR = "payloads"
OUTPUT_FILE = os.path.join(PAYLOAD_DIR, "contacts_odbory.json")
VOICEFLOW_API_KEY = 'REMOVED-VOICEFLOW-KEY'  # Replace with actual API key

# Add department mapping constant
DEPARTMENT_MAPPING = {
    "Odbor správní": "ODB-1",
    "Odbor sociálních věcí a zdravotnictví": "ODB-2",
    "Stavební úřad": "ODB-3",
    "Odbor ekonomický": "ODB-4",
    "Odbor správy nemovitého majetku města": "ODB-5",
    "Odbor územního rozvoje": "ODB-6",
    "Odbor životního prostředí": "ODB-7",
    "Odbor školství, kultury, sportu a památkové péče": "ODB-8",
    "Kancelář starosty a tajemníka": "ODB-9",
    "Útvar kontroly a interního auditu": "ODB-10",
    "Obecní živnostenský úřad": "ODB-11",
    "Odbor dopravy a silničního hospodářství": "ODB-12",
    "Útvar obrany a krizového řízení": "ODB-13",
    "Útvar Zdravého města": "ODB-14",
    "Odbor komunikace, marketingu a cestovního ruchu": "ODB-15",
    "Odbor informačních a komunikačních technologií": "ODB-16",
    "Útvar personální": "ODB-17"
}

# Create log directory if it doesn't exist
os.makedirs(LOG_DIR, exist_ok=True)

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Clear existing log file and add handler for rotating file
if os.path.exists(LOG_FILE):
    open(LOG_FILE, 'w').close()  # Clear the log file

if not any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)

def create_initial_payload() -> Dict[str, Any]:
    """Create the initial payload structure."""
    return {
        "data": {
            "schema": {
                "searchableFields": [
                    "Name", "Title", "Subtitle", "Description",
                    "Place"
                ],
                "metadataFields": [
                    "FirstName", "LastName", "DegreeBefore",
                    "DegreeAfter", "Department", "Origin", "Emails", "Phones", "Category"
                ]
            },
            "name": "Contacts_Table",
            "items": []
        }
    }

def parse_contact(contact: BeautifulSoup, counter: int) -> Dict[str, Any]:
    """Parse a single contact entry from the XML."""
    try:
        # Log the contact being processed
        contact_name = f"{contact.find('jmeno').text} {contact.find('prijmeni').text}" if contact.find('jmeno') and contact.find('prijmeni') else "Unknown"
        logger.info(f"Processing contact: {contact_name}")

        # Get department name and map it to code
        department = contact.find('odbor').text if contact.find('odbor') else ""
        department_code = DEPARTMENT_MAPPING.get(department, department)  # Use original if not found in mapping

        # Create contact dictionary with the new XML structure
        contact_dict = {
            "Name": f"{contact.find('titulPred').text if contact.find('titulPred') else ''} {contact.find('jmeno').text} {contact.find('prijmeni').text}".strip(),
            "Title": contact.find('titulPred').text if contact.find('titulPred') else "",
            "Subtitle": contact.find('titulZa').text if contact.find('titulZa') else "",
            "Description": contact.find('funkce').text if contact.find('funkce') else "",
            "Emails": [contact.find('email').text] if contact.find('email') else [],
            "Phones": [contact.find('telefon').text] if contact.find('telefon') else [],
            "Place": contact.find('odbor').text if contact.find('odbor') else "",
            "FirstName": contact.find('jmeno').text if contact.find('jmeno') else "",
            "LastName": contact.find('prijmeni').text if contact.find('prijmeni') else "",
            "DegreeBefore": contact.find('titulPred').text if contact.find('titulPred') else "",
            "DegreeAfter": contact.find('titulZa').text if contact.find('titulZa') else "",
            "Department": department_code,
            "Origin": "Odbory",
            "Category": "Kontakt"
        }
        
        # Log successful parsing
        logger.info(f"Successfully parsed contact: {contact_name}")
        return contact_dict
    except Exception as e:
        logger.error(f"Error parsing contact {contact_name}: {str(e)}", exc_info=True)
        return None

def save_payload_to_file(payload: Dict[str, Any], filename: str) -> None:
    """Save the payload to a JSON file."""
    try:
        # Don't create the payloads directory - if it doesn't exist, let it raise an error
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Successfully saved payload to {filename}")
    except Exception as e:
        logger.error(f"Error saving payload to file: {str(e)}", exc_info=True)

def upload_to_voiceflow(payload: Dict[str, Any]) -> None:
    """Upload the payload to Voiceflow Knowledge Base."""
    try:
        logger.info("Uploading payload to Voiceflow")
        url = 'https://api.voiceflow.com/v1/knowledge-base/docs/upload/table?overwrite=true'
        headers = {
            'Authorization': VOICEFLOW_API_KEY,
            'accept': 'application/json',
            'content-type': 'application/json'
        }
        
        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code == 200:
            logger.info(f"Successfully uploaded {len(payload['data']['items'])} items to Voiceflow")
        else:
            logger.error(f"Error uploading to Voiceflow: {response.text}")
            
        # Log upload status
        logger.info(f"Upload status: {response.status_code} - {response.reason}")
        
    except Exception as e:
        logger.error(f"Error uploading to Voiceflow: {str(e)}", exc_info=True)

def main():
    try:
        # Fetch RSS feed
        logger.info(f"Fetching RSS feed from {RSS_URL}")
        response = requests.get(RSS_URL)
        response.raise_for_status()
        
        # Parse XML - Updated to match new structure
        soup = BeautifulSoup(response.content, 'xml')
        contacts = soup.find('seznamZamestnancu').find_all('zamestnanec')
        logger.info(f"Found {len(contacts)} contacts in RSS feed")
        
        # Create payload
        payload = create_initial_payload()
        
        # Process each contact
        processed_count = 0
        filtered_count = 0
        for index, contact in enumerate(contacts, 1):
            logger.info(f"Processing contact {index}/{len(contacts)}")
            contact_dict = parse_contact(contact, index)
            if contact_dict:
                payload["data"]["items"].append(contact_dict)
                processed_count += 1
        
        # Save to file
        save_payload_to_file(payload, OUTPUT_FILE)
        
        # Upload to Voiceflow
        upload_to_voiceflow(payload)
        
        logger.info(f"Processing complete. Total contacts: {len(contacts)}")
        logger.info(f"Successfully processed: {processed_count}")
        logger.info(f"Filtered out: {filtered_count}")
        
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}", exc_info=True)

if __name__ == "__main__":
    main()