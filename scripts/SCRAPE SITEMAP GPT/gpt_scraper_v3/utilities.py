"""Shared utility module for GPT Scraper V3.

Provides URL helpers, filename generation, session management, and token counting.
Migrated from scrape_sitemap_GPT_v2.py with the following fixes applied:
  - H5: Session singleton with atexit cleanup
  - H6: Path traversal protection in sanitize_filename()
  - H7: Czech-calibrated token counting with optional tiktoken
  - M3: No redundant function-local re-imports
  - M5: Type hints on all public function signatures
  - M6: Eliminated getattr(globals()) pattern
  - L1: Removed .aspx/.jsp/.php from primary FILE_EXTENSIONS_TO_SKIP
"""
from __future__ import annotations

import atexit
import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from gpt_scraper_v3.config import get_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# H5 fix: Session singleton (replaces requests_retry_session)
# ---------------------------------------------------------------------------

_session: Optional[requests.Session] = None


def get_session() -> requests.Session:
    """Get or create the shared requests session with retry configuration.

    Unlike the V2 ``requests_retry_session()`` which created a **new**
    ``Session`` on every call, this function returns a module-level singleton
    and registers ``atexit`` cleanup exactly once.
    """
    global _session
    if _session is None:
        cfg = get_config()
        _session = requests.Session()
        retry = Retry(
            total=cfg.REQUEST_RETRY_COUNT,
            read=cfg.REQUEST_RETRY_COUNT,
            connect=cfg.REQUEST_RETRY_COUNT,
            backoff_factor=cfg.REQUEST_BACKOFF_FACTOR,
            status_forcelist=cfg.REQUEST_RETRY_CODES,
        )
        adapter = HTTPAdapter(max_retries=retry)
        _session.mount("http://", adapter)
        _session.mount("https://", adapter)
        atexit.register(_session.close)
    return _session


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def normalize_url_query_params(url: str) -> str:
    """Normalize a URL by sorting its query parameters alphabetically."""
    parsed = urlparse(url)
    if not parsed.query:
        return url

    query_params = parse_qs(parsed.query, keep_blank_values=True)
    sorted_query = urlencode(sorted(query_params.items()), doseq=True)
    return urlunparse(parsed._replace(query=sorted_query))


def is_file_url(url: str) -> bool:
    """Check if a URL points directly to a downloadable file.

    Returns ``True`` when the URL's path ends with a known binary/document
    extension **or** when a file-handler pattern is detected in combination
    with download-related query parameters.

    Fix L1: ``.aspx``, ``.jsp``, and ``.php`` are removed from the primary
    skip set because they are valid web-application URLs that should be
    scraped.  They are still caught by the file-handler heuristic below when
    paired with download-style query parameters.
    """
    # Primary file extensions to skip (L1: .aspx/.jsp/.php removed)
    FILE_EXTENSIONS_TO_SKIP = (
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz",
        ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
        ".mp4", ".mp3", ".avi", ".mov", ".wmv",
        ".csv", ".xml", ".json", ".txt", ".rtf",
        ".ashx",
    )

    parsed_url = urlparse(url.lower())
    path = parsed_url.path
    query = parsed_url.query

    # Check path extension
    if path.endswith(FILE_EXTENSIONS_TO_SKIP):
        logger.info(
            "Skipping URL: %s (File extension detected: %s)",
            url, path.rsplit(".", 1)[-1],
        )
        return True

    # File-handler heuristic: handler extension + download-related query params
    file_handlers = (".ashx", ".aspx", ".jsp", ".php")
    if any(handler in path for handler in file_handlers) and (
        "id_dokumenty" in query or "file" in query or "download" in query
    ):
        logger.info(
            "Skipping URL: %s (File handler pattern detected in path/query)", url,
        )
        return True

    return False


