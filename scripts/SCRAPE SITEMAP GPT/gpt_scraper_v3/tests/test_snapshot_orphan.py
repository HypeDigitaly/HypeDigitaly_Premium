"""Snapshot orphan preservation (Wave 3.5, Bug 7).

When URLs disappear from the sitemap but the vector-store cleanup CANNOT run
(no vector store ID, or no cache available — e.g. --skip-vector-cache), the
removed URLs must be KEPT in the saved snapshot. Otherwise they would never be
re-detected as removed and their vector-store files would be orphaned forever.

The branch lives inline in cli.main(). We test the underlying invariant by
replicating the minimal branch conditions (mirroring cli.py:899-941) and
asserting the saved snapshot inputs, then verifying the real
save_known_urls_snapshot persists exactly that set.
"""
from __future__ import annotations

import json

from gpt_scraper_v3 import xml_sitemap as xs


def _snapshot_save_inputs(previous_known_urls, current_known_urls, vs_id, vs_cache):
    """Replicates cli.main()'s Bug-7 snapshot branch, returning the set that
    would be passed to save_known_urls_snapshot (or None if the branch skips)."""
    if not current_known_urls:
        return None  # empty sitemap guard: skip save

    removed_urls = set()
    if previous_known_urls:
        removed_urls = previous_known_urls - current_known_urls

    # >50% removal safety guard: skip BOTH cleanup AND save.
    if previous_known_urls and len(removed_urls) > len(previous_known_urls) * 0.5:
        return None

    result = set(current_known_urls)
    if removed_urls and vs_id and vs_cache is not None:
        # cleanup would run; failed_urls (none here) would be re-added.
        pass
    elif removed_urls:
        # Bug 7: cleanup did NOT run -> preserve removed URLs in the snapshot.
        result.update(removed_urls)
    return result


def test_removed_urls_preserved_when_cleanup_skipped():
    previous = {
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/gone",
    }
    current = {"https://example.com/a", "https://example.com/b"}

    # No vs_id and/or vs_cache None -> cleanup cannot run.
    saved = _snapshot_save_inputs(previous, current, vs_id="", vs_cache=None)
    assert saved is not None
    # The removed URL MUST be preserved for future cleanup.
    assert "https://example.com/gone" in saved
    assert saved == previous  # current + removed == previous here


def test_removed_urls_not_preserved_when_cleanup_runs():
    previous = {"https://example.com/a", "https://example.com/gone"}
    current = {"https://example.com/a"}

    # vs_id and vs_cache present -> cleanup runs; removed URL is dropped from snapshot.
    saved = _snapshot_save_inputs(previous, current, vs_id="vs_test", vs_cache={})
    assert saved == {"https://example.com/a"}
    assert "https://example.com/gone" not in saved


def test_over_50_percent_removal_skips_save():
    previous = {f"https://example.com/{i}" for i in range(10)}
    current = {"https://example.com/0"}  # 9/10 removed -> >50%
    saved = _snapshot_save_inputs(previous, current, vs_id="", vs_cache=None)
    assert saved is None  # both cleanup and save skipped


def test_empty_sitemap_skips_save():
    previous = {"https://example.com/a"}
    saved = _snapshot_save_inputs(previous, set(), vs_id="vs", vs_cache={})
    assert saved is None


def test_save_known_urls_persists_preserved_set(make_config, tmp_path):
    """End-to-end: the preserved set round-trips through the real snapshot save."""
    snap_file = tmp_path / "known_urls.json"
    make_config(KNOWN_URLS_FILE=str(snap_file))

    previous = {"https://example.com/a", "https://example.com/gone"}
    current = {"https://example.com/a"}
    saved = _snapshot_save_inputs(previous, current, vs_id="", vs_cache=None)
    assert saved is not None

    xs.save_known_urls_snapshot(saved)
    assert snap_file.exists()
    assert not (tmp_path / "known_urls.json.tmp").exists()
    on_disk = set(json.loads(snap_file.read_text(encoding="utf-8")))
    assert "https://example.com/gone" in on_disk
