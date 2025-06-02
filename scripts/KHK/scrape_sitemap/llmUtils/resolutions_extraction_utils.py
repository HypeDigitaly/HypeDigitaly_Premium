import json
import logging
from .llm_utils import call_llm_api

# Get logger for this module
logger = logging.getLogger(__name__)

def extract_resolution_details_llm(content, page_title, category, source_url, llm_providers, llm_sequence, max_retries=3, initial_retry_delay=5, api_call_delay=10, default_rok=None, default_datum_konani=None):
    """
    Extracts structured resolution information from text using an LLM call.
    
    Args:
        content (str): The text content to analyze for resolutions
        page_title (str): Title of the page being processed
        category (str): Category of the content
        source_url (str): URL of the source page
        llm_providers (dict): Dictionary of LLM provider configurations
        llm_sequence (str): Comma-separated sequence of LLM provider IDs
        max_retries (int): Maximum number of retry attempts
        initial_retry_delay (int): Initial delay for retries
        api_call_delay (int): Delay between API calls
        default_rok (int, optional): Default year if not found in text
        default_datum_konani (int, optional): Default meeting date if not found in text
        
    Returns:
        list: List of resolution dictionaries or None if extraction fails
    """
    resolution_tool = [
        {
            "name": "extract_resolution_data",
            "description": "Extracts detailed data about resolutions (usnesení) from the provided text and formats it as a valid JSON object.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "resolutions": {
                        "type": "array",
                        "description": "An array of resolution objects extracted from the text. MUST be present.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "BodUsneseni": {"type": "string", "description": "The explicit agenda item number (e.g., 'Bod 1', '1.', 'Položka X') IF AND ONLY IF it is clearly labeled as such in the provided text. If no such explicit label and number are found IN THE TEXT, this field MUST be empty or null."},
                                "Popis": {"type": "string", "description": "Description of the resolution (text or summary of the resolution point)."},
                                "CisloUsneseni": {"type": "string", "description": "The official resolution identifier (e.g., ZK/123/2024, RK/45/2024, Usnesení č. XYZ). This is NOT a voting number (č.hl.) or an agenda item number (BodUsneseni). If no such official resolution identifier is found IN THE TEXT, this field MUST be empty or null."},
                                "DatumKonani": {"type": "integer", "description": "Date of the meeting when the resolution was passed. MUST be in YYYYMMDD format as an integer (e.g., 20241104)."},
                                "Rok": {"type": "integer", "description": "Year of the resolution. MUST be in YYYY format as an integer (e.g., 2024). Attempt to derive from DatumKonani if not explicit."},
                                "Prilohy": {"type": "string", "description": "Links to attachments (PDF, DOC, ZIP, etc.) related to the resolution. Include ALL URLs, comma-separated if multiple. ALL links MUST be in Markdown format [text](URL). If only a URL is found, convert it to Markdown (e.g., [filename.pdf](URL) or [Příloha](URL))."},
                                "Pro": {"type": "string", "description": "Number of votes FOR the resolution, if available."},
                                "Proti": {"type": "string", "description": "Number of votes AGAINST the resolution, if available."},
                                "ZdrzelSe": {"type": "string", "description": "Number of abstentions, if available."},
                                "SubCategory": {
                                    "type": "string",
                                    "description": "The sub-category of the resolution. MUST be one of: 'RK' (Rada kraje), 'ZK' (Zastupitelstvo kraje), 'KK' (Komise kraje), 'VK' (Výbory kraje), 'RM' (Rada města), 'ZM' (Zastupitelstvo města), 'KM' (Komise města), 'VM' (Výbory města). Determine based on context like 'Rada Královéhradeckého kraje', 'Zastupitelstvo města Hradec Králové', etc. If the specific type (Rada, Zastupitelstvo, Komise, Výbor) and level (kraje, města) is clear, use the corresponding code. Prioritize explicit mentions.",
                                    "enum": ["RK", "ZK", "KK", "VK", "RM", "ZM", "KM", "VM"]
                                }
                            },
                            "required": ["Popis", "CisloUsneseni", "SubCategory"]
                        }
                    }
                },
                "required": ["resolutions"]
            }
        }
    ]
    
    tool_choice = {"type": "tool", "name": "extract_resolution_data"}

    system_prompt = f"""# Vaše Role: Jste AI asistent specializovaný na extrakci detailních informací o usneseních rady nebo zastupitelstva z textu.
Váš úkol je identifikovat VŠECHNA usnesení zmíněná v textu a pro KAŽDÉ z nich extrahovat co nejvíce údajů pomocí nástroje `extract_resolution_data`.

## Kontext stránky: '{page_title}' (URL: {source_url})
## Kategorie stránky: '{category}'

## 🔗 KRITICKÉ PRAVIDLO - ZACHOVÁNÍ VŠECH ODKAZŮ (PŘÍLOH) 🔗
**NEJVYŠŠÍ PRIORITA: Identifikujte a zachovejte VŠECHNY odkazy na dokumenty (PDF, DOC, ZIP atd.), které slouží jako přílohy k usnesením! Všechny odkazy v poli `Prilohy` MUSÍ být ve formátu Markdown `[text odkazu](URL)`**

### EXTRAKCE A ZACHOVÁNÍ PŘÍLOH:
1.  **POVINNÉ:** V poli `Prilohy` uveďte VŠECHNY URL odkazy na soubory, které jsou přílohami nebo souvisejícími dokumenty k danému usnesení.
2.  **FORMÁT ODKAZŮ (POVINNÝ MARKDOWN):**
    *   Každý odkaz na přílohu **MUSÍ** být převeden do formátu Markdown: `[Název souboru nebo relevantní text](URL_k_priloze.pdf)`.
    *   Pokud text obsahuje již Markdown odkaz (např. `[Text odkazu](URL_k_priloze.pdf)`), zachovejte PŘESNĚ tento formát.
    *   Pokud text obsahuje prosté URL (např. `https://example.com/priloha.zip`), **MUSÍTE** jej převést na Markdown. Jako text odkazu (`[text]`) použijte název souboru z URL (např. `priloha.zip` -> `[priloha.zip](https://example.com/priloha.zip)`), nebo pokud název souboru není zřejmý, použijte obecný text jako `[Příloha](URL)` nebo `[Dokument](URL)`.
    *   Pokud je více příloh, oddělte jednotlivé Markdown odkazy čárkou. Např.: `[Příloha 1](url1.pdf), [Dokument Alfa](https://server.cz/priloha2.doc)`
3.  **ŽÁDNÁ ZTRÁTA:** Ztráta jakéhokoli odkazu na přílohu je NEPŘÍPUSTNÁ. Každý musí být přítomen a ve formátu Markdown.

## Klíčové Instrukce pro Extrakci:
1.  **Identifikace Usnesení:** Najděte všechny zmínky o konkrétních usneseních. Hledejte čísla usnesení (např. ZK/xx/YYYY, RK/yy/YYYY, Usnesení č. X), data jednání, popisy bodů programu.
2.  **Kompletní Data:** Pro každé usnesení se snažte vyplnit VŠECHNA pole definovaná ve schématu nástroje: `BodUsneseni`, `Popis`, `CisloUsneseni`, `DatumKonani`, `Rok`, `Prilohy`, `Pro`, `Proti`, `ZdrzelSe`, `SubCategory`.
    *   `BodUsneseni`: **Vyplňte POUZE POKUD** text explicitně uvádí číslo bodu jednání, pořadové číslo položky, nebo označení jako 'Bod X', 'X.', 'Položka X', apod., které je jasně identifikovatelné jako číslo pořadí agendy.** Nehledaťe toto číslo v názvech odkazov alebo číslach hlasovania. **Pokud takové explicitní označení bodu v textu NENÍ, MUSÍ toto pole zůstat PRÁZDNÉ nebo null.** Nevymýšlejte si ho.
    *   `Popis`: Textový popis nebo název bodu usnesení.
    *   `CisloUsneseni`: **Vyplňte POUZE POKUD** text explicitně uvádí oficiální číslo/identifikátor usnesení (např. ZK/123/2024, RK/45/2024, Usnesení č. XYZ).** Toto NENÍ číslo hlasování (č.hl.) ani číslo bodu jednání (to patří do `BodUsneseni`). **Pokud takový explicitní identifikátor usnesení v textu NENÍ, MUSÍ toto pole zůstat PRÁZDNÉ nebo null.**
    *   `DatumKonani`: Datum, kdy bylo usnesení přijato. **Pokud je to možné, uveďte hodnotu jako souvislý řetězec ve formátu YYYYMMDD (např. '20241104').** Pokud to není možné, uveďte datum tak, jak je v textu.
    *   `Rok`: Rok přijetí usnesení. **Pokud je to možné, uveďte hodnotu jako čtyřmístný řetězec YYYY (např. '2024').** Často odvoditelné z `DatumKonani`.
    *   `Prilohy`: **VŠECHNY odkazy na přílohy (viz kritické pravidlo výše) ve formátu Markdown.**
    *   `Pro`, `Proti`, `ZdrzelSe`: Počty hlasů, pokud jsou uvedeny.
    *   `SubCategory`: **MUSÍ být jedna z hodnot: 'RK', 'ZK', 'KK', 'VK', 'RM', 'ZM', 'KM', 'VM'.** Určete na základě kontextu, např. "Rada Královéhradeckého kraje" -> "RK", "Zastupitelstvo města Hradec Králové" -> "ZM". Pokud je z textu jasný typ orgánu (Rada, Zastupitelstvo, Komise, Výbor) a úroveň (kraje, města), použijte odpovídající kód. Hledejte explicitní zmínky jako "Usnesení Rady kraje...", "Program jednání Zastupitelstva města...", "Zápis z Komise...", atd. Například, pokud `CisloUsneseni` začíná na 'RK/', použijte 'RK'. Pokud začíná na 'ZK/', použijte 'ZK'.
3.  **Přesnost a Verbatim:** Extrahujte informace PŘESNĚ tak, jak jsou uvedeny. U pole `Prilohy` dbejte na zachování formátu odkazu.
4.  **Více Usnesení:** Pokud text (nebo řádek tabulky) obsahuje informace o více usneseních, vytvořte samostatný objekt v poli "resolutions" pro KAŽDÉ usnesení.
5.  **Chybějící Informace:** Pokud některý údaj chybí (zejména `BodUsneseni` pokud není explicitní), ponechte pole prázdné (null/vynechat) nebo jako prázdný řetězec "". `Popis`, `CisloUsneseni` a `SubCategory` jsou však vysoce preferovaná.
6.  **KROK 0 (Uvozovky):** PŘED extrakcí nahraďte VŠECHNY typy uvozovek v "ZDROJOVÝ TEXT K ANALÝZE" JEDNÍM standardním jednoduchým apostrofem (`'`). Toto je kritické pro validní JSON.

## Příklad (pro řádek tabulky nebo textový fragment):
Text: "1. Stanovení počtu členů, č. ZK/1/1/2024, datum 04.11.2024. Pro: 26, Proti: 15, Zdržel se: 3. Přílohy: [Zápis](zapis.pdf), [Podklady](podklady.zip)"
Očekávaný JSON objekt (PO aplikaci Kroku 0):
```json
{{
  "resolutions": [
    {{
      "BodUsneseni": "1.",
      "Popis": "Stanovení počtu členů",
      "CisloUsneseni": "ZK/1/1/2024",
      "DatumKonani": "20241104",
      "Rok": "2024",
      "Prilohy": "[Zápis](zapis.pdf), [Podklady](podklady.zip)",
      "Pro": "26",
      "Proti": "15",
      "ZdrzelSe": "3",
      "SubCategory": "ZK"
    }}
  ]
}}
```
Jiný příklad textu: "Schválení rozpočtu RK/55/2023, Příloha: rozpoctova_tabulka.xlsx"
Očekávaný JSON objekt (PO aplikaci Kroku 0):
```json
{{
  "resolutions": [
    {{
      "BodUsneseni": "",
      "Popis": "Schválení rozpočtu",
      "CisloUsneseni": "RK/55/2023",
      "DatumKonani": "",
      "Rok": "2023",
      "Prilohy": "[rozpoctova_tabulka.xlsx](rozpoctova_tabulka.xlsx)",
      "Pro": "",
      "Proti": "",
      "ZdrzelSe": "",
      "SubCategory": "RK"
    }}
  ]
}}
```
Analyzujte následující text a extrahujte všechna data o usneseních VČETNĚ VŠECH PŘÍLOH A JEJICH ODKAZŮ **VE FORMÁTU MARKDOWN**."""

    user_prompt = f"""# ZDROJOVÝ TEXT K ANALÝZE (může být celý textový chunk nebo jeden řádek z tabulky):
```
{content}
```

# TVŮJ AKTUÁLNÍ ÚKOL: Použij nástroj `extract_resolution_data` pro extrakci informací o usneseních z výše uvedeného textu.

## 🔗 PRIORITA #1: EXTRAKCE VŠECH PŘÍLOH (ODKAZŮ) VE FORMÁTU MARKDOWN
**KRITICKÉ:** Identifikuj VŠECHNY odkazy na dokumenty (PDF, DOCX, XLSX, ZIP atd.) v textu. Každý takový odkaz **MUSÍ** být v poli `Prilohy` uveden ve formátu Markdown `[text odkazu](URL)`. Pokud je v textu jen holé URL, vytvořte vhodný `text odkazu` (např. název souboru). Žádný odkaz na přílohu nesmí být ztracen a všechny musí být v Markdown.

## DALŠÍ POŽADAVKY:
Vytvoř pole "resolutions" se záznamem pro každé identifikované usnesení. Důrazně dodržuj formát a KROK 0 (uvozovky) ze systémových instrukcí.
Výstup nesmí obsahovat žádný text mimo samotný JSON objekt.
"""

    messages = [{"role": "user", "content": user_prompt}]

    try:
        response_data = call_llm_api(
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=2048,  # Max tokens for resolutions, might be less data per item than contacts
            temperature=0.0,
            tools=resolution_tool,
            tool_choice=tool_choice,
            max_retries=max_retries,
            initial_retry_delay=initial_retry_delay,
            api_call_delay=api_call_delay,
            llm_providers=llm_providers,
            llm_sequence=llm_sequence
        )

        if response_data is None:
            logger.error(f"Nepodařilo se získat data o usneseních pro '{page_title}' (API volání selhalo nebo nevrátilo data).")
            return None

        # Handle tool response for resolutions
        if isinstance(response_data, dict) and 'resolutions' in response_data:
            resolutions = response_data['resolutions']
            
            if isinstance(resolutions, list):
                validated_resolutions = []
                for resolution in resolutions:
                    if isinstance(resolution, dict):
                        # Validate required fields
                        if not resolution.get('Popis') or not resolution.get('SubCategory'):
                            logger.warning(f"Skipping invalid resolution (missing required fields): {resolution}")
                            continue
                        
                        # Helper function to convert date/year values to integers
                        def safe_int_convert(value, field_name):
                            if value is None:
                                return None
                            if isinstance(value, int):
                                return value
                            if isinstance(value, str):
                                if value.strip() == '':
                                    return None
                                try:
                                    return int(value.strip())
                                except ValueError:
                                    logger.warning(f"Could not convert {field_name} '{value}' to integer")
                                    return None
                            return None
                        
                        # Convert and validate date/year fields
                        datum_konani = safe_int_convert(resolution.get('DatumKonani'), 'DatumKonani')
                        rok = safe_int_convert(resolution.get('Rok'), 'Rok')
                        
                        # Use defaults if values are None
                        if datum_konani is None and default_datum_konani is not None:
                            datum_konani = default_datum_konani
                        if rok is None and default_rok is not None:
                            rok = default_rok
                            
                        # Ensure all fields are present with proper types
                        validated_resolution = {
                            "BodUsneseni": resolution.get('BodUsneseni', ''),
                            "Popis": resolution.get('Popis', ''),
                            "CisloUsneseni": resolution.get('CisloUsneseni', ''),
                            "DatumKonani": datum_konani,
                            "Rok": rok,
                            "Prilohy": resolution.get('Prilohy', ''),
                            "Pro": resolution.get('Pro', ''),
                            "Proti": resolution.get('Proti', ''),
                            "ZdrzelSe": resolution.get('ZdrzelSe', ''),
                            "SubCategory": resolution.get('SubCategory', ''),
                            "Category": category,  # Add the main category
                            "Type": "Data",       # Add static Type field
                            "URL": source_url     # Add source URL
                        }
                        validated_resolutions.append(validated_resolution)
                    else:
                        logger.warning(f"Skipping non-dictionary resolution item: {resolution}")
                
                if validated_resolutions:
                    logger.info(f"Successfully extracted {len(validated_resolutions)} resolution(s) from '{page_title}'")
                    return validated_resolutions
                else:
                    logger.warning(f"No valid resolutions found in response for '{page_title}'")
                    return None
            else:
                logger.error(f"Response 'resolutions' field is not a list for '{page_title}': {type(resolutions)}")
                return None
        else:
            logger.error(f"Invalid response structure for resolution extraction from '{page_title}': {response_data}")
            return None

    except Exception as e:
        logger.error(f"Error during resolution extraction for '{page_title}': {str(e)}")
        return None

