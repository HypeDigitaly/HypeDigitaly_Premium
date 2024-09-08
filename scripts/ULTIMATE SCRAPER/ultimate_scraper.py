import requests
import xml.etree.ElementTree as ET
import logging
import json
import anthropic
import time
import os
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import argparse

# Script configuration
SITEMAP_URL = ""  # URL of the XML sitemap
URL_LIST_FILE = "URL_List.txt"  # Path to the text file containing the list of URLs
LANGUAGE = "Čeština"  # Set the desired language for Claude API responses

# Constants for file names and directories
SCRIPT_NAME = "ultimate_scraper"
LOG_DIR = f"{SCRIPT_NAME}_logs"
LAST_RUN_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_last_run_time.txt")
LOG_FILE = os.path.join(LOG_DIR, f"{SCRIPT_NAME}_detailed.log")
PAYLOADS_DIR = "payloads"

# Create directories if they don't exist
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(PAYLOADS_DIR, exist_ok=True)

# Set up logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler(LOG_FILE, encoding='utf-8'),
                        logging.StreamHandler()
                    ])
logger = logging.getLogger(__name__)

# API keys and constants
CLAUDE_API_KEY = "REMOVED-ANTHROPIC-KEY"
JINA_AI_API_KEY = "REMOVED-JINA-KEY"
VOICEFLOW_API_KEY = "REMOVED-VOICEFLOW-KEY"

# TRUE = UPLOAD ONLY UPDATES, NOT EVERYTHING FROM THE BEGINNING
CHECK_MODIFIED_DATE = True

# LIST OF CATEGORIES / TAGS
# [THERE WILL BE CREATED THE SAME NUMBER OF FILES AS ARE THESE CATEGORIES -> AND WE WILL USE THEM AS TAGS AS WELL]
CATEGORIES = [
    "Services",
    "References",
    "SuccessStories",
    "Events",
    "Podcasts",
    "Articles",
    "Documents",
    "Contact"
]

# forced delay in seconds between each processed URL
URL_PROCESSING_DELAY = 30  # DELAY IN SECONDS

def get_last_run_time():
    if os.path.exists(LAST_RUN_FILE):
        with open(LAST_RUN_FILE, 'r') as f:
            return datetime.fromisoformat(f.read().strip())
    return datetime.min.replace(tzinfo=timezone.utc)

def save_current_run_time():
    with open(LAST_RUN_FILE, 'w') as f:
        f.write(datetime.now(timezone.utc).isoformat())

def is_url_modified(lastmod):
    if not CHECK_MODIFIED_DATE:
        return True
    
    if not lastmod:
        return True
    
    last_run_time = get_last_run_time()
    
    try:
        lastmod_date = datetime.fromisoformat(lastmod.rstrip('Z')).replace(tzinfo=timezone.utc)
        return lastmod_date > last_run_time
    except ValueError:
        logger.error(f"Invalid last modification date format: {lastmod}")
        return True

def initialize_payloads():
    payloads = {}
    for category in CATEGORIES:
        table_name = f"{category.lower()}_table"
        payloads[category] = {
            "urls": {
                "data": {
                    "schema": {
                        "searchableFields": ["Title", "URL"]
                    },
                    "name": f"{table_name}_urls",
                    "tags": [category],
                    "items": []
                }
            },
            "content": {
                "data": {
                    "schema": {
                        "searchableFields": ["Question", "Answer"]
                    },
                    "name": f"{table_name}_content",
                    "tags": [category],
                    "items": []
                }
            }
        }
    return payloads

def get_sitemap_content(url):
    logger.info(f"Fetching sitemap content from URL: {url}")
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        logger.error(f"Error fetching sitemap: {str(e)}")
        raise

def parse_sitemap(content):
    root = ET.fromstring(content)
    namespace = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    return [
        {
            'url': elem.find('sm:loc', namespace).text,
            'lastmod': elem.find('sm:lastmod', namespace).text if elem.find('sm:lastmod', namespace) is not None else None
        }
        for elem in root.findall('sm:url', namespace) + root.findall('sm:sitemap', namespace)
    ]

