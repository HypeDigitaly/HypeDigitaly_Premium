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
BASE_URL = "https://www.khk.cz"

# Constants for script name and log directory
SCRIPT_NAME = "scrape_contacts"
LOG_DIR = f"{SCRIPT_NAME}_logs"
LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")

# Create log directory if it doesn't exist
os.makedirs(LOG_DIR, exist_ok=True)

# Set up logging
# Configure root logger for basic info
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[
    logging.StreamHandler() # Default console handler
])

# Get our specific logger
logger = logging.getLogger(__name__)
# Prevent propagating to root logger to avoid duplicate messages if root also has handlers
logger.propagate = False 

# Ensure the logger's level is set (it might inherit from root otherwise)
logger.setLevel(logging.DEBUG) # Set logger level to DEBUG to capture debug messages

# Add handler for rotating file
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s')) # Added function name
file_handler.setLevel(logging.DEBUG) # File handler captures DEBUG level
logger.addHandler(file_handler)

# Add handler for console output
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
console_handler.setLevel(logging.DEBUG) # Console handler shows DEBUG level and above for this run
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
        "X-Wait-For-Selector": "#block-khk-content"
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
                    # Log the beginning of the received HTML for debugging
                    logger.debug(f"Received HTML Content (first 500 chars): {html_content[:500]}")
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

def extract_odbory_contacts(table, department_name, subdepartment_name):
    """Extracts contacts from a table specific to the Odbory structure."""
    items = []
    rows = table.find('tbody').find_all('tr')
    for row in rows:
        cells = row.find_all('td')
        if len(cells) < 5:
            continue

        name_cell = cells[0]
        role_cell = cells[2]
        office_cell = cells[3]
        phone_cell = cells[4]

        name_anchor = name_cell.find('a')
        full_name = name_anchor.get_text(strip=True) if name_anchor else name_cell.get_text(strip=True)
        email = name_anchor['href'].replace('mailto:', '') if name_anchor and name_anchor['href'].startswith('mailto:') else "N/A"

        title, first_name, last_name = split_name_with_title(full_name, "Odbor")
        role = role_cell.get_text(strip=True)
        office = office_cell.get_text(strip=True) # Keep original text including links for now

        phone_numbers = []
        phone_spans = phone_cell.find_all('span', class_='phone-number')
        for span in phone_spans:
            inner_span = span.find('span')
            if inner_span:
                phone_numbers.append(inner_span.get_text(strip=True))
        
        # Convert and join phone numbers
        cleaned_phones = [str(convert_phone_number(p)) for p in phone_numbers if convert_phone_number(p) != "N/A"]
        phone_str = ", ".join(cleaned_phones) if cleaned_phones else "N/A"


        items.append({
            "FullName": full_name,
            "Title": title,
            "FirstName": first_name,
            "LastName": last_name,
            "Role": role,
            "Department": department_name,
            "Subdepartment": subdepartment_name or "N/A", # Use N/A if no subdepartment
            "PhoneNumber": phone_str, # Store as potentially comma-separated string
            "Email": email,
            "Office": office,
            "URL": "N/A", # URL not directly available per person in this structure
            "Origin": "Odbor",
            "Category": "Kontakt",
        })
    return items

def extract_vybor_komise_members(table, committee_name, origin):
    """Extracts members from a table specific to Vybory/Komise structure."""
    items = []
    rows = table.find('tbody').find_all('tr')
    for row in rows:
        cells = row.find_all('td')
        if len(cells) < 3: # Expecting at least Index, Name, Role
            continue

        # Assuming Name is in the second cell (index 1) and Role in the third (index 2)
        full_name = cells[1].get_text(strip=True)
        role = cells[2].get_text(strip=True)
        
        # Check if role indicates chairman (předseda)
        is_chairman = "předseda" in role.lower()
        actual_role = "Předseda" if is_chairman else "Člen" # Standardize role slightly

        email_anchor = cells[1].find('a', href=lambda href: href and href.startswith('mailto:'))
        email = email_anchor['href'].replace('mailto:', '') if email_anchor else "N/A"

        title, first_name, last_name = split_name_with_title(full_name, origin)

        items.append({
            "FullName": full_name,
            "Title": title,
            "FirstName": first_name,
            "LastName": last_name,
            "Role": actual_role,
            "Department": committee_name, # Use committee name as department
            "Subdepartment": "N/A",
            "PhoneNumber": "N/A", # Phone not directly available per member
            "Email": email,
            "Office": "N/A",
            "URL": "N/A",
            "Origin": origin, # "Vybor" or "Komise"
            "Category": "Kontakt",
        })
    return items

