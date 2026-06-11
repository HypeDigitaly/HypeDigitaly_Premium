"""Application-tier 429/Retry-After handling (Wave 3.3, Bug 3).

with_429_retry:
  * 429(Retry-After:0.05) x2 then 200 -> 3 fn calls, final response is the 200.
  * Exhaustion returns the LAST 429 response WITHOUT raising.
  * Unparseable Retry-After -> exponential backoff path is taken (no crash).
"""
from __future__ import annotations

import logging

from gpt_scraper_v3.retry_util import with_429_retry, _parse_retry_after


class _FakeResp:
    def __init__(self, status_code, retry_after=None):
        self.status_code = status_code
        self.headers = {}
        if retry_after is not None:
            self.headers["Retry-After"] = retry_after


class _Caller:
    """A callable returning a scripted sequence of responses, counting calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        idx = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[idx]


def test_retries_then_succeeds():
    caller = _Caller([
        _FakeResp(429, retry_after="0.05"),
        _FakeResp(429, retry_after="0.05"),
        _FakeResp(200),
    ])
    resp = with_429_retry(caller, max_attempts=5, cap_seconds=1.0)
    assert resp.status_code == 200
    assert caller.calls == 3


def test_exhaustion_returns_429_without_raising():
    caller = _Caller([_FakeResp(429, retry_after="0.05")])  # always 429
    resp = with_429_retry(caller, max_attempts=3, cap_seconds=1.0)
    # No exception raised; the last 429 response is returned.
    assert resp.status_code == 429
    assert caller.calls == 3  # max_attempts invocations


def test_first_response_success_no_retry():
    caller = _Caller([_FakeResp(200)])
    resp = with_429_retry(caller, max_attempts=3, cap_seconds=1.0)
    assert resp.status_code == 200
    assert caller.calls == 1


def test_non_429_error_returned_immediately():
    caller = _Caller([_FakeResp(500)])
    resp = with_429_retry(caller, max_attempts=3, cap_seconds=1.0)
    # 500 is not 429 -> returned on the first call (transport layer handles it).
    assert resp.status_code == 500
    assert caller.calls == 1


def test_unparseable_retry_after_uses_backoff(monkeypatch):
    """An unparseable Retry-After must fall back to exponential backoff."""
    # The exp-backoff path is 2 ** (attempt-1); for attempt 1 that's 1.0s. Verify
    # _parse_retry_after returns the backoff value (not a crash) for garbage.
    resp = _FakeResp(429, retry_after="soon")
    delay = _parse_retry_after(resp, attempt=1)
    assert delay == 1.0  # 2 ** 0
    delay2 = _parse_retry_after(resp, attempt=2)
    assert delay2 == 2.0  # 2 ** 1

    # And the full retry loop survives an unparseable header (no exception),
    # with the cap keeping the sleep small.
    sleeps = []
    monkeypatch.setattr("gpt_scraper_v3.retry_util.time.sleep", lambda s: sleeps.append(s))
    caller = _Caller([
        _FakeResp(429, retry_after="not-a-number"),
        _FakeResp(200),
    ])
    resp = with_429_retry(caller, max_attempts=3, cap_seconds=0.01,
                          logger=logging.getLogger("t"))
    assert resp.status_code == 200
    assert caller.calls == 2
    # Base delay was capped at 0.01; jitter adds <= 0.5*attempt.
    assert sleeps and sleeps[0] <= 0.01 + 0.5
