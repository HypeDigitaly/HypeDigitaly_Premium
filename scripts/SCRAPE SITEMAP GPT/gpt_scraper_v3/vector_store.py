"""Vector Store operations for GPT Scraper V3.

All OpenAI Vector Store interactions: file upload, file management,
deduplication caching, chunking strategy, and upload-and-add orchestration.
Migrated from scrape_sitemap_GPT_v2.py (lines 5512-6102).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.utilities import (
    count_tokens_approximate,
    get_session,
    normalize_url_query_params,
)

logger = logging.getLogger(__name__)

# Module-level cache for Vector Store file lookups
_vector_store_cache: Dict[str, Any] = {}


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
        with open(filepath, "rb") as file:
            files = {
                "file": (os.path.basename(filepath), file, "text/plain"),
                "purpose": (None, "assistants"),
            }
            response = get_session().post(
                f"{cfg.OPENAI_API_BASE_URL}/files",
                headers=headers, files=files, timeout=cfg.REQUEST_TIMEOUT,
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
        response = get_session().post(
            f"{cfg.OPENAI_API_BASE_URL}/vector_stores/{vector_store_id}/files",
            headers=headers, json=payload, timeout=cfg.REQUEST_TIMEOUT,
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
            response = get_session().get(
                f"{cfg.OPENAI_API_BASE_URL}/vector_stores/{vector_store_id}/files",
                headers=headers, params=params, timeout=cfg.REQUEST_TIMEOUT,
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
        response = get_session().get(
            f"{cfg.OPENAI_API_BASE_URL}/vector_stores/{vector_store_id}/files/{file_id}",
            headers=headers, timeout=cfg.REQUEST_TIMEOUT,
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
        response = get_session().delete(
            f"{cfg.OPENAI_API_BASE_URL}/vector_stores/{vector_store_id}/files/{file_id}",
            headers=headers, timeout=cfg.REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("deleted"):
            logger.info(f"Successfully deleted file {file_id} from vector store")
            return True
        logger.error(f"Failed to delete file {file_id} from vector store")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error deleting file from vector store: {str(e)}")
        return False
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Unexpected error deleting file from vector store: {str(e)}")
        return False


def build_vector_store_cache(vector_store_id: str) -> Dict[str, Any]:
    """Build a fast lookup cache of all files in Vector Store with their metadata."""
    logger.info(f"Building Vector Store cache for {vector_store_id}")
    print("Building Vector Store cache...")
    start_time = time.time()
    files = list_vector_store_files(vector_store_id)
    logger.info(f"Found {len(files)} files in Vector Store")
    print(f"Found {len(files)} files in Vector Store")
    if len(files) == 0:
        logger.info("Vector Store is empty, skipping cache build")
        print("Vector Store is empty, no cache needed")
        return {}
    url_to_file_cache: Dict[str, Any] = {}
    metadata_fetch_count = 0
    skipped_404_count = 0
    for i, file_info in enumerate(files, 1):
        file_id = file_info.get("id")
        if not file_id:
            continue
        # Progress indicator for large Vector Stores
        if len(files) > 10 and i % max(1, len(files) // 10) == 0:
            progress = (i / len(files)) * 100
            elapsed = time.time() - start_time
            print(f"Cache progress: {progress:.0f}% ({i}/{len(files)}) - {elapsed:.1f}s elapsed")
        try:
            attributes = get_vector_store_file_attributes(vector_store_id, file_id)
            metadata_fetch_count += 1
            if attributes is None:
                skipped_404_count += 1
                logger.debug(f"Skipping file {file_id} (404 Not Found - deleted or processing)")
                continue
        except Exception as e:
            logger.error(f"Failed to get attributes for file {file_id}: {str(e)}")
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
    logger.info(f"Built Vector Store cache in {elapsed_time:.2f}s with {metadata_fetch_count} metadata fetches")
    logger.info(f"Cache contains {len(url_to_file_cache)} files with source URLs")
    if skipped_404_count > 0:
        logger.info(f"Skipped {skipped_404_count} files with 404 errors (deleted or processing)")
        print(f"Skipped {skipped_404_count} files (404 - deleted or processing)")
    print(f"Cache built: {len(url_to_file_cache)} files indexed in {elapsed_time:.1f}s")
    return url_to_file_cache


def find_existing_file_by_url_cached(
    lookup_url: str, cache: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Find an existing file in vector store by lookup URL using pre-built cache."""
    normalized_lookup_url = normalize_url_query_params(lookup_url)
    logger.debug(f"Searching cache for existing file with normalized lookup URL: {normalized_lookup_url}")
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
    for cached_url, cached_file in cache.items():
        if cached_url == normalized_input_url:
            file_id = cached_file["file_id"]
            logger.info(f"Found existing file {file_id} in cache for EXACT URL match: {normalized_input_url}")
            matching_files.append(cached_file["file_info"])
            break  # Only one exact match possible
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
) -> bool:
    """Upload file to OpenAI and add to vector store with deduplication."""
    cfg = get_config()
    logger.info(f"Starting upload and vector store process for: {filepath}")

    # Pre-upload validation: check file size before uploading
    try:
        if not os.path.exists(filepath):
            logger.error(f"File does not exist for validation: {filepath}")
            return False
        with open(filepath, "r", encoding="utf-8") as f:
            file_tokens = count_tokens_approximate(f.read())
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
            for existing_file_item in existing_files:
                existing_file_id = existing_file_item.get("id")
                if delete_vector_store_file(vector_store_id, existing_file_id):
                    logger.info(f"Deleted old file {existing_file_id} with EXACT URL match: {lookup_url}")
                    deleted_count += 1
                else:
                    logger.warning(f"Failed to delete old file {existing_file_id}, continuing with upload")
            print(f"Deleted {deleted_count}/{len(existing_files)} old file(s)")
            # Remove ONLY the exact matching URL from cache
            if vector_store_cache is not None:
                normalized_lookup_url = normalize_url_query_params(lookup_url)
                if normalized_lookup_url in vector_store_cache:
                    del vector_store_cache[normalized_lookup_url]
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
            vector_store_cache[normalized_lookup_url] = {
                "file_id": file_id,
                "file_info": {"id": file_id, "status": result.get("status", "unknown")},
                "attributes": attributes,
            }
            logger.info(f"Updated Vector Store cache with new file: {normalized_lookup_url} -> {file_id}")
        action = "Replaced" if existing_files else "Uploaded"
        logger.info(f"Successfully processed file {filepath} to vector store")
        print(f"{action} in Vector Store: {filepath} (File ID: {file_id})")
        return True
    logger.error(f"Failed to add file to vector store: {filepath}")
    return False
