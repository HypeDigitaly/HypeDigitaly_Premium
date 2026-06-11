"""Tests for the two-tier RateLimiter (Wave 0.1).

Covers:
  * Politeness tier: concurrency cap respected.
  * Politeness tier: minimum start interval spacing.
  * NO-CONVOY: two waiters sleep CONCURRENTLY (wall < serial sum).
  * API tier (openrouter.ai / api.openai.com): no interval delay, concurrency
    cap == API_MAX_CONCURRENT.
  * Disabled -> passthrough no-op slot.
  * reset_rate_limiter() discards the cached singleton.

All timing uses tiny intervals (<=50ms) to keep the suite fast.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from gpt_scraper_v3.rate_limiter import (
    RateLimiter,
    get_rate_limiter,
    reset_rate_limiter,
)


def _fake_cfg(**overrides):
    """A minimal duck-typed config object for RateLimiter (uses getattr)."""
    base = dict(
        RATE_LIMIT_ENABLED=True,
        API_MAX_CONCURRENT=12,
        RATE_LIMIT_MAX_CONCURRENT_PER_DOMAIN=4,
        RATE_LIMIT_MIN_INTERVAL_MS=1000,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _run_concurrently(n: int, target):
    threads = [threading.Thread(target=target, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_politeness_concurrency_cap():
    """No more than RATE_LIMIT_MAX_CONCURRENT_PER_DOMAIN slots held at once."""
    rl = RateLimiter(_fake_cfg(RATE_LIMIT_MAX_CONCURRENT_PER_DOMAIN=3,
                               RATE_LIMIT_MIN_INTERVAL_MS=0))
    url = "https://target.example.com/page"

    active = 0
    peak = 0
    lock = threading.Lock()

    def worker(_i):
        nonlocal active, peak
        with rl.acquire(url):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1

    _run_concurrently(10, worker)
    assert peak <= 3
    assert peak >= 1


def test_politeness_min_interval_spacing():
    """Successive same-domain acquisitions start >= min_interval apart."""
    # cap=1 so acquisitions are forced serial -> interval spacing is observable.
    rl = RateLimiter(_fake_cfg(RATE_LIMIT_MAX_CONCURRENT_PER_DOMAIN=1,
                               RATE_LIMIT_MIN_INTERVAL_MS=50))
    url = "https://spaced.example.com/x"

    starts = []
    starts_lock = threading.Lock()

    def worker(_i):
        with rl.acquire(url):
            with starts_lock:
                starts.append(time.monotonic())

    _run_concurrently(3, worker)
    starts.sort()
    # 3 acquisitions => 2 gaps, each at least ~min_interval (minus scheduling slack).
    gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    for g in gaps:
        assert g >= 0.045, f"gap {g} below min interval"


def test_politeness_no_convoy_concurrent_sleeps():
    """Two waiters with a generous concurrency cap sleep CONCURRENTLY.

    With cap=2 and interval=50ms, both threads acquire their semaphore slots
    immediately, then each computes its own wake time and sleeps OUTSIDE the
    lock. The reservation pattern staggers wake times by one interval, so the
    total wall time should be ~1 interval, NOT 2 (which a convoy would force).
    """
    rl = RateLimiter(_fake_cfg(RATE_LIMIT_MAX_CONCURRENT_PER_DOMAIN=2,
                               RATE_LIMIT_MIN_INTERVAL_MS=50))
    url = "https://convoy.example.com/x"

    barrier = threading.Barrier(2)

    def worker(_i):
        barrier.wait()  # ensure both threads reach acquire near-simultaneously
        with rl.acquire(url):
            pass

    t0 = time.monotonic()
    _run_concurrently(2, worker)
    elapsed = time.monotonic() - t0

    # Serial sum of sleeps would be ~2*interval = 0.10s. No-convoy => ~1 interval.
    # Allow generous headroom but stay well under the serial sum.
    assert elapsed < 0.09, f"convoy detected: {elapsed:.3f}s >= serial sum"


@pytest.mark.parametrize("host_url", [
    "https://openrouter.ai/api/v1/chat/completions",
    "https://api.openai.com/v1/files",
])
def test_api_tier_no_interval_latency(host_url):
    """API-tier hosts add zero interval latency even with a large min_interval."""
    rl = RateLimiter(_fake_cfg(API_MAX_CONCURRENT=8, RATE_LIMIT_MIN_INTERVAL_MS=1000))

    t0 = time.monotonic()
    for _ in range(5):
        with rl.acquire(host_url):
            pass
    elapsed = time.monotonic() - t0
    # If the interval were applied this would take ~4s; it must be near-instant.
    assert elapsed < 0.1, f"API tier paced unexpectedly: {elapsed:.3f}s"


def test_api_tier_concurrency_cap():
    """API tier enforces API_MAX_CONCURRENT concurrent slots."""
    rl = RateLimiter(_fake_cfg(API_MAX_CONCURRENT=2, RATE_LIMIT_MIN_INTERVAL_MS=1000))
    url = "https://api.openai.com/v1/files"

    active = 0
    peak = 0
    lock = threading.Lock()

    def worker(_i):
        nonlocal active, peak
        with rl.acquire(url):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1

    _run_concurrently(8, worker)
    assert peak <= 2
    assert peak >= 1


def test_disabled_passthrough():
    """Disabled limiter yields a valid no-op context manager (no pacing)."""
    rl = RateLimiter(_fake_cfg(RATE_LIMIT_ENABLED=False,
                               RATE_LIMIT_MIN_INTERVAL_MS=1000,
                               RATE_LIMIT_MAX_CONCURRENT_PER_DOMAIN=1))
    url = "https://anything.example.com/x"
    t0 = time.monotonic()
    for _ in range(5):
        with rl.acquire(url):
            pass
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05


def test_reset_rate_limiter_singleton(make_config):
    """get_rate_limiter() caches; reset_rate_limiter() forces a rebuild."""
    make_config()
    reset_rate_limiter()
    try:
        first = get_rate_limiter()
        again = get_rate_limiter()
        assert first is again
        reset_rate_limiter()
        third = get_rate_limiter()
        assert third is not first
    finally:
        reset_rate_limiter()