def save_resolutions_payload(url, resolution_items, category):
    """
    Saves extracted resolution items to a JSON file for Voiceflow.
    Filename: {category}_{url_based_name}_resolutions_table.json
    
    Args:
        url (str): The source URL
        resolution_items (list): List of resolution dictionaries
        category (str): The category of the content
        
    Returns:
        str: Filename if successful, None if failed
    """
    import os
    import re
    import unicodedata
    from urllib.parse import urlparse
    
    def remove_accents(input_str):
        nfkd_form = unicodedata.normalize('NFKD', input_str)
        return ''.join([c for c in nfkd_form if not unicodedata.combining(c)])
    
    output_dir = "payloads"
    os.makedirs(output_dir, exist_ok=True)

    parsed_url = urlparse(url)
    url_path = parsed_url.path.strip('/')
    if not url_path:
        url_path = 'home'
    
    url_based_name = url_path.replace('/', '_')
    url_based_name = remove_accents(url_based_name)
    url_based_name = re.sub(r"[<>:\"/\\|?*]", "_", url_based_name)
    url_based_name = re.sub(r"\s+", "_", url_based_name)
    url_based_name = re.sub(r"_+", "_", url_based_name)
    url_based_name = url_based_name.strip("_").lower()
    url_based_name = url_based_name[:200]  # Max filename length

    # Use the original category for filename uniqueness, but data category will be 'Usneseni'
    filename_category_prefix = category.lower()
    table_name = f"{filename_category_prefix}_{url_based_name}_resolutions_table"
    filename = f"{output_dir}/{table_name}.json"

    # Ensure all required fields for the schema are present in items
    expected_fields = ["BodUsneseni", "Popis", "CisloUsneseni", "DatumKonani", "Rok", 
                       "Prilohy", "Pro", "Proti", "ZdrzelSe", "SubCategory"]

    processed_items = []
    for item in resolution_items:
        if not isinstance(item, dict):
            logger.warning(f"Skipping non-dictionary item in resolution_items for {url}: {item}")
            continue
        
        new_item = {}
        for field in expected_fields:
            new_item[field] = item.get(field, "")  # Default to empty string if missing
        new_item["Category"] = item.get("Category", "")  # Preserve original category from the item
        new_item["Type"] = "Data"  # Add static Type field
        new_item["URL"] = url      # Add source URL
        processed_items.append(new_item)

    payload = {
        "data": {
            "schema": {
                "searchableFields": [
                    "Popis",
                    "CisloUsneseni"
                ],
                "metadataFields": [
                    "BodUsneseni",
                    "DatumKonani",
                    "Rok",
                    "Prilohy",
                    "Pro",
                    "Proti",
                    "ZdrzelSe",
                    "SubCategory",
                    "URL",
                    "Category",
                    "Type"
                ]
            },
            "name": table_name,
            "items": processed_items
        }
    }

    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved resolutions payload for URL '{url}' ({len(processed_items)} items) to file: {filename}")
        return filename
    except Exception as e:
        logger.error(f"Error writing resolutions payload file {filename}: {str(e)}")
        return None

