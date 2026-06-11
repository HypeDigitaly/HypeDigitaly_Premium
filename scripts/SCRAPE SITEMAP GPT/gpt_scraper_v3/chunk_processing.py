"""Chunk processing pipeline for GPT Scraper V3.

Handles processing and saving content chunks with metadata budget management.
Migrated from V2 lines 3274-3466 with the following fixes applied:
  - M5: Type hints on all public function signatures
  - M7: Structured metadata truncation by section priority (not character slicing)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.utilities import (
    count_tokens_approximate, construct_final_filename,
    create_filename_from_url, reserve_unique_filepath_fn,
    write_content_to_reserved_path,
)
from gpt_scraper_v3.metadata_budget import (
    calculate_metadata_token_allocation,
    generate_budget_constrained_page_summary,
    generate_budget_constrained_current_file_summary,
    generate_budget_constrained_overlap_summary,
    generate_budget_constrained_question_section,
    create_budget_constrained_metadata_header,
)
from gpt_scraper_v3.vector_store import upload_and_add_to_vector_store

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# M7 fix: Priority-based metadata truncation
# ---------------------------------------------------------------------------


def _truncate_metadata_by_priority(
    metadata_header: str, question_section: str, chunk_content: str,
    vector_store_limit: int, *,
    overlap_summary: Optional[str], current_file_summary: Optional[str],
    page_summary: Optional[str], url: str, title: str,
    last_modified: Optional[Any], path: Optional[str],
    rss_metadata: Optional[Dict[str, Any]],
    file_order: int, total_files: int,
) -> str:
    """Reduce metadata to fit within *vector_store_limit* via section priority.

    Removes optional metadata sections in order (least critical first):
    1. overlap summary  2. current_file_summary  3. page_summary
    4. question section  5. last resort: truncate at line boundary.
    Content is NEVER truncated.
    """
    content_tokens = count_tokens_approximate(chunk_content)

    def _rebuild(ps: Optional[str], cfs: Optional[str],
                 ov: Optional[str]) -> str:
        return create_budget_constrained_metadata_header(
            url, title, last_modified, path, rss_metadata,
            source_page_summary=ps, file_order=file_order,
            total_files=total_files, current_file_summary=cfs,
            overlap_summary=ov,
        )

    # Remove sections in priority order (least critical first)
    removal_stages: List[Tuple[str, Optional[str], Optional[str], Optional[str]]] = [
        ("overlap summary", None, current_file_summary, page_summary),
        ("current file summary", None, None, page_summary),
        ("page summary", None, None, None),
    ]
    for stage_name, ov, cfs, ps in removal_stages:
        candidate = _rebuild(ps, cfs, ov) + question_section + chunk_content
        if count_tokens_approximate(candidate) <= vector_store_limit:
            logger.warning("PRIORITY TRUNCATION: Removed %s (%d tokens, limit %d)",
                           stage_name, count_tokens_approximate(candidate), vector_store_limit)
            return candidate

    # All optional sections removed -- try without question section
    minimal_header = _rebuild(None, None, None)
    candidate = minimal_header + chunk_content
    if count_tokens_approximate(candidate) <= vector_store_limit:
        logger.warning("PRIORITY TRUNCATION: Removed question + all summaries (%d tokens)",
                       count_tokens_approximate(candidate))
        return candidate

    # Last resort: truncate remaining metadata at a line boundary
    max_meta_tokens = vector_store_limit - content_tokens
    if max_meta_tokens > 0:
        est_chars = int(max_meta_tokens * 3.2)
        truncated = minimal_header[:est_chars]
        last_nl = truncated.rfind("\n")
        if last_nl > est_chars // 2:
            truncated = truncated[:last_nl]
        result = truncated + "\n---\n\n" + chunk_content
        logger.warning("LAST-RESORT TRUNCATION: metadata at line boundary (%d tokens)",
                       count_tokens_approximate(result))
        return result

    logger.error("CRITICAL: Content alone (%d tokens) exceeds limit (%d)!",
                 content_tokens, vector_store_limit)
    return chunk_content


# ---------------------------------------------------------------------------
# Main budget-aware chunking pipeline  (V2 lines 3274-3466)
# ---------------------------------------------------------------------------


def process_and_save_chunks_with_metadata_budget(
    chunks: List[Dict[str, Any]], base_title: str, url: str,
    target_language: str = "Czech",
    upload_to_vector_store: bool = False,
    vector_store_id: Optional[str] = None,
    enable_deduplication: bool = True,
    chunking_strategy: Optional[Dict[str, Any]] = None,
    vector_store_cache: Optional[Dict[str, Any]] = None,
    last_modified: Optional[Any] = None,
    path: Optional[str] = None,
    provider_used: Optional[str] = None,
    rss_metadata: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Process chunks with metadata budget management and save complete files.

    Generates metadata (summaries, questions) within a token budget, validates
    against the vector store limit, and saves each chunk.

    Returns:
        List of saved file paths.
    """
    cfg = get_config()
    logger.info("PROCESSING CHUNKS WITH METADATA BUDGET: %d chunks", len(chunks))
    if not chunks:
        logger.error("No chunks provided for processing")
        return []

    total_chunks = len(chunks)
    saved_files: List[str] = []
    prev_content: Optional[str] = None
    prev_summary: Optional[str] = None

    original_content = "\n".join(c["content"] for c in chunks)
    metadata_budget: int = chunks[0].get("metadata_budget", 2048)
    allocation = calculate_metadata_token_allocation(metadata_budget)

    page_summary = generate_budget_constrained_page_summary(
        original_content, max_tokens=allocation["source_page_summary"],
        title=base_title, url=url,
    )

    for _i, chunk in enumerate(chunks):
        chunk_order: int = chunk["chunk_number"]
        chunk_content: str = chunk["content"]
        content_budget: int = chunk.get("content_budget", 2048)

        logger.info("Processing chunk %d/%d (postfix: %s)",
                     chunk_order, total_chunks, chunk["chunk_postfix"])

        cur_file_summary = generate_budget_constrained_current_file_summary(
            chunk_content, chunk_order, total_chunks,
            max_tokens=allocation["current_file_summary"],
            title=base_title, url=url,
        )

        overlap_summary: Optional[str] = None
        if chunk_order > 1 and prev_content:
            overlap_summary = generate_budget_constrained_overlap_summary(
                page_summary or "", prev_summary or "", prev_content,
                chunk_order - 1, chunk_order, total_chunks,
                max_tokens=allocation["overlap_summary"],
                title=base_title, url=url,
            )

        question_text = generate_budget_constrained_question_section(
            url, base_title, max_tokens=allocation["question_section"],
            page_summary=page_summary, rss_metadata=rss_metadata,
        )

        metadata_header = create_budget_constrained_metadata_header(
            url, base_title, last_modified, path, rss_metadata,
            source_page_summary=page_summary, file_order=chunk_order,
            total_files=total_chunks, current_file_summary=cur_file_summary,
            overlap_summary=overlap_summary,
        )

        q_section = (f"# **QUESTION:**\n\n{question_text}\n\n# **ANSWER:**\n\n"
                     if question_text else "")
        complete = metadata_header + q_section + chunk_content

        # Validate total file size
        vs_limit: int = cfg.DEFAULT_MAX_CHUNK_SIZE
        if count_tokens_approximate(complete) > vs_limit:
            logger.error("CRITICAL: File exceeds limit! %d > %d tokens",
                         count_tokens_approximate(complete), vs_limit)
            complete = _truncate_metadata_by_priority(
                metadata_header, q_section, chunk_content, vs_limit,
                overlap_summary=overlap_summary,
                current_file_summary=cur_file_summary,
                page_summary=page_summary, url=url, title=base_title,
                last_modified=last_modified, path=path,
                rss_metadata=rss_metadata,
                file_order=chunk_order, total_files=total_chunks,
            )

        # Build filename
        base_fn = create_filename_from_url(url, base_title, rss_metadata)
        is_paginated = "_PAGE" in base_title
        page_number: Optional[int] = None
        if is_paginated:
            try:
                pp = base_title.split("_PAGE")[-1]
                page_number = int(pp) if pp.isdigit() else None
            except (ValueError, IndexError):
                page_number = None

        chunk_fn = construct_final_filename(
            base_filename=base_fn, is_paginated=is_paginated,
            page_number=page_number, chunk_postfix=chunk["chunk_postfix"],
        )

        # Build lookup URL for vector store dedup
        if is_paginated:
            page_info = base_title.split("_PAGE")[-1]
            lookup_url = f"{url}#PAGE{page_info}chunk{chunk['chunk_postfix']}"
        else:
            lookup_url = f"{url}#chunk{chunk['chunk_postfix']}"

        # Compute the token count of the FINAL ``complete`` string once. This is
        # the exact content written to disk below (write_content_to_reserved_path
        # writes ``complete`` verbatim, utf-8) -- nothing is prepended/appended
        # between here and the write -- so counted-bytes == written-bytes. We
        # forward both into upload_and_add_to_vector_store (Bug 13) so it skips
        # re-reading the file and re-tokenizing.
        complete_token_count = count_tokens_approximate(complete)
        logger.info("SAVING: %s (%d tokens)", chunk_fn, complete_token_count)

        try:
            os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)
            # Atomically reserve a unique path, preserving the existing
            # ``<chunk_fn>_V{n}`` naming (counter 0 = base, n>0 = versioned).
            # O_CREAT|O_EXCL closes the TOCTOU race two workers hit with a
            # plain os.path.exists loop on the same base name.
            base_fp = os.path.join(cfg.OUTPUT_DIR, f"{chunk_fn}.txt")
            _root, _ext = os.path.splitext(base_fp)

            def _chunk_name_fn(counter: int, _r: str = _root,
                               _e: str = _ext) -> str:
                if counter == 0:
                    return f"{_r}{_e}"
                return f"{_r}_V{counter}{_e}"

            filepath = reserve_unique_filepath_fn(_chunk_name_fn)
            resolved = os.path.realpath(filepath)
            if not resolved.startswith(os.path.realpath(cfg.OUTPUT_DIR)):
                try:
                    os.remove(filepath)
                except OSError:
                    pass
                raise ValueError(f"Path traversal detected: {filepath}")

            # Crash-safe write into the reserved path (temp + os.replace); on
            # failure the 0-byte reservation placeholder is removed for us.
            write_content_to_reserved_path(filepath, complete)
            logger.info("Saved chunk file: %s", filepath)
            print(f"Saved: {filepath}")

            if upload_to_vector_store and vector_store_id:
                ok = upload_and_add_to_vector_store(
                    filepath, vector_store_id, lookup_url, base_title,
                    enable_deduplication, chunking_strategy, vector_store_cache,
                    content=complete, token_count=complete_token_count,
                )
                if not ok:
                    logger.warning("Upload failed for %s (saved locally)", filepath)
            saved_files.append(filepath)
        except (OSError, IOError) as exc:
            logger.error("Error saving %s: %s", chunk_fn, exc)
            continue

        prev_content = chunk_content
        prev_summary = cur_file_summary

    logger.info("BUDGET PROCESSING COMPLETE: %d/%d saved", len(saved_files), total_chunks)
    return saved_files
