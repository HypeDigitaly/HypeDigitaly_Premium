"""Budget-constrained metadata generation for GPT Scraper V3.

Migrated from V2 lines 2660-3272. Each budget-constrained generator uses a
``system`` message with ``temperature=0.0`` and enforces strict token limits.
Duplicated OpenRouter call boilerplate replaced by ``_call_openrouter()`` and
``_enforce_token_limit()``.  Fix M5: type hints on all public signatures.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.openrouter_client import _call_openrouter, _enforce_token_limit
from gpt_scraper_v3.utilities import count_tokens_approximate

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token allocation calculator
# ---------------------------------------------------------------------------

def calculate_metadata_token_allocation(
    metadata_budget: int,
    static_metadata_tokens: int = 100,
) -> Dict[str, int]:
    """Calculate optimal token allocation for dynamic metadata components.

    Distributes tokens among four dynamic sections after reserving space for
    static metadata.  Priority: page summary > file summary > overlap > questions.

    Args:
        metadata_budget: Total available tokens for metadata.
        static_metadata_tokens: Estimated tokens consumed by static fields.

    Returns:
        Token allocation mapping for each metadata component.
    """
    logger.info(
        "CALCULATING METADATA TOKEN ALLOCATION: budget=%d, static=%d",
        metadata_budget, static_metadata_tokens,
    )
    available_for_dynamic = metadata_budget - static_metadata_tokens

    if available_for_dynamic <= 0:
        logger.warning("No tokens available for dynamic metadata after static allocation!")
        return {
            "source_page_summary": 50, "current_file_summary": 50,
            "overlap_summary": 50, "question_section": 50,
        }

    # Priority distribution with per-section caps
    source_page_summary_tokens = min(available_for_dynamic // 4, 300)
    remaining = available_for_dynamic - source_page_summary_tokens
    current_file_summary_tokens = min(remaining // 3, 400)
    remaining -= current_file_summary_tokens
    overlap_summary_tokens = min(remaining // 2, 250)
    remaining -= overlap_summary_tokens
    question_section_tokens = remaining

    allocation: Dict[str, int] = {
        "source_page_summary": source_page_summary_tokens,
        "current_file_summary": current_file_summary_tokens,
        "overlap_summary": overlap_summary_tokens,
        "question_section": question_section_tokens,
        "static_metadata": static_metadata_tokens,
        "total_allocated": (
            static_metadata_tokens + source_page_summary_tokens
            + current_file_summary_tokens + overlap_summary_tokens
            + question_section_tokens
        ),
    }
    logger.info(
        "METADATA TOKEN ALLOCATION COMPLETE: page_summary=%d, file_summary=%d, "
        "overlap=%d, questions=%d, static=%d, total=%d (budget=%d)",
        allocation["source_page_summary"], allocation["current_file_summary"],
        allocation["overlap_summary"], allocation["question_section"],
        allocation["static_metadata"], allocation["total_allocated"], metadata_budget,
    )
    return allocation

# ---------------------------------------------------------------------------
# Budget-constrained page summary
# ---------------------------------------------------------------------------

def generate_budget_constrained_page_summary(
    markdown_content: str, max_tokens: int, title: str = "", url: str = "",
) -> Optional[str]:
    """Generate page summary with strict token budget constraints.

    Args:
        markdown_content: Original markdown content to analyse.
        max_tokens: Maximum tokens allowed for the summary.
        title: Page title for context.
        url: Source URL for context.

    Returns:
        Budget-constrained page summary, or ``None`` on failure.
    """
    cfg = get_config()
    if not cfg.OPENROUTER_API_KEY:
        logger.info("OpenRouter API key not configured, skipping page summary")
        return None
    target_language = cfg.OPENROUTER_TARGET_LANGUAGE
    logger.info("Generating budget-constrained page summary (max=%d tokens) for %s",
                max_tokens, url)

    system_msg = (
        "You are a professional content analyst specializing in ultra-concise, "
        "high-level summaries. You NEVER mention specific individual names, "
        "personal titles, or detailed information. You focus ONLY on structural "
        "information, counts, and content types. Your responses are extremely "
        "brief, factual, and SMS-like in length."
    )
    user_msg = (
        f"EXPERT PAGE CONTENT ANALYST: Create SHORT, FACTUAL summary about "
        f"entire webpage content for metadata with STRICT TOKEN BUDGET COMPLIANCE.\n\n"
        f"## CRITICAL: STRICT TOKEN BUDGET COMPLIANCE MANDATORY!\n"
        f"## TASK: Create 1-2 paragraph summary about the ENTIRE PAGE CONTENT\n\n"
        f"- **OUTPUT LANGUAGE:** {target_language}\n"
        f"- **CRITICAL TOKEN LIMIT:** {max_tokens} tokens ABSOLUTE MAXIMUM "
        f"(NON-NEGOTIABLE!)\n"
        f"- **LENGTH:** Maximum 1-2 paragraphs\n"
        f"- **FACTUAL DENSITY:** Maximum essential information per token\n"
        f"- **PURPOSE:** Provide context for understanding across chunked files\n\n"
        f"### **MANDATORY NUMERICAL DATA HANDLING:**\n"
        f"- **EXPLICIT COUNTS ONLY:** Only use counts explicitly stated in the "
        f"source content. NEVER count elements yourself. Use estimates "
        f"(dozens/hundreds) if the count is missing.\n\n"
        f"## SOURCE CONTENT TO ANALYZE:\n{markdown_content}"
    )

    result = _call_openrouter(
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=max_tokens + 50,
        call_name=f"BUDGET_PAGE_SUMMARY_{max_tokens}T",
        url=url, temperature=0.0,
    )
    if result:
        result = _enforce_token_limit(result, max_tokens=max_tokens)
        if result:
            logger.info("Generated budget page summary: %d tokens (budget=%d)",
                        count_tokens_approximate(result), max_tokens)
    return result

# ---------------------------------------------------------------------------
# Budget-constrained current file summary
# ---------------------------------------------------------------------------

def generate_budget_constrained_current_file_summary(
    file_content: str, file_order: int, total_files: int,
    max_tokens: int, title: str = "", url: str = "",
) -> Optional[str]:
    """Generate current file summary with strict token budget constraints.

    Args:
        file_content: Content of the current file to summarise.
        file_order: Current file position (1-based).
        total_files: Total number of files in sequence.
        max_tokens: Maximum tokens allowed for the summary.
        title: Page title for context.
        url: Source URL for context.

    Returns:
        Budget-constrained current file summary, or ``None`` on failure.
    """
    cfg = get_config()
    if not cfg.OPENROUTER_API_KEY:
        logger.info("OpenRouter API key not configured, skipping current file summary")
        return None
    target_language = cfg.OPENROUTER_TARGET_LANGUAGE
    logger.info("Generating budget-constrained current file summary (%d/%d, max=%d tokens)",
                file_order, total_files, max_tokens)

    system_msg = (
        "You are a professional file content analyst specializing in ultra-concise "
        "file summaries within document sequences. You NEVER mention specific "
        "individual names, personal titles, or detailed information. You focus "
        "ONLY on file position, structural boundaries, and content counts. Your "
        "responses are extremely brief, factual, and SMS-like in length."
    )
    user_msg = (
        f"EXPERT CONTEXTUAL FILE ANALYST: Create ULTRA-SHORT summary of current "
        f"file in sequence.\n\n"
        f"## TASK: Create 1-2 SHORT sentences about CURRENT FILE "
        f"({file_order}/{total_files})\n\n"
        f"- **OUTPUT LANGUAGE:** {target_language}\n"
        f"- **CRITICAL TOKEN LIMIT:** {max_tokens} tokens MAXIMUM\n"
        f"- **LENGTH:** Maximum 1-2 short sentences (like SMS messages)\n"
        f"- **ABSOLUTELY FORBIDDEN:** DO NOT mention any specific individual "
        f"names, personal titles, or detailed information\n"
        f"- **FOCUS:** Only file position, overall counts, and general content type\n\n"
        f"### **WHAT TO INCLUDE:**\n"
        f'- File position: "cast {file_order} z {total_files}"\n'
        f"- Total number of items/people in THIS file (numbers only) "
        f"**(ONLY if the number is explicitly stated in the source content. "
        f"NEVER count elements yourself.)**\n"
        f"- Basic content type and structure\n\n"
        f"### **WHAT TO NEVER MENTION:**\n"
        f"- Individual names or surnames\n- Specific department names\n"
        f"- Personal titles (Ing., Mgr., etc.)\n- Any detailed information\n\n"
        f"### **OUTPUT FORMAT:**\n"
        f"Provide ONLY the 1-2 extremely short sentences in {target_language}. "
        f"No formatting, no headings.\n\n"
        f"## CURRENT FILE CONTENT TO ANALYZE (File {file_order}/{total_files}):\n"
        f"{file_content}"
    )

    result = _call_openrouter(
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=max_tokens + 50,
        call_name=f"BUDGET_CURRENT_FILE_SUMMARY_{file_order}_{total_files}_{max_tokens}T",
        url=url, temperature=0.0,
    )
    if result:
        result = _enforce_token_limit(result, max_tokens=max_tokens)
        if result:
            logger.info("Generated budget current file summary: %d tokens (budget=%d)",
                        count_tokens_approximate(result), max_tokens)
    return result

# ---------------------------------------------------------------------------
# Budget-constrained overlap summary
# ---------------------------------------------------------------------------

def generate_budget_constrained_overlap_summary(
    original_page_summary: str, previous_file_summary: str,
    previous_file_content: str, previous_file_order: int,
    current_file_order: int, total_files: int,
    max_tokens: int, title: str = "", url: str = "",
) -> Optional[str]:
    """Generate overlap summary bridging previous and current file.

    Args:
        original_page_summary: Summary of the entire original page.
        previous_file_summary: Summary of the previous file.
        previous_file_content: Full content of the previous file.
        previous_file_order: Previous file position (1-based).
        current_file_order: Current file position (1-based).
        total_files: Total number of files in sequence.
        max_tokens: Maximum tokens allowed for the summary.
        title: Page title for context.
        url: Source URL for context.

    Returns:
        Budget-constrained overlap summary, or ``None`` on failure.
    """
    cfg = get_config()
    if not cfg.OPENROUTER_API_KEY:
        logger.info("OpenRouter API key not configured, skipping overlap summary")
        return None
    target_language = cfg.OPENROUTER_TARGET_LANGUAGE
    logger.info("Generating budget-constrained overlap summary (prev:%d->curr:%d, max=%d tokens)",
                previous_file_order, current_file_order, max_tokens)

    system_msg = (
        "You are a professional semantic bridging specialist creating ultra-concise "
        "transition summaries between file sequences. You NEVER mention specific "
        "individual names, personal titles, or detailed information. You focus ONLY "
        "on structural boundaries, file positions, and content flow connections. "
        "Your responses are extremely brief, factual, and SMS-like in length."
    )
    user_msg = (
        f"EXPERT SEMANTIC BRIDGING SPECIALIST: Create PRECISE overlap summary "
        f"with STRICT TOKEN BUDGET COMPLIANCE.\n\n"
        f"## CRITICAL: STRICT TOKEN BUDGET COMPLIANCE MANDATORY!\n"
        f"## TASK: Create SEMANTIC BRIDGE between PREVIOUS file "
        f"({previous_file_order}/{total_files}) and CURRENT file "
        f"({current_file_order}/{total_files})\n\n"
        f"- **OUTPUT LANGUAGE:** {target_language}\n"
        f"- **CRITICAL TOKEN LIMIT:** {max_tokens} tokens ABSOLUTE MAXIMUM "
        f"(NON-NEGOTIABLE!)\n"
        f"- **LENGTH:** Maximum 1-2 paragraphs\n"
        f"- **SEMANTIC PRECISION:** EXACT transition details with logical flow "
        f"preservation. **CRITICAL: Any numerical data used (e.g., agenda item "
        f"numbers, department counts) MUST be explicitly present in the source "
        f"content (PREVIOUS FILE CONTENT section). NEVER count elements yourself.**\n\n"
        f"## INPUT DATA FOR OVERLAP ANALYSIS:\n\n"
        f"### ORIGINAL PAGE SUMMARY:\n"
        f"{original_page_summary or 'Not available'}\n\n"
        f"### PREVIOUS FILE SUMMARY (File {previous_file_order}/{total_files}):\n"
        f"{previous_file_summary or 'Not available'}\n\n"
        f"### PREVIOUS FILE CONTENT (File {previous_file_order}/{total_files}) "
        f"- ANALYZE HOW IT ENDED:\n{previous_file_content}"
    )

    result = _call_openrouter(
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=max_tokens + 50,
        call_name=(f"BUDGET_OVERLAP_SUMMARY_{previous_file_order}"
                   f"_TO_{current_file_order}_{max_tokens}T"),
        url=url, temperature=0.0,
    )
    if result:
        result = _enforce_token_limit(result, max_tokens=max_tokens)
        if result:
            logger.info("Generated budget overlap summary: %d tokens (budget=%d)",
                        count_tokens_approximate(result), max_tokens)
    return result

# ---------------------------------------------------------------------------
# Budget-constrained question section
# ---------------------------------------------------------------------------

def generate_budget_constrained_question_section(
    url: str, title: str, max_tokens: int = 200,
    page_summary: Optional[str] = None,
    rss_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate question section with strict token budget constraints.

    Falls back to a deterministic keyword string when the API key is missing
    or the API response is too short.

    Args:
        url: Source URL.
        title: Page title.
        max_tokens: Maximum tokens allowed for the question section.
        page_summary: Optional page summary for enhanced context.
        rss_metadata: Optional RSS metadata for enhanced event context.

    Returns:
        Budget-constrained question section string (always non-empty).
    """
    cfg = get_config()
    target_language = cfg.OPENROUTER_TARGET_LANGUAGE
    logger.info("Generating budget-constrained question section (max=%d tokens) for %s",
                max_tokens, url)

    is_events_rss = (
        rss_metadata and rss_metadata.get("event_metadata") and "eventId=" in url
    )

    def _fallback() -> str:
        if is_events_rss:
            event_data = rss_metadata.get("event_metadata", {})  # type: ignore[union-attr]
            name = event_data.get("event_name", title)
            text = (f"{name} | akce | udalost | Co je {name}? | "
                    f"Kdy se kona {name}? | Kdo organizuje {name}?")
        else:
            text = (f"{title} | informace | kontakt | Co je {title}? | "
                    f"Informace o {title} | Kontakt na {title}")
        return text[: max_tokens * 4]

    if not cfg.OPENROUTER_API_KEY:
        logger.info("OpenRouter API key not configured, using fallback for question section")
        return _fallback()

    # Build prompt
    summary_context = (f"\nPage Summary: {page_summary[:300]}..."
                       if page_summary else "\nPage Summary: Not available")
    if is_events_rss:
        event_data = rss_metadata.get("event_metadata", {})  # type: ignore[union-attr]
        event_context = (
            f"\nEvent Details:\n"
            f"- Event Name: {event_data.get('event_name', 'Unknown')}\n"
            f"- Event Type: {event_data.get('event_type', 'Unknown')}\n"
            f"- Organizer: {event_data.get('organizer', 'Unknown')}")
        user_msg = (
            f"EVENT-SPECIFIC KEYWORD AND QUESTION GENERATOR: Generate "
            f"event-focused search terms with STRICT TOKEN BUDGET.\n\n"
            f"## CRITICAL TOKEN LIMIT: {max_tokens} tokens MAXIMUM\n"
            f"Generate: 3 event keywords | 3 RAG-optimized questions\n"
            f"Output ONLY pipe-separated string.\n\n"
            f"URL: {url}\nTitle: {title}{event_context}{summary_context}\n"
            f"Language: {target_language}")
    else:
        user_msg = (
            f"KEYWORD AND QUESTION GENERATOR: Generate search terms with "
            f"STRICT TOKEN BUDGET.\n\n"
            f"## CRITICAL TOKEN LIMIT: {max_tokens} tokens MAXIMUM\n"
            f"Generate: 3 keywords | 3 RAG-optimized questions\n"
            f"Output ONLY pipe-separated string.\n\n"
            f"URL: {url}\nTitle: {title}{summary_context}\n"
            f"Language: {target_language}")

    system_msg = (
        "You are a professional keyword and question generator specializing in "
        "ultra-concise, RAG-optimized search terms. You create pipe-separated "
        "keywords and questions without any additional formatting or "
        "explanations. Your output is extremely brief and focused."
    )
    result = _call_openrouter(
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=max_tokens + 30,
        call_name=f"BUDGET_QUESTION_SECTION_{max_tokens}T",
        url=url, temperature=0.0,
    )
    if result and len(result) > 10:
        enforced = _enforce_token_limit(result, max_tokens=max_tokens)
        if enforced:
            logger.info("Generated budget question section: %d tokens (budget=%d)",
                        count_tokens_approximate(enforced), max_tokens)
            return enforced
    logger.warning("Question section API response too short or empty, using fallback")
    return _fallback()

