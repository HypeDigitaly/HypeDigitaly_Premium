"""Tests for the one-time fragment-key cleanup migration script (Wave 4).

Covers:
  * ``classify_key`` -- explicit table including the multi-letter chunk
    (``chunkAA``), single-segment paginated (``PAGE2chunkA``), polluted
    (``zou#chunkA``), and bare/unknown cases. ``summarizedA`` is asserted to be
    UNKNOWN (the suffix is never generated -- see the script's grep finding).
  * Orphan decision -- a fragment-polluted key is only marked DELETE when its
    canonical base has a legit key in the store OR appears in the XML snapshot;
    otherwise it is SKIPPED (never orphaned).
  * Dry-run safety -- ``main()`` without ``--apply`` deletes NOTHING.
  * ``apply_deletions`` -- only polluted keys flagged DELETE are deleted; legit
    and unknown keys are never touched; cache entries are invalidated.

All OpenAI access is mocked (offline): ``build_vector_store_cache`` and
``delete_vector_store_file`` are monkeypatched on the cleanup module.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from gpt_scraper_v3 import cleanup_fragment_keys as cfk


BASE = "https://e.com/o-webu"


# -- classify_key table -------------------------------------------------------

@pytest.mark.parametrize(
    "key, expected_cls, expected_base",
    [
        (f"{BASE}#chunkA", cfk.LEGIT, BASE),               # single-letter chunk
        (f"{BASE}#chunkAA", cfk.LEGIT, BASE),              # multi-letter chunk
        (f"{BASE}#PAGE2chunkA", cfk.LEGIT, BASE),          # paginated, single segment
        (f"{BASE}#PAGE10chunkBB", cfk.LEGIT, BASE),        # paginated multi-digit/letter
        (f"{BASE}#summarizedA", cfk.UNKNOWN, None),        # NEVER generated -> unknown
        (f"{BASE}#zou#chunkA", cfk.FRAGMENT_POLLUTED, BASE),   # polluted
        (f"{BASE}#accessibility#chunkA", cfk.FRAGMENT_POLLUTED, BASE),
        (f"{BASE}#cookies#PAGE2chunkA", cfk.FRAGMENT_POLLUTED, BASE),  # polluted + paginated
        (f"{BASE}#zou", cfk.UNKNOWN, None),                # single non-suffix fragment
        (BASE, cfk.UNKNOWN, None),                         # bare url, no fragment
        (f"{BASE}#chunkA#chunkB", cfk.UNKNOWN, None),      # first frag IS a suffix -> ambiguous
    ],
)
def test_classify_key_table(key: str, expected_cls: str, expected_base):
    cls, base = cfk.classify_key(key)
    assert cls == expected_cls
    assert base == expected_base


# -- Orphan decision ----------------------------------------------------------

def _cache(*keys: str) -> Dict[str, Any]:
    """Build a minimal vector-store-style cache dict for the given keys."""
    return {
        k: {"file_id": f"file-{i}", "file_info": {"id": f"file-{i}"}}
        for i, k in enumerate(keys)
    }


def _decision_for(decisions, key):
    return next(d for d in decisions if d.cache_key == key)


def test_polluted_deleted_when_base_has_legit_key():
    """Polluted key deleted because a legit chunk of the same base exists."""
    cache = _cache(f"{BASE}#zou#chunkA", f"{BASE}#chunkA")
    decisions = cfk.build_decisions(cache, snapshot_urls=set())

    polluted = _decision_for(decisions, f"{BASE}#zou#chunkA")
    legit = _decision_for(decisions, f"{BASE}#chunkA")

    assert polluted.classification == cfk.FRAGMENT_POLLUTED
    assert polluted.base_has_legit is True
    assert polluted.action == cfk.ACTION_DELETE
    assert legit.action == cfk.ACTION_KEEP


def test_polluted_deleted_when_base_in_snapshot():
    """Polluted key deleted because the base is in the current XML snapshot,
    even though no legit chunk exists in the store."""
    cache = _cache(f"{BASE}#zou#chunkA")
    decisions = cfk.build_decisions(cache, snapshot_urls={BASE})

    polluted = _decision_for(decisions, f"{BASE}#zou#chunkA")
    assert polluted.base_has_legit is False
    assert polluted.base_in_snapshot is True
    assert polluted.action == cfk.ACTION_DELETE


def test_polluted_skipped_when_only_copy():
    """Polluted key is the ONLY representation of the page -> never orphaned."""
    cache = _cache(f"{BASE}#zou#chunkA")
    decisions = cfk.build_decisions(cache, snapshot_urls=set())

    polluted = _decision_for(decisions, f"{BASE}#zou#chunkA")
    assert polluted.classification == cfk.FRAGMENT_POLLUTED
    assert polluted.base_has_legit is False
    assert polluted.base_in_snapshot is False
    assert polluted.action == cfk.ACTION_SKIP


def test_unknown_never_deleted():
    cache = _cache(f"{BASE}#summarizedA", f"{BASE}#zou")
    decisions = cfk.build_decisions(cache, snapshot_urls={BASE})
    for d in decisions:
        assert d.classification == cfk.UNKNOWN
        assert d.action == cfk.ACTION_SKIP


def test_summary_counts():
    cache = _cache(
        f"{BASE}#chunkA",         # legit
        f"{BASE}#zou#chunkA",     # polluted -> delete (base has legit)
        f"{BASE}#summarizedA",    # unknown
    )
    decisions = cfk.build_decisions(cache, snapshot_urls=set())
    counts = cfk.summarize(decisions)
    assert counts[cfk.LEGIT] == 1
    assert counts[cfk.FRAGMENT_POLLUTED] == 1
    assert counts[cfk.UNKNOWN] == 1
    assert counts["would_delete"] == 1


# -- apply_deletions ----------------------------------------------------------

def test_apply_deletions_only_targets_delete_actions(monkeypatch):
    cache = _cache(
        f"{BASE}#chunkA",         # legit -> keep
        f"{BASE}#zou#chunkA",     # polluted -> delete (base has legit)
        f"{BASE}#summarizedA",    # unknown -> skip
    )
    decisions = cfk.build_decisions(cache, snapshot_urls=set())

    deleted_ids: List[str] = []

    def _fake_delete(vs_id: str, file_id: str) -> bool:
        deleted_ids.append(file_id)
        return True

    monkeypatch.setattr(cfk, "delete_vector_store_file", _fake_delete)

    deleted, failed = cfk.apply_deletions(decisions, "vs_test", cache)
    assert deleted == 1
    assert failed == 0
    # Exactly the polluted key's file deleted; legit + unknown untouched.
    assert len(deleted_ids) == 1
    # Cache entry for the deleted key is invalidated; the others remain.
    assert f"{BASE}#zou#chunkA" not in cache
    assert f"{BASE}#chunkA" in cache
    assert f"{BASE}#summarizedA" in cache


def test_apply_deletions_failure_is_counted_and_continues(monkeypatch):
    cache = _cache(
        f"{BASE}#zou#chunkA", f"{BASE}#cookies#chunkA", f"{BASE}#chunkA",
    )
    decisions = cfk.build_decisions(cache, snapshot_urls=set())

    def _fake_delete(vs_id: str, file_id: str) -> bool:
        # Fail the first, succeed the rest.
        return file_id != "file-0"

    monkeypatch.setattr(cfk, "delete_vector_store_file", _fake_delete)
    deleted, failed = cfk.apply_deletions(decisions, "vs_test", cache)
    assert deleted == 1
    assert failed == 1


# -- main() dry-run safety ----------------------------------------------------

def _install_offline_main(monkeypatch, cache: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Patch build_vector_store_cache + delete_vector_store_file on the module.

    Returns the list that records every delete call so a test can assert it
    stays empty in dry-run.
    """
    delete_calls: List[Tuple[str, str]] = []

    monkeypatch.setattr(cfk, "build_vector_store_cache", lambda vs_id: dict(cache))

    def _fake_delete(vs_id: str, file_id: str) -> bool:
        delete_calls.append((vs_id, file_id))
        return True

    monkeypatch.setattr(cfk, "delete_vector_store_file", _fake_delete)
    return delete_calls


