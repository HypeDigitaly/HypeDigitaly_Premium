import json
import logging
import re
from .llm_utils import call_llm_api

# Get logger for this module
logger = logging.getLogger(__name__)

def extract_json_from_text(text):
    """Extract valid JSON from potentially malformed text"""
    # Try direct parsing first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Multiple fallback methods
        try:
            # Try to find JSON pattern starting with {"qa_pairs":
            pattern = r'(\{[\s\S]*"qa_pairs"[\s\S]*\})'
            match = re.search(pattern, text)
            if match:
                return json.loads(match.group(1))
        except:
            pass
        
        try:
            # Try to fix common JSON errors
            fixed_text = re.sub(r',\s*\}', '}', text)  # Remove trailing commas
            fixed_text = re.sub(r',\s*\]', ']', fixed_text)
            # Attempt to parse the fixed text
            # Added check to ensure it's a dictionary with 'qa_pairs' list
            parsed = json.loads(fixed_text)
            if isinstance(parsed, dict) and 'qa_pairs' in parsed and isinstance(parsed['qa_pairs'], list):
                return parsed 
        except json.JSONDecodeError:
            # If fixing common errors didn't work, proceed to last resort
            pass
        except Exception as e:
             logger.error(f"Unexpected error during JSON fixing/parsing: {str(e)}")
             # Proceed to last resort
             pass
        
        # Last resort - basic structure with empty pairs if all else fails
        logger.warning(f"Could not extract valid JSON structure from text. Returning empty list placeholder.")
        return {"qa_pairs": []}

