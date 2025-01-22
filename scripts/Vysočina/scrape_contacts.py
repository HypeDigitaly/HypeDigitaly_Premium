import requests
import json
import logging
import os
from bs4 import BeautifulSoup
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Any

# Constants
SCRIPT_NAME = "scrape_contacts"
LOG_DIR = f"{SCRIPT_NAME}_logs"
LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")
RSS_URL = "https://www.kr-vysocina.cz/rss/?23"
PAYLOAD_DIR = "payloads"
OUTPUT_FILE = os.path.join(PAYLOAD_DIR, "contacts_table.json")
VOICEFLOW_API_KEY = 'REMOVED-VOICEFLOW-KEY'  # Replace with actual API key

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
                    "DegreeAfter", "Departments", "Emails", "Phones", "Category"
                ]
            },
            "name": "Contacts_Table",
            "items": []
        }
    }

def parse_contact(contact: BeautifulSoup) -> Dict[str, Any]:
    """Parse a single contact entry from the XML."""
    try:
        # Log the contact being processed
        contact_name = contact.find('name').text if contact.find('name') else "Unknown"
        logger.info(f"Processing contact: {contact_name}")

        # Extract departments from categories - simplified to array of integers
        departments = []
        categories = contact.find('categories')
        if categories:
            # Find all categoryId elements and store just the integer values
            category_ids = categories.find_all('categoryId')
            departments = [int(category_id.text) for category_id in category_ids]

        # Extract emails and phones
        emails = [email.text for email in contact.find('emails').find_all('email')] if contact.find('emails') else []
        phones = [phone.text for phone in contact.find('phones').find_all('phone')] if contact.find('phones') else []

        # Create contact dictionary
        contact_dict = {
            "Name": contact.find('name').text if contact.find('name') else "",
            "Title": contact.find('title').text if contact.find('title') else "",
            "Subtitle": contact.find('subtitle').text if contact.find('subtitle') else "",
            "Description": contact.find('description').text if contact.find('description') else "",
            "Emails": emails,
            "Phones": phones,
            "Place": contact.find('place').text if contact.find('place') else "",
            "FirstName": contact.find('firstname').text if contact.find('firstname') else "",
            "LastName": contact.find('lastname').text if contact.find('lastname') else "",
            "DegreeBefore": contact.find('degreebefore').text if contact.find('degreebefore') else "",
            "DegreeAfter": contact.find('degreeafter').text if contact.find('degreeafter') else "",
            "Departments": departments,
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
        
        # Parse XML
        soup = BeautifulSoup(response.content, 'xml')
        contacts = soup.find_all('contact')
        logger.info(f"Found {len(contacts)} contacts in RSS feed")
        
        # Create payload
        payload = create_initial_payload()
        
        # Process each contact
        processed_count = 0
        filtered_count = 0
        for index, contact in enumerate(contacts, 1):
            logger.info(f"Processing contact {index}/{len(contacts)}")
            contact_dict = parse_contact(contact)
            if contact_dict:
                # Filter out unwanted contacts
                name = contact_dict.get("Name", "").lower()
                description = contact_dict.get("Description", "").lower()
                if name not in ["správce", "admin teplice"] and description not in ["správce", "admin teplice"]:
                    payload["data"]["items"].append(contact_dict)
                    processed_count += 1
                else:
                    filtered_count += 1
                    logger.info(f"Filtered out contact: {contact_dict.get('Name')} (admin/správce)")
        
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