def is_url_blacklisted_by_path(
    url: str, blacklisted_paths: List[str],
) -> bool:
    """Check if *url* matches any blacklisted relative path prefix."""
    parsed = urlparse(url)
    url_path = unquote(parsed.path).rstrip("/")
    for bl_path in blacklisted_paths:
        normalized_bl = unquote(bl_path).rstrip("/")
        if not normalized_bl:
            continue
        if url_path == normalized_bl or url_path.startswith(normalized_bl + "/"):
            logger.info(
                "Skipping URL %s: Matches blacklisted relative path %s",
                url, bl_path,
            )
            return True
    return False


# ---------------------------------------------------------------------------
# String / filename helpers
# ---------------------------------------------------------------------------


def remove_accents(input_str: str) -> str:
    """Remove diacritical marks (accents) from *input_str*."""
    nfkd_form = unicodedata.normalize("NFKD", input_str)
    return "".join(c for c in nfkd_form if not unicodedata.combining(c))


def sanitize_filename(filename: str) -> str:
    """Sanitize *filename* for safe filesystem usage.

    Fix H6: prevents path traversal via ``..`` sequences and strips leading/
    trailing dots in addition to underscores.
    """
    filename = remove_accents(filename)
    filename = filename.replace("..", "_")  # H6: prevent traversal
    filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
    filename = re.sub(r"\s+", "_", filename)
    filename = re.sub(r"_+", "_", filename)
    filename = filename.strip("_.")  # H6: also strip dots
    cfg = get_config()
    return filename[: cfg.MAX_FILENAME_LENGTH]


# ---------------------------------------------------------------------------
# Chunk postfix helper (extracted from V2 nested function)
# ---------------------------------------------------------------------------