# ---------------------------------------------------------------------------
# Budget-constrained metadata header assembly
# ---------------------------------------------------------------------------

def create_budget_constrained_metadata_header(
    url: str, title: str,
    last_modified: Optional[datetime] = None,
    path: Optional[str] = None,
    rss_metadata: Optional[Dict[str, Any]] = None,
    source_page_summary: Optional[str] = None,
    file_order: Optional[int] = None,
    total_files: Optional[int] = None,
    current_file_summary: Optional[str] = None,
    overlap_summary: Optional[str] = None,
) -> str:
    """Assemble metadata header with static fields and optional dynamic summaries.

    Args:
        url: Source URL of the content.
        title: Page title.
        last_modified: Last modification date from sitemap.
        path: Navigation path from sitemap.
        rss_metadata: RSS-specific metadata.
        source_page_summary: Source page summary text.
        file_order: Current file position in sequence (1-based).
        total_files: Total number of files in sequence.
        current_file_summary: Summary of the current file.
        overlap_summary: Overlap summary text.

    Returns:
        Assembled metadata header string.
    """
    # Format last modified date
    last_mod_str = "Unknown"
    if last_modified:
        if hasattr(last_modified, "strftime"):
            last_mod_str = last_modified.strftime("%Y-%m-%d %H:%M:%S")
        else:
            last_mod_str = str(last_modified)

    metadata_lines: List[str] = [
        "## ZDROJOVA URL:", f"### **{url}**", "",
        "## TITULEK:", f"### **{title or 'N/A'}**", "",
        "## DATUM POSLEDNI MODIFIKACE:", f"### **{last_mod_str}**", "",
    ]

    if file_order is not None and total_files is not None:
        metadata_lines.extend([
            "## FILE ORDER:", f"### **File {file_order} of {total_files}**", "",
        ])
    if source_page_summary and source_page_summary.strip():
        metadata_lines.extend([
            "## SOURCE PAGE SUMMARY:", f"### **{source_page_summary.strip()}**", "",
        ])
    if current_file_summary and current_file_summary.strip():
        metadata_lines.extend([
            "## CURRENT FILE SUMMARY:", f"### **{current_file_summary.strip()}**", "",
        ])
    if overlap_summary and overlap_summary.strip():
        metadata_lines.extend([
            "## PREVIOUS FILE OVERLAP SUMMARY:", f"### **{overlap_summary.strip()}**", "",
        ])
    metadata_lines.extend(["---", "", ""])
    return "\n".join(metadata_lines)