def convert_to_qa(content, title, category, llm_providers, llm_sequence, max_retries=3, initial_retry_delay=5, api_call_delay=10):
    """Converts content to Q/A pairs using the configured LLM provider, forcing structured JSON output for Anthropic."""
    
    # Define the tool schema for structured Q/A extraction
    qa_tool = [
        {
            "name": "extract_qa_pairs",
            "description": "Extracts unique, non-redundant Question/Answer pairs from the provided text and assesses for contact information and resolution information, formatting them as a valid JSON object according to the specified schema.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "qa_pairs": {
                        "type": "array",
                        "description": "An array of unique Question/Answer objects extracted from the text. Each Q/A pair must be distinct and non-redundant. This is of utmost importance and must be ALWAYS present no matter what. Presence of this array is life or death scenario.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "Question": {"type": "string", "description": "The question formulated from the text content, including multiple variations separated by ' | '. Must be unique and not duplicate information covered by other Q/A pairs."},
                                "Answer": {"type": "string", "description": "The comprehensive answer extracted verbatim from the text, including any relevant Markdown formatting for links and images. Should contain all relevant details for the specific question."}, 
                                "Category": {"type": "string", "description": "The category assigned to this Q/A pair."}
                            },
                            "required": ["Question", "Answer", "Category"]
                        }
                    },
                    "contains_contact_info": {
                        "type": "boolean",
                        "description": "Set to true if the analyzed text contains identifiable human contact details (names, emails, phone numbers, job titles), otherwise false."
                    },
                    "contains_resolutions": {
                        "type": "boolean",
                        "description": "Set to true if the analyzed text contains information about resolutions (usnesení) from zastupitelstva, rada, komise, or výbory (councils, committees, boards), otherwise false."
                    }
                },
                "required": ["qa_pairs", "contains_contact_info", "contains_resolutions"]
            }
        }
    ]
    
    # Force the model to use the defined tool
    tool_choice = {"type": "tool", "name": "extract_qa_pairs"}
    
    # System prompt for Q/A extraction
    system_prompt = f"""# ROLE: Information extraction specialist for topic '{title}' using `extract_qa_pairs` tool.

🚨 **CRITICAL MISSION: 100% COMPLETE EXTRACTION - ZERO OMISSIONS - ZERO REDUNDANCY** 🚨

## 🇨🇿 **ALL ANSWERS MUST BE IN CZECH LANGUAGE - NO EXCEPTIONS** 🇨🇿

## 📍 **SECTION-SPECIFIC CONTEXT AWARENESS** 📍
**TITLE ANALYSIS**: The title '{title}' contains important hierarchical information:
- If title contains " - " (dash separator), the part AFTER the dash indicates the SPECIFIC SECTION being processed
- If title contains " > " (arrow separator), it shows hierarchical context (parent > child sections)
- Questions MUST be specific to the EXACT SECTION indicated in the title, not general page questions
- For subsections (like "oddělení legislativní a právní"), questions should focus on that specific subsection

## MANDATORY REQUIREMENTS:
- **SECTION-SPECIFIC QUESTIONS**: Generate questions that are specific to the section indicated in the title.
- **UNIQUE Q/A PAIRS**: Each question must be distinct and cover different aspects of information. NO duplicate or overlapping Q/A pairs.
- **COMPREHENSIVE ANSWERS**: Group related information into single comprehensive answers rather than creating multiple similar Q/A pairs.
- **COMPLETE MARKDOWN TABLES**: All Markdown tables MUST be extracted verbatim and in their entirety into the 'Answer' fields. This includes preserving all rows, columns, content, and any internal Markdown formatting, within the table cells. Do NOT summarize or reformat tables.
- **COMPLETE LISTS**: Contact lists (ALL persons), resolution lists (ALL resolutions)
- **ALL URLs/FILES**: PDF/DOC/XLS/ZIP/images/links preserved in Markdown
- **ASSESSMENTS**: Set `contains_contact_info` & `contains_resolutions` (true/false)

## 🔗 URL/FILE PRESERVATION (CRITICAL):
**All types**: PDF/DOC/XLS/ZIP/images/web/email/phone/FTP/social media
**Format**: Plain URLs → `[name](URL)` | Existing Markdown → preserve exactly
**Integration**: Embed naturally in answers, comma-separated if multiple
**Examples**: `[Document.pdf](file.pdf)`, `[Site](https://example.com)`, `[email](mailto:)`, `![Image](img.jpg)`

## EXTRACTION STRATEGY:
- **SYSTEMATIC SCAN**: Beginning→end coverage of all relevant content
- **VERBATIM ANSWERS**: Complete, untruncated text in CZECH language. For tabular data, the entire Markdown table must be included in the answer.
- **DETAILED CONTACT LISTS**: Create comprehensive Q/A pairs for individuals or groups. Questions and Answers must specifically include roles, departments, areas of expertise, or sub-departments when this information is present in the source text. For example, if the text mentions "Ing. Jan Novák, Vedoucí odboru Plánování, jan.novak@example.com", the Question should be specific like "Kdo je vedoucí Odboru Plánování a jaké jsou jeho kontaktní údaje?" and the Answer should include all details.
- **RESOLUTION LISTS**: Complete coverage of ALL resolutions with attachments
- **QUESTION FORMAT**: Multiple variations within each question: Czech main | Czech variants | English. For contacts, ensure questions ask for specific roles/departments if applicable (guided by text).
- **CONSOLIDATE RELATED INFO**: If multiple pieces of information answer the same general question, combine them into one comprehensive Q/A pair.
- **SPECIFIC QUESTIONS**: Create focused, specific questions that clearly differentiate the information being asked and are tailored to the section context (guided by `title`).
- **NO REDUNDANT Q/A PAIRS**: Avoid creating multiple Q/A pairs that essentially ask for the same information in different ways.

## CONTACT ASSESSMENT: True if identifiable individuals with names + contact details (email, phone) AND/OR specific roles/departments.
## RESOLUTION ASSESSMENT: True if contains usnesení/rozhodnutí from councils/committees

## PREPROCESSING: Replace ALL quote types with single apostrophes (`'`) before analysis.

## EXAMPLES FOR SECTION-SPECIFIC QUESTIONS:

### Example 1: General Department Context
**Title**: "Kontakty - Odbor IT"
**Text**: "Odbor IT:\\n\\n| Jméno | Pozice | Email | Telefon |\\n|-------|---------|--------|---------|\\n| Ing. Jan Novák | Vedoucí odboru | jan.novak@khk.cz | 123456789 |\\n| Mgr. Eva Bílá | Referent | eva.bila@khk.cz | 987654321 |"
**Expected JSON**:
```json
{{
  "qa_pairs": [
    {{
      "Question": "Kdo jsou pracovníci Odboru IT a jaké jsou jejich kontaktní údaje? | Seznam zaměstnanců IT odboru | Who are the IT department employees and their contacts?",
      "Answer": "Odbor IT:\\n\\n| Jméno | Pozice | Email | Telefon |\\n|-------|---------|--------|---------|\\n| Ing. Jan Novák | Vedoucí odboru | jan.novak@khk.cz | 123456789 |\\n| Mgr. Eva Bílá | Referent | eva.bila@khk.cz | 987654321 |",
      "Category": "Kontakt"
    }}
  ],
  "contains_contact_info": true,
  "contains_resolutions": false
}}
```

### Example 2: Specific Subsection Context  
**Title**: "Kontakty - KAH - odbor kancelář hejtmana - oddělení legislativní a právní"
**Text**: "oddělení legislativní a právní\\n\\n| Příjmení, jméno, titul | Činnost | Email | Telefon |\\n|------------------------|---------|-------|----------|\\n| Tunová Ida Mgr. | právnička | itunova@khk.cz | 495817124 |\\n| Felzmannová Martina Mgr. | právnička | mfelzmannova@khk.cz | 607029476 |"
**Expected JSON**:
```json
{{
  "qa_pairs": [
    {{
      "Question": "Kdo jsou pracovníci oddělení legislativní a právní a jaké jsou jejich kontaktní údaje? | Seznam zaměstnanců legislativního oddělení | Who works in the legal and legislative department and what are their contacts?",
      "Answer": "oddělení legislativní a právní\\n\\n| Příjmení, jméno, titul | Činnost | Email | Telefon |\\n|------------------------|---------|-------|----------|\\n| Tunová Ida Mgr. | právnička | itunova@khk.cz | 495817124 |\\n| Felzmannová Martina Mgr. | právnička | mfelzmannova@khk.cz | 607029476 |",
      "Category": "Kontakt"
    }}
  ],
  "contains_contact_info": true,
  "contains_resolutions": false
}}
```

### Example 3: Role-Based Section
**Title**: "Kontakty - ředitel"
**Text**: "ředitel\\n\\n| Příjmení, jméno, titul | Činnost | Dveře | Telefon |\\n|------------------------|---------|-------|----------|\\n| Vrba Miroslav Ing. MPA | ředitel | 1a-N4.118 | 495817280 |"
**Expected JSON**:
```json
{{
  "qa_pairs": [
    {{
      "Question": "Kdo je ředitel a jaké jsou jeho kontaktní údaje? | Kontaktní údaje na ředitele | Who is the director and what are his contact details?",
      "Answer": "ředitel\\n\\n| Příjmení, jméno, titul | Činnost | Dveře | Telefon |\\n|------------------------|---------|-------|----------|\\n| Vrba Miroslav Ing. MPA | ředitel | 1a-N4.118 | 495817280 |",
      "Category": "Kontakt"
    }}
  ],
  "contains_contact_info": true,
  "contains_resolutions": false
}}
```

**CRITICAL PATTERN**: Notice how each example shows:
- Questions specific to the EXACT section in the title (not generic questions)
- Complete table preservation in answers
- Proper boolean flags based on content assessment
- Section-focused language: "oddělení legislativní a právní" NOT "odbor kancelář hejtmana" (this should be derived from `title`)

Extract COMPLETE information with FULL URL and TABLE preservation while maintaining ABSOLUTE UNIQUENESS of Q/A pairs and SECTION-SPECIFIC question relevance."""

    user_prompt = f"""# SOURCE TEXT:
```
{content}
```
# TASK: Use `extract_qa_pairs` tool for extraction, following all rules in the system prompt.

🚨 **MISSION: EXTRACT EVERY PIECE OF INFORMATION METICULOUSLY - NO REDUNDANCY - SECTION-SPECIFIC FOCUS** 🚨

**CRITICAL CONTEXT ANALYSIS**: 
- Current processing title: '{title}'
- Section focus: {"Specific subsection - " + title.split(' - ')[-1] if ' - ' in title else "General content"}
- Questions must be tailored to this specific section context.

**REQUIREMENTS (Adhere to System Prompt for full details):**
- **100% COMPLETENESS**: All details relevant to the specific section indicated in title (no truncation/summarization).
- **SECTION-SPECIFIC QUESTIONS**: Questions must focus on the exact section/subsection shown in the title.
- **ZERO REDUNDANCY**: Each Q/A pair must be unique and address different information. NO duplicate questions.
- **CZECH ANSWERS**: All Answer fields MUST be in Czech language.
- **PRESERVE MARKDOWN TABLES**: Extract entire Markdown tables into 'Answer' fields without any loss of data or structure.
- **CONSOLIDATE RELATED INFO**: Group similar information into comprehensive single Q/A pairs rather than creating multiple variations.
- **SPECIFIC QUESTIONS**: Create focused, distinct questions that clearly differentiate the information being requested and reflect the section context (guided by `title`).
- **COMPLETE LISTS**: Contact lists (ALL persons), resolutions (ALL items).
- **ALL URLs/FILES**: Every link preserved in Markdown format.
- **ASSESSMENTS**: Set contact_info & resolutions boolean flags accurately.
- **PREPROCESSING**: Apply quote→apostrophe conversion.

**SECTION-AWARE QUESTION EXAMPLES**:
- For "oddělení legislativní a právní": Ask about "pracovníci oddělení legislativní a právní" not "pracovníci odboru"
- For "ředitel": Ask about "ředitel" specifically, not general staff
- For subsections: Focus questions on that specific subsection, guided by `title`.

**ANTI-REDUNDANCY CHECKLIST:**
- ✅ Each Q/A pair addresses a different aspect of the section content
- ✅ Related information is consolidated into single comprehensive answers
- ✅ Multiple question variations included within each Q/A pair using "|" separators
- ✅ No duplicate Q/A pairs covering the same information
- ✅ Questions are specific to the section indicated in the title

**ZERO TOLERANCE**: 50 contacts = extract ALL 50 in optimized groupings | 20 resolutions = extract ALL 20 in logical groupings | 10 tables = extract ALL 10 completely | Multiple attachments = preserve ALL.

**OUTPUT**: Valid JSON via tool schema with Czech answers, section-specific questions, ensuring all specified data (especially tables and contacts) is fully represented in UNIQUE, NON-REDUNDANT Q/A pairs."""

    messages = [{"role": "user", "content": user_prompt}]
    
    # Call the API, providing the tool definition and forcing its use for Anthropic
    response_data = call_llm_api(
        messages=messages, 
        system_prompt=system_prompt, 
        max_tokens=8192,
        temperature=0,
        tools=qa_tool,
        tool_choice=tool_choice,
        max_retries=max_retries,
        initial_retry_delay=initial_retry_delay,
        api_call_delay=api_call_delay,
        llm_providers=llm_providers,
        llm_sequence=llm_sequence
    )

    # Debug logging for problematic Claude QA responses
    log_full_response_for_debug = False
    problematic_qa_pairs_source_string = None

    if response_data is None:
        logger.error(f"Nepodařilo se získat Q/A páry pro '{title}' (API volání selhalo nebo nevrátilo data). ")
    elif isinstance(response_data, dict):
        if 'qa_pairs' in response_data:
            potential_pairs_val = response_data['qa_pairs']
            if isinstance(potential_pairs_val, str):
                problematic_qa_pairs_source_string = potential_pairs_val
                parsed_val_for_check = extract_json_from_text(potential_pairs_val)
                if parsed_val_for_check == {"qa_pairs": []} and \
                   potential_pairs_val.strip().lower() not in ('[]', '{"qa_pairs": []}', '{"qa_pairs":null}', '{"qa_pairs": ""}','{}'):
                    log_full_response_for_debug = True
                    logger.warning(f"QA EXTRACTION DEBUG TRIGGER: 'qa_pairs' was a string that extract_json_from_text likely converted to placeholder for '{title}'.")
        else:
            log_full_response_for_debug = True
            logger.warning(f"QA EXTRACTION DEBUG TRIGGER: 'qa_pairs' key missing in response_data dictionary for '{title}'.")
    elif isinstance(response_data, str):
        log_full_response_for_debug = True
        problematic_qa_pairs_source_string = response_data
        logger.warning(f"QA EXTRACTION DEBUG TRIGGER: Entire response_data from LLM was a string for '{title}'.")
    else:
        log_full_response_for_debug = True
        logger.warning(f"QA EXTRACTION DEBUG TRIGGER: Unexpected response_data type received from LLM for '{title}'. Type: {type(response_data)}")

    if log_full_response_for_debug:
        logger.info(f"======== QA EXTRACTION DEBUG: RAW LLM RESPONSE FOR '{title}' ========")
        try:
            if isinstance(response_data, dict):
                 logger.info(f"Full response_data (dict): {json.dumps(response_data, ensure_ascii=False, indent=2)}")
            elif isinstance(response_data, str):
                 logger.info(f"Full response_data (string): {response_data}")
            else:
                 logger.info(f"Full response_data (type {type(response_data)}): {str(response_data)}")
            
            if problematic_qa_pairs_source_string and problematic_qa_pairs_source_string is not response_data:
                logger.info(f"Problematic 'qa_pairs' source string content: {problematic_qa_pairs_source_string}")
            
            print(f"QA EXTRACTION DEBUG: Full LLM API response for '{title}' logged to detailed log due to a processing issue (see log for details).")
        except Exception as e:
            logger.error(f"QA EXTRACTION DEBUG: Error encountered while trying to log full response_data: {str(e)}")
            logger.info(f"Problematic response_data (snippet if full logging failed): {str(response_data)[:1000]}...")
        logger.info(f"======== END QA EXTRACTION DEBUG: RAW LLM RESPONSE FOR '{title}' ========")

    # Handle tool response
    qa_pairs = None
    contains_contact_info = False
    contains_resolutions = False

    if isinstance(response_data, dict):
        # Extract contains_contact_info first
        contains_contact_info = response_data.get('contains_contact_info', False)
        if not isinstance(contains_contact_info, bool):
            logger.warning(f"Hodnota 'contains_contact_info' není boolean: {contains_contact_info}. Nastavuji na False.")
            contains_contact_info = False
        logger.info(f"Assessment for contains_contact_info for '{title}': {contains_contact_info}")

        # Extract contains_resolutions
        contains_resolutions = response_data.get('contains_resolutions', False)
        if not isinstance(contains_resolutions, bool):
            logger.warning(f"Hodnota 'contains_resolutions' není boolean: {contains_resolutions}. Nastavuji na False.")
            contains_resolutions = False
        logger.info(f"Assessment for contains_resolutions for '{title}': {contains_resolutions}")

        if 'qa_pairs' in response_data:
            potential_pairs = response_data['qa_pairs']

            # Check if potential_pairs is a string and try to parse it
            if isinstance(potential_pairs, str):
                logger.info(f"Model returned qa_pairs as a string for '{title}'. Attempting robust parsing.")
                parsed_data = extract_json_from_text(potential_pairs)
                if parsed_data and isinstance(parsed_data, dict) and 'qa_pairs' in parsed_data and parsed_data['qa_pairs'] is not None:
                    is_placeholder = (parsed_data == {"qa_pairs": []}) and '[TABLE DATA]' not in potential_pairs
                    
                    potential_pairs = parsed_data['qa_pairs']
                    if not is_placeholder:
                        logger.info(f"Successfully parsed string using extract_json_from_text for '{title}'.")
                    else:
                        logger.warning(f"extract_json_from_text returned placeholder for '{title}'. Original string likely unparseable.")
                else:
                    logger.error(f"Failed to parse JSON string robustly for 'qa_pairs' for '{title}'. Input string preview: {potential_pairs[:500]}...")
                    potential_pairs = None

            if isinstance(potential_pairs, list):
                validated_internal_pairs = []
                valid_structure = True
                for item in potential_pairs:
                    if not isinstance(item, dict):
                        logger.error(f"Položka v 'qa_pairs' není slovník pro '{title}': {item}")
                        valid_structure = False
                        break
                    if "Question" not in item or not isinstance(item.get("Question"), str):
                        logger.error(f"Chybí klíč 'Question' nebo není string v položce pro '{title}': {item}")
                        valid_structure = False
                        break
                    if "Answer" not in item or not isinstance(item.get("Answer"), str):
                        logger.error(f"Chybí klíč 'Answer' nebo není string v položce pro '{title}': {item}")
                        valid_structure = False
                        break
                    validated_internal_pairs.append({"Question": item["Question"], "Answer": item["Answer"]})

                if valid_structure:
                    qa_pairs = validated_internal_pairs
                    logger.info(f"Úspěšně extrahováno a validováno {len(qa_pairs)} Q/A párů pomocí nástroje (klíč 'qa_pairs') pro '{title}'.")
                else:
                    logger.error(f"Seznam 'qa_pairs' nalezen pro '{title}', ale vnitřní struktura položek neodpovídá schématu ['Question', 'Answer'].")
            else:
                logger.error(f"Klíč 'qa_pairs' nalezen v odpovědi nástroje pro '{title}', ale hodnota není seznam (typ: {type(potential_pairs)}). Data: {response_data}")
        else:
            logger.error(f"Odpověď nástroje pro '{title}' je slovník, ale neobsahuje očekávaný klíč 'qa_pairs'. Klíče: {list(response_data.keys())}. Data: {response_data}")

    elif isinstance(response_data, str):
         logger.warning(f"Model vrátil textovou odpověď místo očekávaného JSON z nástroje pro '{title}'. Text: {response_data[:200]}...")
         try:
             qa_data = json.loads(response_data)
             if isinstance(qa_data, dict) and 'qa_pairs' in qa_data and isinstance(qa_data['qa_pairs'], list):
                 potential_pairs = qa_data['qa_pairs']
                 validated_internal_pairs = []
                 valid_structure = True
                 for item in potential_pairs:
                     if not isinstance(item, dict) or "Question" not in item or not isinstance(item.get("Question"), str) or "Answer" not in item or not isinstance(item.get("Answer"), str):
                         logger.error(f"Položka v parsovaných 'qa_pairs' neodpovídá schématu ['Question', 'Answer'] pro '{title}': {item}")
                         valid_structure = False
                         break
                     validated_internal_pairs.append({"Question": item["Question"], "Answer": item["Answer"]})

                 if valid_structure:
                     qa_pairs = validated_internal_pairs
                     logger.info(f"Úspěšně parsováno a validováno {len(qa_pairs)} Q/A párů z textové odpovědi pro '{title}'.")
                 else:
                      logger.error(f"Textovou odpověď se podařilo parsovat, ale vnitřní struktura 'qa_pairs' neodpovídá schématu ['Question', 'Answer']. Parsed data: {qa_data}")
             else:
                 logger.error(f"Textovou odpověď nelze parsovat do očekávané struktury {{'qa_pairs': [...]}}. Parsed data: {qa_data}")
         except json.JSONDecodeError:
             logger.error(f"Textovou odpověď nelze parsovat jako JSON.")

    else:
        logger.error(f"Neočekávaný typ odpovědi ({type(response_data)}) z call_llm_api pro '{title}'. Data: {response_data}")

    # If we successfully extracted a list of pairs, add the category
    if qa_pairs is not None:
        valid_pairs_with_category = []
        for pair in qa_pairs:
             # Ensure 'Category' key exists and is set, otherwise default it
             if 'Category' not in pair or not pair['Category']: # Check if None or empty string
                 logger.warning(f"Q/A pair missing 'Category', defaulting to '{category}'. Pair: {pair}")
                 pair['Category'] = category
             valid_pairs_with_category.append(pair)
        
        if not valid_pairs_with_category and isinstance(response_data, dict) and 'qa_pairs' in response_data and response_data['qa_pairs'] == []:
             logger.info(f"Model vrátil validní prázdný seznam 'qa_pairs' pro '{title}', protože nebyly nalezeny žádné relevantní informace.")

        return valid_pairs_with_category, contains_contact_info, contains_resolutions
    else:
        logger.warning(f"Nepodařilo se extrahovat platný seznam Q/A párů z odpovědi modelu/nástroje pro '{title}'. Spouští se fallback.")
        return None, contains_contact_info, contains_resolutions

