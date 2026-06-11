"""Run-scoped VisitedSet exactly-once guard (Wave 3.6, Bug 14).

  * 50 threads racing the same URL -> exactly one True.
  * Canonical dedup: query-param order and #fragment variants collapse.
  * Independent instances don't share state (repeated main() safety).
"""
from __future__ import annotations

import threading

from gpt_scraper_v3.pagination_processing import VisitedSet


def test_50_thread_race_single_true():
    vs = VisitedSet()
    url = "https://example.com/suburl"
    results = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(50)

    def worker():
        barrier.wait()
        r = vs.mark(url)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r) == 1
    assert len(results) == 50


def test_canonical_dedup_query_order_and_fragment():
    vs = VisitedSet()
    # First claim wins.
    assert vs.mark("https://example.com/p?a=1&b=2") is True
    # Reordered query params -> same canonical form -> already claimed.
    assert vs.mark("https://example.com/p?b=2&a=1") is False
    # Fragment variant -> stripped -> same canonical form.
    assert vs.mark("https://example.com/p?a=1&b=2#section") is False


def test_distinct_urls_each_claimable():
    vs = VisitedSet()
    assert vs.mark("https://example.com/one") is True
    assert vs.mark("https://example.com/two") is True
    # Re-marking one -> already claimed.
    assert vs.mark("https://example.com/one") is False


def test_independent_instances_isolated():
    a = VisitedSet()
    b = VisitedSet()
    url = "https://example.com/x"
    assert a.mark(url) is True
    # A fresh instance (e.g. a second main() run) starts empty.
    assert b.mark(url) is True
