"""Unified OpenRouter API client for GPT Scraper V3.

Eliminates ~600 lines of duplicated call boilerplate from V2 via a single
shared ``_call_openrouter()`` helper.

Fixes: H3 (shared helper), C3 (debug-level logging), M5 (type hints).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from requests.exceptions import RequestException

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.rate_limiter import get_rate_limiter
from gpt_scraper_v3.retry_util import with_429_retry
from gpt_scraper_v3.token_tracker import log_openrouter_token_usage
from gpt_scraper_v3.utilities import (
    count_tokens_approximate,
    get_session,
)

logger: logging.Logger = logging.getLogger(__name__)
OPENROUTER_API_URL: str = "https://openrouter.ai/api/v1/chat/completions"

# Sentence boundary splitter for sentence-aware truncation (E3)
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+')

# Lowercase fragments that must never appear in a clean summary (E4).
# Their presence indicates the model leaked its own instructions / English text.
_LEAK_MARKERS: Tuple[str, ...] = (
    "it doesn't say",
    "there are 1",
    "e.g.,",
    "tokens maximum",
    "token limit",
    "output only",
    "non-negotiable",
    "do not mention",
    "analyst:",
    "## task",
    "## critical",
    "source content",
    "current file content",
    "<obsah>",
    "strict token",
)

# ---------------------------------------------------------------------------
# Core unified helper  (H3 fix)
# ---------------------------------------------------------------------------


def _call_openrouter(
    messages: List[Dict[str, str]],
    max_tokens: int,
    call_name: str,
    url: str = "",
    temperature: Optional[float] = None,
    response_format: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Unified OpenRouter API call.  Handles headers, model selection, provider
    routing, prompt caching, HTTP POST with retry, response parsing with
    reasoning-mode fallback, token-usage logging, and error handling.

    Returns content string or ``None`` on failure.
    """
    cfg = get_config()
    if not cfg.OPENROUTER_API_KEY:
        logger.info("OpenRouter API key not configured, skipping %s", call_name)
        return None

    headers: Dict[str, str] = {
        "Authorization": f"Bearer {cfg.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": url if url else "https://api.openrouter.ai",
        "X-Title": f"HypeDigitaly {call_name}",
    }

    effective_temp = temperature if temperature is not None else cfg.OPENROUTER_TEMPERATURE
    payload: Dict[str, Any] = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": effective_temp,
        "top_p": cfg.OPENROUTER_TOP_P,
    }

    # Model selection: array "models" vs single "model"
    if isinstance(cfg.OPENROUTER_MODELS, list) and len(cfg.OPENROUTER_MODELS) > 1:
        payload["models"] = cfg.OPENROUTER_MODELS
    else:
        primary = cfg.OPENROUTER_MODELS[0] if isinstance(cfg.OPENROUTER_MODELS, list) else cfg.OPENROUTER_MODELS
        payload["model"] = primary

    if cfg.OPENROUTER_PROVIDER_CONFIG:
        payload["provider"] = cfg.OPENROUTER_PROVIDER_CONFIG
    if cfg.OPENROUTER_CACHE_ENABLED:
        payload["cache"] = {"type": cfg.OPENROUTER_CACHE_TYPE}
    if response_format is not None:
        payload["response_format"] = response_format

    try:
        logger.info("Sending %s request to OpenRouter API", call_name)

        def _do_post() -> "requests.Response":
            # Limiter acquire INSIDE the retried closure so a sleeping 429 retry
            # re-acquires the API slot rather than holding it while waiting.
            with get_rate_limiter().acquire(OPENROUTER_API_URL):
                return get_session().post(
                    OPENROUTER_API_URL, headers=headers, json=payload,
                    timeout=cfg.REQUEST_TIMEOUT * 2,
                )

        response = with_429_retry(
            _do_post,
            max_attempts=cfg.API_RETRY_MAX_ATTEMPTS,
            cap_seconds=cfg.API_RETRY_CAP_SECONDS,
            logger=logger,
            call_name=f"OpenRouter {call_name}",
        )
        response.raise_for_status()

        # C3 fix: debug only, truncated to 500 chars
        logger.debug("OpenRouter response for %s: %s", call_name, response.text[:500])

        data: Dict[str, Any] = response.json()
        log_openrouter_token_usage(data, call_name, url)

        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]
            content = message.get("content", "").strip()
            # Reasoning-mode fallback (e.g. openai/gpt-5)
            if not content and message.get("reasoning"):
                content = message.get("reasoning", "").strip()
                logger.debug("Extracted content from reasoning field for %s", call_name)
            if content:
                return content
            logger.error("Empty content returned from OpenRouter for %s", call_name)
            return None

        logger.error("No choices returned from OpenRouter API for %s", call_name)
        return None
    except RequestException as exc:
        logger.error("Request error with OpenRouter API for %s: %s", call_name, exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error with OpenRouter API for %s: %s", call_name, exc)
        return None


# ---------------------------------------------------------------------------
# Token-limit enforcement helper
# ---------------------------------------------------------------------------


def _enforce_token_limit(text: Optional[str], max_tokens: int) -> Optional[str]:
    """Truncate *text* to stay within *max_tokens*, preferring whole sentences.

    Keeps the same contract as before: ``None`` in -> ``None`` out, and text
    already within budget is returned unchanged. When truncation is required,
    accumulates whole sentences (split on ``.!?`` boundaries) up to the budget
    and returns the largest whole-sentence prefix. If even the first sentence
    exceeds the budget, falls back to a clean word-boundary cut.
    """
    if text is None:
        return None
    text = text.strip()
    if count_tokens_approximate(text) <= max_tokens:
        return text
    logger.warning("Text exceeds token limit (> %d) -- sentence-aware truncating", max_tokens)

    sentences = _SENTENCE_SPLIT.split(text)
    accumulated = ""
    for sentence in sentences:
        candidate = (accumulated + " " + sentence).strip() if accumulated else sentence.strip()
        if count_tokens_approximate(candidate) <= max_tokens:
            accumulated = candidate
        else:
            break

    if accumulated:
        return accumulated

    # Even the first sentence is over budget: clean word-boundary cut.
    return text[:int(max_tokens * 3.2)].rsplit(" ", 1)[0].rstrip(",;:- ") + " …"


# ---------------------------------------------------------------------------
# Output validation + clean fallback (E4)
# ---------------------------------------------------------------------------


def _looks_like_instruction_leak(text: str) -> bool:
    """Return True if *text* contains any known instruction/English-leak marker."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _LEAK_MARKERS)


def _is_mid_word_cut(text: str) -> bool:
    """Return True if *text* appears to have been cut mid-word / mid-sentence.

    Considers the stripped text cut if it is empty, or if its last character is
    not sentence-final punctuation (``.!?…``) and it does not end with a closing
    quote or parenthesis.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    last = stripped[-1]
    if last in '.!?…':
        return False
    if last in '")':
        return False
    return True


def validate_summary(text: Optional[str], min_chars: int = 15) -> Optional[str]:
    """Validate a generated summary, returning a cleaned string or ``None``.

    Strips surrounding quotes/whitespace and rejects (returns ``None``) summaries
    that are empty, shorter than *min_chars*, contain an instruction leak, or look
    mid-word cut. A WARNING with a short snippet is logged on rejection.
    """
    if text is None:
        return None
    cleaned = text.strip().strip('"').strip("'").strip("“”„‚").strip()
    if not cleaned or len(cleaned) < min_chars:
        logger.warning("Summary rejected (empty/too short): %r", cleaned[:60])
        return None
    if _looks_like_instruction_leak(cleaned):
        logger.warning("Summary rejected (instruction/English leak): %r", cleaned[:60])
        return None
    if _is_mid_word_cut(cleaned):
        logger.warning("Summary rejected (mid-word/sentence cut): %r", cleaned[-60:])
        return None
    return cleaned


# ---------------------------------------------------------------------------
# Canonical prompt imports (placed after _call_openrouter so that the
# circular ``llm_prompts -> openrouter_client._call_openrouter`` resolves)
# ---------------------------------------------------------------------------

from gpt_scraper_v3.llm_prompts import (  # noqa: E402
    get_page_summary_instructions,
    get_current_file_summary_instructions,
    get_overlap_summary_instructions,
)

# ---------------------------------------------------------------------------
# Public caller functions
# ---------------------------------------------------------------------------


def generate_page_summary_via_openrouter(
    markdown_content: str, title: str = "", url: str = "", target_language: str = "",
) -> Optional[str]:
    """Generate a short 1-2 sentence page summary for the SOURCE PAGE SUMMARY metadata."""
    cfg = get_config()
    if not cfg.OPENROUTER_API_KEY:
        return None
    lang = target_language or cfg.OPENROUTER_TARGET_LANGUAGE
    prompt = get_page_summary_instructions(target_language=lang, url=url, title=title) + \
        f"\n\n## SOURCE CONTENT TO ANALYZE:\n{markdown_content}"
    result = _call_openrouter(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=280, call_name="PAGE_SUMMARY_GENERATION", url=url,
    )
    if result:
        result = _enforce_token_limit(result, max_tokens=190)
        logger.info("Generated page summary for %s: %d tokens", url,
                     count_tokens_approximate(result or ""))
    return result


def generate_current_file_summary_via_openrouter(
    file_content: str, file_order: int, total_files: int,
    title: str = "", url: str = "", target_language: str = "",
) -> Optional[str]:
    """Generate a summary of the current chunked file within its sequence."""
    cfg = get_config()
    if not cfg.OPENROUTER_API_KEY:
        return None
    lang = target_language or cfg.OPENROUTER_TARGET_LANGUAGE
    instructions = get_current_file_summary_instructions(
        file_order, total_files, target_language=lang, url=url, title=title,
    )
    prompt = (f"{instructions}\n\n## CURRENT FILE CONTENT TO ANALYZE "
              f"(File {file_order}/{total_files}):\n{file_content}")
    result = _call_openrouter(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=320,
        call_name=f"CURRENT_FILE_SUMMARY_GENERATION_{file_order}_{total_files}",
        url=url,
    )
    if result:
        result = _enforce_token_limit(result, max_tokens=240)
        logger.info("Generated current file summary (%d/%d) for %s: %d tokens",
                     file_order, total_files, url, count_tokens_approximate(result or ""))
    return result


def generate_overlap_summary_via_openrouter(
    original_page_summary: str, previous_file_summary: str,
    previous_file_content: str, previous_file_order: int,
    current_file_order: int, total_files: int,
    title: str = "", url: str = "", target_language: str = "",
) -> Optional[str]:
    """Generate an overlap summary bridging the previous file to the current one."""
    cfg = get_config()
    if not cfg.OPENROUTER_API_KEY:
        return None
    lang = target_language or cfg.OPENROUTER_TARGET_LANGUAGE
    instructions = get_overlap_summary_instructions(
        previous_file_order, current_file_order, total_files,
        target_language=lang, url=url, title=title,
    )
    prompt = (
        f"{instructions}\n\n## INPUT DATA FOR OVERLAP ANALYSIS:\n\n"
        f"### ORIGINAL PAGE SUMMARY:\n{original_page_summary or 'Not available'}\n\n"
        f"### PREVIOUS FILE SUMMARY (File {previous_file_order}/{total_files}):\n"
        f"{previous_file_summary or 'Not available'}\n\n"
        f"### PREVIOUS FILE CONTENT (File {previous_file_order}/{total_files}) "
        f"- ANALYZE HOW IT ENDED:\n{previous_file_content}")
    result = _call_openrouter(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=280,
        call_name=f"OVERLAP_SUMMARY_GENERATION_{previous_file_order}_TO_{current_file_order}",
        url=url,
    )
    if result:
        result = _enforce_token_limit(result, max_tokens=190)
        logger.info("Generated overlap summary (prev:%d->curr:%d/%d) for %s: %d tokens",
                     previous_file_order, current_file_order, total_files, url,
                     count_tokens_approximate(result or ""))
    return result