def test_main_dry_run_with_real_config_deletes_nothing(monkeypatch, write_config_json, reset_config_singleton, capsys):
    """main() with a real --config path, dry-run -> zero deletions."""
    cfg_path = write_config_json()

    cache = _cache(
        f"{BASE}#chunkA",
        f"{BASE}#zou#chunkA",
        f"{BASE}#summarizedA",
    )
    delete_calls = _install_offline_main(monkeypatch, cache)
    # Avoid touching the real snapshot file on disk.
    monkeypatch.setattr(cfk, "load_known_urls_snapshot", lambda: set())

    rc = cfk.main(["--config", cfg_path, "--vector-store-id", "vs_test"])
    out = capsys.readouterr().out

    assert rc == 0
    assert delete_calls == []
    assert "DRY-RUN" in out
    assert "fragment_polluted: 1" in out
    assert "WOULD delete:      1" in out


def test_main_apply_deletes_polluted_only(monkeypatch, write_config_json, reset_config_singleton, capsys):
    """main() with --apply deletes the polluted key only; legit/unknown kept."""
    cfg_path = write_config_json()

    cache = _cache(
        f"{BASE}#chunkA",
        f"{BASE}#zou#chunkA",
        f"{BASE}#summarizedA",
    )
    delete_calls = _install_offline_main(monkeypatch, cache)
    monkeypatch.setattr(cfk, "load_known_urls_snapshot", lambda: set())

    rc = cfk.main(["--config", cfg_path, "--vector-store-id", "vs_test", "--apply"])
    assert rc == 0
    # Exactly one delete call, for the polluted key's file id.
    assert len(delete_calls) == 1


