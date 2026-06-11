"""Two-tier rate limiter for GPT Scraper V3.

Provides a process-wide :class:`RateLimiter` with two tiers selected by the host
of the requested URL:

* **API tier** (``API_HOSTS`` = ``openrouter.ai``, ``api.openai.com``):
  concurrency-only via a ``BoundedSemaphore(API_MAX_CONCURRENT)`` with **no**
  minimum interval. Interval pacing on these hosts would serialise the system to
  roughly one LLM call/sec and cancel the parallelism; throttling for APIs is
  handled by 429/Retry-After logic elsewhere.
* **Politeness tier** (every other host): per-domain
  ``BoundedSemaphore(RATE_LIMIT_MAX_CONCURRENT_PER_DOMAIN)`` PLUS a minimum start
  interval of ``RATE_LIMIT_MIN_INTERVAL_MS`` between successive acquisitions on
  the same domain.

Critical no-convoy design: the per-domain minimum interval is enforced via a
slot-reservation pattern. Under the domain's lock we compute
``wake = max(now, next_allowed_ts)`` and advance ``next_allowed_ts = wake +
interval``; the lock is then RELEASED and ``time.sleep(wake - now)`` happens
OUTSIDE the lock so multiple waiters sleep concurrently rather than convoying
behind a single held lock.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterator, Optional
from urllib.parse import urlparse

from gpt_scraper_v3.config import ScraperConfig, get_config
from gpt_scraper_v3.logging_setup import get_logger

logger = get_logger()

# API-tier hosts: concurrency-only, never interval-paced.
API_HOSTS: frozenset = frozenset({"openrouter.ai", "api.openai.com"})


def _domain_key(url: str) -> str:
    """Return the lowercase netloc with a leading ``www.`` stripped."""
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


@dataclass
class _DomainState:
    """Per-key concurrency + interval state for the politeness tier."""

    semaphore: threading.BoundedSemaphore
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_allowed_ts: float = 0.0


class RateLimiter:
    """Process-wide two-tier rate limiter.

    Built lazily from a :class:`ScraperConfig`. Use :func:`get_rate_limiter` for
    the shared singleton rather than constructing directly.
    """

    def __init__(self, cfg: ScraperConfig) -> None:
        self._enabled: bool = bool(getattr(cfg, "RATE_LIMIT_ENABLED", True))
        self._api_max_concurrent: int = max(1, int(getattr(cfg, "API_MAX_CONCURRENT", 12)))
        self._per_domain_max: int = max(1, int(getattr(cfg, "RATE_LIMIT_MAX_CONCURRENT_PER_DOMAIN", 4)))
        self._min_interval: float = max(0.0, float(getattr(cfg, "RATE_LIMIT_MIN_INTERVAL_MS", 1000)) / 1000.0)

        # API-tier semaphore is shared across all API hosts (concurrency-only).
        self._api_semaphore: threading.BoundedSemaphore = threading.BoundedSemaphore(self._api_max_concurrent)

        # Politeness-tier per-domain state, guarded for lazy creation.
        self._domains: Dict[str, _DomainState] = {}
        self._domains_lock: threading.Lock = threading.Lock()

    # -- Internal helpers -----------------------------------------------------

    def _get_domain_state(self, key: str) -> _DomainState:
        """Lazily create and return the :class:`_DomainState` for *key*."""
        state = self._domains.get(key)
        if state is not None:
            return state
        with self._domains_lock:
            state = self._domains.get(key)
            if state is None:
                state = _DomainState(semaphore=threading.BoundedSemaphore(self._per_domain_max))
                self._domains[key] = state
            return state

    @contextmanager
    def _noop_slot(self) -> Iterator[None]:
        """A valid context manager that does nothing (disabled passthrough)."""
        yield

    @contextmanager
    def _api_slot(self, host: str) -> Iterator[None]:
        """Concurrency-only slot for API-tier hosts (no interval)."""
        self._api_semaphore.acquire()
        try:
            yield
        finally:
            self._api_semaphore.release()

    @contextmanager
    def _politeness_slot(self, key: str) -> Iterator[None]:
        """Per-domain slot: concurrency cap + min start interval (no convoy)."""
        state = self._get_domain_state(key)
        # F5: surface concurrency-cap queueing at DEBUG without changing timing.
        # Try a non-blocking acquire first; only if the cap is saturated do we
        # log and fall back to the (identical) blocking acquire.
        if state.semaphore.acquire(blocking=False):
            pass
        else:
            logger.debug("rate-limit: domain '%s' at concurrency cap, queueing", key)
            state.semaphore.acquire()
        try:
            if self._min_interval > 0:
                # Slot-reservation: advance next_allowed_ts under the lock, then
                # sleep OUTSIDE the lock so concurrent waiters do not convoy.
                now = time.monotonic()
                with state.lock:
                    wake = max(now, state.next_allowed_ts)
                    state.next_allowed_ts = wake + self._min_interval
                delay = wake - now
                if delay > 0:
                    # F5: interval wait visible under --debug.
                    logger.debug("rate-limit: domain '%s' waiting %.2fs", key, delay)
                    time.sleep(delay)
            yield
        finally:
            state.semaphore.release()

    # -- Public API -----------------------------------------------------------

    def acquire(self, url: str):
        """Return a context manager reserving a rate-limit slot for *url*.

        The returned context manager releases the underlying semaphore on exit.
        When the limiter is disabled it yields a no-op slot that is still a valid
        context manager.
        """
        if not self._enabled:
            return self._noop_slot()
        host = urlparse(url).netloc.lower()
        if host in API_HOSTS:
            return self._api_slot(host)
        return self._politeness_slot(_domain_key(url))


# -- Singleton access ---------------------------------------------------------

_rate_limiter: Optional[RateLimiter] = None
_rate_limiter_lock = threading.Lock()


def get_rate_limiter() -> RateLimiter:
    """Return the process-wide :class:`RateLimiter` singleton (lazy, thread-safe).

    Reads :func:`get_config` lazily on first use via a double-checked lock.
    """
    global _rate_limiter
    if _rate_limiter is None:
        with _rate_limiter_lock:
            if _rate_limiter is None:
                _rate_limiter = RateLimiter(get_config())
    return _rate_limiter


def reset_rate_limiter() -> None:
    """Discard the cached singleton so the next call rebuilds from fresh config.

    Provided for repeated ``main()`` calls and tests.
    """
    global _rate_limiter
    with _rate_limiter_lock:
        _rate_limiter = None
