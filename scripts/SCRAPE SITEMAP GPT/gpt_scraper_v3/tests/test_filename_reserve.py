"""Filename reservation + crash-safe writes (Wave 1.3).

  * reserve_unique_filepath_fn: 8 threads on the same base -> 8 distinct files.
  * write_content_to_reserved_path failure -> no 0-byte file left behind.
  * build_local_files_cache skips 0-byte files (xml_sitemap.py).
"""
from __future__ import annotations

import os
import threading

import pytest

from gpt_scraper_v3 import utilities as u
from gpt_scraper_v3 import xml_sitemap as xs


def _name_fn(base_dir, stem):
    def fn(counter):
        suffix = "" if counter == 0 else f"_V{counter}"
        return os.path.join(base_dir, f"{stem}{suffix}.txt")
    return fn


def test_concurrent_reservation_distinct_files(tmp_path):
    reserved = []
    reserved_lock = threading.Lock()
    barrier = threading.Barrier(8)
    name_fn = _name_fn(str(tmp_path), "doc")

    def worker():
        barrier.wait()
        path = u.reserve_unique_filepath_fn(name_fn)
        with reserved_lock:
            reserved.append(path)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(reserved) == 8
    # All distinct.
    assert len(set(reserved)) == 8
    # All exist (0-byte placeholders).
    for p in reserved:
        assert os.path.exists(p)


def test_write_failure_leaves_no_empty_file(tmp_path, monkeypatch):
    name_fn = _name_fn(str(tmp_path), "fail")
    path = u.reserve_unique_filepath_fn(name_fn)
    assert os.path.exists(path)

    # Force os.replace to fail AFTER the temp file is written.
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(u.os, "replace", boom)

    with pytest.raises(OSError):
        u.write_content_to_reserved_path(path, "some real content")

    monkeypatch.setattr(u.os, "replace", real_replace)
    # Neither the placeholder nor any temp file should remain.
    assert not os.path.exists(path), "0-byte placeholder left behind after failure"
    leftover_temps = [f for f in os.listdir(tmp_path) if f.startswith("fail")]
    assert leftover_temps == [], f"temp/landmine files left: {leftover_temps}"


def test_successful_write_replaces_placeholder(tmp_path):
    name_fn = _name_fn(str(tmp_path), "ok")
    path = u.reserve_unique_filepath_fn(name_fn)
    u.write_content_to_reserved_path(path, "hello world")
    assert os.path.exists(path)
    assert os.path.getsize(path) > 0
    assert path.endswith("ok.txt")
    # No stray temp files.
    assert sorted(os.listdir(tmp_path)) == ["ok.txt"]


def test_build_local_files_cache_skips_zero_byte(tmp_path):
    # A 0-byte file (crash placeholder) must not poison the resume cache.
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")

    good = tmp_path / "good.txt"
    good.write_text(
        "## **ZDROJOVÁ URL:**\n### **https://example.com/real-page**\n\nbody",
        encoding="utf-8",
    )

    cache = xs.build_local_files_cache(str(tmp_path))
    # Only the good (non-empty) file with an extractable URL is indexed.
    assert any("real-page" in k for k in cache)
    # No cache entry points at the empty placeholder.
    assert all("empty.txt" != v["filename"] for v in cache.values())
