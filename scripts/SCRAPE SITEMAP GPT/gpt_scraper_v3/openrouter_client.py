"""Unified OpenRouter API client for GPT Scraper V3.

Eliminates ~600 lines of duplicated call boilerplate from V2 via a single
shared ``_call_openrouter()`` helper.

Fixes: H3 (shared helper), C3 (debug-level logging), M5 (type hints).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from requests.exceptions import RequestException

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.token_tracker import log_openrouter_token_usage
from gpt_scraper_v3.utilities import (
    analyze_content_for_massive_data_warning,
    count_tokens_approximate,
    get_session,
)

logger: logging.Logger = logging.getLogger(__name__)
OPENROUTER_API_URL: str = "https://openrouter.ai/api/v1/chat/completions"

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
        response = get_session().post(
            OPENROUTER_API_URL, headers=headers, json=payload,
            timeout=cfg.REQUEST_TIMEOUT * 2,
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
    """Truncate *text* to stay within *max_tokens* at a word boundary."""
    if text is None:
        return None
    tokens = count_tokens_approximate(text)
    if tokens <= max_tokens:
        return text
    logger.warning("Text exceeds token limit (%d > %d) -- truncating", tokens, max_tokens)
    estimated_chars = int(max_tokens * 3.2)
    truncated = text[:estimated_chars]
    last_space = truncated.rfind(" ")
    if last_space > estimated_chars // 2:
        truncated = truncated[:last_space]
    return truncated + "..."


# ---------------------------------------------------------------------------
# Canonical prompt imports (placed after _call_openrouter so that the
# circular ``llm_prompts -> openrouter_client._call_openrouter`` resolves)
# ---------------------------------------------------------------------------

from gpt_scraper_v3.llm_prompts import (  # noqa: E402
    get_page_summary_instructions,
    get_current_file_summary_instructions,
    get_overlap_summary_instructions,
    get_standard_summarization_instructions,
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


# ---------------------------------------------------------------------------
# Content summarization functions
# ---------------------------------------------------------------------------


def create_summarization_prompt(
    markdown_content: str, target_tokens: int = 4000,
    target_language: str = "English", url: str = "", title: str = "",
) -> str:
    """Create an optimised summarization prompt for RAG content processing."""
    instructions = get_standard_summarization_instructions(
        target_tokens, target_language=target_language, url=url, title=title,
    )
    return f"{instructions}\n\n## SOURCE:\n{markdown_content}"


def _process_single_chunk_through_openrouter(chunk_content: str, target_tokens: int) -> str:
    """Process a single chunk through OpenRouter. Returns original on failure."""
    cfg = get_config()
    if not cfg.OPENROUTER_API_KEY:
        logger.warning("OpenRouter API key not configured for chunk processing")
        return chunk_content
    logger.info("Processing single chunk through OpenRouter (target: %d tokens)", target_tokens)
    result = _call_openrouter(
        messages=[{"role": "user", "content": chunk_content}],
        max_tokens=target_tokens, call_name="SINGLE_CHUNK_PROCESSING",
    )
    if result and len(result) > 10:
        logger.info("Successfully processed chunk: %d tokens", count_tokens_approximate(result))
        return result
    logger.error("Processed chunk content too short or empty, returning original")
    return chunk_content


def summarize_content_via_openrouter(
    markdown_content: str, title: str = "", url: str = "", target_tokens: int = 3800,
) -> Tuple[Optional[str], bool]:
    """Summarise markdown content via OpenRouter with strict token limits.

    Returns ``(summarized_content, success_flag)``.
    """
    cfg = get_config()
    if not cfg.OPENROUTER_API_KEY:
        logger.info("OpenRouter API key not configured, skipping summarization for %s", url)
        return None, False

    original_tokens = count_tokens_approximate(markdown_content)
    logger.info("Original content token count: %d", original_tokens)
    content_analysis = analyze_content_for_massive_data_warning(markdown_content, url, title)
    logger.info("Content analysis: %s", content_analysis.get("message", ""))

    if content_analysis.get("recommendation") == "chunking_required":
        logger.info("LARGE CONTENT (%s tokens) -- chunking may be required upstream",
                     f"{original_tokens:,}")
    elif original_tokens > 8000:
        logger.info("LARGE CONTENT WARNING: %s tokens -- monitor for loss", f"{original_tokens:,}")

    logger.info("Processing content through OpenRouter API for RAG optimization")
    prompt = create_summarization_prompt(
        markdown_content, target_tokens, cfg.OPENROUTER_TARGET_LANGUAGE, url, title)
    result = _call_openrouter(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=target_tokens + 200, call_name="CONTENT_SUMMARIZATION", url=url,
    )
    if result:
        logger.info("OpenRouter summarization response: %d tokens", count_tokens_approximate(result))
        return result, True
    return None, False