def categorize_url(url):
    url_lower = url.lower()
    return categorize_url_claude(url)

def log_claude_response(response, context):
    logger.debug(f"Claude API Response for {context}:")
    logger.debug(f"Response content: {response.content}")
    logger.debug(f"Response model: {response.model}")
    logger.debug(f"Response role: {response.role}")
    logger.debug(f"Response type: {response.type}")
    logger.debug(f"Response usage: {response.usage}")

def categorize_url_claude(url):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    prompt = f"""Analyze the following complete URL:

{url}

Based on the entire URL structure and any keywords present anywhere in the URL, categorize this URL into ONE of the following categories:

{', '.join(CATEGORIES)}

Look at the entire URL, including the domain, path, and any query parameters. If you're unsure, choose the most likely category based on the complete URL structure.

RESPOND ONLY with the category name, nothing else.
"""

    message = client.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=50,
        temperature=0,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    log_claude_response(message, f"categorize_url_claude for {url}")
    
    category = message.content[0].text.strip()
    
    if category not in CATEGORIES:
        logger.warning(f"Claude returned an unexpected category: {category}. Using 'Uncategorized'.")
        return "Uncategorized"
    
    return category

def get_title_from_url(url):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    prompt = f"""URL: {url}

TASK:
Create a grammatically correct page title in {LANGUAGE} based on the structure of the given URL.

RULES:
1. Use only information from the URL path (exclude protocol and domain).
2. Maintain the exact number and order of words from the URL.
3. Replace hyphens with spaces.
4. Capitalize each word appropriately, according to {LANGUAGE} grammar rules.
5. Do not change or add any extra words.
6. The length of the title must be exactly the same as the number of words in the URL path.
7. Ensure the resulting text is grammatically correct and makes sense in {LANGUAGE}, like a native speaker born in the corresponding country would write it.

RESPONSE FORMAT:
Respond only with the resulting title without any additional information or explanation.

OUTPUT:

# Response Language: You must absolutely respond only in the following language: {LANGUAGE}"""

    message = client.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=50,
        temperature=0,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    title = message.content[0].text.strip()
    return title

def get_html_content(url):
    logger.info(f"Fetching HTML content from URL: {url}")
    api_url = f'https://r.jina.ai/{url}'
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {JINA_AI_API_KEY}",
        "X-Return-Format": "markdown"
    }
    
    try:
        response = requests.get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if data['status'] == 20000 and 'data' in data:
            content = data['data'].get('content', '')
            title = data['data'].get('title', '')
            metadata = {
                'title': title,
                'url': data['data'].get('url', url)
            }
            return content, metadata
        else:
            error_message = data.get('status', 'Unknown error')
            logger.error(f"Error fetching content: {error_message}")
            raise ValueError(f"API Error: {error_message}")
    except requests.RequestException as e:
        logger.error(f"Error calling Jina AI API: {str(e)}")
        raise

