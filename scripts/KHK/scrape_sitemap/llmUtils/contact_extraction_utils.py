import json
import logging
from .llm_utils import call_llm_api

# Get logger for this module
logger = logging.getLogger(__name__)

def extract_contact_details_llm(content, page_title, category, source_url, llm_providers, llm_sequence, max_retries=3, initial_retry_delay=5, api_call_delay=10):
    """
    Extracts structured contact information from text using an LLM call.
    """
    
    # Debug: Log the input parameters to see what section context we're getting
    logger.info(f"🏢 CONTACT EXTRACTION DEBUG:")
    logger.info(f"  📋 Page Title: '{page_title}'")
    logger.info(f"  📂 Category: '{category}'")
    logger.info(f"  🔗 Source URL: '{source_url}'")
    logger.info(f"  📄 Content Preview (first 200 chars): '{content[:200]}'")
    
    # Analyze title for section context
    section_focus = "General content"
    if ' - ' in page_title:
        section_focus = "Specific subsection - " + page_title.split(' - ')[-1]
        logger.info(f"  🎯 DETECTED SECTION FOCUS: '{section_focus}'")
    else:
        logger.info(f"  🎯 NO SPECIFIC SECTION DETECTED, using: '{section_focus}'")
    
    contact_tool = [
        {
            "name": "extract_contact_details",
            "description": "Extracts detailed contact information for individuals from the provided text and formats it as a valid JSON object.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "contacts": {
                        "type": "array",
                        "description": "An array of contact objects extracted from the text. This MUST be present.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "FirstName": {"type": "string", "description": "First name of the contact. If multiple first names, include all."},
                                "LastName": {"type": "string", "description": "Last name of the contact. If multiple last names (e.g. double barrelled), include all."},
                                "FullName": {"type": "string", "description": "Full name of the contact, including titles if available directly with the name."},
                                "Title": {"type": "string", "description": "Academic or professional titles (e.g., Ing., Mgr., Ph.D.). May be part of FullName but also separate if clearly delineated."},
                                "Role": {"type": "string", "description": "Job title or role of the contact (e.g., Vedoucí oddělení, Referent)."},
                                "Department": {"type": "string", "description": "The main department or organizational unit the contact belongs to. (eg.)"},
                                "Subdepartment": {"type": "string", "description": "A sub-department or more specific team, if applicable."},
                                "Email": {"type": "string", "description": "Email address of the contact."},
                                "PhoneNumber": {"type": "string", "description": "Phone number of the contact. Include all listed numbers, separated by comma or semicolon."},
                                "ProfileURL": {"type": "string", "description": "Direct URL to the contact's profile page, if available in the text."},
                                "Office": {"type": "string", "description": "Office number or location description (e.g., Dveře č. 123, Budova A)."},
                                "OfficeURL": {"type": "string", "description": "URL specifically for the office location or map, if available."},
                                "Origin": {
                                    "type": "string", 
                                    "enum": ["Odbor", "Vybor", "Komise", "Rada", "Zastupitelstvo"],
                                    "description": "The organizational origin/type of the contact based on the page title and context. Must be one of: Odbor, Vybor, Komise, Rada, Zastupitelstvo."
                                }
                            },
                            "required": ["LastName", "FullName", "Origin"] # Origin is now required
                        }
                    }
                },
                "required": ["contacts"]
            }
        }
    ]
    tool_choice = {"type": "tool", "name": "extract_contact_details"}

    system_prompt = f'''# ROLE: AI specialist for complete contact extraction from text using `extract_contact_details` tool.

🚨 **CRITICAL MISSION: EXTRACT EVERY SINGLE PERSON - ZERO OMISSIONS** 🚨

## CONTEXT: '{page_title}' | Category: '{category}' | URL: {source_url}

## 📍 **SECTION-SPECIFIC CONTEXT AWARENESS (DERIVED FROM PAGE TITLE AND CONTENT)** 📍
**PAGE TITLE ANALYSIS**: The title '{page_title}' often contains crucial hierarchical information regarding departments or subdepartments.
- If title contains " - " (dash separator), the part AFTER the dash often indicates the SPECIFIC SECTION being processed (e.g., a department or subdepartment).
- If title contains " > " (arrow separator), it implies a hierarchical context (parent > child sections).
- **YOUR TASK**: Carefully analyze the `page_title` and the immediate surrounding text in the `content` to infer the `Department` and `Subdepartment` for each extracted contact.

## MANDATORY REQUIREMENTS:
- **EVERY PERSON** mentioned MUST be extracted (complete lists, partial info, staff directories).
- **SECTION-SPECIFIC EXTRACTION**: Focus on contacts relevant to the section implied by the `page_title` and surrounding text.
- **ALL CONTACT DATA** preserved: emails, phones, offices, titles, roles, departments.
- **SYSTEMATIC SCAN**: Beginning→end, double-check completeness.
- **SEPARATE OBJECTS**: Each person = individual JSON object.
- **DEPARTMENT/SUBDEPARTMENT INFERENCE**: Populate `Department` and `Subdepartment` fields by carefully analyzing the `page_title` and the textual context around where the contact is mentioned. If the `page_title` is like "Kontakty - Odbor X - Oddělení Y", then "Odbor X" is likely the Department and "Oddělení Y" the Subdepartment. If the context is less clear, make the best possible inference.

## 🔗 URL PRESERVATION (CONTACT-SPECIFIC):
**ProfileURL**: Personal profiles, LinkedIn, Facebook → only if explicitly mentioned
**OfficeURL**: Office maps, building plans → only if explicitly mentioned  
**Email/Phone links**: `[email](mailto:)` | `[phone](tel:)` formats supported
**NO GENERAL URLs**: Don't use page source URL for personal profiles

## EXTRACTION FIELDS (ALL PERSONS):
**REQUIRED**: `LastName`, `FullName` (with titles if present), `Origin`
**OPTIONAL**: `FirstName`, `Title`, `Role`, `Department`, `Subdepartment`, `Email`, `PhoneNumber`, `ProfileURL`, `Office`, `OfficeURL`
**MULTIPLE VALUES**: Comma-separated (multiple phones/emails per person)
**DEPARTMENT CONTEXT**: Use `page_title` and surrounding text to populate Department/Subdepartment fields accurately.
**ORIGIN DETERMINATION**: Analyze `page_title` and content to determine organizational origin. Must be one of: "Odbor", "Vybor", "Komise", "Rada", "Zastupitelstvo".

## PREPROCESSING: Replace ALL quote types with single apostrophes (`'`) before analysis.

## SECTION-AWARE DEPARTMENT ASSIGNMENT (Based on `page_title` and Content Analysis):
- If `page_title` shows "oddělení legislativní a právní", attempt to set Subdepartment to "oddělení legislativní a právní".
- If `page_title` shows "KAH - odbor kancelář hejtmana", attempt to set Department to "KAH - odbor kancelář hejtmana".
- If `page_title` shows "ředitel", use this to inform the Role.
- Use hierarchical context from `page_title` and the content itself to populate Department/Subdepartment fields.

## ORIGIN DETERMINATION RULES (Based on `page_title` and Content Analysis):
**CRITICAL**: Every contact MUST have an Origin field set to one of the enum values based on the nature of the contact/page:
- **"Odbor"**: If `page_title` contains "odbor", "oddělení", "úřad", "kancelář", or refers to administrative departments/offices
- **"Vybor"**: If `page_title` contains "výbor", "committee", or refers to committee structures  
- **"Komise"**: If `page_title` contains "komise", "commission", or refers to commission structures
- **"Rada"**: If `page_title` contains "rada", "council", "radní", or refers to council/advisory structures
- **"Zastupitelstvo"**: If `page_title` contains "zastupitelstvo", "zastupitel", "assembly", or refers to representative assembly structures
**DEFAULT FALLBACK**: If unclear from title/content, analyze the organizational context and choose the most appropriate value. "Odbor" is the default for general administrative contacts.

## VALIDATION CHECKLIST:
✓ Scanned entire text? ✓ Every person captured? ✓ All contact details preserved? ✓ Applied quote preprocessing? ✓ Department/Subdepartment fields reflect context derived from `page_title` and content? ✓ Origin field set correctly based on page title analysis?

## EXAMPLE WITH SECTION CONTEXT (DERIVED FROM PAGE TITLE AND CONTENT):
**Page Title**: "Kontakty - KAH - odbor kancelář hejtmana - oddělení legislativní a právní"
**Text**: "oddělení legislativní a právní\n\n| Příjmení, jméno, titul | Činnost | Email | Telefon | Dveře |\n|------------------------|---------|-------|----------|-------|\n| Tunová Ida Mgr. | právnička | itunova@khk.cz | 495817124, 720987134 | 4a-N3.411 |\n| Felzmannová Martina Mgr. | právnička | mfelzmannova@khk.cz | 607029476 | 4a-N3.411 |"
**Expected JSON** (after preprocessing and analysis of title/content):
```json
{{
  "contacts": [
    {{
      "FirstName": "Ida",
      "LastName": "Tunová",
      "FullName": "Tunová Ida Mgr.",
      "Title": "Mgr.",
      "Role": "právnička",
      "Department": "KAH - odbor kancelář hejtmana", // Inferred from title
      "Subdepartment": "oddělení legislativní a právní", // Inferred from title & content
      "Email": "itunova@khk.cz",
      "PhoneNumber": "495817124, 720987134",
      "ProfileURL": "",
      "Office": "4a-N3.411",
      "OfficeURL": "",
      "Origin": "Odbor" // Inferred from "odbor kancelář hejtmana" in title
    }},
    {{
      "FirstName": "Martina",
      "LastName": "Felzmannová",
      "FullName": "Felzmannová Martina Mgr.",
      "Title": "Mgr.",
      "Role": "právnička",
      "Department": "KAH - odbor kancelář hejtmana", // Inferred from title
      "Subdepartment": "oddělení legislativní a právní", // Inferred from title & content
      "Email": "mfelzmannova@khk.cz",
      "PhoneNumber": "607029476",
      "ProfileURL": "",
      "Office": "4a-N3.411",
      "OfficeURL": "",
      "Origin": "Odbor" // Inferred from "odbor kancelář hejtmana" in title
    }}
  ]
}}
```

**CRITICAL PATTERN**: Notice how:
- Department and Subdepartment are inferred from the `page_title` and potentially confirmed or refined by surrounding text.
- Origin is determined from the `page_title` analysis ("odbor kancelář hejtmana" → "Odbor").
- Section context from `page_title` and content determines accurate organizational placement.

**ZERO TOLERANCE: No person omissions, no contact data losses, no shortcuts. Department/Subdepartment/Origin fields must reflect context derived from `page_title` and content analysis.**

Extract ALL individuals with COMPLETE contact information, ACCURATE section-based department assignment, and CORRECT Origin determination based on `page_title` and content analysis.'''

    user_prompt = f'''# TEXT TO ANALYZE:
```
{content}
```

# TASK: Use `extract_contact_details` tool for extraction.

🚨 **MISSION: EXTRACT EVERY SINGLE PERSON** 🚨

**CRITICAL CONTEXT ANALYSIS (FROM PAGE TITLE & CONTENT)**: 
- Current processing title: '{page_title}'
- Section focus: {"Specific subsection - " + page_title.split(' - ')[-1] if ' - ' in page_title else "General content"}
- **Department/Subdepartment fields MUST be inferred from the `page_title` and surrounding text within the `content`.**

**REQUIREMENTS:**
- **100% COMPLETENESS**: Every person mentioned, no exceptions (staff lists, directories).
- **SECTION-SPECIFIC EXTRACTION**: Focus on contacts relevant to the section implied by the `page_title` and surrounding text.
- **ALL CONTACT DATA**: Names, titles, emails, phones, offices, departments, origin.
- **ACCURATE DEPARTMENT ASSIGNMENT**: Use `page_title` and surrounding text in the `content` to populate Department/Subdepartment fields.
- **CORRECT ORIGIN DETERMINATION**: Analyze `page_title` to set Origin field to one of: "Odbor", "Vybor", "Komise", "Rada", "Zastupitelstvo".
- **URL PRESERVATION**: ProfileURL/OfficeURL only if explicitly mentioned.
- **MULTIPLE VALUES**: Comma-separated for multiple emails/phones per person.
- **PREPROCESSING**: Apply quote→apostrophe conversion.

**SECTION-AWARE DEPARTMENT & ORIGIN EXAMPLES (Based on `page_title` and Content Analysis)**:
- Title "...oddělení legislativní a právní" → Infer Subdepartment: "oddělení legislativní a právní", Origin: "Odbor".
- Title "...KAH - odbor kancelář hejtmana" → Infer Department: "KAH - odbor kancelář hejtmana", Origin: "Odbor".
- Title "...výbor pro..." → Origin: "Vybor".
- Title "...komise..." → Origin: "Komise".
- Title "...rada..." → Origin: "Rada".
- Title "...zastupitelstvo..." → Origin: "Zastupitelstvo".
- Title "...ředitel" → Use this to inform the Role, Origin: "Odbor" (default for administrative).

**ZERO TOLERANCE**: 100 contacts = extract ALL 100 | 3 phone numbers = include ALL 3.

**OUTPUT**: Valid JSON via tool schema with section-aware Department/Subdepartment assignment and correct Origin determination based on `page_title` and content analysis.
'''
    messages = [{"role": "user", "content": user_prompt}]
    
    response_data = call_llm_api(
        messages=messages,
        system_prompt=system_prompt,
        max_tokens=4096,
        temperature=0.0,
        tools=contact_tool,
        tool_choice=tool_choice,
        max_retries=max_retries,
        initial_retry_delay=initial_retry_delay,
        api_call_delay=api_call_delay,
        llm_providers=llm_providers,
        llm_sequence=llm_sequence
    )

    # Debug logging for contact extraction response
    logger.info(f"======== CONTACT EXTRACTION DEBUG: RAW LLM RESPONSE FOR '{page_title}' ========")
    if response_data is None:
        logger.info("Full response_data is None.")
    elif isinstance(response_data, dict):
        try:
            logger.info(f"Full response_data (dict): {json.dumps(response_data, ensure_ascii=False, indent=2)}")
        except Exception as e:
            logger.error(f"Error trying to json.dumps response_data (dict): {str(e)}")
            logger.info(f"Problematic response_data (dict as string): {str(response_data)}")
    elif isinstance(response_data, str):
        logger.info(f"Full response_data (string): {response_data}")
    else:
        logger.info(f"Full response_data (type {type(response_data)}): {str(response_data)}")
    logger.info(f"======== END CONTACT EXTRACTION DEBUG: RAW LLM RESPONSE FOR '{page_title}' ========")

    if isinstance(response_data, dict) and 'contacts' in response_data:
        contacts = response_data['contacts']
        if isinstance(contacts, list):
            logger.info(f"Úspěšně extrahováno {len(contacts)} kontaktů pomocí nástroje pro stránku '{page_title}'.")
            # Add source URL and category to each contact item
            for contact_item in contacts:
                if isinstance(contact_item, dict):
                    contact_item['SourceURL'] = source_url
                    contact_item['Category'] = category
            return contacts
        else:
            logger.error(f"Klíč 'contacts' v odpovědi nástroje pro '{page_title}' není seznam (typ: {type(contacts)}). Data: {response_data}")
            return []
    elif response_data is None:
         logger.error(f"Nepodařilo se získat kontaktní údaje pro '{page_title}' (API volání selhalo nebo nevrátilo data).")
         return []
    else:
        logger.error(f"Neočekávaný formát odpovědi nebo chybí klíč 'contacts' při extrakci kontaktů pro '{page_title}'. Data: {str(response_data)[:500]}...")
        return [] 