def extract_tajemnik(table, committee_name, origin):
    """Extracts secretary (Tajemník) info from Vybory/Komise structure."""
    items = []
    rows = table.find('tbody').find_all('tr')
    for row in rows:
        cells = row.find_all('td')
        if len(cells) < 4: # Expecting Name, Department, Phone, Email
            continue

        full_name = cells[0].get_text(strip=True)
        department = cells[1].get_text(strip=True) # Department of the secretary
        phone_str = cells[2].get_text(strip=True)
        email_anchor = cells[3].find('a', href=lambda href: href and href.startswith('mailto:'))
        email = email_anchor['href'].replace('mailto:', '') if email_anchor else "N/A"

        title, first_name, last_name = split_name_with_title(full_name, origin)
        
        # Convert phone number
        cleaned_phone = str(convert_phone_number(phone_str)) if convert_phone_number(phone_str) != "N/A" else "N/A"

        items.append({
            "FullName": full_name,
            "Title": title,
            "FirstName": first_name,
            "LastName": last_name,
            "Role": "Tajemník", # Role is Secretary
            "Department": committee_name, # Associate with the committee/commission
            "Subdepartment": f"Původní odbor: {department}", # Store secretary's original dept here
            "PhoneNumber": cleaned_phone,
            "Email": email,
            "Office": "N/A",
            "URL": "N/A",
            "Origin": origin,
            "Category": "Kontakt",
        })
    return items

