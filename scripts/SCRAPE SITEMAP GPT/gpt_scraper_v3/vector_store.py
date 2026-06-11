"""Vector Store operations for GPT Scraper V3.

All OpenAI Vector Store interactions: file upload, file management,
deduplication caching, chunking strategy, and upload-and-add orchestration.
Migrated from scrape_sitemap_GPT_v2.py (lines 5512-6102).
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import contextlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.rate_limiter import get_rate_limiter
from gpt_scraper_v3.retry_util import with_429_retry
from gpt_scraper_v3.utilities import (
    count_tokens_approximate,
    get_session,
    normalize_url_query_params,
)

logger = logging.getLogger(__name__)

# Module-level cache for Vector Store file lookups
_vector_store_cache: Dict[str, Any] = {}

# --- Concurrency infrastructure (Wave 0.7) ----------------------------------
# RLock guarding ALL mutations of the shared vector-store cache dict, the
# maintained inverted index, and the per-key lock registry. This lock guards
# ONLY in-memory dict/index mutations -- it is NEVER held across a network call.
_vs_cache_lock = threading.RLock()

# Maintained inverted index: normalized base URL (without #fragment) -> set of
# cache keys whose base derives to it. Kept in sync with the cache dict on
# build / insert / delete so cleanup_removed_urls can look up O(1) without
# rebuilding an ad-hoc index each call.
_vs_base_index: Dict[str, Set[str]] = defaultdict(set)

# Per-lookup-key in-flight locks. A worker uploading/replacing a given lookup
# URL holds the corresponding key lock across its whole find->delete->upload->
# insert sequence so two workers racing the SAME lookup URL serialize. Created
# lazily under _vs_cache_lock via _get_key_lock().
_key_locks: Dict[str, threading.Lock] = {}


def _base_from_cache_key(cache_key: str) -> str:
    """Derive the normalized base URL from a cache key.

    Reuses the exact normalization cleanup_removed_urls historically applied
    when building its ad-hoc inverted index: strip any #fragment, then
    normalize query params.
    """
    return normalize_url_query_params(cache_key.split("#")[0])


def _index_add(cache_key: str) -> None:
    """Add a cache key to the maintained inverted index. Caller holds _vs_cache_lock."""
    _vs_base_index[_base_from_cache_key(cache_key)].add(cache_key)


def _index_remove(cache_key: str) -> None:
    """Remove a cache key from the maintained inverted index. Caller holds _vs_cache_lock."""
    base = _base_from_cache_key(cache_key)
    keys = _vs_base_index.get(base)
    if keys is not None:
        keys.discard(cache_key)
        if not keys:
            del _vs_base_index[base]


def _rebuild_base_index(cache: Dict[str, Any]) -> None:
    """Rebuild the maintained inverted index from scratch for *cache*.

    Called single-threaded after a fresh cache dict is built.
    """
    with _vs_cache_lock:
        _vs_base_index.clear()
        for cache_key in cache:
            _vs_base_index[_base_from_cache_key(cache_key)].add(cache_key)


def _get_key_lock(normalized_lookup_url: str) -> threading.Lock:
    """Return (creating if needed) the in-flight lock for a normalized lookup URL."""
    with _vs_cache_lock:
        lock = _key_locks.get(normalized_lookup_url)
        if lock is None:
            lock = threading.Lock()
            _key_locks[normalized_lookup_url] = lock
        return lock


def _openai_headers(cfg: Any, *, content_type: bool = False) -> Dict[str, str]:
    """Build common OpenAI API headers."""
    headers = {"Authorization": f"Bearer {cfg.OPENAI_API_KEY}", "OpenAI-Beta": "assistants=v2"}
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def upload_file_to_openai(filepath: str) -> Optional[str]:
    """Upload a file to OpenAI Files API and return the file ID."""
    cfg = get_config()
    logger.info(f"Uploading file to OpenAI: {filepath}")
    if not os.path.exists(filepath):
        logger.error(f"File does not exist: {filepath}")
        return None
    headers = _openai_headers(cfg)
    try:
        def _do_upload() -> requests.Response:
            # Re-open the file handle each attempt: a consumed handle cannot be
            # re-POSTed. Limiter acquire is INSIDE the closure so a sleeping 429
            # retry re-acquires the API slot rather than holding it while waiting.
            with open(filepath, "rb") as file:
                files = {
                    "file": (os.path.basename(filepath), file, "text/plain"),
                    "purpose": (None, "assistants"),
                }
                with get_rate_limiter().acquire(cfg.OPENAI_API_BASE_URL):
                    return get_session().post(
                        f"{cfg.OPENAI_API_BASE_URL}/files",
                        headers=headers, files=files, timeout=cfg.REQUEST_TIMEOUT,
                    )

        response = with_429_retry(
            _do_upload,
            max_attempts=cfg.API_RETRY_MAX_ATTEMPTS,
            cap_seconds=cfg.API_RETRY_CAP_SECONDS,
            logger=logger,
            call_name="OpenAI upload_file",
        )
        response.raise_for_status()
        data = response.json()
        file_id = data.get("id")
        if file_id:
            logger.info(f"Successfully uploaded file to OpenAI. File ID: {file_id}")
            return file_id
        logger.error("OpenAI file upload failed: No file ID returned")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error uploading file to OpenAI: {str(e)}")
        return None
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Unexpected error uploading file to OpenAI: {str(e)}")
        return None


def add_file_to_vector_store(
    file_id: str, vector_store_id: str,
    attributes: Optional[Dict[str, Any]] = None,
    chunking_strategy: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Add a file to an OpenAI Vector Store."""
    cfg = get_config()
    logger.info(f"Adding file {file_id} to vector store {vector_store_id}")
    headers = _openai_headers(cfg, content_type=True)
    payload: Dict[str, Any] = {"file_id": file_id}
    if attributes:
        payload["attributes"] = attributes
    if chunking_strategy:
        payload["chunking_strategy"] = chunking_strategy
        logger.info(f"Using chunking strategy: {chunking_strategy}")
    try:
        logger.debug("Vector Store payload: %.500s", json.dumps(payload, indent=2)[:500])

        def _do_add() -> requests.Response:
            with get_rate_limiter().acquire(cfg.OPENAI_API_BASE_URL):
                return get_session().post(
                    f"{cfg.OPENAI_API_BASE_URL}/vector_stores/{vector_store_id}/files",
                    headers=headers, json=payload, timeout=cfg.REQUEST_TIMEOUT,
                )

        response = with_429_retry(
            _do_add,
            max_attempts=cfg.API_RETRY_MAX_ATTEMPTS,
            cap_seconds=cfg.API_RETRY_CAP_SECONDS,
            logger=logger,
            call_name="OpenAI add_file_to_vector_store",
        )
        response.raise_for_status()
        data = response.json()
        logger.debug("Vector Store API Response: %.500s", json.dumps(data, indent=2)[:500])
        if "attributes" in data:
            logger.debug("Attributes confirmed: %.500s", str(data.get('attributes'))[:500])
        else:
            logger.warning("NO attributes field in API response! Attributes may not have been saved.")
            logger.warning("   This will cause deduplication failure and file accumulation!")
        if data.get("status") in ("completed", "in_progress"):
            logger.info(f"Successfully added file to vector store. Status: {data.get('status')}")
            return data
        logger.error(
            f"Failed to add file to vector store. "
            f"Status: {data.get('status')}, Error: {data.get('last_error')}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error adding file to vector store: {str(e)}")
        return None
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Unexpected error adding file to vector store: {str(e)}")
        return None


