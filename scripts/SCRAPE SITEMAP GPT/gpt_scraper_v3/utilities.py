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
import functools
import logging
import os
import re
import tempfile
import threading
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from gpt_scraper_v3.config import get_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 0.2: Thread-local HTTP session (replaces the module-level singleton).
#
# Each worker thread gets its own ``requests.Session`` (sessions are not
# guaranteed thread-safe under concurrent use). Created sessions are tracked in
# a lock-guarded registry so they can all be closed deterministically on
# teardown via ``close_all_sessions()``. Workers also close their own session
# in their ``finally`` block (1.1); ``close_all_sessions()`` is the backstop.
# ---------------------------------------------------------------------------

_thread_local = threading.local()
_session_registry: List[requests.Session] = []
_session_registry_lock = threading.Lock()

# F7: Generation token bumped by close_all_sessions(). A thread-local session
# whose stored generation is stale (e.g. it was closed by a prior
# close_all_sessions() during a previous main() run) is discarded and rebuilt
# on the next get_session(), so no thread (incl. main) keeps using a closed
# session across repeated in-process main() calls.
_session_generation = 0


def get_session() -> requests.Session:
    """Get or create the current thread's requests session with retry config.

    Returns a per-thread :class:`requests.Session` (lazily built on first use
    in each thread). Every created session is registered in a lock-guarded
    module-level registry so :func:`close_all_sessions` can close them all.
    Registration is idempotent per thread via a thread-local flag.
    """
    session: Optional[requests.Session] = getattr(_thread_local, "session", None)
    if session is not None:
        # F7: If this thread's session predates the latest close_all_sessions()
        # (stale generation), it has been closed already -- discard and rebuild.
        if getattr(_thread_local, "generation", -1) == _session_generation:
            return session
        try:
            session.close()
        except Exception:  # pragma: no cover - defensive cleanup
            logger.debug("Failed to close a stale thread-local session", exc_info=True)
        _thread_local.session = None
        _thread_local.registered = False

    cfg = get_config()
    session = requests.Session()
    retry = Retry(
        total=cfg.REQUEST_RETRY_COUNT,
        read=cfg.REQUEST_RETRY_COUNT,
        connect=cfg.REQUEST_RETRY_COUNT,
        backoff_factor=cfg.REQUEST_BACKOFF_FACTOR,
        status_forcelist=cfg.REQUEST_RETRY_CODES,
        respect_retry_after_header=True,
    )
    pool_size = cfg.API_MAX_CONCURRENT + 2
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=pool_size,
        pool_maxsize=pool_size,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    _thread_local.session = session
    # F7: Stamp this session with the current generation so close_all_sessions()
    # can detect (and force a rebuild of) it on a later run.
    _thread_local.generation = _session_generation
    if not getattr(_thread_local, "registered", False):
        with _session_registry_lock:
            _session_registry.append(session)
        _thread_local.registered = True
    return session


def close_all_sessions() -> None:
    """Close every registered session and clear the registry.

    Backstop teardown for sessions whose owning thread did not close them.
    Clearing the registry makes repeated ``main()`` calls safe (no stale,
    already-closed sessions accumulate across runs).
    """
    global _session_generation
    with _session_registry_lock:
        # F7: Bump the generation under the registry lock so any thread that
        # still holds a (now-closed) thread-local session rebuilds it on its
        # next get_session() call.
        _session_generation += 1
        sessions = list(_session_registry)
        _session_registry.clear()
    for session in sessions:
        try:
            session.close()
        except Exception:  # pragma: no cover - defensive cleanup
            logger.debug("Failed to close a registered session", exc_info=True)


atexit.register(close_all_sessions)


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


def canonical_url(url: str) -> str:
    """Return a canonical form of *url* for cross-run identity comparison.

    Bug 12: a single canonical form is used at every place that compares URLs
    across runs (the known-URLs snapshot build in ``cli.py`` and the lastmod
    matching in ``url_processing.find_url_last_modified``) so the same page
    never appears as both "new" and "removed" due to inconsistent normalization.

    Canonicalization steps:
      1. Strip the ``#fragment`` (reuses :func:`strip_url_fragment`).
      2. Sort query parameters alphabetically (reuses
         :func:`normalize_url_query_params`).

    **Trailing-slash policy: preserve as-is.** The existing ``*_known_urls.json``
    snapshots store URLs verbatim with respect to the trailing slash — both
    slashed (``.../portal/``) and unslashed (``https://www.epece.cz``) forms
    coexist. Stripping or appending a slash here would reclassify a large
    fraction of stored URLs as new/removed on the next run, so this helper
    leaves the path's trailing slash untouched.
    """
    if not url:
        return url
    return normalize_url_query_params(strip_url_fragment(url))