def extract_contacts(soup, url, title):
    items = []
    # Find the main content block based on observed structures
    main_content = soup.find('div', id='block-khk-content')
    if not main_content:
        logger.warning(f"Main content block 'div#block-khk-content' not found for URL: {url}")
        return items # Return empty list if no main content

    # --- Logic for Odbory.html ---
    # Find potential department headers (h3) and table wrappers directly within main content
    potential_headers = main_content.find_all('h3')
    potential_table_wrappers = main_content.find_all('div', class_='table-wrapper')

    logger.debug(f"Found {len(potential_headers)} potential h3 headers in main content.")
    logger.debug(f"Found {len(potential_table_wrappers)} potential table wrappers in main content.")

    # Try to associate headers with subsequent tables
    current_department_name = "Neznámý odbor" # Default department
    processed_tables = set() # Keep track of tables already processed

    for header in potential_headers:
        current_department_name = header.get_text(strip=True)
        logger.debug(f"Processing potential department header: {current_department_name}")

        # Find the next sibling table wrapper after this header
        # We need to check siblings carefully, as the structure might vary
        next_element = header.find_next_sibling()
        table_wrapper = None
        while next_element:
            if next_element.name == 'div' and 'table-wrapper' in next_element.get('class', []):
                 table_wrapper = next_element
                 break # Found the first table wrapper after the header
            if next_element.name == 'h3': # Stop if we hit the next header
                 break
            next_element = next_element.find_next_sibling()


        if table_wrapper and table_wrapper not in processed_tables:
            table = table_wrapper.find('table')
            if table:
                logger.debug(f"Found table associated with header: {current_department_name}")
                processed_tables.add(table_wrapper) # Mark as processed

                caption = table.find('caption')
                subdepartment_name = caption.get_text(strip=True) if caption else ""
                logger.debug(f"Table caption (subdepartment): '{subdepartment_name}'")

                # Check if it looks like an Odbor table (use existing criteria)
                th_elements = table.select('thead th')
                th_texts = [th.get_text(strip=True).lower() for th in th_elements]
                logger.debug(f"Table headers found: {th_texts}")

                is_odbory_table = 'příjmení, jméno, titul' in th_texts and 'činnost' in th_texts
                logger.debug(f"Checking if it's an Odbory table: {is_odbory_table}")

                if is_odbory_table:
                    logger.info(f"Parsing Odbor table for: {current_department_name} / {subdepartment_name}")
                    items.extend(extract_odbory_contacts(table, current_department_name, subdepartment_name))
                else:
                    logger.debug("Table did not match Odbory header criteria.")
            else:
                logger.debug(f"No table element found inside the table wrapper following header: {current_department_name}")
        else:
            logger.debug(f"No unprocessed table wrapper found following header: {current_department_name}")

    # Process any tables that were not preceded by an h3 (might happen at the start or if structure is unusual)
    logger.debug("Checking for any remaining table wrappers not associated with a header.")
    for wrapper in potential_table_wrappers:
        if wrapper not in processed_tables:
             # Check if this wrapper has a preceding h3 sibling (even if distant)
             # to avoid processing tables already potentially linked above
             has_preceding_h3 = False
             prev_element = wrapper.find_previous_sibling()
             while prev_element:
                 if prev_element.name == 'h3':
                     has_preceding_h3 = True
                     break
                 prev_element = prev_element.find_previous_sibling()

             if not has_preceding_h3:
                 table = wrapper.find('table')
                 if table:
                     logger.debug("Found table wrapper potentially not associated with a preceding h3.")
                     processed_tables.add(wrapper) # Mark as processed

                     caption = table.find('caption')
                     subdepartment_name = caption.get_text(strip=True) if caption else "N/A" # Default subdept if no caption
                     current_department_name = "Neznámý odbor (bez záhlaví)" # Assign a default department name

                     th_elements = table.select('thead th')
                     th_texts = [th.get_text(strip=True).lower() for th in th_elements]
                     is_odbory_table = 'příjmení, jméno, titul' in th_texts and 'činnost' in th_texts

                     if is_odbory_table:
                         logger.info(f"Parsing Odbor table (no preceding header) for: {current_department_name} / {subdepartment_name}")
                         items.extend(extract_odbory_contacts(table, current_department_name, subdepartment_name))
                     else:
                         logger.debug("Orphan table did not match Odbory header criteria.")
             else:
                 logger.debug(f"Skipping table wrapper as it has a preceding h3: {wrapper.find('table').find('caption').get_text(strip=True) if wrapper.find('table') and wrapper.find('table').find('caption') else 'Unknown Caption'}")

    # --- Logic for Vybory.html and Komise.html ---
    sections = main_content.select('article > div.node-content > div.paragraph--type--body')
    if sections:
         content_div = sections[0] # Assuming the main content is in the first such div
         current_committee_name = "Neznámý"
         origin = "UNKNOWN"

         for element in content_div.find_all(['h2', 'h3', 'div'], recursive=False):
             if element.name in ['h2', 'h3'] and element.has_attr('id'):
                 current_committee_name = element.get_text(strip=True)
                 origin = "Vybor" if element.name == 'h2' else "Komise"
                 logger.info(f"Found section: {current_committee_name} (Origin: {origin})")

             elif element.name == 'div' and 'table-wrapper' in element.get('class', []):
                 table = element.find('table')
                 if table:
                     # Distinguish between member table and secretary table
                     th_texts = [th.get_text(strip=True).lower() for th in table.select('thead th')]
                     if 'příjmení, jméno, titul' in th_texts and 'odbor' in th_texts: # Secretary table
                         logger.info(f"Parsing Tajemník table for: {current_committee_name}")
                         items.extend(extract_tajemnik(table, current_committee_name, origin))
                     elif 'předseda' in table.get_text().lower() or 'člen' in table.get_text().lower(): # Member table
                         logger.info(f"Parsing Member table for: {current_committee_name}")
                         items.extend(extract_vybor_komise_members(table, current_committee_name, origin))
                     else:
                         logger.warning(f"Unrecognized table structure found under {current_committee_name}")

    if not items:
         logger.warning(f"No contacts extracted for URL: {url} with title: {title}")

    return items

