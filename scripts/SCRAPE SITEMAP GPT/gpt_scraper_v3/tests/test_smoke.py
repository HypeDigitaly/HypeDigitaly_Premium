"""Smoke tests proving the pytest harness and fixtures work end-to-end.

These do NOT test any feature behavior -- they only verify that:
  * the package and its core modules import,
  * the config fixtures install a usable ScraperConfig singleton,
  * the real loader fixture runs against a minimal JSON,
  * the offline fixture neutralizes the network/timestamp seams.

Later waves add the actual RSS / dedup / migration tests.
"""
from __future__ import annotations


def test_package_imports():
    """Core gpt_scraper_v3 modules import without side effects."""
    import gpt_scraper_v3  # noqa: F401
    from gpt_scraper_v3 import config  # noqa: F401
    from gpt_scraper_v3 import utilities  # noqa: F401
    from gpt_scraper_v3 import xml_sitemap  # noqa: F401
    from gpt_scraper_v3 import vector_store  # noqa: F401
    from gpt_scraper_v3 import url_processing  # noqa: F401
    from gpt_scraper_v3 import rss_feeds  # noqa: F401

    assert gpt_scraper_v3.__version__


def test_get_config_raises_before_load():
    """get_config() must raise until a config is installed (singleton contract)."""
    import gpt_scraper_v3.config as config_module

    # The reset fixture is not in play here, but if no test loaded a config the
    # singleton is None. Guard the assertion so order-independence holds: either
    # it's None (raises) or some other test left one installed -- we only assert
    # the "raises when None" branch.
    if config_module._cfg is None:
        import pytest

        with pytest.raises(RuntimeError):
            config_module.get_config()


def test_make_config_factory_installs_singleton(make_config):
    """make_config() builds a ScraperConfig and get_config() returns it."""
    from gpt_scraper_v3.config import get_config

    cfg = make_config(
        BASE_URL="https://test.example.com",
        RSS_FEEDS=["https://test.example.com/rss"],
        RSS_DATE_THRESHOLD=None,
        CHECK_LAST_MODIFIED=2,
        KNOWN_URLS_FILE="smoke_known_urls.json",
    )

    assert get_config() is cfg
    assert cfg.BASE_URL == "https://test.example.com"
    assert cfg.RSS_FEEDS == ["https://test.example.com/rss"]
    assert cfg.CHECK_LAST_MODIFIED == 2
    assert cfg.KNOWN_URLS_FILE == "smoke_known_urls.json"
    # Derived URL fields populated by the factory.
    assert cfg.BASE_NETLOC == "test.example.com"
    assert cfg.BASE_SCHEME == "https"


def test_make_config_rejects_unknown_field(make_config):
    """Typos in field names fail loudly rather than silently no-op'ing."""
    import pytest

    with pytest.raises(TypeError):
        make_config(NOT_A_REAL_FIELD=123)


def test_loaded_config_runs_real_loader(loaded_config):
    """The real load_configuration() path works against a minimal JSON."""
    from gpt_scraper_v3.config import get_config

    cfg = loaded_config(
        overrides={
            "website": {"base_url": "https://loaded.example.com"},
            "processing": {"check_last_modified": 2},
        }
    )

    assert get_config() is cfg
    assert cfg.BASE_URL == "https://loaded.example.com"
    assert cfg.CHECK_LAST_MODIFIED == 2
    # Identifier-derived file name. The default filename is
    # "scrape_sitemap_GPT_config_TestSite_v3.json"; extract_config_identifier
    # captures everything after "_config_", i.e. "TestSite_v3".
    assert cfg.KNOWN_URLS_FILE == "TestSite_v3_known_urls.json"


def test_offline_fixture_blocks_network(offline, make_config):
    """get_session() is patched and get_last_run_timestamp() returns our value."""
    make_config()  # vector_store / utilities call get_config() internally.

    from gpt_scraper_v3 import utilities
    from gpt_scraper_v3 import xml_sitemap

    # get_session is now our fake; calling a verb records but never hits network.
    session = utilities.get_session()
    assert session is offline.session
    resp = session.get("https://api.openai.com/v1/vector_stores/vs_test/files")
    assert resp.status_code == 200
    assert offline.session.calls[-1][0] == "GET"

    # Timestamp seam returns None (first-run) by default, and is overridable.
    assert xml_sitemap.get_last_run_timestamp("rss") is None
    offline.set_last_run("SENTINEL", timestamp_type="rss")
    assert xml_sitemap.get_last_run_timestamp("rss") == "SENTINEL"
    assert xml_sitemap.get_last_run_timestamp("combined") is None
