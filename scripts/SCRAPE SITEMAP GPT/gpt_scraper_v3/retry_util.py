"""Application-tier 429/Retry-After handling for GPT Scraper V3 (Bug 3, Wave 3.3).

The transport layer (urllib3 ``Retry`` with 429 in the forcelist) handles brief,
automatic 429 retries. This module adds a SECOND, application-level tier for the
OpenRouter and OpenAI HTTP calls that honours a long ``Retry-After`` header and,
critically, on exhaustion RETURNS the final response instead of raising.

Why never raise: callers already inspect non-2xx responses and degrade gracefully
(``_call_openrouter`` falls back to ``None``; Jina-style strategy chains fall
through to the next strategy). Raising here would bypass that logic and break the
fall-through precedence (wrapper inner, strategy chain outer).

Limiter interaction: ``fn`` is expected to acquire the rate-limiter slot, perform
the HTTP call, and release the slot -- i.e. the limiter ``with`` block lives
INSIDE ``fn``. That means each retry RE-ACQUIRES the limiter slot, so a sleeping
retry never holds a politeness/API concurrency slot while it waits.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable

import requests

logger: logging.Logger = logging.getLogger(__name__)


def _parse_retry_after(response: requests.Response, attempt: int) -> float:
    """Return the delay (seconds) before the next attempt.

    Prefers the ``Retry-After`` header parsed as float seconds. If the header is
    missing or unparseable, falls back to exponential backoff ``2.0 ** (attempt-1)``.
    """
    header = response.headers.get("Retry-After")
    if header is not None:
        try:
            return float(header.strip())
        except (ValueError, AttributeError):
            # F16: the HTTP-date form of Retry-After (RFC 7231) is intentionally
            # NOT parsed here -- it falls through to exponential backoff below.
            # Acceptable: servers we hit emit numeric-seconds Retry-After.
            pass
    return 2.0 ** (attempt - 1)


def with_429_retry(
    fn: Callable[[], requests.Response],
    *,
    max_attempts: int,
    cap_seconds: float,
    logger: logging.Logger = logger,
    call_name: str = "",
) -> requests.Response:
    """Call *fn* with application-tier 429/Retry-After retries.

    Args:
        fn: A zero-arg callable returning a ``requests.Response``. It MUST perform
            the full HTTP call each invocation (including acquiring/releasing the
            rate-limiter slot) so retries re-acquire the slot rather than holding
            it while sleeping.
        max_attempts: Maximum number of times *fn* is called (>= 1).
        cap_seconds: Upper bound on the base sleep delay before jitter.
        logger: Logger used for retry/exhaustion messages.
        call_name: Short label for log context.

    Returns:
        The first non-429 response, or -- if every attempt yields 429 -- the LAST
        429 response. This function NEVER raises on 429; callers' existing non-2xx
        handling degrades gracefully.
    """
    attempts = max(1, int(max_attempts))
    response: requests.Response = fn()
    for attempt in range(1, attempts + 1):
        if response.status_code != 429:
            return response
        if attempt >= attempts:
            logger.error(
                "429 retry exhausted for %s after %d attempt(s); returning 429 response",
                call_name or "API call", attempt,
            )
            return response
        base_delay = min(_parse_retry_after(response, attempt), float(cap_seconds))
        # F15: cap_seconds bounds the BASE delay only; the added jitter (up to
        # 0.5*attempt) may push the actual sleep slightly past cap_seconds by
        # design -- it spreads retries to avoid a thundering herd.
        jitter = random.uniform(0.0, 0.5 * attempt)
        delay = base_delay + jitter
        logger.warning(
            "429 from %s (attempt %d/%d); sleeping %.2fs before retry",
            call_name or "API call", attempt, attempts, delay,
        )
        time.sleep(delay)
        response = fn()
    return response