def split_name_with_title(full_name, origin="UNKNOWN"):
    original_full_name = full_name # Keep original for logging
    logger.debug(f"Splitting name: '{original_full_name}' with origin: {origin}")
    full_name = full_name.strip()
    full_name = re.sub(r'\s+', ' ', full_name) # Consolidate multiple spaces

    # Comprehensive Titles Set (consider adding more if needed)
    # Prioritize longer titles first if overlapping (e.g., Ph.D. before Dr.)
    titles_set = {
        'prof.', 'doc.', 'PhDr.', 'JUDr.', 'RNDr.', 'MUDr.', 'MVDr.', 'PaedDr.',
        'Ing.', 'Mgr.', 'Bc.', 'MgA.', 'DiS.', 'Dr.',
        'Ph.D.', 'CSc.', 'MBA', 'MPA', 'et', 'DrSc.', 'ThD.', 'ArtD.',
        # Variations without periods for robustness
        'prof', 'doc', 'PhDr', 'JUDr', 'RNDr', 'MUDr', 'MVDr.', 'PaedDr',
        'Ing', 'Mgr', 'Bc', 'MgA', 'DiS', 'Dr',
        'PhD', 'CSc', 'MBA', 'MPA', 'et', 'DrSc', 'ThD', 'ArtD'
    }
    # Add common combined titles if necessary, e.g., 'Ing. arch.'? Requires careful splitting.

    parts = full_name.split(' ')
    parts = [p for p in parts if p] # Remove empty parts

    prefix_titles = []
    suffix_titles = []
    name_core_parts = []

    # Extract Prefix Titles
    current_index = 0
    while current_index < len(parts):
        # Check for multi-word titles first? e.g., "Ing. arch." - difficult with simple set.
        # Sticking to single word titles for now.
        part_lower = parts[current_index].lower().rstrip(',') # Check lower case, strip trailing comma if any
        if part_lower in titles_set or parts[current_index] in titles_set:
            prefix_titles.append(parts[current_index])
            current_index += 1
        else:
            break # First non-title part found

    # Extract Suffix Titles (from the end backwards)
    # Need to know where name parts start relative to original 'parts' list
    start_of_potential_name = current_index
    end_index = len(parts) - 1
    while end_index >= start_of_potential_name:
        part_lower = parts[end_index].lower().rstrip(',')
        if part_lower in titles_set or parts[end_index] in titles_set:
            # Prepend to suffix_titles to maintain order
            suffix_titles.insert(0, parts[end_index])
            end_index -= 1
        else:
            break # Last non-title part found

    # The remaining parts form the core name
    name_core_parts = parts[start_of_potential_name : end_index + 1]
    name_core_string = ' '.join(name_core_parts)
    logger.debug(f"Identified Prefix Titles: {prefix_titles}")
    logger.debug(f"Identified Suffix Titles: {suffix_titles}")
    logger.debug(f"Identified Name Core String: '{name_core_string}'")


    # --- Name Component Processing ---
    first_name = ""
    last_name = ""
    order_determined_by = "default" # default, comma

    # 1. Check for comma in the core name string
    core_parts_by_comma = name_core_string.split(',', 1)
    if len(core_parts_by_comma) == 2:
        p1 = core_parts_by_comma[0].strip()
        p2 = core_parts_by_comma[1].strip()
        if p1 and p2: # Both parts must be non-empty
            logger.debug(f"Comma found splitting core name into: '{p1}' and '{p2}'")
            last_name = p1
            first_name = p2
            order_determined_by = "comma"
        else:
            logger.debug("Comma found but resulted in empty part(s), ignoring.")
            # Fall through to default space split, treat comma as part of name or remove?
            # Let's remove the comma before space splitting if it wasn't valid separator
            name_core_string = name_core_string.replace(',', ' ')
            name_core_string = re.sub(r'\s+', ' ', name_core_string).strip() # Clean up spaces again

    # 2. Default: Split by space if comma didn't determine order
    if order_determined_by == "default":
        name_words = name_core_string.split(' ')
        name_words = [w for w in name_words if w] # Clean empty strings

        if not name_words:
            logger.warning(f"No name components found after title extraction for '{original_full_name}'")
        elif len(name_words) == 1:
            # Single name component, assume LastName
            last_name = name_words[0]
            logger.debug(f"Single name word found: assigned to LastName: '{last_name}'")
        else:
            # More than one component - Default assumption: LastName FirstName
            # This seemed more consistent in fixing errors than assuming FirstName LastName
            first_name = name_words[-1]
            last_name = ' '.join(name_words[:-1])
            logger.debug(f"Multiple name words found: Assigned FirstName='{first_name}', LastName='{last_name}' (default logic)")


    # --- Title Cleanup and Combination ---
    # Remove 'et' if present, as it's conjunction, not really a title
    cleaned_prefix_titles = [t for t in prefix_titles if t.lower() != 'et']
    cleaned_suffix_titles = [t for t in suffix_titles if t.lower() != 'et']

    # Remove trailing commas/periods from individual titles before joining
    cleaned_prefix_titles = [t.rstrip('.,') for t in cleaned_prefix_titles]
    cleaned_suffix_titles = [t.rstrip('.,') for t in cleaned_suffix_titles]

    # Re-add periods for standard titles like Ing., Mgr. etc. if they were removed
    standard_abbr = {'Ing', 'Mgr', 'Bc', 'Dr', 'MUDr', 'MVDr', 'JUDr', 'RNDr', 'PhDr', 'PaedDr', 'MgA', 'DiS'}
    final_titles_list = []
    for t in cleaned_prefix_titles + cleaned_suffix_titles:
        title_core = t.rstrip('.')
        if title_core in standard_abbr and not t.endswith('.'):
             final_titles_list.append(title_core + '.')
        else:
             final_titles_list.append(t)


    full_title = ' '.join(final_titles_list).strip()
    full_title = re.sub(r'\s+', ' ', full_title) # Consolidate spaces again

    logger.debug(f"Final split result -> Title: '{full_title}', FirstName: '{first_name}', LastName: '{last_name}'")
    return full_title, first_name, last_name

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
    
    # Updated schema to include Email and Office
    payload = {
        "data": {
            "schema": {
                "searchableFields": ["FullName", "Title", "Role", "Department", "Subdepartment", "PhoneNumber", "Email", "Office", "URL", "Origin"],
                "metadataFields": ["FirstName", "LastName", "Role", "Title", "Department", "Subdepartment", "PhoneNumber", "Email", "Office", "Origin", "Category"]
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
                    # Use the original title from the input file for naming
                    input_title = entry.get('Title', 'Untitled') # Get title from the input entry
                    sanitized_input_title = sanitize_filename(input_title)
                    base_name = f"contacts_{sanitized_input_title}_table"
                    filename = f"{base_name}.json"
                    file_path = os.path.join(OUTPUT_DIRECTORY, filename)
                    
                    # Use the new base_name for the Voiceflow table name as well
                    json_output = {
                        "data": {
                            "schema": {
                                "searchableFields": [
                                    "FullName", "Title", "Role", "Department", "Subdepartment", "PhoneNumber", "Email", "Office", "URL", "Origin"
                                ],
                                "metadataFields": [
                                     "FirstName", "LastName", "Role", "Title", "Department", "Subdepartment", "PhoneNumber", "Email", "Office", "Origin", "Category"
                                ]
                            },
                            "name": base_name,
                            "items": items
                        }
                    }

                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(json_output, f, ensure_ascii=False, indent=2)

                    logger.info(f"Successfully processed URL: {url}")
                    logger.info(f"Saved to file: {file_path}")

                    if upload_to_voiceflow_flag:
                        # Upload using the new base_name
                        upload_to_voiceflow(base_name, items)
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