def convert_to_qa(content, title):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    system_prompt = f"""You are an expert in information extraction and JSON creation. Your task is to analyze the provided content and create structured question-answer pairs in JSON format. All output must be in English and directly related to the topic '{title}'."""
    
    user_prompt = f"""
# YOUR ONLY TASK: Create a JSON array with question-answer pairs from the provided content. All output must be in "{LANGUAGE}" language and must directly relate to the topic "{title}".

# EXPECTED JSON FORMAT OF YOUR RESPONSE:
{{
  "qa_pairs": [
    {{
      "Question": "Question 1 ? | Alternative 1? | Alternative 2?",
      "Answer": "Comprehensive factual answer."
    }},
    {{
      "Question": "Question 2 ? | Alternative 1? | Alternative 2?",
      "Answer": "Comprehensive factual answer."
    }},
    ...
  ]
}}

## RULES:
1. Create at least 5 Q&A pairs, ideally more, until you exhaust all relevant and factual content.
2. Each question must have 3 formulations separated by |.
3. Focus on HIGHLY SPECIFIC data: numbers, amounts, dates, names.
4. Questions and answers must be SPECIFIC and DIRECTLY related to "{title}".
5. Extract ALL relevant key information on the topic.
6. CRITICALLY IMPORTANT: Include all relevant URL links DIRECTLY in the answers, EXACTLY in the format they appear in the source Markdown content.
7. Omit irrelevant information (headers, footers, GDPR, cookies, etc.) and other information not explicitly directly related to "{title}".
8. Ensure 100% valid JSON.
9. Do not use nested structures in answers.

## FORMATTING:
- Provide only the bare JSON array.
- No quotes around the entire array.
- No additional text outside of JSON.
- Properly escape quotes.

# CONTENT TO PROCESS:
---
{content}
---

# IMPORTANT: Output = clean JSON array. Extract as MANY AS POSSIBLE AND SPECIFIC, FACTUAL Q&A pairs (min. 5) covering all relevant information for "{title}".

# Response Language: You must absolutely respond and formulate your entire response solely in the following language: "{LANGUAGE}"
"""

    logger.debug(f"Claude API Q&A Prompt: {user_prompt[:500]}...")
    
    response = call_anthropic_api_with_retry(client, system_prompt, user_prompt)
    response_text = response.content[0].text.strip()
    logger.debug(f"Raw Claude API Q&A Response: {response_text}")
    
    try:
        # Attempt to find and extract the JSON part from the response
        json_start = response_text.find('{')
        json_end = response_text.rfind('}') + 1
        if json_start != -1 and json_end != -1:
            json_str = response_text[json_start:json_end]
            qa_data = json.loads(json_str)
            qa_pairs = qa_data.get('qa_pairs', [])
        else:
            raise ValueError("Cannot find valid JSON in the response")

        if not isinstance(qa_pairs, list) or len(qa_pairs) < 5:
            logger.error(f"Invalid Q&A pairs structure or insufficient number of pairs for {title}")
            logger.error(f"Parsed Q&A pairs: {qa_pairs}")
            raise ValueError("Invalid Q&A pairs structure or insufficient number of pairs")
        return qa_pairs
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing JSON response for {title}: {str(e)}")
        logger.error(f"Raw response causing error: {response_text}")
        raise

