"""Token tracker lock stress test (Wave 0.6).

8 threads x 100 log_openrouter_token_usage calls each must accumulate exact
totals with no lost updates / torn reads.
"""
from __future__ import annotations

import threading

from gpt_scraper_v3 import token_tracker


def test_token_usage_concurrent_totals_exact():
    token_tracker.reset_token_usage()
    try:
        n_threads = 8
        calls_per_thread = 100
        # Each call reports a fixed usage payload.
        payload = {"usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}}

        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()
            for _ in range(calls_per_thread):
                token_tracker.log_openrouter_token_usage(payload, call_name="stress")

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total_calls = n_threads * calls_per_thread
        summary = token_tracker.get_token_usage_summary()
        assert summary["api_calls_count"] == total_calls
        assert summary["total_prompt_tokens"] == total_calls * 3
        assert summary["total_completion_tokens"] == total_calls * 5
        assert summary["total_tokens"] == total_calls * 8
    finally:
        token_tracker.reset_token_usage()


def test_reset_token_usage_zeroes_all():
    token_tracker.log_openrouter_token_usage(
        {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
    )
    token_tracker.reset_token_usage()
    summary = token_tracker.get_token_usage_summary()
    assert all(v == 0 for v in summary.values())


def test_summary_returns_copy_not_alias():
    token_tracker.reset_token_usage()
    try:
        summary = token_tracker.get_token_usage_summary()
        summary["total_tokens"] = 99999
        # Mutating the returned dict must not affect the internal accumulator.
        assert token_tracker.get_token_usage_summary()["total_tokens"] == 0
    finally:
        token_tracker.reset_token_usage()
