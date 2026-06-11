"""tiktoken encoder memoization + fallback (Wave 3.4, Bug 5).

  * _get_tiktoken_encoder() builds the encoder once (lru_cache) across many
    count_tokens_approximate() calls.
  * On import/build failure: caller warns ONCE and uses the /3.2 char fallback.
"""
from __future__ import annotations

import logging

from gpt_scraper_v3 import utilities as u


def _clear_state():
    u._get_tiktoken_encoder.cache_clear()
    u._tiktoken_warned = False


def test_encoder_built_once_across_many_calls(monkeypatch):
    _clear_state()
    try:
        build_count = {"n": 0}

        class _FakeEnc:
            def encode(self, text):
                return list(text)  # 1 token per char (deterministic)

        class _FakeTiktoken:
            def encoding_for_model(self, model):
                build_count["n"] += 1
                return _FakeEnc()

        import sys
        monkeypatch.setitem(sys.modules, "tiktoken", _FakeTiktoken())

        for _ in range(50):
            assert u.count_tokens_approximate("hello") == 5

        # lru_cache(maxsize=1) -> encoder constructed exactly once.
        assert build_count["n"] == 1
    finally:
        _clear_state()


def test_fallback_warns_once_and_uses_ratio(monkeypatch, caplog):
    _clear_state()
    try:
        import sys
        # Force the import inside _get_tiktoken_encoder to fail every call.
        monkeypatch.setitem(sys.modules, "tiktoken", None)

        text = "a" * 32  # 32 / 3.2 == 10
        with caplog.at_level(logging.WARNING, logger=u.logger.name):
            for _ in range(10):
                assert u.count_tokens_approximate(text) == 10

        warnings = [r for r in caplog.records
                    if "tiktoken unavailable" in r.getMessage()]
        # Warned exactly once despite 10 calls.
        assert len(warnings) == 1
    finally:
        _clear_state()


def test_lru_cache_does_not_memoize_exceptions(monkeypatch):
    """A transient failure must NOT become a permanent 'no encoder' state:
    once tiktoken becomes importable, the encoder is built and used."""
    _clear_state()
    try:
        import sys

        # First: failing import.
        monkeypatch.setitem(sys.modules, "tiktoken", None)
        text = "a" * 32
        assert u.count_tokens_approximate(text) == 10  # fallback path

        # Now make tiktoken importable; lru_cache did not memoize the failure.
        class _FakeEnc:
            def encode(self, t):
                return list(t)

        class _FakeTiktoken:
            def encoding_for_model(self, model):
                return _FakeEnc()

        monkeypatch.setitem(sys.modules, "tiktoken", _FakeTiktoken())
        assert u.count_tokens_approximate("hello") == 5  # tiktoken path now works
    finally:
        _clear_state()


def test_empty_text_zero():
    _clear_state()
    try:
        assert u.count_tokens_approximate("") == 0
    finally:
        _clear_state()
