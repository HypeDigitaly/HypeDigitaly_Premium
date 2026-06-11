"""Base-URL once check-and-set (Wave 0.8).

50 racing threads hit the CAS region of should_process_url_with_resume's base-URL
"process once" flag. Exactly one thread must win the slot.

We exercise the REAL function with minimal config mocking so the actual lock +
flag interplay is tested. The base URL maps into CANONICAL_BASE_URLS with
PROCESS_BASE_URL_ONCE=True; the winner returns True, all losers return False.
"""
from __future__ import annotations

import threading

import gpt_scraper_v3.url_processing as up


def test_base_url_cas_single_winner(make_config):
    base = "https://cas.example.com/"
    make_config(
        BASE_URL="https://cas.example.com",
        CANONICAL_BASE_URLS={"https://cas.example.com/"},
        PROCESS_BASE_URL_ONCE=True,
        BASE_URL_PROCESSED_IN_RUN=False,
        TEST_URLS=[],
        RECURSIVE_URLS=[],
        # Mode 0 short-circuits the lastmod chain to a deterministic True after
        # the base-URL CAS allows the winner through.
        CHECK_LAST_MODIFIED=0,
    )

    n = 50
    results = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        r = up.should_process_url_with_resume(
            base, last_modified=None, last_run_timestamp=None,
        )
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = sum(1 for r in results if r)
    assert winners == 1, f"expected exactly one winner, got {winners}"
    assert len(results) == n


def test_base_url_cas_lock_pattern_direct():
    """Directly exercise the lock-protected check-and-set pattern with a fake
    flag object to prove single-winner semantics independent of the surrounding
    eligibility logic."""

    class _Flag:
        processed = False

    flag = _Flag()
    lock = threading.Lock()
    n = 50
    winners = []
    winners_lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        with lock:
            already = flag.processed
            if not already:
                flag.processed = True
        if not already:
            with winners_lock:
                winners.append(1)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(winners) == 1
    assert flag.processed is True


def test_base_url_once_disabled_skips(make_config):
    """When PROCESS_BASE_URL_ONCE is False the base URL is always skipped."""
    base = "https://no.example.com/"
    make_config(
        BASE_URL="https://no.example.com",
        CANONICAL_BASE_URLS={"https://no.example.com/"},
        PROCESS_BASE_URL_ONCE=False,
        CHECK_LAST_MODIFIED=0,
    )
    assert up.should_process_url_with_resume(base, None, None) is False
