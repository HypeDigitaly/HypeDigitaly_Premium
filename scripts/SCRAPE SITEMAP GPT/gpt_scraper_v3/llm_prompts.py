"""LLM prompt templates and question generation for GPT Scraper V3.

Canonical source of truth for all prompt templates (M4).
Fixes: M4 (single source), M5 (type hints), M3 (no re-imports).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.openrouter_client import _call_openrouter

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt instruction functions (canonical versions)
# ---------------------------------------------------------------------------


def get_standard_summarization_instructions(
    target_tokens: int = 4000, target_language: str = "English",
    url: str = "", title: str = "",
) -> str:
    """Get standardized summarization instructions ensuring ALL prompts use identical text."""
    # Language instruction for non-English targets
    language_instruction = (
        f"- **OUTPUT LANGUAGE:** {target_language} (translate while preserving ALL factual data)\n"
        if target_language.lower() != "english"
        else ""
    )

    return f"""\U0001f916 EXPERT CONTENT SUMMARIZATION SPECIALIST: You are a professional content analyst who MUST create concise, RAG-optimized summaries using ADVANCED COMPRESSION STRATEGIES and INTELLIGENT INFORMATION ARCHITECTURE.

## \U0001f6a8 CRITICAL SUMMARIZATION REQUIREMENTS:
{language_instruction}
### **\U0001f522 MANDATORY NUMERICAL DATA HANDLING (NON-NEGOTIABLE):**
- **PRESERVATION:** You MUST preserve and use ALL exact counts, numbers, and figures that are ALREADY PRESENT in the source markdown content (e.g., "35 council members", "12 agenda items").
- **\U0001f6a8 STRICT COUNTING PROHIBITION:** You MUST NEVER count elements (e.g., contacts, list items, table rows) yourself if the exact number is NOT explicitly stated in the source text.
- **ESTIMATES ONLY:** If an exact count is missing, you may use general estimates (e.g., "dozens", "hundreds", "thousands"), but NEVER provide an exact calculated count.

### **\U0001f4cf MANDATORY SIZE REDUCTION WITH INTELLIGENCE:**
- **STRICT TOKEN LIMIT:** {target_tokens} tokens MAXIMUM (non-negotiable)
- **\U0001f6a8 EXPANSION FORBIDDEN:** Your output MUST be at least 20% SHORTER than input
- **STRATEGIC COMPRESSION:** Aim for 50-70% size reduction using INTELLIGENT DENSITY MAXIMIZATION
- **QUALITY VALIDATION:** Self-verify compression success before submission

### **\U0001f9e0 ADVANCED CONTENT PROCESSING METHODOLOGY:**

**PHASE 1: CONTENT PURIFICATION**
- **ELIMINATE:** Cookies, GDPR notices, HTML/CSS/JS code, navigation menus, ads, boilerplate text, metadata
- **PRESERVE ABSOLUTELY:** ALL contacts, phone numbers, emails, URLs, specifications, organizational data, addresses
- **STRUCTURAL INTEGRITY:** Maintain complete contact hierarchy and department structures
- **MEDIA EXTRACTION:** Convert all images to `![alt](url)` and all links to `[text](url)` format - MANDATORY

**PHASE 2: INTELLIGENT COMPRESSION STRATEGIES**
1. **DESCRIPTION CONDENSATION:** Transform verbose explanations into high-density bullet points
2. **REDUNDANCY ELIMINATION:** Remove repetitive information while preserving unique elements
3. **PROCEDURAL SUMMARIZATION:** Condense processes into essential steps with factual anchors
4. **COMPLETENESS VERIFICATION:** Ensure ALL critical data, contacts, and specifications remain intact
5. **ARCHITECTURAL OPTIMIZATION:** Use efficient markdown formatting for maximum information density

**PHASE 3: SEMANTIC STRUCTURE OPTIMIZATION**
- **HIERARCHICAL PRESERVATION:** Maintain logical information flow and relationships
- **FACTUAL ANCHORING:** Preserve key reference points for context navigation
- **COMPRESSION VALIDATION:** Verify essential information retained at target density

### **\U0001f4ca ADAPTIVE CONTENT INTELLIGENCE:**
**ORGANIZATIONAL CONTENT:** Preserve complete hierarchies, exact counts, contact completeness
**PROCEDURAL CONTENT:** Maintain step sequences, decision points, process flows
**TEMPORAL CONTENT:** Keep chronological order, date references, sequence logic
**REFERENCE CONTENT:** Preserve cross-references, legal citations, document relationships

