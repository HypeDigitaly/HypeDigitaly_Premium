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

# Configuration
JSON_FILE_PATH = 'URL_List.txt'
FIRECRAWL_API_KEY = '[INSERT FIRECRAWL API KEY]'
CLAUDE_API_KEY = "[INSERT CLAUDE API KEY]"
VOICEFLOW_API_KEY = "[INSERT VF API KEY]"
START_INDEX = 0
UPPER_THRESHOLD = None
RETRY_ATTEMPTS = 3
RETRY_DELAY = 600
OUTPUT_DIRECTORY = 'payloads'
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v0/scrape"
BASE_URL = "https://www.kr-ustecky.cz"

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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

def scrape_url_with_retries(url, api_key, max_attempts, delay):
    logger.info(f"\nAttempting to scrape URL: {url}")
    attempts = 0
    while attempts < max_attempts:
        try:
            logger.info(f"Scrape attempt {attempts + 1}/{max_attempts}")
            payload = {
                "pageOptions": {
                    "includeHtml": True,
                    "onlyMainContent": True,
                    "onlyIncludeTags": ["#stred"]
                },
                "url": url
            }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }

            response = requests.post(FIRECRAWL_API_URL, json=payload, headers=headers)
            response.raise_for_status()

            logger.info("Scrape successful")
            return response.json()
        except Exception as e:
            attempts += 1
            logger.error(f"Attempt {attempts} failed. Error: {str(e)}. Retrying in {delay} seconds...")
            time.sleep(delay)
    raise Exception(f"Failed to scrape URL: {url} after {max_attempts} attempts.")

def sanitize_filename(filename):
    sanitized = re.sub(r'[^\w\-_\. ]', '', filename)
    sanitized = sanitized.replace(' ', '_')
    return sanitized[:255]

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
            elif "oddělení" in strong_text.lower():
                current_subdepartment = strong_text
            else:
                current_subdepartment = ""
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
                "metadataFields": ["FirstName", "LastName", "Title", "Department", "Subdepartment", "Origin"]
            },
            "name": table_name,
            "tags": ["Kontakt"],
            "items": items
        }
    }
    
    log_filename = f"logs/{table_name}_log.txt"
    
    if not os.path.exists('logs'):
        os.makedirs('logs')
    
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

def process_urls(url_data, firecrawl_api_key, start_index=0, upper_threshold=None, upload_to_voiceflow_flag=False):
    count = 0
    end_index = upper_threshold if upper_threshold else len(url_data)

    logger.info(f"\nProcessing URLs from index {start_index} to {end_index}")

    if not os.path.exists(OUTPUT_DIRECTORY):
        os.makedirs(OUTPUT_DIRECTORY)
        logger.info(f"Created directory: {OUTPUT_DIRECTORY}")
    else:
        logger.info(f"Output directory already exists: {OUTPUT_DIRECTORY}")

    for i, entry in enumerate(url_data[start_index:end_index], start=start_index):
        initial_url = entry['URL']
        logger.info(f"\n--- Processing URL {i+1}/{end_index}: {initial_url} ---")

        try:
            response = scrape_url_with_retries(initial_url, firecrawl_api_key, RETRY_ATTEMPTS, RETRY_DELAY)
            
            if response.get('success', False):
                data = response.get('data', {})
                metadata = data.get('metadata', {})
                title = metadata.get('title', 'Untitled')
                url = metadata.get('sourceURL', initial_url)
                html_content = data.get('html', '')

                if html_content:
                    soup = BeautifulSoup(html_content, 'html.parser')
                    items = extract_contacts(soup, url, title)

                    if items:
                        # Remove 'Ústecký kraj' from the table name
                        table_name = re.sub(r'\s*Ústecký kraj\s*$', '', title).strip()
                        
                        json_output = {
                            "data": {
                                "schema": {
                                    "searchableFields": [
                                        "FullName", "Role", "Department", "Subdepartment", "PhoneNumber", "URL", "Origin"
                                    ],
                                    "metadataFields": [
                                        "FirstName", "LastName", "Department", "Subdepartment", "Origin"
                                    ]
                                },
                                "name": sanitize_filename(table_name),
                                "tags": ["Kontakt"],
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
                    logger.info(f"No HTML content retrieved for URL: {url}")
            else:
                logger.error(f"API response was not successful for URL: {initial_url}")
                if 'data' in response and 'metadata' in response['data']:
                    logger.error(f"Page status code: {response['data']['metadata'].get('pageStatusCode')}")
                    logger.error(f"Page error: {response['data']['metadata'].get('pageError')}")

        except Exception as e:
            logger.error(f"Error processing URL: {initial_url}")
            logger.error(f"Error details: {str(e)}")

        count += 1
        if count % 3 == 0 and count < end_index:
            logger.info("\nResting for 60 seconds to respect rate limits...")
            time.sleep(60)

def upload_existing_files(directory):
    for filename in os.listdir(directory):
        if filename.endswith('.json'):
            file_path = os.path.join(directory, filename)
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                table_name = data['data']['name']
                items = data['data']['items']
                upload_to_voiceflow(table_name, items)

# Main execution (continued)
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

            process_urls(url_data, FIRECRAWL_API_KEY, START_INDEX, UPPER_THRESHOLD, args.upload_to_voiceflow)

    except Exception as e:
        logger.error(f"An error occurred during script execution: {str(e)}")

    finally:
        logger.info("Script completed")