# -- local _V pruning ---------------------------------------------------------

def test_plan_local_version_pruning_keeps_base_and_highest(tmp_path):
    d = tmp_path
    # Base file + V1..V3 for one base; only V1 for another.
    for fn in ("O_Webu_A.txt", "O_Webu_A_V1.txt", "O_Webu_A_V2.txt",
               "O_Webu_A_V3.txt", "Rss_A.txt", "Rss_A_V1.txt"):
        (d / fn).write_text("x", encoding="utf-8")

    to_delete, to_keep = cfk.plan_local_version_pruning(str(d))
    to_delete_names = {__import__("os").path.basename(p) for p in to_delete}
    to_keep_names = {__import__("os").path.basename(p) for p in to_keep}

    # Highest version kept for each base.
    assert to_keep_names == {"O_Webu_A_V3.txt", "Rss_A_V1.txt"}
    # Lower versions deleted; base (non-versioned) files NOT in either list.
    assert to_delete_names == {"O_Webu_A_V1.txt", "O_Webu_A_V2.txt"}


def test_run_local_version_pruning_dry_run_keeps_files(tmp_path, capsys):
    d = tmp_path
    for fn in ("X_A.txt", "X_A_V1.txt", "X_A_V2.txt"):
        (d / fn).write_text("x", encoding="utf-8")

    deleted, failed = cfk.run_local_version_pruning(str(d), apply=False)
    assert (deleted, failed) == (0, 0)
    # Nothing removed.
    assert (d / "X_A_V1.txt").exists()
    assert (d / "X_A_V2.txt").exists()


def test_run_local_version_pruning_apply_removes_stale(tmp_path):
    d = tmp_path
    for fn in ("X_A.txt", "X_A_V1.txt", "X_A_V2.txt"):
        (d / fn).write_text("x", encoding="utf-8")

    deleted, failed = cfk.run_local_version_pruning(str(d), apply=True)
    assert deleted == 1
    assert failed == 0
    assert (d / "X_A.txt").exists()        # base kept
    assert (d / "X_A_V2.txt").exists()     # highest kept
    assert not (d / "X_A_V1.txt").exists()  # stale removed