## \U0001f4dd REQUIRED OUTPUT FORMAT:

# **QUESTION:**

[3-6 search variants based on URL/TITLE: "{title}" and "{url}" | separated]

Extract key terms from URL path and title only. Create search variations including contact queries, "who is" questions, and specific information requests.

**EXAMPLE:**
Hejtman | Hejtman Kralovehradecky kraj | Kontakt hejtman | Kdo je hejtman ? | Kdo je hejtman Kralovehradeckeho kraje? | Jake jsou kontaktni udaje a informace o hejtmanovi Kralovehradeckeho kraje?

# **ANSWER:**

## **URL BRIEF SUMMARY:**

[Concise 2-3 sentences capturing the essential purpose and content of the page]

## **URL CONTENT FULL SUMMARY:**

[STRATEGICALLY COMPRESSED but COMPLETE summary in {target_language} using EFFICIENT markdown formatting starting with ### headings. Focus on information density - maximum facts in minimum space while preserving ALL contacts and critical data.]

## \U0001f3af PROFESSIONAL SUMMARIZATION CHECKLIST:
**BEFORE SUBMITTING, VERIFY:**
- [ ] Output is SIGNIFICANTLY shorter than input (minimum 20% reduction)
- [ ] ALL contacts, phones, emails, addresses are preserved 100%
- [ ] All images converted to `![alt](url)` format
- [ ] All links converted to `[text](url)` format
- [ ] Efficient markdown structure used (###, ####, -, *, 1., etc.)
- [ ] {target_language} language used consistently
- [ ] Token limit {target_tokens} respected
- [ ] NO technical debris or boilerplate included
- [ ] Essential information density maximized
- [ ] Factual anchors preserved for context navigation

## \U0001f6a8 ENHANCED SUMMARIZATION SUCCESS CRITERIA:
\u2705 **STRATEGIC COMPRESSION:** Achieved intelligent size reduction while preserving all essential data
\u2705 **FACTUAL COMPLETENESS:** No critical information lost, all contacts and references intact
\u2705 **SEMANTIC EFFICIENCY:** Maximum information density per token used
\u2705 **STRUCTURAL INTEGRITY:** Perfect markdown architecture with all media extracted
\u2705 **CONTEXTUAL PRESERVATION:** Key factual anchors maintained for chunked file navigation

**REMEMBER:** You are creating INTELLIGENT SUMMARIES, not expansions. Demonstrate advanced prompt engineering expertise by achieving maximum information density while maintaining complete factual accuracy and contextual continuity."""


def create_standard_fallback_content(
    section_title: str, chunk_number: int, total_chunks: int,
    title: str, url: str, content: str,
) -> str:
    """Create standardized fallback content when summarization fails."""
    return f"""# **QUESTION:**

{section_title} | section {chunk_number} | {title} | {url}

# **ANSWER:**

## **URL BRIEF SUMMARY:**

### \U0001f4cb {section_title} - Cast {chunk_number} z {total_chunks}

\u26a0\ufe0f **SECTION PRESERVATION MODE**: Complete content for this section preserved below with mandatory image and link extraction in markdown format.

## **URL CONTENT FULL SUMMARY:**

### \U0001f4cb {section_title} - Sekce {chunk_number} z {total_chunks}

**\U0001f5bc\ufe0f MANDATORY NOTE:** All images converted to markdown format `![alt](url)` and all links to `[text](url)` as required by standardized instructions.

### \U0001f4c4 Obsah sekce:

{content}

### \U0001f517 Odkazy
- **Complete document**: [{url}]({url})
"""


def get_page_summary_instructions(
    target_language: str = "Czech", url: str = "", title: str = "",
) -> str:
    """Get instructions for generating a short 1-2 paragraph page summary."""
    # Language instruction for non-English targets
    language_instruction = (
        f"- **OUTPUT LANGUAGE:** {target_language}\n"
        if target_language.lower() != "english"
        else ""
    )

    return f"""\U0001f916 EXPERT PAGE CONTENT ANALYST: Create ULTRA-SHORT, HIGH-LEVEL summary about entire webpage content.

## \U0001f3af TASK: Create 1-2 SHORT sentences about ENTIRE PAGE CONTENT

{language_instruction}### **\U0001f4cb CRITICAL REQUIREMENTS:**
- **LENGTH:** Maximum 1-2 short sentences (like SMS messages)
- **\U0001f6a8 ABSOLUTELY FORBIDDEN:** DO NOT mention any specific individual names, personal titles, specific department names, or any detailed information
- **FOCUS:** Only overall counts, content type, and general structure
- **PURPOSE:** Provide basic context about what type of content the page contains
### **\u2705 WHAT TO INCLUDE:**
- **EXPLICIT COUNTS ONLY:** Total number of people/items/sections (numbers only) IF the number is explicitly stated in the source content. NEVER count elements yourself. Use estimates (dozens/hundreds) if the count is missing.
- Type of content (contact list, meeting documents, news, etc.)
- Basic organizational structure (how many main sections/categories)
- Available contact data types (phone, email, etc.)

### **\u274c WHAT TO NEVER MENTION:**
- Individual names or surnames
- Specific department names or abbreviations
- Personal titles (Ing., Mgr., etc.)
- Specific phone numbers or emails
- Any detailed information

### **\U0001f3af OUTPUT FORMAT:**
Provide ONLY the 1-2 extremely short sentences in {target_language}. No formatting, no headings.

**CORRECT EXAMPLE:**
Stranka obsahuje telefonni seznam krajskeho uradu s celkem 300 zamestnanci rozdelenych do 20 organizacnich jednotek. Vsechny kontakty maji kompletni udaje vcetne funkce, telefonu a emailu.
"""


# ---------------------------------------------------------------------------
# Question generation functions (use _call_openrouter)
# ---------------------------------------------------------------------------


def _build_question_fallback(title: str, url: str) -> str:
    """Build a simple fallback QUESTION string from title and URL path."""
    parsed_path = urlparse(url).path
    return (
        f"{title} | {parsed_path} | key info | "
        f"What is {title}? | Details on {title} | Contact for {title}"
    )


def generate_question_section(
    url: str, title: str, target_language: str = "Czech",
    page_summary: Optional[str] = None,
    rss_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate QUESTION section (3 keywords + 3 questions) via OpenRouter API."""
    cfg = get_config()
    logger.info("Generating QUESTION section via OpenRouter API for URL: %s, Title: %s", url, title)

    # Check if this is an Events RSS URL with rich metadata
    is_events_rss = (
        rss_metadata
        and rss_metadata.get("event_metadata")
        and "eventId=" in url
    )

    if is_events_rss:
        logger.info("EVENTS RSS DETECTED: Using event-specific question generation")
        return generate_events_question_section(url, title, target_language, rss_metadata)

    # Check if OpenRouter is configured
    if not cfg.OPENROUTER_API_KEY:
        logger.info("OpenRouter API key not configured, using fallback for QUESTION section")
        # Fallback: Simple keywords and questions from title/url/summary
        keywords: List[str] = title.split()[:3] if title else urlparse(url).path.split("/")[-2:]
        summary_keywords: List[str] = page_summary.split()[:3] if page_summary else []
        all_keywords = list(set(keywords + summary_keywords))[:3]
        fallback = " | ".join(
            all_keywords
            + [f"What is {title}?", f"Details about {title}", f"Information on {title}"]
        )
        return fallback

    # Create prompt for QUESTION generation, including page summary for better output
    summary_context = (
        f"\n\nPage Summary: {page_summary[:500]}..."
        if page_summary
        else "\n\nPage Summary: Not available"
    )
    prompt = f"""\U0001f916 KEYWORD AND QUESTION GENERATOR: Based primarily on the URL and title, refined by the page summary, generate:
- 3 most pertinent keywords (from URL path, title, and summary)
- 3 RAG-optimized, keyword-rich, open-ended questions for vector search/RAG

Output ONLY a single pipe-separated string: keyword1 | keyword2 | keyword3 | question1 | question2 | question3

URL: {url}
Title: {title}{summary_context}

Language: {target_language}

EXAMPLE OUTPUT (for URL: https://www.khk.cz/kraj/hejtman, Title: Hejtman | Kralovehradecky kraj):
Hejtman | Hejtman Kralovehradeckeho kraje | Kontakt hejtman | Kdo je hejtman? | Kdo je hejtmanem Kralovehradeckeho kraje? | Jake jsou kontaktni udaje hejtmana Kralovehradeckeho kraje?"""

    result = _call_openrouter(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
        call_name="QUESTION_SECTION_GENERATION",
        url=url,
        temperature=0.3,
    )

    if result and len(result) > 10:
        logger.info("Generated QUESTION section: %s...", result[:100])
        return result

    logger.warning("QUESTION generation failed or too short, using fallback")
    return _build_question_fallback(title, url)


def generate_events_question_section(
    url: str, title: str, target_language: str = "Czech",
    rss_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate event-specific QUESTION section using rich RSS event metadata."""
    cfg = get_config()
    logger.info("Generating EVENT-SPECIFIC QUESTION section for: %s", url)

    # Extract event metadata
    event_data: Dict[str, Any] = rss_metadata.get("event_metadata", {}) if rss_metadata else {}
    event_name: str = event_data.get("event_name", "Unknown Event")
    event_type: str = event_data.get("event_type", "Unknown")
    organizer: str = event_data.get("organizer", "Unknown")
    place: str = event_data.get("place", "Unknown")
    event_id: str = event_data.get("event_id", "Unknown")
    start_date: str = event_data.get("start_date", "Unknown")
    start_time: str = event_data.get("start_time", "Unknown")

    def _event_fallback() -> str:
        return (
            f"{event_name} | {event_type} | {organizer} | "
            f"Co je {event_name}? | Kdy se kona {event_name}? | Kdo organizuje {event_name}?"
        )

    # Check if OpenRouter is configured
    if not cfg.OPENROUTER_API_KEY:
        logger.info("OpenRouter API key not configured, using event-specific fallback")
        # Create event-specific fallback using RSS metadata
        keywords: List[str] = [event_name, event_type, organizer][:3]
        keywords = [k for k in keywords if k != "Unknown"][:3]

        # Add generic keywords if needed
        while len(keywords) < 3:
            keywords.extend(["udalost", "akce", "program"])
        keywords = keywords[:3]

        questions = [
            f"Co je {event_name}?",
            f"Kdy a kde se kona {event_name}?",
            f"Kdo organizuje {event_name}?",
        ]

        fallback = " | ".join(keywords + questions)
        logger.info("Event-specific fallback generated: %s...", fallback[:100])
        return fallback

    # Create event-specific prompt with rich metadata context
    event_context = f"""
Event Details:
- Event Name: {event_name}
- Event Type: {event_type}
- Organizer: {organizer}
- Place: {place}
- Date: {start_date}
- Time: {start_time}
- Event ID: {event_id}"""

    prompt = f"""\U0001f916 EVENT-SPECIFIC KEYWORD AND QUESTION GENERATOR: Based on the event metadata and enhanced title, generate event-focused search terms:
- 3 most relevant event keywords (event name, type, organizer, location)
- 3 RAG-optimized questions about this specific event

Output ONLY a single pipe-separated string: keyword1 | keyword2 | keyword3 | question1 | question2 | question3

URL: {url}
Enhanced Title: {title}{event_context}

Language: {target_language}

EXAMPLE OUTPUT (for Event: MUZEUM CTE DETEM, Type: ostatni, Organizer: Oblastni muzeum):
MUZEUM CTE DETEM | Oblastni muzeum | program pro deti | Co je akce MUZEUM CTE DETEM? | Kdy se kona MUZEUM CTE DETEM v Oblastnim muzeu? | Jake aktivity nabizi program MUZEUM CTE DETEM?"""

    result = _call_openrouter(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
        call_name="EVENTS_QUESTION_SECTION_GENERATION",
        url=url,
        temperature=0.3,
    )

    if result and len(result) > 10:
        logger.info("Generated EVENT-SPECIFIC QUESTION section: %s...", result[:100])
        return result

    logger.warning("Events QUESTION generation failed or too short, using event-specific fallback")
    return _event_fallback()


# ---------------------------------------------------------------------------
# File summary instruction functions (canonical versions)
# ---------------------------------------------------------------------------


def get_current_file_summary_instructions(
    file_order: int, total_files: int, target_language: str = "Czech",
    url: str = "", title: str = "",
) -> str:
    """Get instructions for generating a current-file summary within a chunked sequence."""
    # Language instruction for non-English targets
    language_instruction = (
        f"- **OUTPUT LANGUAGE:** {target_language}\n"
        if target_language.lower() != "english"
        else ""
    )

    return f"""\U0001f916 EXPERT CONTEXTUAL FILE CONTENT ANALYST: You are a specialist who creates FACTUAL summaries of chunked file content with PRECISE details about its position and content in the complete document sequence.

## \U0001f6a8 CRITICAL: VECTOR STORE TOKEN COMPLIANCE MANDATORY!
## \U0001f3af TASK: Create an EXTREMELY SHORT, CONCISE, FACTUAL summary (1-2 short sentences) of CURRENT FILE ({file_order}/{total_files}) with PRECISE CONTEXTUAL DETAILS.

{language_instruction}
### **\U0001f4cb CURRENT FILE SUMMARY REQUIREMENTS:**
- **LENGTH:** Maximum 1-2 extremely short sentences
- **FACTUAL PRECISION:** EXACT counts, numbers, dates, and structural details. **CRITICAL: ONLY use counts explicitly present in the source content. NEVER count elements yourself.**
- **\u274c FORBIDDEN:** DO NOT mention specific individual names, personal titles, detailed contact information (e.g., specific phone numbers or emails), or token counts.
- **CONTEXTUAL POSITIONING:** Clear explanation of this file's role in the {total_files}-file sequence.
- **CONTENT DENSITY:** Maximum factual information per word.

### **\U0001f4ca ADAPTIVE CONTENT ANALYSIS FOR FILE {file_order}/{total_files}:**
**For ANY content type, include these EXACT details:**
- **\U0001f4cd PRECISE POSITION:** Explicitly state "cast {file_order} z {total_files}"
- **\U0001f4ca QUANTIFIED CONTENT:** Exact counts of key elements (contacts, agenda items, documents, sections, etc.) **(ONLY if the count is explicitly stated in the source text)**
- **\U0001f3af STRUCTURAL COVERAGE:** Which specific sections, departments, topics, or categories THIS file covers
- **\U0001f517 SEQUENCE INTEGRATION:** How this file fits into the overall document flow
- **\U0001f4cb CONTENT BOUNDARIES:** Where this file starts and ends within the complete document structure

**Content-specific factual requirements:**
- **ORGANIZATIONAL LISTS:** Exact department/section names, employee counts, hierarchical positions
- **MEETING DOCUMENTS:** Specific agenda item numbers, topic categories, date/time details
- **CONTACT LISTS:** Exact number of contacts, organizational units, contact completeness
- **LEGAL DOCUMENTS:** Article/section numbers, regulatory references, effective dates
- **NEWS/EVENTS:** Specific dates, locations, participant counts, event details

### **\U0001f3af OUTPUT FORMAT:**
Provide ONLY the 2-3 paragraph summary in {target_language}. No additional formatting, no headings, just the factual summary paragraphs.

**ENHANCED EXAMPLE OUTPUT:**
Tento soubor predstavuje cast {file_order} z {total_files} celkoveho dokumentu "{title}" a obsahuje kompletni kontaktni udaje pro 23 zamestnancu v ramci 3 organizacnich jednotek: dokonceni oddeleni krizoveho rizeni (7 zamestnancu), cele oddeleni vnitrni a kyberneticke bezpecnosti (5 zamestnancu) a cele oddeleni prevence kriminality (2 zamestnanci), nasledne kompletni Odbor digitalizace s vedoucim a asistentkou plus 5 podrizenych oddeleni s celkem 15 zamestnanci. Pro kazdeho jsou uvedeny jmeno, funkce, telefonni cisla, e-mail a cislo mistnosti.

Organizacne soubor zacina dokoncenim kontaktu z oddeleni krizoveho rizeni (OBRKR) pokracujiciho z predchoziho souboru a systematicky pokryva vsechna dalsi oddeleni Odboru bezpecnosti, cely Odbor digitalizace vcetne vsech jeho specializovanych oddeleni (DIGSP, DIGCP, DIGDR, DIGDM, DIGDA), a zacina Odborem informatiky (INF). Jako {file_order}. cast z {total_files} poskytuje uplnou kontinuitu organizacni struktury bez mezer v hierarchickem pokryti krajskeho uradu."""


def get_overlap_summary_instructions(
    previous_file_order: int, current_file_order: int, total_files: int,
    target_language: str = "Czech", url: str = "", title: str = "",
) -> str:
    """Get instructions for generating an overlap summary bridging sequential files."""
    # Language instruction for non-English targets
    language_instruction = (
        f"- **OUTPUT LANGUAGE:** {target_language}\n"
        if target_language.lower() != "english"
        else ""
    )

    return f"""\U0001f916 EXPERT SEMANTIC BRIDGING SPECIALIST: You are a specialist who creates EXTREMELY CONCISE and PRECISE overlap summaries using ADVANCED TRANSITION ANALYSIS to describe exactly where the previous file ended and how the current file continues.

    ## \U0001f3af TASK: Create an EXTREMELY SHORT, CONCISE SEMANTIC BRIDGE (1-2 sentences) between PREVIOUS file ({previous_file_order}/{total_files}) and CURRENT file ({current_file_order}/{total_files}) using SYSTEMATIC BOUNDARY ANALYSIS.

    {language_instruction}
    ### **\U0001f4cb OVERLAP SUMMARY REQUIREMENTS:**
    - **LENGTH:** Maximum 1-2 extremely short sentences
    - **SEMANTIC PRECISION:** MUST mention the EXACT last entity (structural boundary, last name, or item number) of the previous file and the EXACT first entity of the current file.
    - **\u274c FORBIDDEN:** DO NOT mention specific individual names, personal titles, or token counts. Prioritize structural boundaries over names.
    - **CONTEXTUAL BRIDGE:** Strategic information transfer for seamless file continuity.
    - **BOUNDARY INTELLIGENCE:** Sophisticated analysis of content break points and connections.

    ### **\U0001f9e0 ADVANCED SEMANTIC TRANSITION METHODOLOGY:**

    **ANALYTICAL PHASE 1: ENDPOINT BOUNDARY ANALYSIS**
    Systematically identify WHERE and HOW the previous file concluded:
    - **STRUCTURAL ENDPOINT:** Last organizational unit, agenda item, document section, or content category.
    - **EXACT LAST ENTITY:** Identify the precise name, figure, or item number where the previous file ended.

    **ANALYTICAL PHASE 2: CONTINUATION POINT MAPPING**
    Identify EXACTLY how current file connects and continues:
    - **LOGICAL SUCCESSION:** Next immediate element in sequence (next employee, agenda item, section).
    - **EXACT FIRST ENTITY:** Identify the precise name, figure, or item number where the current file begins.

    **ANALYTICAL PHASE 3: CONTEXTUAL BRIDGE CONSTRUCTION**
    Create ESSENTIAL factual context for seamless understanding:
    - **REFERENCE ANCHORS:** Key organizational/structural positions for orientation.
    - **QUANTIFIED TRANSITIONS:** Specific counts and positions for precise navigation.

    ### **\U0001f4ca CONTENT-ADAPTIVE TRANSITION TECHNIQUES:**

    **\U0001f3e2 ORGANIZATIONAL TRANSITIONS:** Specific department->next department, EXACT employee position, hierarchical level maintenance.
    **\U0001f4cb PROCEDURAL TRANSITIONS:** Agenda item numbers, topic progression, decision flow continuity.
    **\U0001f465 CONTACT SEQUENCE TRANSITIONS:** Alphabetical position, organizational order, structural transition (use last name only if necessary).
    **\U0001f4d1 DOCUMENT STRUCTURE TRANSITIONS:** Article/section numbers, legal progression, regulatory sequence.
    **\U0001f5de\ufe0f TEMPORAL TRANSITIONS:** Event chronology, publication sequences, time-based organization.
    **\U0001f4ca DATA TRANSITIONS:** Table continuations, statistical categories, dataset boundaries.

    ### **\U0001f3af OUTPUT FORMAT:**
    Provide ONLY the 1-2 extremely short sentence overlap summary in {target_language}. No additional formatting, no headings, just the semantically bridged summary.

    **PROFESSIONAL EXAMPLE OUTPUT:**
    Predchozi soubor ({previous_file_order}/{total_files}) koncil profilem zastupitele Motesickeho (SPD), cimz uzavrel prvni cast detailniho vyctu. Aktualni soubor ({current_file_order}/{total_files}) plynule navazuje pokracovanim seznamu zastupitelu, zacinajicim profilem Vildumetzove (ANO), a pokracuje az do konce kompletniho seznamu."""
