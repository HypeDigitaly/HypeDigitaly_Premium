import requests
import xml.etree.ElementTree as ET
import logging
import json
import anthropic
import time
import os
import argparse
from datetime import datetime, timedelta
from urllib.parse import urlparse, urljoin

# Nastavení loggeru
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# API klíče a konstanty
CLAUDE_API_KEY = "[INSERT API KEY]"
VOICEFLOW_API_KEY = "[INSERT API KEY]"
BASE_URL = "https://icuk.cz/sitemap.xml"

# Seznam kategorií
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

def initialize_payloads():
    payloads = {}
    for category in CATEGORIES:
        table_name = f"{category.lower()}_table"
        payloads[category] = {
            "data": {
                "schema": {
                    "searchableFields": ["Title", "URL"]
                },
                "name": table_name,
                "tags": [category],
                "items": []
            }
        }
    return payloads

def get_sitemap_content(url):
    logger.info(f"Získávání obsahu sitemapy z URL: {url}")
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        logger.error(f"Chyba při získávání sitemapy: {str(e)}")
        raise

def parse_sitemap(content):
    root = ET.fromstring(content)
    namespace = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    return [
        elem.find('sm:loc', namespace).text
        for elem in root.findall('sm:url', namespace) + root.findall('sm:sitemap', namespace)
    ]

def categorize_url(url):
    url_lower = url.lower()
    
    # Expanded keyword matching on the entire URL
    if any(keyword in url_lower for keyword in ['podcast', 'audio']):
        return "Podcasts"
    elif any(keyword in url_lower for keyword in ['pro-region', 'pro-firmy', 'pro-skoly', 'sluzby', 'services']):
        return "Services"
    elif any(keyword in url_lower for keyword in ['reference', 'klienti', 'clients']):
        return "References"
    elif any(keyword in url_lower for keyword in ['success-story', 'uspech', 'case-study']):
        return "SuccessStories"
    elif any(keyword in url_lower for keyword in ['udalost', 'akce', 'event']):
        return "Events"
    elif any(keyword in url_lower for keyword in ['post', 'clanek', 'article', 'blog']):
        return "Articles"
    elif any(keyword in url_lower for keyword in ['kontakt', 'contact', 'about-us', 'o-nas']):
        return "Contact"
    elif any(keyword in url_lower for keyword in ['dokument', 'document', 'pdf', 'download', 'stahnout']):
        return "Documents"
    else:
        # If no clear category, use Claude API
        return categorize_url_claude(url)

def categorize_url_claude(url):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    prompt = f"""Analyze the following complete URL from the icuk.cz website:

{url}

Based on the entire URL structure and any keywords present anywhere in the URL, categorize this URL into ONE of the following categories:

{', '.join(CATEGORIES)}

Consider these guidelines:
- URLs containing 'podcast' or audio-related terms should be "Podcasts"
- URLs about services, regions, companies, or schools should be "Services"
- URLs with 'reference' or client-related terms should be "References"
- URLs about success stories or case studies should be "SuccessStories"
- URLs related to events or activities should be "Events"
- URLs containing blog posts or articles should be "Articles"
- URLs with downloadable files or resources should be "Documents"
- URLs with contact information or about the company should be "Contact"

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
    
    category = message.content[0].text.strip()
    
    if category not in CATEGORIES:
        logger.warning(f"Claude vrátil neočekávanou kategorii: {category}. Použije se 'Uncategorized'.")
        return "Uncategorized"
    
    return category

def get_title_from_url(url):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    prompt = f"""Given the following URL:

{url}

Please generate a short, descriptive title for this page. The title should be in Czech, include diacritics, and be no more than 10 words long.

IMPORTANT:
1. Respond ONLY with the title.
2. Do not include any explanations or additional text.
3. Ensure the title is in Czech and includes proper diacritics.
"""

    message = client.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=50,
        temperature=0.7,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    title = message.content[0].text.strip()
    return title

def process_sitemap(url, payloads, processed_urls=None):
    if processed_urls is None:
        processed_urls = set()

    if url in processed_urls:
        return

    processed_urls.add(url)
    content = get_sitemap_content(url)
    urls = parse_sitemap(content)

    for child_url in urls:
        if child_url.endswith('.xml'):
            process_sitemap(child_url, payloads, processed_urls)
        else:
            category = categorize_url(child_url)
            title = get_title_from_url(child_url)
            payloads[category]["data"]["items"].append({"Title": title, "URL": child_url})
            log_processed_url(child_url, category, title)
            save_payloads_to_files(payloads)  # Update payloads after each URL

def log_processed_url(url, category, title):
    log_file = "logs/scraper_url_list.log"
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{timestamp} - URL: {url}, Category: {category}, Title: {title}\n")

def save_payloads_to_files(payloads):
    output_dir = "payloads"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    for category, payload in payloads.items():
        table_name = f"{category.lower()}_table"
        filename = f"{output_dir}/{table_name}_payload.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Aktualizován payload pro tabulku '{table_name}' v souboru: {filename}")

def upload_to_voiceflow(payloads):
    logger.info("Nahrávání dat do Voiceflow")
    url = 'https://api.voiceflow.com/v1/knowledge-base/docs/upload/table?overwrite=true'
    headers = {
        'Authorization': VOICEFLOW_API_KEY,
        'accept': 'application/json',
        'content-type': 'application/json'
    }
    
    for category, payload in payloads.items():
        if payload["data"]["items"]:  # Only upload if there are items in this category
            table_name = payload["data"]["name"]
            response = requests.post(url, headers=headers, json=payload)
            
            if response.status_code == 200:
                logger.info(f"Úspěšně nahráno {len(payload['data']['items'])} položek pro tabulku '{table_name}'")
            else:
                logger.error(f"Chyba při nahrávání tabulky '{table_name}': {response.text}")

def main():
    logger.info(f"Začátek zpracování sitemapy: {BASE_URL}")
    
    try:
        payloads = initialize_payloads()
        process_sitemap(BASE_URL, payloads)
        
        logger.info("Zpracování sitemapy dokončeno. Nahrávání dat do Voiceflow.")
        upload_to_voiceflow(payloads)
    
    except Exception as e:
        logger.error(f"Došlo k chybě při zpracování: {str(e)}", exc_info=True)
        return

    logger.info("Zpracování a nahrávání dokončeno")

if __name__ == "__main__":
    main()