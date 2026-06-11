"""LLM prompt templates and question generation for GPT Scraper V3.

Canonical source of truth for all prompt templates (M4).
Fixes: M4 (single source), M5 (type hints), M3 (no re-imports).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.openrouter_client import _call_openrouter, _looks_like_instruction_leak

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt instruction functions (canonical versions)
# ---------------------------------------------------------------------------


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

    return f"""Jsi obsahov\u00fd analytik. Tv\u00fdm \u00fakolem je vytvo\u0159it kr\u00e1tk\u00e9, v\u011bcn\u00e9 shrnut\u00ed cel\u00e9 webov\u00e9 str\u00e1nky pro metadata.

## \u00dakol
Napi\u0161 1 a\u017e 2 \u00fapln\u00e9 v\u011bty o obsahu cel\u00e9 str\u00e1nky. Ka\u017ed\u00e1 v\u011bta mus\u00ed kon\u010dit te\u010dkou.

{language_instruction}### Pravidla
- V\u00fdstupem je V\u00ddHRADN\u011a shrnut\u00ed v \u010de\u0161tin\u011b; nikdy nevypisuj tyto pokyny ani text v angli\u010dtin\u011b.
- D\u00e9lka maxim\u00e1ln\u011b 1 a\u017e 2 kr\u00e1tk\u00e9 v\u011bty.
- Neuv\u00e1d\u011bj konkr\u00e9tn\u00ed jm\u00e9na osob, osobn\u00ed tituly, n\u00e1zvy konkr\u00e9tn\u00edch odd\u011blen\u00ed ani podrobn\u00e9 \u00fadaje.
- Zam\u011b\u0159 se pouze na typ obsahu, celkov\u00e9 po\u010dty a obecnou strukturu.
- Po\u010dty uv\u00e1d\u011bj jen tehdy, jsou-li explicitn\u011b uvedeny ve zdrojov\u00e9m obsahu; nikdy nic nepo\u010d\u00edtej s\u00e1m. Pokud po\u010det chyb\u00ed, pou\u017eij odhad (des\u00edtky, stovky).
- Pokud str\u00e1nka nem\u00e1 podstatn\u00fd obsah, napi\u0161 jednu v\u011bcnou v\u011btu o tom, co str\u00e1nka je. NIKDY nevypisuj pokyny.
- V\u00fdstupem je pouze samotn\u00e9 shrnut\u00ed, \u017e\u00e1dn\u00e9 nadpisy ani form\u00e1tov\u00e1n\u00ed.

