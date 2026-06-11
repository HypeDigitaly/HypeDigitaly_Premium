"""Persistent VS cache snapshot (Wave 2.2).

  * load/save_vs_cache_snapshot round-trip.
  * Corrupt / wrong-shape file -> {} fallback (silent full rebuild).
  * build_vector_store_cache warm path: attribute GETs only for NEW file_ids,
    content identical to a cold rebuild.
"""
from __future__ import annotations

import json

from gpt_scraper_v3 import vector_store as vs


def test_save_load_roundtrip(make_config, tmp_path):
    snap = tmp_path / "vs_cache.json"
    make_config(VS_CACHE_FILE=str(snap))

    # In-memory cache is keyed by normalized lookup URL -> {file_id, attributes, ...}
    cache = {
        "https://example.com/a": {
            "file_id": "file_a",
            "file_info": {"id": "file_a"},  # NOT persisted
            "attributes": {"lookup_source_url": "https://example.com/a"},
        },
        "https://example.com/b": {
            "file_id": "file_b",
            "file_info": {"id": "file_b"},
            "attributes": {"lookup_source_url": "https://example.com/b"},
        },
    }
    vs.save_vs_cache_snapshot(cache)
    assert snap.exists()
    assert not (tmp_path / "vs_cache.json.tmp").exists()

    loaded = vs.load_vs_cache_snapshot()
    # Snapshot is keyed by file_id -> {"attributes": {...}}.
    assert set(loaded.keys()) == {"file_a", "file_b"}
    assert loaded["file_a"]["attributes"]["lookup_source_url"] == "https://example.com/a"
    # file_info is NOT persisted.
    assert "file_info" not in loaded["file_a"]


def test_load_missing_file_returns_empty(make_config, tmp_path):
    make_config(VS_CACHE_FILE=str(tmp_path / "absent.json"))
    assert vs.load_vs_cache_snapshot() == {}


def test_load_corrupt_file_returns_empty(make_config, tmp_path):
    snap = tmp_path / "vs_cache.json"
    snap.write_text("{ this is not valid json", encoding="utf-8")
    make_config(VS_CACHE_FILE=str(snap))
    assert vs.load_vs_cache_snapshot() == {}


def test_load_wrong_shape_returns_empty(make_config, tmp_path):
    snap = tmp_path / "vs_cache.json"
    snap.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    make_config(VS_CACHE_FILE=str(snap))
    assert vs.load_vs_cache_snapshot() == {}


def test_load_filters_malformed_entries(make_config, tmp_path):
    snap = tmp_path / "vs_cache.json"
    snap.write_text(json.dumps({
        "good_id": {"attributes": {"lookup_source_url": "https://x/y"}},
        "bad_no_attrs": {"something": 1},
        "bad_attrs_type": {"attributes": "not-a-dict"},
    }), encoding="utf-8")
    make_config(VS_CACHE_FILE=str(snap))
    loaded = vs.load_vs_cache_snapshot()
    assert set(loaded.keys()) == {"good_id"}


def test_build_warm_path_fetches_only_new_ids(make_config, tmp_path, monkeypatch):
    """Warm build: a persisted snapshot for file_a means only file_b (new) gets
    an attribute GET, and the resulting cache matches a cold rebuild."""
    snap = tmp_path / "vs_cache.json"
    make_config(VS_CACHE_FILE=str(snap), AUX_PARALLEL_WORKERS=4)

    # Persisted snapshot already knows file_a's attributes.
    snap.write_text(json.dumps({
        "file_a": {"attributes": {"lookup_source_url": "https://example.com/a"}},
    }), encoding="utf-8")

    # The live store lists both file_a (known) and file_b (new).
    monkeypatch.setattr(vs, "list_vector_store_files", lambda vsid: [
        {"id": "file_a"}, {"id": "file_b"},
    ])

    attr_calls = []

    def fake_get_attrs(vsid, fid):
        attr_calls.append(fid)
        return {"lookup_source_url": f"https://example.com/{'a' if fid == 'file_a' else 'b'}"}

    monkeypatch.setattr(vs, "get_vector_store_file_attributes", fake_get_attrs)

    cache = vs.build_vector_store_cache("vs_test")

    # Only file_b (the snapshot miss) triggered an attribute GET.
    assert attr_calls == ["file_b"]
    # Cache content is the full expected set (file_a reused from snapshot).
    assert set(cache.keys()) == {"https://example.com/a", "https://example.com/b"}
    assert cache["https://example.com/a"]["file_id"] == "file_a"
    assert cache["https://example.com/b"]["file_id"] == "file_b"


def test_build_cold_equals_warm_content(make_config, tmp_path, monkeypatch):
    """A cold rebuild (no snapshot) produces the same cache content as the warm
    path above."""
    make_config(VS_CACHE_FILE=str(tmp_path / "vs_cache.json"), AUX_PARALLEL_WORKERS=4)

    monkeypatch.setattr(vs, "list_vector_store_files", lambda vsid: [
        {"id": "file_a"}, {"id": "file_b"},
    ])
    monkeypatch.setattr(vs, "get_vector_store_file_attributes", lambda vsid, fid: {
        "lookup_source_url": f"https://example.com/{'a' if fid == 'file_a' else 'b'}"
    })

    cache = vs.build_vector_store_cache("vs_test")
    assert set(cache.keys()) == {"https://example.com/a", "https://example.com/b"}
    assert cache["https://example.com/a"]["file_id"] == "file_a"
    assert cache["https://example.com/b"]["file_id"] == "file_b"