def call_anthropic_api_with_retry(client, system_prompt, user_prompt, max_retries=3, retry_delay=5):
    for attempt in range(max_retries):
        try:
            message = client.messages.create(
                model="claude-3-5-sonnet-20240620",
                max_tokens=4000,
                temperature=0,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": user_prompt}
                ]
            )
            return message
        except Exception as e:
            logger.error(f"Error calling Claude API (attempt {attempt + 1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"Waiting {retry_delay} seconds before next attempt...")
                time.sleep(retry_delay)
    logger.error(f"Maximum number of attempts ({max_retries}) reached when calling Claude API.")
    raise Exception("Maximum number of attempts reached when calling Claude API")

def save_payload_to_file(category, payload_type, data):
    table_name = f"{category.lower()}_{payload_type}"
    filename = os.path.join(PAYLOADS_DIR, f"{table_name}_payload.json")
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved payload for '{table_name}' to file: {filename}")

def upload_to_voiceflow_single(category, payload_type, data):
    logger.info(f"Uploading data to Voiceflow for '{category}_{payload_type}'")
    url = 'https://api.voiceflow.com/v1/knowledge-base/docs/upload/table?overwrite=true'
    headers = {
        'Authorization': VOICEFLOW_API_KEY,
        'accept': 'application/json',
        'content-type': 'application/json'
    }
    
    response = requests.post(url, headers=headers, json=data)
    
    if response.status_code == 200:
        logger.info(f"Successfully uploaded {len(data['data']['items'])} items for '{category}_{payload_type}'")
    else:
        logger.error(f"Error uploading for '{category}_{payload_type}': {response.text}")

def save_qa_payload_to_file(title, data):
    filename = os.path.join(PAYLOADS_DIR, f"{title}_payload.json")
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved Q/A payload for '{title}' to file: {filename}")

def upload_qa_to_voiceflow(title, data):
    logger.info(f"Uploading Q/A data to Voiceflow for '{title}'")
    url = 'https://api.voiceflow.com/v1/knowledge-base/docs/upload/table?overwrite=true'
    headers = {
        'Authorization': VOICEFLOW_API_KEY,
        'accept': 'application/json',
        'content-type': 'application/json'
    }
    
    response = requests.post(url, headers=headers, json=data)
    
    if response.status_code == 200:
        logger.info(f"Successfully uploaded {len(data['data']['items'])} Q/A items for '{title}'")
    else:
        logger.error(f"Error uploading Q/A for '{title}': {response.text}")

def process_sitemap_unified(url, payloads, processed_urls=None):
    if processed_urls is None:
        processed_urls = set()

    if url in processed_urls:
        return

    processed_urls.add(url)
    logger.info(f"Processing sitemap: {url}")
    content = get_sitemap_content(url)
    urls = parse_sitemap(content)

    for item in urls:
        child_url = item['url']
        lastmod = item['lastmod']
        
        if child_url.endswith('.xml'):
            process_sitemap_unified(child_url, payloads, processed_urls)
        else:
            process_single_url(child_url, lastmod, payloads)

    # Upload URL data to Voiceflow after processing each sub-sitemap
    for category, payload in payloads.items():
        upload_to_voiceflow_single(category, "urls", payload["urls"])

def process_single_url(url, lastmod, payloads):
    logger.info(f"Processing URL: {url}")
    category = categorize_url(url)
    title = get_title_from_url(url)
    
    # Process for categories
    payloads[category]["urls"]["data"]["items"].append({"Title": title, "URL": url})
    save_payload_to_file(category, "urls", payloads[category]["urls"])
    
    # Process content
    if is_url_modified(lastmod):
        try:
            content, metadata = get_html_content(url)
            qa_pairs = convert_to_qa(content, title)
            
            qa_payload = {
                "data": {
                    "schema": {
                        "searchableFields": ["Question", "Answer"]
                    },
                    "name": title,
                    "tags": [category],
                    "items": qa_pairs
                }
            }
            save_qa_payload_to_file(title, qa_payload)
            upload_qa_to_voiceflow(title, qa_payload)
            
        except Exception as e:
            logger.error(f"Error processing content for URL {url}: {str(e)}", exc_info=True)
    else:
        logger.info(f"Skipping URL (not modified): {url}")
    
    # Add delay after processing URL
    time.sleep(URL_PROCESSING_DELAY)

def clear_log_file():
    with open(LOG_FILE, 'w', encoding='utf-8'):
        pass  # Clears the content of the file

def main():
    clear_log_file()  # Clear log file before each run
    logger.info(f"Starting processing: {datetime.now()}")
    logger.info(f"Last script run: {get_last_run_time()}")
    
    payloads = initialize_payloads()
    
    if SITEMAP_URL:
        logger.info(f"Processing sitemap: {SITEMAP_URL}")
        try:
            process_sitemap_unified(SITEMAP_URL, payloads)
        except Exception as e:
            logger.error(f"Error processing sitemap: {str(e)}", exc_info=True)
    elif URL_LIST_FILE:
        logger.info(f"Processing URL list from file: {URL_LIST_FILE}")
        try:
            with open(URL_LIST_FILE, 'r') as file:
                for line in file:
                    url = line.strip()
                    if url:
                        process_single_url(url, None, payloads)
            
            # Upload URL data to Voiceflow after processing all URLs
            for category, payload in payloads.items():
                upload_to_voiceflow_single(category, "urls", payload["urls"])
        except Exception as e:
            logger.error(f"Error processing URL list: {str(e)}", exc_info=True)
    else:
        logger.error("No sitemap URL or URL list file provided. Please set either SITEMAP_URL or URL_LIST_FILE.")
        return

    save_current_run_time()
    logger.info(f"Processing completed: {datetime.now()}")

if __name__ == "__main__":
    main()
