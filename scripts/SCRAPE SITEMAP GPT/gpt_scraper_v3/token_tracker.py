"""OpenRouter token usage tracking for GPT Scraper V3."""
from __future__ import annotations

import logging
from typing import Any, Dict

logger: logging.Logger = logging.getLogger(__name__)

# Token usage accumulator
global_token_usage: Dict[str, int] = {
    "total_prompt_tokens": 0,
    "total_completion_tokens": 0,
    "total_tokens": 0,
    "api_calls_count": 0,
}


def log_openrouter_token_usage(
    response_data: Dict[str, Any],
    call_name: str = "",
    url: str = "",
) -> None:
    """Log and accumulate token usage from an OpenRouter API response.

    Args:
        response_data: The JSON response from the OpenRouter API.
        call_name: Descriptive name of the API call for logging.
        url: Associated URL for context.
    """
    try:
        usage: Dict[str, Any] = response_data.get("usage", {})
        if usage:
            prompt_tokens: int = usage.get("prompt_tokens", 0)
            completion_tokens: int = usage.get("completion_tokens", 0)
            total_tokens: int = usage.get("total_tokens", 0)

            global_token_usage["total_prompt_tokens"] += prompt_tokens
            global_token_usage["total_completion_tokens"] += completion_tokens
            global_token_usage["total_tokens"] += total_tokens
            global_token_usage["api_calls_count"] += 1

            logger.info("OPENROUTER TOKEN USAGE - %s:", call_name)
            logger.info("   Input tokens (prompt): %s", f"{prompt_tokens:,}")
            logger.info("   Output tokens (completion): %s", f"{completion_tokens:,}")
            logger.info("   Total tokens: %s", f"{total_tokens:,}")
            if url:
                logger.info("   URL: %s", url)

            logger.info("RUNNING TOTALS - Total API calls: %d", global_token_usage["api_calls_count"])
            logger.info("   Total input tokens: %s", f"{global_token_usage['total_prompt_tokens']:,}")
            logger.info("   Total output tokens: %s", f"{global_token_usage['total_completion_tokens']:,}")
            logger.info("   Total tokens: %s", f"{global_token_usage['total_tokens']:,}")
        else:
            logger.warning("No usage data found in OpenRouter response for %s", call_name)
    except Exception as e:
        logger.error("Error logging token usage for %s: %s", call_name, str(e))


def get_token_usage_summary() -> Dict[str, int]:
    """Return a copy of the current token usage totals."""
    return dict(global_token_usage)


def reset_token_usage() -> None:
    """Reset all token counters to zero. Useful for test isolation."""
    for key in global_token_usage:
        global_token_usage[key] = 0