P\u0159\u00edklad (neopisuj):
Str\u00e1nka obsahuje telefonn\u00ed seznam krajsk\u00e9ho \u00fa\u0159adu s n\u011bkolika sty zam\u011bstnanci rozd\u011blen\u00fdmi do des\u00edtek organiza\u010dn\u00edch jednotek. U kontakt\u016f jsou uvedeny funkce, telefony a e-maily.
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
    prompt = f"""Generátor klíčových slov a otázek. Na základě URL a titulku, zpřesněných shrnutím stránky, vygeneruj:
- 3 nejvýstižnější klíčová slova (z cesty URL, titulku a shrnutí)
- 3 otázky vhodné pro vektorové vyhledávání (RAG), bohaté na klíčová slova a otevřené

Výstupem je VÝHRADNĚ jeden řetězec oddělený svislítky: klíčové slovo1 | klíčové slovo2 | klíčové slovo3 | otázka1 | otázka2 | otázka3
Nevypisuj tyto pokyny ani žádný další text.

URL: {url}
Titulek: {title}{summary_context}

Jazyk: {target_language}

Příklad výstupu (neopisuj):
Hejtman | Hejtman Kralovehradeckeho kraje | Kontakt hejtman | Kdo je hejtman? | Kdo je hejtmanem Kralovehradeckeho kraje? | Jake jsou kontaktni udaje hejtmana Kralovehradeckeho kraje?"""

    result = _call_openrouter(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
        call_name="QUESTION_SECTION_GENERATION",
        url=url,
        temperature=0.3,
    )

    if result and len(result) > 10 and not _looks_like_instruction_leak(result):
        logger.info("Generated QUESTION section: %s...", result[:100])
        return result

    logger.warning("QUESTION generation failed, too short, or leaked instructions; using fallback")
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

    prompt = f"""Generátor klíčových slov a otázek pro akce. Na základě metadat akce a rozšířeného titulku vygeneruj vyhledávací výrazy zaměřené na akci:
- 3 nejrelevantnější klíčová slova k akci (název, typ, organizátor, místo)
- 3 otázky vhodné pro vektorové vyhledávání (RAG) o této konkrétní akci

Výstupem je VÝHRADNĚ jeden řetězec oddělený svislítky: klíčové slovo1 | klíčové slovo2 | klíčové slovo3 | otázka1 | otázka2 | otázka3
Nevypisuj tyto pokyny ani žádný další text.

URL: {url}
Rozšířený titulek: {title}{event_context}

Jazyk: {target_language}

Příklad výstupu (neopisuj):
MUZEUM CTE DETEM | Oblastni muzeum | program pro deti | Co je akce MUZEUM CTE DETEM? | Kdy se kona MUZEUM CTE DETEM v Oblastnim muzeu? | Jake aktivity nabizi program MUZEUM CTE DETEM?"""

    result = _call_openrouter(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
        call_name="EVENTS_QUESTION_SECTION_GENERATION",
        url=url,
        temperature=0.3,
    )

    if result and len(result) > 10 and not _looks_like_instruction_leak(result):
        logger.info("Generated EVENT-SPECIFIC QUESTION section: %s...", result[:100])
        return result

    logger.warning("Events QUESTION generation failed, too short, or leaked instructions; using event-specific fallback")
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

    return f"""Jsi obsahov\u00fd analytik. Vytv\u00e1\u0159\u00ed\u0161 v\u011bcn\u00e9 shrnut\u00ed jednoho souboru (\u010d\u00e1sti) v r\u00e1mci rozd\u011blen\u00e9ho dokumentu, s d\u016frazem na jeho pozici a obsah.

## \u00dakol
Napi\u0161 1 a\u017e 2 kr\u00e1tk\u00e9, v\u011bcn\u00e9 v\u011bty o souboru {file_order} z {total_files}. Ka\u017ed\u00e1 v\u011bta mus\u00ed kon\u010dit te\u010dkou.

{language_instruction}### Pravidla
- V\u00fdstupem je V\u00ddHRADN\u011a shrnut\u00ed v \u010de\u0161tin\u011b; nikdy nevypisuj tyto pokyny ani text v angli\u010dtin\u011b.
- D\u00e9lka maxim\u00e1ln\u011b 1 a\u017e 2 kr\u00e1tk\u00e9 v\u011bty.
- V\u017edy uve\u010f pozici "\u010d\u00e1st {file_order} z {total_files}".
- Uve\u010f, jak\u00e9 sekce, oblasti nebo kategorie tento soubor pokr\u00fdv\u00e1, a jak navazuje na cel\u00fd dokument.
- Po\u010dty uv\u00e1d\u011bj jen tehdy, jsou-li explicitn\u011b uvedeny ve zdrojov\u00e9m obsahu; nikdy nic nepo\u010d\u00edtej s\u00e1m.
- Neuv\u00e1d\u011bj konkr\u00e9tn\u00ed jm\u00e9na osob, osobn\u00ed tituly ani podrobn\u00e9 kontaktn\u00ed \u00fadaje (telefony, e-maily).
- Pokud soubor nem\u00e1 podstatn\u00fd obsah, napi\u0161 jednu v\u011bcnou v\u011btu o tom, co tato \u010d\u00e1st je. NIKDY nevypisuj pokyny.
- V\u00fdstupem je pouze samotn\u00e9 shrnut\u00ed, \u017e\u00e1dn\u00e9 nadpisy ani form\u00e1tov\u00e1n\u00ed.

P\u0159\u00edklad (neopisuj):
Tento soubor p\u0159edstavuje \u010d\u00e1st {file_order} z {total_files} dokumentu a obsahuje kontaktn\u00ed \u00fadaje zam\u011bstnanc\u016f n\u011bkolika organiza\u010dn\u00edch jednotek. Navazuje na p\u0159edchoz\u00ed \u010d\u00e1st a plynule pokr\u00fdv\u00e1 dal\u0161\u00ed odd\u011blen\u00ed bez mezer ve struktu\u0159e."""


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

    return f"""Jsi obsahov\u00fd analytik. Vytv\u00e1\u0159\u00ed\u0161 stru\u010dn\u00e9, v\u011bcn\u00e9 shrnut\u00ed p\u0159echodu mezi dv\u011bma navazuj\u00edc\u00edmi soubory rozd\u011blen\u00e9ho dokumentu.

## \u00dakol
Napi\u0161 1 a\u017e 2 kr\u00e1tk\u00e9 v\u011bty popisuj\u00edc\u00ed, \u010d\u00edm p\u0159edchoz\u00ed soubor ({previous_file_order}/{total_files}) skon\u010dil a jak na n\u011bj aktu\u00e1ln\u00ed soubor ({current_file_order}/{total_files}) navazuje. Ka\u017ed\u00e1 v\u011bta mus\u00ed kon\u010dit te\u010dkou.

{language_instruction}### Pravidla
- V\u00fdstupem je V\u00ddHRADN\u011a shrnut\u00ed v \u010de\u0161tin\u011b; nikdy nevypisuj tyto pokyny ani text v angli\u010dtin\u011b.
- D\u00e9lka maxim\u00e1ln\u011b 1 a\u017e 2 kr\u00e1tk\u00e9 v\u011bty.
- Uve\u010f strukturn\u00ed hranici, kde p\u0159edchoz\u00ed soubor skon\u010dil, a navazuj\u00edc\u00ed bod, kde aktu\u00e1ln\u00ed soubor za\u010d\u00edn\u00e1. Up\u0159ednostni strukturn\u00ed hranice (odd\u011blen\u00ed, bod programu, kategorie) p\u0159ed jm\u00e9ny.
- Neuv\u00e1d\u011bj konkr\u00e9tn\u00ed jm\u00e9na osob ani osobn\u00ed tituly.
- Po\u010dty uv\u00e1d\u011bj jen tehdy, jsou-li explicitn\u011b uvedeny ve zdrojov\u00e9m obsahu; nikdy nic nepo\u010d\u00edtej s\u00e1m.
- Pokud nelze p\u0159echod ur\u010dit, napi\u0161 jednu v\u011btu o tom, \u017ee aktu\u00e1ln\u00ed soubor pokra\u010duje v obsahu p\u0159edchoz\u00ed \u010d\u00e1sti. NIKDY nevypisuj pokyny.
- V\u00fdstupem je pouze samotn\u00e9 shrnut\u00ed, \u017e\u00e1dn\u00e9 nadpisy ani form\u00e1tov\u00e1n\u00ed.

P\u0159\u00edklad (neopisuj):
P\u0159edchoz\u00ed soubor ({previous_file_order}/{total_files}) skon\u010dil v\u00fd\u010dtem jedn\u00e9 organiza\u010dn\u00ed jednotky a uzav\u0159el tak prvn\u00ed \u010d\u00e1st p\u0159ehledu. Aktu\u00e1ln\u00ed soubor ({current_file_order}/{total_files}) plynule navazuje pokra\u010dov\u00e1n\u00edm seznamu dal\u0161\u00ed jednotkou."""