def convert_to_qa_with_retry(content, title, category, llm_providers, llm_sequence, max_retries=3):
    """Try multiple approaches to generate QA pairs with retries"""
    # Note: enhanced_context_for_qa is not directly used in this retry wrapper,
    # but it will be passed down if convert_to_qa is called from a context where it's available.
    # This wrapper is more about content simplification retries.
    for attempt in range(max_retries):
        try:
            # First try with full content
            if attempt == 0:
                qa_pairs, contains_contacts, contains_resolutions = convert_to_qa(content, title, category, llm_providers, llm_sequence)
                if qa_pairs:
                    return qa_pairs, contains_contacts, contains_resolutions
            
            # Second try with simplified content
            elif attempt == 1:
                simplified = re.sub(r'<table.*?</table>', '[TABLE DATA]', content, flags=re.DOTALL)
                qa_pairs, contains_contacts, contains_resolutions = convert_to_qa(simplified, title, category, llm_providers, llm_sequence)
                if qa_pairs:
                    return qa_pairs, contains_contacts, contains_resolutions
            
            # Last try with minimal extraction approach
            else:
                text_only = re.sub(r'<[^>]+>', ' ', content)
                text_only = re.sub(r'\s+', ' ', text_only).strip()
                qa_pairs, contains_contacts, contains_resolutions = convert_to_qa(text_only[:5000], title, category, llm_providers, llm_sequence)
                if qa_pairs:
                    return qa_pairs, contains_contacts, contains_resolutions
                
        except Exception as e:
            logger.error(f"Error in convert_to_qa attempt {attempt+1}: {str(e)}")
    
    logger.error(f"All QA extraction attempts failed for {title}")
    return None, False, False 