def strip_url_fragment(url: str) -> str:
    """Remove the ``#fragment`` component from an absolute URL.

    Used at URL extraction time so that fragment variants of the same page
    (``/o-webu#zou``, ``/o-webu#cookies``) collapse to one canonical URL.

    IMPORTANT: never call this on vector-store lookup/cache keys — those
    deliberately carry ``#chunkX`` suffixes that encode chunk identity.
    """
    if not url or "#" not in url:
        return url
    return urlunparse(urlparse(url)._replace(fragment=""))


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
# 1.3: Atomic filename reservation + crash-safe content writes
#
# The old ``while os.path.exists(filepath)`` collision loops in file_saving.py
# and chunk_processing.py had a TOCTOU race: two worker threads could both see
# ``_V1`` as free and then clobber each other's output.
#
# ``reserve_unique_filepath_fn`` closes the race by atomically *creating* each
# candidate path with ``O_CREAT | O_EXCL`` (atomic on Windows/NTFS) under a
# process-wide lock. The winner gets the path; losers fall through to the next
# counter. The created file is a 0-byte placeholder reservation.
#
# Because that 0-byte placeholder would itself poison the resume cache and pass
# upload validation as an empty doc, real content MUST be written via
# ``write_content_to_reserved_path``, which writes to a temp file in the same
# directory and ``os.replace``-s it over the reservation. On any failure the
# placeholder is removed (best-effort). End state after any failure is always:
# no file, or a complete file -- never a 0-byte landmine.
# ---------------------------------------------------------------------------

_filename_lock = threading.Lock()


def reserve_unique_filepath_fn(name_fn: Callable[[int], str]) -> str:
    """Atomically reserve a unique filepath, preserving structured naming.

    ``name_fn(counter)`` must return the candidate absolute path for a given
    counter, where ``counter == 0`` yields the base (unversioned) name and
    ``counter > 0`` yields the structured ``_V{n}`` variant. This callback
    design lets each caller reproduce its existing ``_V{n}`` naming scheme
    exactly (e.g. via :func:`construct_final_filename` with ``dedup_counter``).

    Under :data:`_filename_lock`, candidates are tried in counter order. Each
    candidate is created with ``os.open(O_CREAT | O_EXCL | O_WRONLY)`` -- an
    atomic operation on Windows/NTFS -- so exactly one thread can win any given
    name. ``FileExistsError`` advances to the next counter. The returned path
    refers to a freshly created **0-byte placeholder**: callers must overwrite
    it via :func:`write_content_to_reserved_path` (or remove it) so no empty
    file is left behind.

    Returns:
        The absolute path of the reserved (0-byte) file.
    """
    counter = 0
    with _filename_lock:
        while True:
            path = name_fn(counter)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                counter += 1
                continue
            os.close(fd)
            return path


def write_content_to_reserved_path(path: str, content: str) -> None:
    """Crash-safely write *content* into a path reserved by the helper above.

    Writes to a uniquely named temp file in the same directory (so
    :func:`os.replace` is an atomic same-filesystem rename) and replaces the
    0-byte reservation placeholder with the complete file. If anything fails,
    both the temp file and the 0-byte placeholder are removed (best-effort) so
    the end state is *no file*, never an empty one.

    Raises:
        OSError: re-raised after cleanup if the write/replace fails.
    """
    directory = os.path.dirname(path) or "."
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up the temp file and the 0-byte reservation placeholder so a
        # failed write never leaves an empty landmine behind.
        for stale in (tmp_path, path):
            try:
                if os.path.exists(stale):
                    os.remove(stale)
            except OSError:  # pragma: no cover - best-effort cleanup
                logger.debug("Failed to remove %s after write error", stale,
                             exc_info=True)
        raise


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


# Module-level flag so the tiktoken-unavailable warning is emitted only once
# (Bug 5). ``count_tokens_approximate`` runs on a hot path -- if tiktoken cannot
# be loaded we must not flood the log with one warning per call.
_tiktoken_warned = False


@functools.lru_cache(maxsize=1)
def _get_tiktoken_encoder():  # type: ignore[no-untyped-def]
    """Return a cached ``tiktoken`` encoder for ``gpt-4`` (Bug 5).

    Imports ``tiktoken`` and builds the encoder exactly once, then memoizes the
    result so subsequent ``count_tokens_approximate`` calls reuse it instead of
    re-importing and re-creating the encoder on every call.

    Raises on failure (import error or build error) ON PURPOSE: ``lru_cache``
    does NOT memoize exceptions, so a transient failure does not become a
    permanent "no encoder ever" state -- the next call retries the import. The
    caller catches the exception and degrades to the character-based fallback.

    The returned encoder's ``encode`` method is thread-safe, so the cached
    instance can be shared across worker threads without a lock.
    """
    import tiktoken  # type: ignore[import-untyped]

    return tiktoken.encoding_for_model("gpt-4")


def count_tokens_approximate(text: str) -> int:
    """Approximate token count, calibrated for Czech text (~3.2 chars/token).

    Uses a memoized ``tiktoken`` encoder (built once via
    :func:`_get_tiktoken_encoder`) if available, otherwise falls back to a
    character-based estimation.  Czech text with diacritics has a lower
    chars-per-token ratio than English (~4), so the fallback uses **3.2**
    instead of the V2 value of 4.
    """
    if not text:
        return 0
    try:
        enc = _get_tiktoken_encoder()
        return len(enc.encode(text))
    except Exception:
        global _tiktoken_warned
        if not _tiktoken_warned:
            _tiktoken_warned = True
            logger.warning(
                "tiktoken unavailable; using Czech-calibrated character-based "
                "token estimation (ratio ~3.2). This warning is shown once.",
                exc_info=True,
            )
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