def get_chunk_postfix(chunk_number: int, total_chunks: int = 0) -> str:
    """Generate an alphabetical chunk postfix string for multi-chunk files.

    Single letters ``A``--``Z`` are used for the first 26 chunks.  Beyond that
    the scheme extends to ``AA``, ``AB``, etc.

    Args:
        chunk_number: Zero-based chunk index.
        total_chunks: Total number of chunks (informational, not used in logic).

    Returns:
        A string like ``"A"``, ``"Z"``, ``"AA"``, ``"AB"``, etc.
    """
    if chunk_number < 26:
        return chr(ord("A") + chunk_number)
    first_letter = chr(ord("A") + (chunk_number // 26) - 1)
    second_letter = chr(ord("A") + (chunk_number % 26))
    return first_letter + second_letter


# ---------------------------------------------------------------------------
# Filename generation from URL
# ---------------------------------------------------------------------------

# Czech diacritics character class used in event-name cleaning regex
_CZECH_CHARS = r"áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ"


def _clean_event_name(raw_name: str) -> str:
    """Normalise a raw event name (or title) into a filename-safe string."""
    cleaned = re.sub(rf"[^\w\s\-\u2013{_CZECH_CHARS}]", "_", raw_name)
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned


def create_filename_from_url(
    url: str,
    title: str = "",
    rss_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Create a base filename from URL path segments.

    Supports enhanced naming for Events XML / RSS URLs that carry an
    ``eventId`` query parameter.

    Args:
        url: Source URL to derive the filename from.
        title: Optional page title used for event filenames or fallback.
        rss_metadata: Optional RSS metadata dict for event naming.

    Returns:
        A sanitised base filename **without** extension or postfixes.
    """
    try:
        parsed_url = urlparse(url)
        path_parts = [
            part for part in parsed_url.path.split("/") if part.strip()
        ]

        # --- Special handling: Events XML URLs with eventId parameter ---
        if parsed_url.query and "eventId=" in parsed_url.query:
            query_params = parse_qs(parsed_url.query)
            event_id = query_params.get("eventId", [None])[0]

            if event_id:
                # Prefer RSS metadata event name
                if rss_metadata and rss_metadata.get("event_metadata"):
                    event_data = rss_metadata["event_metadata"]
                    event_name = event_data.get("event_name", "").strip()
                    if event_name:
                        clean_name = _clean_event_name(event_name)
                        event_filename = sanitize_filename(
                            f"Event_{event_id}_{clean_name}"
                        )
                        logger.debug(
                            "Created Events RSS filename: %s -> %s",
                            url, event_filename,
                        )
                        return event_filename

                # Fallback: derive name from title
                if title and title.strip():
                    clean_title = title.strip()
                    # Remove trailing date/time like "(25.09.2025 16:00)"
                    clean_title = re.sub(
                        r"\s*\([^)]*\d{2}\.\d{2}\.\d{4}[^)]*\)$",
                        "",
                        clean_title,
                    )
                    clean_title = _clean_event_name(clean_title)
                    event_filename = sanitize_filename(
                        f"Event_{event_id}_{clean_title}"
                    )
                    logger.debug(
                        "Created Events XML filename: %s -> %s",
                        url, event_filename,
                    )
                    return event_filename

                # Last resort: eventId only
                event_filename = f"Event_{event_id}"
                logger.debug(
                    "Created Events XML filename (no title): %s -> %s",
                    url, event_filename,
                )
                return sanitize_filename(event_filename)

        # --- No path segments: homepage fallback ---
        if not path_parts:
            if title and title.strip():
                return sanitize_filename(title)
            return "Homepage"

        # --- Standard path-based filename ---
        processed_parts: List[str] = []
        for part in path_parts:
            part = unquote(part)
            # Strip file extension
            if "." in part:
                part_without_ext = part.rsplit(".", 1)[0]
                if part_without_ext:
                    part = part_without_ext
            # Normalise separators and title-case
            clean_part = part.replace("-", " ").replace("_", " ").replace(".", " ")
            title_case_part = clean_part.title().replace(" ", "_")
            if title_case_part:
                processed_parts.append(title_case_part)

        if processed_parts:
            base_filename = "_".join(processed_parts)

            # Append unique ``id`` query parameter when present
            if parsed_url.query:
                query_params = parse_qs(parsed_url.query)
                unique_id = query_params.get("id", [None])[0]
                if unique_id:
                    base_filename = f"{base_filename}_{unique_id}"
                    logger.debug("Appended unique ID from query: %s", unique_id)

            base_filename = sanitize_filename(base_filename)
            logger.debug(
                "Created filename from URL: %s -> %s", url, base_filename,
            )
            return base_filename

        # Fallback if no usable parts
        if title and title.strip():
            return sanitize_filename(title)
        return "Unknown_Page"

    except Exception as exc:
        logger.error("Error creating filename from URL %s: %s", url, exc)
        if title and title.strip():
            return sanitize_filename(title)
        return "Error_Page"


def construct_final_filename(
    base_filename: str,
    is_paginated: bool = False,
    page_number: Optional[int] = None,
    chunk_postfix: Optional[str] = None,
    dedup_counter: Optional[int] = None,
) -> str:
    """Construct the final filename with CHUNK + PAGE + VERSION postfixes.

    Ordering: ``<base>_<chunk>_PAGE<n>_V<n>``

    Args:
        base_filename: Base name from :func:`create_filename_from_url`.
        is_paginated: Whether pagination applies.
        page_number: Page number (used only when *is_paginated* is ``True``).
        chunk_postfix: Alphabetical chunk suffix (``"A"``, ``"B"``, ...).
        dedup_counter: De-duplication version counter (``> 0``).

    Returns:
        Complete filename **without** the ``.txt`` extension.
    """
    filename_parts: List[str] = [base_filename]

    # Chunk postfix FIRST
    if chunk_postfix:
        filename_parts.append(chunk_postfix)

    # Pagination postfix SECOND
    if is_paginated and page_number is not None:
        filename_parts.append(f"PAGE{page_number}")

    # Deduplication postfix LAST
    if dedup_counter and dedup_counter > 0:
        filename_parts.append(f"V{dedup_counter}")

    final_filename = "_".join(filename_parts)
    logger.debug("Constructed filename: %s", final_filename)
    return final_filename


# ---------------------------------------------------------------------------
# Content analysis
# ---------------------------------------------------------------------------


def analyze_content_for_massive_data_warning(
    content: str,
    url: str = "",
    title: str = "",
) -> Dict[str, Any]:
    """Analyse *content* length and structure, returning risk-level metadata.

    Helps callers decide whether chunking or summarisation is needed before
    uploading to a vector store.

    Args:
        content: The text to analyse.
        url: Source URL (for logging context).
        title: Content title (for logging context).

    Returns:
        A dict with keys ``estimated_tokens``, ``risk_level``, ``message``,
        ``structure_indicators``, ``dominant_structure``, ``dominant_count``,
        ``total_structural_elements``, and ``recommendation``.
    """
    if not content:
        return {"analysis": "empty", "recommendation": "none"}

    estimated_tokens = count_tokens_approximate(content)

    # Universal structure detection
    structure_indicators: Dict[str, int] = {
        "table_rows": len(re.findall(r"^\|.*\|.*\|", content, re.MULTILINE)),
        "list_items": len(re.findall(r"^[\s]*[-*+]\s+", content, re.MULTILINE)),
        "numbered_items": len(re.findall(r"^\s*\d+\.\s+", content, re.MULTILINE)),
        "headings": len(re.findall(r"^#+\s+", content, re.MULTILINE)),
        "contact_elements": len(
            re.findall(
                r"\b\d{9,15}\b|[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                content,
            )
        ),
        "lines": len(content.split("\n")),
    }

    total_structure = sum(structure_indicators.values())
    dominant_structure = max(structure_indicators.items(), key=lambda x: x[1])

    # Risk classification
    if estimated_tokens > 20_000:
        risk_level = "CRITICAL"
        message = (
            f"CRITICAL: {estimated_tokens:,} tokens "
            f"- Guaranteed information loss without chunking"
        )
    elif estimated_tokens > 15_000:
        risk_level = "HIGH"
        message = (
            f"HIGH RISK: {estimated_tokens:,} tokens "
            f"- Likely information loss"
        )
    elif estimated_tokens > 8_000:
        risk_level = "MODERATE"
        message = (
            f"MODERATE: {estimated_tokens:,} tokens "
            f"- Monitor for completeness"
        )
    else:
        risk_level = "LOW"
        message = f"SAFE: {estimated_tokens:,} tokens - Normal processing"

    return {
        "estimated_tokens": estimated_tokens,
        "risk_level": risk_level,
        "message": message,
        "structure_indicators": structure_indicators,
        "dominant_structure": dominant_structure[0],
        "dominant_count": dominant_structure[1],
        "total_structural_elements": total_structure,
        "recommendation": (
            "chunking_required" if estimated_tokens > 15_000 else "monitor"
        ),
    }


# ---------------------------------------------------------------------------
# Token counting  (H7 fix: Czech-calibrated ratio + optional tiktoken)
# ---------------------------------------------------------------------------


def count_tokens_approximate(text: str) -> int:
    """Approximate token count, calibrated for Czech text (~3.2 chars/token).

    Uses ``tiktoken`` if available, otherwise falls back to a character-based
    estimation.  Czech text with diacritics has a lower chars-per-token ratio
    than English (~4), so the fallback uses **3.2** instead of the V2 value
    of 4.
    """
    if not text:
        return 0
    try:
        import tiktoken  # type: ignore[import-untyped]

        enc = tiktoken.encoding_for_model("gpt-4")
        return len(enc.encode(text))
    except (ImportError, Exception):
        pass
    # Czech-calibrated fallback (ratio ~3.2 instead of 4)
    return max(1, int(len(text) / 3.2))


def html_to_plain_text(html: str) -> str:
    """Convert HTML to plain text by removing scripts/styles and extracting text.

    Strips ``script``, ``style``, ``meta``, ``link``, and ``noscript`` tags,
    then extracts visible text with double-newline paragraph separation.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "meta", "link", "noscript"]):
        tag.decompose()
    text_content = soup.get_text(separator="\n", strip=True)
    lines = [ln.strip() for ln in text_content.split("\n") if ln.strip()]
    return "\n\n".join(lines)
