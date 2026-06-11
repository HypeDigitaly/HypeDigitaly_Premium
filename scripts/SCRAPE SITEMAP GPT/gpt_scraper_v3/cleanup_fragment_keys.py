"""One-time maintenance script: clean up fragment-polluted vector-store keys.

Background (see CODE_REVIEW_RSS_DEDUP_2026-06-10.md Findings F2/F3/F7 and
IMPLEMENTATION_PLAN_RSS_DEDUP_FIXES.md Wave 4):

Before the fragment-stripping fix (Wave 2), URLs carrying a ``#fragment``
(e.g. ``/o-webu#zou``) were stored in the OpenAI vector store under keys like
``https://site/o-webu#zou#chunkA`` -- the page anchor (``#zou``) leaked into the
lookup key *in front of* the legitimate ``#chunk{X}`` suffix. The same physical
page therefore ended up stored multiple times under distinct fragment keys.

This script finds those orphaned "fragment-polluted" keys and (with ``--apply``)
deletes them from the vector store, **but only** when the real (canonical) page
is still represented -- either by a legitimate ``#chunk`` key in the store, or by
its presence in the current XML known-URLs snapshot. It never deletes the only
copy of a page.

Run (dry-run is the default -- nothing is deleted without ``--apply``)::

    python -m gpt_scraper_v3.cleanup_fragment_keys \
        --config scrape_sitemap_GPT_config_UsteckyKraj_v3.json \
        --vector-store-id vs_xxx \
        [--apply] [--prune-local-versions]

IMPORTANT -- on the ``#summarized`` suffix:
    A grep of the entire codebase (gpt_scraper_v3/ and scrape_sitemap_GPT_v2.py)
    confirmed that the ONLY lookup-key suffixes actually generated are
    ``#chunk{POSTFIX}`` and ``#PAGE{N}chunk{POSTFIX}`` (chunk_processing.py:221,223).
    ``#summarized{X}`` appears ONLY in comments/docstrings -- it is NEVER produced.
    The classifier therefore does NOT treat ``summarized*`` as a legitimate
    suffix; any such token is classified "unknown" and is never auto-deleted.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from gpt_scraper_v3.config import get_config, load_configuration
from gpt_scraper_v3.vector_store import (
    build_vector_store_cache,
    delete_vector_store_file,
)
from gpt_scraper_v3.xml_sitemap import load_known_urls_snapshot

logger = logging.getLogger(__name__)

# Legitimate trailing-suffix tokens that the scraper actually generates.
#   chunk_processing.py:223 -> f"{url}#chunk{postfix}"          (postfix = A, AA, ...)
#   chunk_processing.py:221 -> f"{url}#PAGE{n}chunk{postfix}"
# NOTE: ``summarized[A-Z]+`` is intentionally OMITTED -- it is never generated
# (see module docstring). Adding it would risk classifying an unknown token as
# legit. Keeping the allow-list strict makes the classifier conservative.
_LEGIT_SUFFIX_RE = re.compile(r"^(chunk[A-Z]+|PAGE\d+chunk[A-Z]+)$")

# Classification labels
LEGIT = "legit"
FRAGMENT_POLLUTED = "fragment_polluted"
UNKNOWN = "unknown"

# Action labels (dry-run table)
ACTION_DELETE = "DELETE"
ACTION_KEEP = "keep"
ACTION_SKIP = "skip"


@dataclass
class KeyDecision:
    """The full classification + orphan decision for a single cache key."""

    cache_key: str
    classification: str
    canonical_base: Optional[str]
    base_has_legit: bool
    base_in_snapshot: bool
    action: str  # ACTION_DELETE / ACTION_KEEP / ACTION_SKIP


def classify_key(cache_key: str) -> Tuple[str, Optional[str]]:
    """Classify a vector-store cache key.

    Splits on ``#``: ``parts[0]`` is the base URL, ``parts[1:]`` the fragments.

    Returns ``(classification, canonical_base)`` where classification is one of:
      * ``LEGIT``             -- exactly one fragment, and it is a legit suffix
                                 (``chunkA``, ``chunkAA``, ``PAGE2chunkA``).
                                 canonical_base is parts[0]. NEVER deleted.
      * ``FRAGMENT_POLLUTED`` -- >=2 fragments, the LAST is a legit suffix and the
                                 FIRST is NOT (e.g. ``...#zou#chunkA``).
                                 canonical_base is parts[0].
      * ``UNKNOWN``           -- anything else (no fragment, a non-suffix single
                                 fragment, or a polluted-looking key whose first
                                 fragment is itself a legit suffix). canonical_base
                                 is None. NEVER auto-deleted -- logged for review.
    """
    parts = cache_key.split("#")
    base = parts[0]
    frags = parts[1:]

    if not frags:
        # Bare URL with no fragment at all -- not something the scraper stores
        # as a chunk key; treat conservatively as unknown.
        return UNKNOWN, None

    if len(frags) == 1:
        if _LEGIT_SUFFIX_RE.match(frags[0]):
            return LEGIT, base
        return UNKNOWN, None

    # len(frags) >= 2
    last_is_suffix = bool(_LEGIT_SUFFIX_RE.match(frags[-1]))
    first_is_suffix = bool(_LEGIT_SUFFIX_RE.match(frags[0]))
    if last_is_suffix and not first_is_suffix:
        return FRAGMENT_POLLUTED, base

    # e.g. last frag not a suffix, or first frag already a legit suffix
    # (ambiguous / unexpected shape) -> never auto-delete.
    return UNKNOWN, None


def build_decisions(
    cache: Dict[str, Any],
    snapshot_urls: Set[str],
) -> List[KeyDecision]:
    """Classify every cache key and decide an action for each.

    Orphan rule (delete only when the canonical page survives elsewhere):
        A ``fragment_polluted`` key is deleted ONLY IF its canonical base has a
        ``legit`` key in the store OR the base appears in the current XML
        snapshot. Otherwise it is the only representation of that content and is
        SKIPPED (never orphaned).
    """
    # First pass: classify all keys; collect the set of canonical bases that
    # have at least one legit key in the store.
    classifications: Dict[str, Tuple[str, Optional[str]]] = {}
    legit_bases: Set[str] = set()
    for key in cache:
        cls, base = classify_key(key)
        classifications[key] = (cls, base)
        if cls == LEGIT and base is not None:
            legit_bases.add(base)

    # Second pass: build per-key decisions.
    decisions: List[KeyDecision] = []
    for key in cache:
        cls, base = classifications[key]
        base_has_legit = bool(base) and base in legit_bases
        base_in_snapshot = bool(base) and base in snapshot_urls

        if cls == LEGIT:
            action = ACTION_KEEP
        elif cls == UNKNOWN:
            action = ACTION_SKIP
        else:  # FRAGMENT_POLLUTED
            if base_has_legit or base_in_snapshot:
                action = ACTION_DELETE
            else:
                # Only copy of this content lives under the fragment key.
                action = ACTION_SKIP

        decisions.append(
            KeyDecision(
                cache_key=key,
                classification=cls,
                canonical_base=base,
                base_has_legit=base_has_legit,
                base_in_snapshot=base_in_snapshot,
                action=action,
            )
        )

    # Stable, readable ordering: deletions first, then by key.
    decisions.sort(key=lambda d: (d.action != ACTION_DELETE, d.cache_key))
    return decisions


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"  # ellipsis


def print_dry_run_table(decisions: List[KeyDecision]) -> None:
    """Print the dry-run table: cache_key | classification | canonical_base |
    base_has_legit | base_in_snapshot | action."""
    headers = (
        "cache_key", "classification", "canonical_base",
        "has_legit", "in_snapshot", "action",
    )
    widths = (60, 17, 50, 9, 11, 10)

    def _row(cols: Tuple[str, ...]) -> str:
        return " | ".join(_truncate(c, w).ljust(w) for c, w in zip(cols, widths))

    print(_row(headers))
    print("-+-".join("-" * w for w in widths))
    for d in decisions:
        print(_row((
            d.cache_key,
            d.classification,
            d.canonical_base or "",
            "yes" if d.base_has_legit else "no",
            "yes" if d.base_in_snapshot else "no",
            d.action,
        )))


def summarize(decisions: List[KeyDecision]) -> Dict[str, int]:
    """Return counts per classification plus a ``would_delete`` count."""
    counts: Dict[str, int] = {LEGIT: 0, FRAGMENT_POLLUTED: 0, UNKNOWN: 0, "would_delete": 0}
    for d in decisions:
        counts[d.classification] = counts.get(d.classification, 0) + 1
        if d.action == ACTION_DELETE:
            counts["would_delete"] += 1
    return counts


def apply_deletions(
    decisions: List[KeyDecision],
    vector_store_id: str,
    cache: Dict[str, Any],
) -> Tuple[int, int]:
    """Delete every key marked ACTION_DELETE. Returns (deleted, failed).

    Each deletion is wrapped so one failure never aborts the run. On success the
    cache entry is invalidated (mirrors cleanup_removed_urls()).
    """
    deleted = 0
    failed = 0
    to_delete = [d for d in decisions if d.action == ACTION_DELETE]
    total = len(to_delete)
    for i, d in enumerate(to_delete, 1):
        entry = cache.get(d.cache_key)
        if not entry:
            logger.warning("Cache entry vanished for key %s; skipping", d.cache_key)
            continue
        file_id = entry.get("file_id")
        if not file_id:
            logger.warning("No file_id for key %s; skipping", d.cache_key)
            continue
        try:
            ok = delete_vector_store_file(vector_store_id, file_id)
        except Exception as exc:  # never let one failure abort the batch
            logger.error("Exception deleting %s (file %s): %s", d.cache_key, file_id, exc)
            ok = False
        if ok:
            deleted += 1
            cache.pop(d.cache_key, None)
            logger.info("Deleted [%d/%d] %s (file %s)", i, total, d.cache_key, file_id)
            print(f"[{i}/{total}] Deleted: {d.cache_key} (file {file_id})")
        else:
            failed += 1
            logger.warning("Failed to delete %s (file %s)", d.cache_key, file_id)
            print(f"[{i}/{total}] FAILED:  {d.cache_key} (file {file_id})")
    return deleted, failed


# --- Optional F7: local stale _V<N> file pruning -----------------------------

_VERSION_RE = re.compile(r"^(?P<base>.+)_V(?P<num>\d+)\.txt$", re.IGNORECASE)


def plan_local_version_pruning(output_dir: str) -> Tuple[List[str], List[str]]:
    """Plan pruning of stale ``*_V<N>.txt`` files in ``output_dir``.

    For each base name, the base file (no ``_V``) is always kept, and the
    HIGHEST-numbered ``_V<N>`` file is kept. Every other ``_V<N>`` is a deletion
    candidate.

    Returns ``(to_delete, to_keep_versioned)`` -- lists of absolute paths. The
    base files (non-versioned) are implicitly kept and not listed.
    """
    to_delete: List[str] = []
    to_keep: List[str] = []
    if not output_dir or not os.path.isdir(output_dir):
        logger.warning("Output dir not found for version pruning: %s", output_dir)
        return to_delete, to_keep

    # base name -> list of (version_int, absolute_path)
    versions: Dict[str, List[Tuple[int, str]]] = {}
    for name in os.listdir(output_dir):
        m = _VERSION_RE.match(name)
        if not m:
            continue
        base = m.group("base")
        try:
            num = int(m.group("num"))
        except ValueError:
            continue
        versions.setdefault(base, []).append((num, os.path.join(output_dir, name)))

    for base, items in versions.items():
        items.sort(key=lambda t: t[0])
        highest_path = items[-1][1]
        to_keep.append(highest_path)
        for _num, path in items[:-1]:
            to_delete.append(path)

    to_delete.sort()
    to_keep.sort()
    return to_delete, to_keep


def run_local_version_pruning(output_dir: str, apply: bool) -> Tuple[int, int]:
    """Print and (with ``apply``) execute local ``_V<N>`` pruning.

    Returns ``(deleted, failed)``. Dry-run prints what WOULD be removed.
    """
    to_delete, to_keep = plan_local_version_pruning(output_dir)
    print("")
    print("=== Local _V<N> version pruning (F7) ===")
    print(f"Output dir: {output_dir}")
    print(f"Highest-version files kept: {len(to_keep)}")
    print(f"Stale version files {'to delete' if apply else 'that WOULD be deleted'}: {len(to_delete)}")
    for path in to_delete:
        print(("DELETE " if apply else "WOULD DELETE ") + path)

    if not apply:
        if to_delete:
            print("(dry-run) Pass --apply to remove the stale version files above.")
        return 0, 0

    deleted = 0
    failed = 0
    for path in to_delete:
        try:
            os.remove(path)
            deleted += 1
            logger.info("Removed stale version file: %s", path)
        except OSError as exc:
            failed += 1
            logger.error("Failed to remove %s: %s", path, exc)
            print(f"FAILED to remove: {path} ({exc})")
    print(f"Local pruning complete: removed {deleted}, failed {failed}")
    return deleted, failed


# --- CLI ---------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gpt_scraper_v3.cleanup_fragment_keys",
        description=(
            "One-time cleanup of fragment-polluted vector-store keys "
            "(e.g. 'url#zou#chunkA'). Dry-run by default -- nothing is deleted "
            "without --apply."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", required=True,
        help="Path to the scraper config JSON (provides OPENAI_API_KEY, "
             "identifier, OUTPUT_DIR, and the default vector store id).",
    )
    parser.add_argument(
        "--vector-store-id", default=None,
        help="Vector store id to clean. Overrides the id from --config "
             "(cfg.OPENAI_VECTOR_STORE_ID) when provided.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually delete fragment-polluted files. WITHOUT this flag the "
             "script is a pure dry-run and deletes NOTHING.",
    )
    parser.add_argument(
        "--prune-local-versions", action="store_true",
        help="Also prune stale local '*_V<N>.txt' files in OUTPUT_DIR, keeping "
             "the base file and the highest-numbered version per base name. "
             "Still requires --apply to actually delete (dry-run otherwise).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # 1. Load config (populates the get_config() singleton).
    load_configuration(args.config)
    cfg = get_config()

    vector_store_id = args.vector_store_id or cfg.OPENAI_VECTOR_STORE_ID
    if not vector_store_id:
        print("ERROR: No vector store id (pass --vector-store-id or set it in the config).")
        return 2

    mode = "APPLY (deletions enabled)" if args.apply else "DRY-RUN (no deletions)"
    print("=" * 70)
    print("Fragment-key cleanup")
    print(f"  Config:          {args.config}")
    print(f"  Vector store id: {vector_store_id}")
    print(f"  Mode:            {mode}")
    print("=" * 70)

    # 2. Build the vector store cache (reuses existing helper -- real API call).
    cache = build_vector_store_cache(vector_store_id)
    if not cache:
        print("Vector store cache is empty -- nothing to classify.")
        # Still allow local pruning if requested.
        if args.prune_local_versions:
            run_local_version_pruning(cfg.OUTPUT_DIR or "", args.apply)
        return 0

    # 3. Load the current XML snapshot (empty set if absent -- first run).
    try:
        snapshot_urls = load_known_urls_snapshot()
    except Exception as exc:  # snapshot is advisory; never abort on its failure
        logger.warning("Could not load known-URLs snapshot: %s", exc)
        snapshot_urls = set()
    print(f"Loaded {len(snapshot_urls)} URLs from XML known-URLs snapshot.")

    # 4. Classify + decide.
    decisions = build_decisions(cache, snapshot_urls)
    counts = summarize(decisions)

    # 5. Report.
    print("")
    print_dry_run_table(decisions)
    print("")
    print("Summary:")
    print(f"  legit:             {counts.get(LEGIT, 0)}")
    print(f"  fragment_polluted: {counts.get(FRAGMENT_POLLUTED, 0)}")
    print(f"  unknown:           {counts.get(UNKNOWN, 0)}")
    print(f"  total keys:        {len(decisions)}")

    if not args.apply:
        print(f"  WOULD delete:      {counts.get('would_delete', 0)}")
        print("")
        print("DRY-RUN: no files were deleted. Re-run with --apply to delete.")
    else:
        print(f"  to delete:         {counts.get('would_delete', 0)}")
        print("")
        deleted, failed = apply_deletions(decisions, vector_store_id, cache)
        print("")
        print(f"Done: deleted {deleted}, failed {failed}.")

    # 6. Optional local _V pruning (clearly separate, OFF unless flagged).
    if args.prune_local_versions:
        run_local_version_pruning(cfg.OUTPUT_DIR or "", args.apply)

    return 0


if __name__ == "__main__":
    sys.exit(main())
