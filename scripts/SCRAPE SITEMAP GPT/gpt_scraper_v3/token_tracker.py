"""OpenRouter token usage tracking for GPT Scraper V3."""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict

logger: logging.Logger = logging.getLogger(__name__)

# Token usage accumulator
global_token_usage: Dict[str, int] = {
    "total_prompt_tokens": 0,
    "total_completion_tokens": 0,
    "total_tokens": 0,
    "api_calls_count": 0,
}

# Guards all reads/writes of ``global_token_usage`` so concurrent worker
# threads can safely accumulate token usage without torn reads or lost updates.
_token_lock = threading.Lock()


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

            with _token_lock:
                global_token_usage["total_prompt_tokens"] += prompt_tokens
                global_token_usage["total_completion_tokens"] += completion_tokens
                global_token_usage["total_tokens"] += total_tokens
                global_token_usage["api_calls_count"] += 1
                # Capture running totals under the lock so the logging block
                # below reports a consistent, untorn snapshot.
                running_api_calls: int = global_token_usage["api_calls_count"]
                running_prompt: int = global_token_usage["total_prompt_tokens"]
                running_completion: int = global_token_usage["total_completion_tokens"]
                running_total: int = global_token_usage["total_tokens"]

            # F6: one atomic INFO line per call so usage lines don't interleave
            # across workers (the multi-line block scrambled under concurrency).
            logger.info(
                "OpenRouter usage [%s] in=%s out=%s total=%s | running calls=%d total=%s",
                call_name,
                f"{prompt_tokens:,}",
                f"{completion_tokens:,}",
                f"{total_tokens:,}",
                running_api_calls,
                f"{running_total:,}",
            )

            # Verbose breakdown kept for --debug only.
            logger.debug("OPENROUTER TOKEN USAGE - %s:", call_name)
            logger.debug("   Input tokens (prompt): %s", f"{prompt_tokens:,}")
            logger.debug("   Output tokens (completion): %s", f"{completion_tokens:,}")
            logger.debug("   Total tokens: %s", f"{total_tokens:,}")
            if url:
                logger.debug("   URL: %s", url)
            logger.debug("RUNNING TOTALS - Total API calls: %d", running_api_calls)
            logger.debug("   Total input tokens: %s", f"{running_prompt:,}")
            logger.debug("   Total output tokens: %s", f"{running_completion:,}")
            logger.debug("   Total tokens: %s", f"{running_total:,}")
        else:
            logger.warning("No usage data found in OpenRouter response for %s", call_name)
    except Exception as e:
        logger.error("Error logging token usage for %s: %s", call_name, str(e))


def get_token_usage_summary() -> Dict[str, int]:
    """Return a copy of the current token usage totals (taken under the lock)."""
    with _token_lock:
        return dict(global_token_usage)


def reset_token_usage() -> None:
    """Reset all token counters to zero. Useful for test isolation."""
    with _token_lock:
        for key in global_token_usage:
            global_token_usage[key] = 0