def list_vector_store_files(vector_store_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    """List all files in a vector store, handling pagination."""
    cfg = get_config()
    logger.info(f"Listing files in vector store {vector_store_id}")
    headers = _openai_headers(cfg, content_type=True)
    all_files: List[Dict[str, Any]] = []
    after: Optional[str] = None
    try:
        while True:
            params: Dict[str, Any] = {"limit": limit}
            if after:
                params["after"] = after

            def _do_list(_params: Dict[str, Any] = params) -> requests.Response:
                with get_rate_limiter().acquire(cfg.OPENAI_API_BASE_URL):
                    return get_session().get(
                        f"{cfg.OPENAI_API_BASE_URL}/vector_stores/{vector_store_id}/files",
                        headers=headers, params=_params, timeout=cfg.REQUEST_TIMEOUT,
                    )

            response = with_429_retry(
                _do_list,
                max_attempts=cfg.API_RETRY_MAX_ATTEMPTS,
                cap_seconds=cfg.API_RETRY_CAP_SECONDS,
                logger=logger,
                call_name="OpenAI list_vector_store_files",
            )
            response.raise_for_status()
            data = response.json()
            all_files.extend(data.get("data", []))
            if not data.get("has_more", False):
                break
            after = data.get("last_id")
        logger.info(f"Retrieved {len(all_files)} files from vector store")
        return all_files
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error listing vector store files: {str(e)}")
        return []
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Unexpected error listing vector store files: {str(e)}")
        return []


def get_vector_store_file_attributes(
    vector_store_id: str, file_id: str,
) -> Optional[Dict[str, Any]]:
    """Get attributes of a file in the vector store with graceful 404 handling.

    Returns a dict of attributes, empty dict on non-404 errors, or None for 404.
    """
    cfg = get_config()
    logger.debug(f"Getting attributes for file {file_id} in vector store {vector_store_id}")
    headers = _openai_headers(cfg, content_type=True)
    try:
        def _do_get_attrs() -> requests.Response:
            with get_rate_limiter().acquire(cfg.OPENAI_API_BASE_URL):
                return get_session().get(
                    f"{cfg.OPENAI_API_BASE_URL}/vector_stores/{vector_store_id}/files/{file_id}",
                    headers=headers, timeout=cfg.REQUEST_TIMEOUT,
                )

        response = with_429_retry(
            _do_get_attrs,
            max_attempts=cfg.API_RETRY_MAX_ATTEMPTS,
            cap_seconds=cfg.API_RETRY_CAP_SECONDS,
            logger=logger,
            call_name="OpenAI get_vector_store_file_attributes",
        )
        response.raise_for_status()
        data = response.json()
        retrieved_attributes = data.get("attributes", {})
        if retrieved_attributes:
            logger.debug(f"Retrieved attributes for file {file_id}: {list(retrieved_attributes.keys())}")
        else:
            logger.warning(f"File {file_id} has NO attributes or empty attributes dict!")
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Full API response: %.500s", json.dumps(data, indent=2)[:500])
        return retrieved_attributes
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            logger.debug(f"File {file_id} not found (404) - likely deleted or still processing")
            return None  # Signal "skip this file" vs {} for "no attributes"
        logger.error(f"HTTP error getting file attributes: {str(e)}")
        return {}
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error getting file attributes: {str(e)}")
        return {}
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Unexpected error getting file attributes: {str(e)}")
        return {}


def find_existing_file_by_url(
    vector_store_id: str, lookup_url: str,
) -> Optional[Dict[str, Any]]:
    """Find an existing file in vector store by EXACT lookup URL match.

    WARNING: SLOW legacy method -- O(n*m) with many API calls.
    Use find_existing_file_by_url_cached() with a pre-built cache instead.
    """
    logger.warning(f"Using SLOW legacy lookup for EXACT lookup URL: {lookup_url} (consider using cache)")
    normalized_lookup_url = normalize_url_query_params(lookup_url)
    files = list_vector_store_files(vector_store_id)
    for file_info in files:
        file_id = file_info.get("id")
        if not file_id:
            continue
        attributes = get_vector_store_file_attributes(vector_store_id, file_id)
        file_lookup_url = (
            (attributes or {}).get("lookup_source_url") or (attributes or {}).get("source_url")
        )
        if file_lookup_url:
            normalized_file_lookup_url = normalize_url_query_params(file_lookup_url)
            if normalized_file_lookup_url == normalized_lookup_url:
                logger.info(f"Found existing file {file_id} with EXACT URL match: {normalized_lookup_url}")
                return file_info
    logger.info(f"No existing file found with EXACT URL: {normalized_lookup_url}")
    return None


def delete_vector_store_file(vector_store_id: str, file_id: str) -> bool:
    """Delete a file from the vector store (but not the actual file)."""
    cfg = get_config()
    logger.info(f"Deleting file {file_id} from vector store {vector_store_id}")
    headers = _openai_headers(cfg, content_type=True)
    try:
        def _do_delete() -> requests.Response:
            with get_rate_limiter().acquire(cfg.OPENAI_API_BASE_URL):
                return get_session().delete(
                    f"{cfg.OPENAI_API_BASE_URL}/vector_stores/{vector_store_id}/files/{file_id}",
                    headers=headers, timeout=cfg.REQUEST_TIMEOUT,
                )

        response = with_429_retry(
            _do_delete,
            max_attempts=cfg.API_RETRY_MAX_ATTEMPTS,
            cap_seconds=cfg.API_RETRY_CAP_SECONDS,
            logger=logger,
            call_name="OpenAI delete_vector_store_file",
        )
        response.raise_for_status()
        data = response.json()
        if data.get("deleted"):
            logger.info(f"Successfully deleted file {file_id} from vector store")
            return True
        logger.error(f"Failed to delete file {file_id} from vector store")
        return False
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            logger.info(f"File {file_id} already absent from vector store (404) - treating delete as successful")
            return True
        logger.error(f"HTTP error deleting file from vector store: {str(e)}")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error deleting file from vector store: {str(e)}")
        return False
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Unexpected error deleting file from vector store: {str(e)}")
        return False


def load_vs_cache_snapshot() -> Dict[str, Dict[str, Any]]:
    """Load the persisted Vector Store attribute snapshot.

    The snapshot maps ``file_id -> {"attributes": {...}}``. Attributes
    (``lookup_source_url`` etc.) are immutable for a given file_id, so a warm
    run can reconstruct cache entries for already-known files WITHOUT issuing an
    attribute GET per file.

    Keyed by ``file_id`` (not lookup URL) so the diff against the live file list
    is a trivial set operation and reconstruction is unambiguous.

    Returns:
        A dict mapping file_id -> {"attributes": {...}}. Returns an empty dict if
        the file is missing (first run) or corrupted (-> full rebuild). Logs INFO
        on miss/corruption -- never raises.
    """
    path = get_config().VS_CACHE_FILE
    if not path or not os.path.exists(path):
        logger.info("No persisted Vector Store cache snapshot found (%s) - full rebuild", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.info("VS cache snapshot %s has unexpected shape - full rebuild", path)
            return {}
        # Defensive: keep only well-formed entries (file_id -> {"attributes": dict}).
        snapshot: Dict[str, Dict[str, Any]] = {}
        for file_id, entry in data.items():
            if (
                isinstance(file_id, str)
                and isinstance(entry, dict)
                and isinstance(entry.get("attributes"), dict)
            ):
                snapshot[file_id] = {"attributes": entry["attributes"]}
        logger.info("Loaded VS cache snapshot from %s (%d entries)", path, len(snapshot))
        return snapshot
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.info("VS cache snapshot %s unreadable/corrupted (%s) - full rebuild", path, e)
        return {}


def save_vs_cache_snapshot(cache: Dict[str, Any]) -> None:
    """Atomically persist the Vector Store attribute snapshot.

    Serializes the in-memory cache (keyed by normalized lookup URL ->
    {"file_id", "file_info", "attributes"}) into a file_id-keyed snapshot
    containing only the reusable fields (``attributes``); ``file_info`` is NOT
    persisted because it is reconstructed from the fresh ``list_vector_store_files``
    call on the next run. Written via ``.tmp`` + ``os.replace`` (mirrors
    ``_save_url_set`` in xml_sitemap.py). Never raises.
    """
    path = get_config().VS_CACHE_FILE
    if not path:
        return
    snapshot: Dict[str, Dict[str, Any]] = {}
    for entry in cache.values():
        file_id = entry.get("file_id")
        attributes = entry.get("attributes")
        if file_id and isinstance(attributes, dict):
            snapshot[file_id] = {"attributes": attributes}
    temp_file = path + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, sort_keys=True)
        os.replace(temp_file, path)
        logger.info("Saved VS cache snapshot (%d entries) to %s", len(snapshot), path)
    except (OSError, TypeError) as e:
        logger.error("Error saving VS cache snapshot to %s: %s", path, e)
    finally:
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except OSError:
            pass


def snapshot_vs_cache(cache: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of *cache* taken under ``_vs_cache_lock``.

    F8: lets callers (e.g. ``cli.main``'s finally) obtain a consistent,
    serialization-safe copy of the live vector-store cache without reaching into
    this module's private lock. The copy is taken while any straggling worker
    mutation is excluded by the lock.
    """
    with _vs_cache_lock:
        return dict(cache)


def build_vector_store_cache(vector_store_id: str) -> Dict[str, Any]:
    """Build a fast lookup cache of all files in Vector Store with their metadata.

    Warm-start optimization (Wave 2.2): a persisted ``{identifier}_vs_cache.json``
    snapshot of immutable per-file attributes is loaded and reused. Attribute GETs
    are issued ONLY for file_ids absent from the snapshot. Misses are fetched in
    parallel (Wave 2.1) via a ThreadPoolExecutor over the API-tier limiter. The
    resulting cache is byte-for-byte identical to a cold serial rebuild.
    """
    logger.info(f"Building Vector Store cache for {vector_store_id}")
    # F9: Reset the per-key in-flight lock registry per run so it cannot grow
    # unbounded across repeated main() calls. Safe: cache builds run before the
    # worker pool starts, so no thread holds a key lock at this point.
    with _vs_cache_lock:
        _key_locks.clear()
    start_time = time.time()
    # (a) Always list the current file set (cheap, paginated).
    files = list_vector_store_files(vector_store_id)
    logger.info(f"Found {len(files)} files in Vector Store")
    if len(files) == 0:
        logger.info("Vector Store is empty, skipping cache build")
        _rebuild_base_index({})
        # (e/f) Prune vanished entries: empty store -> empty snapshot.
        save_vs_cache_snapshot({})
        return {}

    # (b) Load the persisted attribute snapshot (file_id -> {"attributes": ...}).
    snapshot = load_vs_cache_snapshot()
    live_file_ids: Set[str] = {f.get("id") for f in files if f.get("id")}

    cfg = get_config()

    # Worker: fetch attributes for a single file. Returns (file_id, attributes)
    # where attributes is None on 404 ("skip"), {} on non-404 error, or a dict.
    def _fetch_attrs(file_id: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        try:
            return file_id, get_vector_store_file_attributes(vector_store_id, file_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.error(f"Failed to get attributes for file {file_id}: {str(e)}")
            return file_id, {}

    # (d) Determine which file_ids need a fresh attribute GET (snapshot miss).
    file_ids_in_order: List[str] = [f.get("id") for f in files if f.get("id")]
    miss_ids: List[str] = [fid for fid in file_ids_in_order if fid not in snapshot]
    metadata_fetch_count = len(miss_ids)

    # Fetch misses in parallel; preserve mapping by file_id (order within the
    # final cache dict is governed by file order below, so last-writer-wins on
    # duplicate normalized URLs is identical to the serial build).
    fetched_attrs: Dict[str, Optional[Dict[str, Any]]] = {}
    if miss_ids:
        workers = max(1, min(cfg.AUX_PARALLEL_WORKERS, len(miss_ids)))
        reused = len(file_ids_in_order) - metadata_fetch_count
        # F4: announce the (potentially minutes-long) parallel fetch phase so a
        # cold build doesn't look like a hang.
        logger.info(
            "VS cache: fetching %d attribute sets with %d workers (%d reused from snapshot)",
            len(miss_ids), workers, reused)
        n_miss = len(miss_ids)
        progress_step = max(1, n_miss // 10)  # ~every 10% (guard div-by-zero)
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for file_id, attributes in ex.map(_fetch_attrs, miss_ids):
                fetched_attrs[file_id] = attributes
                done += 1
                if done % progress_step == 0 or done == n_miss:
                    logger.info("VS cache: fetched %d/%d attribute sets (%.0f%%)",
                                done, n_miss, done / n_miss * 100)

    url_to_file_cache: Dict[str, Any] = {}
    skipped_404_count = 0
    # Single-threaded merge in file order -> identical last-writer-wins as serial.
    for i, file_info in enumerate(files, 1):
        file_id = file_info.get("id")
        if not file_id:
            continue
        # Progress indicator for large Vector Stores
        if len(files) > 10 and i % max(1, len(files) // 10) == 0:
            progress = (i / len(files)) * 100
            elapsed = time.time() - start_time
            logger.info("Cache merge progress: %.0f%% (%d/%d) - %.1fs elapsed",
                        progress, i, len(files), elapsed)
        # (c) Reuse snapshot attributes WITHOUT a GET; (d) else use fetched ones.
        if file_id in snapshot:
            attributes: Optional[Dict[str, Any]] = snapshot[file_id]["attributes"]
        else:
            attributes = fetched_attrs.get(file_id, {})
        if attributes is None:
            skipped_404_count += 1
            logger.debug(f"Skipping file {file_id} (404 Not Found - deleted or processing)")
            continue
        lookup_url = attributes.get("lookup_source_url") or attributes.get("source_url")
        if lookup_url:
            normalized_lookup_url = normalize_url_query_params(lookup_url)
            url_to_file_cache[normalized_lookup_url] = {
                "file_id": file_id, "file_info": file_info, "attributes": attributes,
            }
            logger.debug(f"Cached file {file_id}: {normalized_lookup_url}")
        else:
            logger.warning(f"File {file_id} has NO lookup URL in attributes - cannot cache!")
            logger.warning(f"   Attributes received: {attributes}")
            logger.warning("   This file will NOT be deduplicated and may accumulate!")
    elapsed_time = time.time() - start_time
    reused = len(file_ids_in_order) - metadata_fetch_count
    logger.info(
        f"Built Vector Store cache in {elapsed_time:.2f}s with {metadata_fetch_count} "
        f"metadata fetches ({reused} reused from snapshot)")
    logger.info(f"Cache contains {len(url_to_file_cache)} files with source URLs")
    if skipped_404_count > 0:
        logger.info(f"Skipped {skipped_404_count} files with 404 errors (deleted or processing)")
    logger.info("Cache built: %d files indexed in %.1fs",
                len(url_to_file_cache), elapsed_time)
    # Build the maintained inverted index once, single-threaded, from the fresh cache.
    _rebuild_base_index(url_to_file_cache)
    # (e/f) Persist the updated snapshot. Built from the fresh cache, so file_ids
    # that vanished from the live list are naturally pruned (not present), and
    # only file_ids still present in the store are kept.
    save_vs_cache_snapshot(url_to_file_cache)
    return url_to_file_cache


def cleanup_removed_urls(
    removed_urls: Set[str],
    vector_store_id: str,
    vector_store_cache: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Set[str]]:
    """Delete vector store files for URLs that have been removed from the sitemap.

    Args:
        removed_urls: Set of base URLs that have been removed.
        vector_store_id: OpenAI Vector Store ID.
        vector_store_cache: Pre-built cache from build_vector_store_cache().

    Returns:
        Tuple of (deleted_count, failed_urls) where deleted_count is the number
        of successfully deleted files and failed_urls is the set of base URLs
        whose deletion failed (so the caller can preserve them in the snapshot
        for retry on the next run).

    Note:
        Handles chunk variants (#chunkA, #chunkB, #summarizedA etc.) by building
        an inverted index for O(1) lookup per removed URL.
    """
    # Early returns
    if not removed_urls:
        logger.info("No removed URLs to clean up")
        return 0, set()

    if not vector_store_id:
        logger.error("Cannot cleanup removed URLs: vector_store_id is empty")
        return 0, set()

    if vector_store_cache is None:
        logger.warning("Cannot cleanup removed URLs: vector_store_cache is None (cache required)")
        return 0, set()

    logger.info(f"Starting cleanup for {len(removed_urls)} removed URLs")
    print(f"Cleaning up {len(removed_urls)} removed URLs from Vector Store...")

    # Step B: For each removed URL, find and delete matching vector store files.
    # Uses the maintained inverted index (_vs_base_index) for O(1) base lookups
    # instead of rebuilding an ad-hoc index. Matching/normalization semantics are
    # preserved: removed URLs are normalized the same way, and the index keys are
    # derived from cache keys via the same base-from-key normalization.
    #
    # Wave 2.5: the actual delete_vector_store_file HTTP calls are fanned out via
    # a ThreadPoolExecutor (API-tier limiter already wraps each call). Cache/index
    # mutations and count/failed_urls folding happen single-threaded in the
    # consuming thread (under _vs_cache_lock for mutations), so the per-key
    # delete-by-identity semantics are byte-identical to the serial version.
    cfg = get_config()

    # Build the flat work list of (removed_url, cache_key, file_id) under the
    # lock-protected snapshot of the index/cache, preserving the same skip rules.
    delete_tasks: List[Tuple[str, str, str]] = []
    for removed_url in removed_urls:
        normalized_removed = normalize_url_query_params(removed_url)
        with _vs_cache_lock:
            matching_keys = list(_vs_base_index.get(normalized_removed, set()))

        if not matching_keys:
            logger.info(f"No vector store files found for removed URL: {removed_url}")
            continue

        logger.info(f"Found {len(matching_keys)} vector store file(s) for removed URL: {removed_url}")

        for cache_key in matching_keys:
            with _vs_cache_lock:
                cache_entry = vector_store_cache.get(cache_key)
            if not cache_entry:
                continue
            file_id = cache_entry.get("file_id")
            if not file_id:
                continue
            delete_tasks.append((removed_url, cache_key, file_id))

    deleted_count = 0
    failed_urls: Set[str] = set()

    def _delete_one(task: Tuple[str, str, str]) -> Tuple[str, str, str, bool]:
        removed_url, cache_key, file_id = task
        ok = delete_vector_store_file(vector_store_id, file_id)
        return removed_url, cache_key, file_id, ok

    if delete_tasks:
        workers = max(1, min(cfg.AUX_PARALLEL_WORKERS, len(delete_tasks)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            # Single consuming thread folds results -> no locks needed for the
            # counters; cache/index mutations are guarded by _vs_cache_lock.
            for removed_url, cache_key, file_id, ok in ex.map(_delete_one, delete_tasks):
                if ok:
                    deleted_count += 1
                    logger.info(f"Deleted vector store file {file_id} (cache key: {cache_key})")
                    # Invalidate cache entry + index, delete-by-identity: only remove
                    # if the cached entry still points at the file_id we just deleted.
                    with _vs_cache_lock:
                        if vector_store_cache.get(cache_key, {}).get("file_id") == file_id:
                            del vector_store_cache[cache_key]
                            _index_remove(cache_key)
                else:
                    logger.warning(f"Failed to delete vector store file {file_id} for removed URL: {removed_url}")
                    failed_urls.add(removed_url)

    # Step C: Log and print summary
    logger.info(f"Cleanup complete: Deleted {deleted_count} vector store files for {len(removed_urls)} removed URLs")
    if failed_urls:
        logger.warning(f"Cleanup had {len(failed_urls)} URL(s) with failed deletions (will be retried next run)")
    print(f"Cleanup complete: Deleted {deleted_count} vector store file(s)")

    return deleted_count, failed_urls


def find_existing_file_by_url_cached(
    lookup_url: str, cache: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Find an existing file in vector store by lookup URL using pre-built cache."""
    normalized_lookup_url = normalize_url_query_params(lookup_url)
    logger.debug(f"Searching cache for existing file with normalized lookup URL: {normalized_lookup_url}")
    # F10: Guard the cache read under _vs_cache_lock to match the plural
    # sibling's contract (no unlocked reads against concurrent cache mutation).
    with _vs_cache_lock:
        cached_file = cache.get(normalized_lookup_url)
        if cached_file:
            file_id = cached_file["file_id"]
            logger.info(f"Found existing file {file_id} in cache for URL: {lookup_url} (norm: {normalized_lookup_url})")
            return cached_file["file_info"]
    logger.debug(f"No existing file found in cache for lookup URL: {lookup_url}")
    return None


def find_existing_files_by_url_cached(
    lookup_url: str, cache: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Find existing file by EXACT lookup URL using pre-built cache.

    Only matches the EXACT same lookup_source_url (including #chunk fragments)
    to prevent different chunks (A, B, C) from being treated as duplicates.
    """
    normalized_input_url = normalize_url_query_params(lookup_url)
    logger.debug(f"Searching cache for EXACT match of lookup_source_url: {normalized_input_url}")
    matching_files: List[Dict[str, Any]] = []
    # Bug 4: O(1) direct dict lookup instead of a full-cache iteration. The lock
    # still guards the read against concurrent cache mutation under parallel
    # workers; the cache is keyed by the normalized lookup URL, so a single
    # ``get`` reproduces the previous EXACT-match semantics. Return shape is
    # preserved: a list carrying the matched entry's ``["file_info"]``.
    with _vs_cache_lock:
        cached_file = cache.get(normalized_input_url)
        if cached_file is not None:
            file_id = cached_file["file_id"]
            logger.info(f"Found existing file {file_id} in cache for EXACT URL match: {normalized_input_url}")
            matching_files.append(cached_file["file_info"])
    if not matching_files:
        logger.debug(f"No existing file found in cache for EXACT URL: {normalized_input_url}")
    return matching_files


def create_chunking_strategy(
    strategy_type: str = "auto", max_chunk_size: int = 800, chunk_overlap: int = 400,
) -> Dict[str, Any]:
    """Create a chunking strategy object for OpenAI Vector Store.

    Args:
        strategy_type: "auto" or "static".
        max_chunk_size: Only for static strategy (100-4096).
        chunk_overlap: Only for static strategy (>= 0, <= max_chunk_size / 2).

    Returns:
        Valid chunking strategy dict for OpenAI Vector Store API.

    Raises:
        ValueError: If static strategy parameters violate OpenAI constraints.
    """
    if strategy_type.lower() == "auto":
        if max_chunk_size != 800 or chunk_overlap != 400:
            logger.warning("AUTO strategy ignores custom values. Using OpenAI defaults: 800 tokens, 400 overlap")
        logger.info("Using AUTO chunking strategy (OpenAI default: 800 tokens per chunk, 400 overlap)")
        return {"type": "auto"}
    elif strategy_type.lower() == "static":
        if not isinstance(max_chunk_size, int) or max_chunk_size < 100 or max_chunk_size > 4096:
            raise ValueError(
                f"OpenAI constraint violation: max_chunk_size must be integer "
                f"between 100-4096 tokens, got: {max_chunk_size}")
        if not isinstance(chunk_overlap, int) or chunk_overlap < 0:
            raise ValueError(
                f"OpenAI constraint violation: chunk_overlap must be "
                f"non-negative integer, got: {chunk_overlap}")
        max_allowed_overlap = max_chunk_size // 2
        if chunk_overlap > max_allowed_overlap:
            raise ValueError(
                f"OpenAI constraint violation: chunk_overlap ({chunk_overlap}) must NOT exceed "
                f"half of max_chunk_size. For chunk_size={max_chunk_size}, max overlap is {max_allowed_overlap}")
        logger.info(f"Using STATIC chunking strategy ({max_chunk_size} tokens, {chunk_overlap} overlap)")
        logger.info(f"OpenAI validation passed: chunk_overlap ({chunk_overlap}) <= max/2 ({max_allowed_overlap})")
        if chunk_overlap == 0:
            logger.info("OPTIMIZATION: Zero overlap configured for maximum processing efficiency")
        if max_chunk_size == 4096:
            logger.info("OPTIMIZATION: Maximum chunk size (4096) configured for largest context windows")
        return {
            "type": "static",
            "static": {
                "max_chunk_size_tokens": max_chunk_size,
                "chunk_overlap_tokens": chunk_overlap,
            },
        }
    else:
        logger.warning(f"Unknown chunking strategy '{strategy_type}', falling back to AUTO (OpenAI default)")
        return {"type": "auto"}


def upload_and_add_to_vector_store(
    filepath: str, vector_store_id: str, url: Optional[str] = None,
    title: Optional[str] = None, enable_deduplication: bool = True,
    chunking_strategy: Optional[Dict[str, Any]] = None,
    vector_store_cache: Optional[Dict[str, Any]] = None,
    content: Optional[str] = None, token_count: Optional[int] = None,
) -> bool:
    """Upload file to OpenAI and add to vector store with deduplication.

    Bug 13 (token/IO reuse): when the caller already has the exact content it
    just wrote to *filepath* and its precomputed token count, it may pass them
    via *content* and *token_count* to skip BOTH the disk re-read and the
    re-tokenize in pre-upload validation. The caller MUST pass the SAME string
    it wrote to disk (counted-bytes == written-bytes); the 0-byte/empty guard
    then runs off those provided values. Both params are optional and default
    to ``None``, preserving the legacy disk-read + re-tokenize path.
    """
    cfg = get_config()
    logger.info(f"Starting upload and vector store process for: {filepath}")

    # Pre-upload validation: check file size before uploading
    try:
        if not os.path.exists(filepath):
            logger.error(f"File does not exist for validation: {filepath}")
            return False
        # Bug 13: reuse the caller-provided content + token count when supplied
        # (the file was just written from this exact string), skipping the disk
        # re-read AND the re-tokenize. Falls back to the legacy read+count path
        # when either is absent for full backward compatibility.
        if content is not None and token_count is not None:
            file_content = content
            file_tokens = token_count
        else:
            with open(filepath, "r", encoding="utf-8") as f:
                file_content = f.read()
            file_tokens = count_tokens_approximate(file_content)
        # Reject empty / 0-token documents BEFORE any API call. A 0-byte file
        # (e.g. an orphaned filename-reservation placeholder from a crash) must
        # never be uploaded -- it wastes an API round-trip and pollutes the
        # vector store with an empty doc.
        if file_tokens == 0 or not file_content.strip():
            logger.error(
                "UPLOAD REJECTED: %s is empty (0 tokens) -- skipping upload",
                filepath,
            )
            print(f"UPLOAD BLOCKED: {os.path.basename(filepath)} (empty file)")
            return False
        max_allowed_tokens = cfg.DEFAULT_MAX_CHUNK_SIZE
        if chunking_strategy and chunking_strategy.get("type") == "static":
            static_config = chunking_strategy.get("static", {})
            max_allowed_tokens = static_config.get("max_chunk_size_tokens", cfg.DEFAULT_MAX_CHUNK_SIZE)
        logger.info(f"PRE-UPLOAD VALIDATION: File has {file_tokens:,} tokens, limit is {max_allowed_tokens:,}")
        if file_tokens > max_allowed_tokens:
            logger.error(f"VECTOR STORE SIZE VIOLATION: {filepath} has {file_tokens:,} > {max_allowed_tokens:,}")
            logger.error("UPLOAD REJECTED: File must be re-chunked before upload")
            print(f"UPLOAD BLOCKED: {os.path.basename(filepath)} ({file_tokens:,} > {max_allowed_tokens:,} limit)")
            return False
        logger.info("PRE-UPLOAD VALIDATION PASSED: File size within Vector Store limits")
    except Exception as e:
        logger.error(f"Error during pre-upload validation: {str(e)}")
        return False

    # Extract base URL and lookup URL for deduplication
    base_url = url.split("#")[0] if url and "#" in url else url
    lookup_url = url  # Used for deduplication (can include #chunkA, #summarizedB etc.)

    # Hold the per-lookup-key in-flight lock across the WHOLE find->delete->
    # upload->insert sequence so two workers racing the same lookup URL
    # serialize. The global _vs_cache_lock still guards only dict/index
    # mutations and is never held across the network calls below.
    with contextlib.ExitStack() as _key_guard:
        if lookup_url:
            _key_guard.enter_context(_get_key_lock(normalize_url_query_params(lookup_url)))

        # Step 1: Check for existing file if deduplication is enabled (EXACT match only)
        existing_files: List[Dict[str, Any]] = []
        if enable_deduplication and lookup_url:
            if vector_store_cache is not None:
                existing_files = find_existing_files_by_url_cached(lookup_url, vector_store_cache)
            else:
                existing_file = find_existing_file_by_url(vector_store_id, lookup_url)
                if existing_file:
                    existing_files = [existing_file]
            if existing_files:
                logger.info(f"Found {len(existing_files)} existing file(s) with EXACT URL: {lookup_url}")
                print(f"Found {len(existing_files)} existing file(s) with EXACT match for: {lookup_url}")
                deleted_count = 0
                deleted_existing_ids: List[str] = []
                for existing_file_item in existing_files:
                    existing_file_id = existing_file_item.get("id")
                    if delete_vector_store_file(vector_store_id, existing_file_id):
                        logger.info(f"Deleted old file {existing_file_id} with EXACT URL match: {lookup_url}")
                        deleted_count += 1
                        deleted_existing_ids.append(existing_file_id)
                    else:
                        logger.warning(f"Failed to delete old file {existing_file_id}, continuing with upload")
                print(f"Deleted {deleted_count}/{len(existing_files)} old file(s)")
                # Remove ONLY the exact matching URL from cache (delete-by-identity:
                # only drop the cache key if it still points at a file we deleted).
                if vector_store_cache is not None:
                    normalized_lookup_url = normalize_url_query_params(lookup_url)
                    with _vs_cache_lock:
                        cached_fid = vector_store_cache.get(normalized_lookup_url, {}).get("file_id")
                        if normalized_lookup_url in vector_store_cache and cached_fid in deleted_existing_ids:
                            del vector_store_cache[normalized_lookup_url]
                            _index_remove(normalized_lookup_url)
                            logger.debug(f"Removed EXACT URL from cache: {normalized_lookup_url}")
                        else:
                            logger.debug(f"URL not found in cache for removal: {normalized_lookup_url}")

        # Step 2: Upload file to OpenAI
        file_id = upload_file_to_openai(filepath)
        if not file_id:
            logger.error(f"Failed to upload file to OpenAI: {filepath}")
            return False

        # Step 3: Prepare attributes for the vector store file
        attributes: Dict[str, str] = {}
        if url:
            attributes["source_url"] = (base_url or "")[:512]
            attributes["lookup_source_url"] = (lookup_url or "")[:512]
        if title:
            attributes["title"] = title[:512]
        attributes["upload_timestamp"] = datetime.now().isoformat()[:512]
        attributes["script_name"] = cfg.SCRIPT_NAME[:512]
        logger.info("Preparing attributes for Vector Store file:")
        logger.info(f"   source_url: {attributes.get('source_url', 'N/A')}")
        logger.info(f"   lookup_source_url: {attributes.get('lookup_source_url', 'N/A')}")
        logger.info(f"   title: {attributes.get('title', 'N/A')[:50]}...")
        logger.info(f"   upload_timestamp: {attributes.get('upload_timestamp', 'N/A')}")
        logger.info(f"   script_name: {attributes.get('script_name', 'N/A')}")

        # Step 4: Add file to vector store
        result = add_file_to_vector_store(file_id, vector_store_id, attributes, chunking_strategy)
        if result:
            # Update cache with newly uploaded file to prevent re-upload during same run
            if vector_store_cache is not None and lookup_url:
                normalized_lookup_url = normalize_url_query_params(lookup_url)
                with _vs_cache_lock:
                    vector_store_cache[normalized_lookup_url] = {
                        "file_id": file_id,
                        "file_info": {"id": file_id, "status": result.get("status", "unknown")},
                        "attributes": attributes,
                    }
                    _index_add(normalized_lookup_url)
                logger.info(f"Updated Vector Store cache with new file: {normalized_lookup_url} -> {file_id}")
            action = "Replaced" if existing_files else "Uploaded"
            logger.info(f"Successfully processed file {filepath} to vector store")
            print(f"{action} in Vector Store: {filepath} (File ID: {file_id})")
            return True
        logger.error(f"Failed to add file to vector store: {filepath}")
        return False
