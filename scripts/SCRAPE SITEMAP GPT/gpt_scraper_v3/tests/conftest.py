"""Shared pytest fixtures for the GPT Scraper V3 test suite.

This conftest provides:

1. ``project_root`` on ``sys.path`` so ``import gpt_scraper_v3`` works no matter
   where pytest is invoked from (the package lives one directory *below* the
   repo root, and the repo root is what must be importable).
2. Config fixtures that populate the module-level ``ScraperConfig`` singleton
   (``config._cfg``), since the whole codebase reaches configuration through the
   ``get_config()`` singleton rather than passing a config object around.
3. Offline fixtures that monkeypatch the network/vector-store seams
   (``utilities.get_session``, ``xml_sitemap.get_last_run_timestamp``) so tests
   never touch the real internet or the OpenAI API.

Design notes
------------
* The codebase loads config exactly once via ``load_configuration()`` which
  assigns the result to ``config._cfg`` (a module global) and returns it.
  ``get_config()`` raises ``RuntimeError`` until that happens. Every other
  module imports the *function* ``get_config`` (not the value), so setting
  ``config._cfg`` is sufficient to make ``get_config()`` return our test object
  everywhere.
* We expose two fixtures:
    - ``make_config``: a *factory* that constructs a ``ScraperConfig`` directly
      (no JSON, no validation) and installs it as the singleton. Tests pass
      field overrides as kwargs (``RSS_FEEDS=[...]``, ``CHECK_LAST_MODIFIED=2``,
      ``BASE_URL=...``, etc.). This is the fast path for unit tests that just
      need a few fields set.
    - ``loaded_config``: drives the *real* ``load_configuration()`` against a
      minimal JSON file written to ``tmp_path``. Use this when a test needs to
      exercise the actual loading/validation/path-derivation logic.
  Both restore the previous singleton on teardown so tests stay isolated.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Dict, Optional

import pytest

# --- Make the repo root importable -------------------------------------------
# This file is at <root>/gpt_scraper_v3/tests/conftest.py, so the repo root
# (which contains the ``gpt_scraper_v3`` package) is three levels up.
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from gpt_scraper_v3 import config as config_module  # noqa: E402
from gpt_scraper_v3.config import ScraperConfig  # noqa: E402


# -- Singleton helpers --------------------------------------------------------

def _install_config(cfg: ScraperConfig) -> ScraperConfig:
    """Install ``cfg`` as the module-level singleton consulted by get_config()."""
    config_module._cfg = cfg
    return cfg


@pytest.fixture
def reset_config_singleton():
    """Save/restore ``config._cfg`` around a test so the singleton never leaks."""
    previous = config_module._cfg
    try:
        yield
    finally:
        config_module._cfg = previous


# -- Direct-construction config factory ---------------------------------------

@pytest.fixture
def make_config(reset_config_singleton) -> Callable[..., ScraperConfig]:
    """Factory that builds a ``ScraperConfig`` and installs it as the singleton.

    Usage::

        def test_something(make_config):
            cfg = make_config(
                BASE_URL="https://example.com",
                RSS_FEEDS=["https://example.com/rss"],
                CHECK_LAST_MODIFIED=2,
            )
            assert get_config().BASE_URL == "https://example.com"

    Any keyword argument that matches a ``ScraperConfig`` field is applied;
    unknown fields raise ``TypeError`` (guards against typos in tests). A small
    set of sensible, fully-offline defaults is provided so the resulting config
    is usable without supplying everything.
    """

    field_names = {f.name for f in ScraperConfig.__dataclass_fields__.values()}

    def _factory(**overrides: Any) -> ScraperConfig:
        unknown = set(overrides) - field_names
        if unknown:
            raise TypeError(
                f"Unknown ScraperConfig field(s) in make_config(): {sorted(unknown)}"
            )

        defaults: Dict[str, Any] = {
            "SCRIPT_NAME": "test_scraper",
            "BASE_URL": "https://example.com",
            "OPENAI_API_KEY": "sk-test-key",
            "OPENAI_VECTOR_STORE_ID": "vs_test",
            "CHECK_LAST_MODIFIED": 1,
            "LAST_RUN_FILE": "test_last_run_time.txt",
            "RSS_LAST_RUN_FILE": "test_rss_last_run_time.txt",
            "SITEMAP_LAST_RUN_FILE": "test_sitemap_last_run_time.txt",
            "KNOWN_URLS_FILE": "test_known_urls.json",
        }
        defaults.update(overrides)

        cfg = ScraperConfig(**defaults)

        # Derive URL-related fields the same way load_configuration() does, so a
        # directly-constructed config behaves consistently when BASE_URL is set.
        if cfg.BASE_URL and cfg.PARSED_BASE_URL is None:
            from urllib.parse import urlparse

            parsed = urlparse(cfg.BASE_URL)
            cfg.PARSED_BASE_URL = parsed
            cfg.BASE_NETLOC = parsed.netloc
            cfg.NON_WWW_BASE_NETLOC = (
                cfg.BASE_NETLOC[4:]
                if cfg.BASE_NETLOC.startswith("www.")
                else cfg.BASE_NETLOC
            )
            cfg.BASE_SCHEME = parsed.scheme

        return _install_config(cfg)

    return _factory


# -- Real-loader config fixture (minimal JSON in tmp_path) --------------------

def _minimal_raw_config() -> Dict[str, Any]:
    """A minimal raw config dict that passes ``validate_config``/``load_configuration``."""
    return {
        "website": {
            "base_url": "https://example.com",
            "sitemap_url": "https://example.com/sitemap",
            "xml_sitemap_url": "https://example.com/sitemap.xml",
            "blacklisted_urls": [],
            "blacklisted_relative_url_paths": [],
            "rss_feeds": [],
            "recursive_urls": [],
            "paginated_urls": [],
            "test_urls": [],
            "IncludeBaseURL": 0,
        },
        "api_keys": {
            # No provider keys required because provider_sequence has none of
            # jina/firecrawl below.
            "openai": "sk-test-openai",
        },
        "content_providers": {
            "jina": {"remove_selectors": "", "target_selectors": ""},
            "firecrawl": {"name": "firecrawl"},
            "provider_sequence": "",
        },
        "vector_store": {
            "id": "vs_test",
            "enable_deduplication": True,
            "chunking_strategy": "auto",
            "max_chunk_size": 800,
            "chunk_overlap": 400,
            "content_token_offset": 0,
            "content_ratio": 0.65,
        },
        "http_settings": {
            "request_timeout": 30,
            "retry_codes": [500, 502, 503, 504, 524],
            "retry_count": 3,
            "backoff_factor": 0.3,
        },
        "processing": {
            "check_last_modified": 1,
            "max_filename_length": 200,
        },
        "script_info": {"name": "scrape_sitemap_test_v3", "version": "3.0.0"},
    }


@pytest.fixture
def write_config_json(tmp_path) -> Callable[..., str]:
    """Return a factory that writes a (possibly customized) config JSON to tmp_path.

    The factory accepts an optional ``overrides`` dict that is deep-merged into
    the minimal base config, plus an optional ``filename`` (used to derive the
    config *identifier* and therefore the per-run file names).
    """

    def _deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(base)
        for k, v in extra.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = _deep_merge(out[k], v)
            else:
                out[k] = v
        return out

    def _factory(
        overrides: Optional[Dict[str, Any]] = None,
        filename: str = "scrape_sitemap_GPT_config_TestSite_v3.json",
    ) -> str:
        raw = _minimal_raw_config()
        if overrides:
            raw = _deep_merge(raw, overrides)
        path = tmp_path / filename
        path.write_text(json.dumps(raw), encoding="utf-8")
        return str(path)

    return _factory


@pytest.fixture
def loaded_config(write_config_json, reset_config_singleton, monkeypatch, tmp_path):
    """Run the *real* ``load_configuration()`` against a minimal tmp_path JSON.

    Returns a factory: ``loaded_config(overrides=None, filename=...)`` ->
    ``ScraperConfig``. The fixture cd's into ``tmp_path`` first so the
    identifier-derived relative output/log paths land in the temp dir rather
    than the repo. The singleton is restored on teardown by
    ``reset_config_singleton``.
    """
    monkeypatch.chdir(tmp_path)

    def _factory(
        overrides: Optional[Dict[str, Any]] = None,
        filename: str = "scrape_sitemap_GPT_config_TestSite_v3.json",
    ) -> ScraperConfig:
        cfg_path = write_config_json(overrides=overrides, filename=filename)
        return config_module.load_configuration(cfg_path)

    return _factory


# -- Offline network / vector-store fixtures ----------------------------------

class _FakeResponse:
    """Minimal stand-in for ``requests.Response`` used by offline tests."""

    def __init__(self, status_code: int = 200, json_data: Any = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text
        self.content = text.encode("utf-8")

    def json(self) -> Any:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """A requests.Session look-alike whose verbs all return a benign response.

    Tests can inspect ``calls`` (a list of ``(method, url)`` tuples) or override
    behavior by reassigning ``response_factory``. By default every call returns
    an empty 200 response, guaranteeing no real network access.
    """

    def __init__(self):
        self.calls: list = []
        self.response_factory: Callable[..., _FakeResponse] = lambda *a, **k: _FakeResponse()

    def _record(self, method: str, url: str, **kwargs) -> _FakeResponse:
        self.calls.append((method, url))
        return self.response_factory(method, url, **kwargs)

    def get(self, url, **kwargs):
        return self._record("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._record("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._record("DELETE", url, **kwargs)

    def put(self, url, **kwargs):
        return self._record("PUT", url, **kwargs)

    def close(self):
        pass


@pytest.fixture
def fake_session() -> _FakeSession:
    """Provide a fresh fake session object (not yet installed)."""
    return _FakeSession()


@pytest.fixture
def offline(monkeypatch, fake_session):
    """Patch the network + timestamp seams so tests run fully offline.

    Patches:
      * ``utilities.get_session`` -> returns ``fake_session`` (covers all HTTP,
        including every OpenAI vector-store call which routes through
        ``get_session()``).
      * ``xml_sitemap.get_last_run_timestamp`` -> returns ``None`` by default
        (first-run semantics). Override per-test via
        ``offline.set_last_run(dt)`` or ``offline.last_run_map[...]``.

    Returns a small controller object exposing ``session`` and helpers.
    """
    from gpt_scraper_v3 import utilities as utilities_module
    from gpt_scraper_v3 import xml_sitemap as xml_sitemap_module

    monkeypatch.setattr(utilities_module, "get_session", lambda: fake_session)

    class _OfflineController:
        def __init__(self):
            self.session = fake_session
            # Map of timestamp_type -> datetime|None; default returns None.
            self.last_run_map: Dict[str, Any] = {}
            self._default_last_run: Any = None

        def _get_last_run(self, timestamp_type: str = "combined"):
            return self.last_run_map.get(timestamp_type, self._default_last_run)

        def set_last_run(self, value, timestamp_type: Optional[str] = None):
            if timestamp_type is None:
                self._default_last_run = value
            else:
                self.last_run_map[timestamp_type] = value

    controller = _OfflineController()
    monkeypatch.setattr(
        xml_sitemap_module, "get_last_run_timestamp", controller._get_last_run
    )

    return controller