def extract_default_date_year_from_text(text_content, page_title, llm_providers, llm_sequence, max_retries=3, initial_retry_delay=5, api_call_delay=10):
    """
    Tries to extract a default year (YYYY integer) and date (YYYYMMDD integer) from text
    using an LLM call with a dedicated tool.
    
    Args:
        text_content (str): The text content to analyze for dates
        page_title (str): Title of the page being processed (for context)
        llm_providers (dict): Dictionary of LLM provider configurations
        llm_sequence (str): Comma-separated sequence of LLM provider IDs
        max_retries (int): Maximum number of retry attempts
        initial_retry_delay (int): Initial delay for retries
        api_call_delay (int): Delay between API calls
        
    Returns:
        tuple: (default_rok_int, default_datum_konani_int), where values can be None if not found
    """
    if not text_content or not isinstance(text_content, str) or not text_content.strip():
        logger.warning("extract_default_date_year_from_text: Input text_content is empty or invalid.")
        return None, None

    date_extraction_tool = [
        {
            "name": "report_extracted_date_year",
            "description": "Reports the primary extracted year and full date from the text.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "year": {
                        "type": ["integer", "null"],
                        "description": "The primary four-digit year (YYYY) extracted from the text. Null if not found."
                    },
                    "date_yyyymmdd": {
                        "type": ["integer", "null"],
                        "description": "The primary full date from the text, formatted as an eight-digit integer YYYYMMDD. Null if not found or not convertible."
                    }
                }
                # No 'required' fields, as either or both can be null.
            }
        }
    ]
    
    tool_choice_date = {"type": "tool", "name": "report_extracted_date_year"}

    system_prompt = """You are an expert date extraction assistant.
Your task is to analyze the provided text, which typically consists of a page title and a summary of its content.
Identify the single most prominent or primary year (YYYY) and the single most prominent or primary full date that best represents the main temporal context of this document.
The full date should be convertible to a YYYYMMDD integer format.
If a full date is identified, the year should correspond to that date.
If multiple dates or years are present, choose the one that seems most official or globally relevant for the document (e.g., a meeting date for meeting minutes, a publication year for a report).
Use the 'report_extracted_date_year' tool to provide your findings.
If a year cannot be determined, provide null for 'year'.
If a full date cannot be determined or converted to YYYYMMDD format, provide null for 'date_yyyymmdd'.
IMPORTANT: Return integers, not strings."""

    user_prompt = f"""Please extract the primary year and date from the following page content:

Page Title: {page_title}

Content: {text_content[:3000]}...

Focus on finding the most representative date/year for this document. Look for:
- Meeting dates for resolution documents
- Publication dates
- Document creation dates
- Any prominent date that represents when these resolutions were made

Return the year as a YYYY integer (e.g., 2024) and the date as a YYYYMMDD integer (e.g., 20241104).

Now, call the 'report_extracted_date_year' tool with your findings."""

    messages = [{"role": "user", "content": user_prompt}]

    try:
        response_data = call_llm_api(
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=512,
            temperature=0.0,
            tools=date_extraction_tool,
            tool_choice=tool_choice_date,
            max_retries=max_retries,
            initial_retry_delay=initial_retry_delay,
            api_call_delay=api_call_delay,
            llm_providers=llm_providers,
            llm_sequence=llm_sequence
        )

        if response_data is None:
            logger.error(f"Failed to extract default date/year from text for '{page_title}' (API call failed or returned no data).")
            return None, None

        # Handle tool response for date extraction
        if isinstance(response_data, dict):
            year = response_data.get('year')
            date_yyyymmdd = response_data.get('date_yyyymmdd')
            
            # Ensure they are integers (they should be from the tool, but validate)
            default_rok_int = year if isinstance(year, int) else None
            default_datum_konani_int = date_yyyymmdd if isinstance(date_yyyymmdd, int) else None
            
            logger.info(f"Successfully extracted default values for '{page_title}': Rok={default_rok_int}, DatumKonani={default_datum_konani_int}")
            return default_rok_int, default_datum_konani_int
        else:
            logger.error(f"Invalid response structure for date extraction from '{page_title}': {response_data}")
            return None, None

    except Exception as e:
        logger.error(f"Error during default date/year extraction for '{page_title}': {str(e)}")
        return None, None 