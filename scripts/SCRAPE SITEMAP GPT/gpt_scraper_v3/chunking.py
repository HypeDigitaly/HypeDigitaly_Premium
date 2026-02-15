"""Adaptive content chunking algorithms for GPT Scraper V3.

Migrated from scrape_sitemap_GPT_v2.py with fixes:
  M5 -- type hints on all public signatures
  M3 -- no redundant function-local re-imports
  All global variable access replaced with get_config()
  get_chunk_postfix / count_tokens_approximate imported from utilities
"""
from __future__ import annotations

import logging
import re
import warnings
from typing import Any, Dict, List

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.utilities import count_tokens_approximate, get_chunk_postfix, sanitize_filename

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CZECH_KEEP = (
    r'\u00E1\u010D\u010F\u00E9\u011B\u00ED\u0148\u00F3\u0159\u0161\u0165'
    r'\u00FA\u016F\u00FD\u017E\u00C1\u010C\u010E\u00C9\u011A\u00CD\u0147'
    r'\u00D3\u0158\u0160\u0164\u00DA\u016E\u00DD\u017D'
)


def _make_budget_chunk(content: str, tokens: int, postfix: str,
                       chunk_num: int, content_budget: int,
                       metadata_budget: int) -> Dict[str, Any]:
    return {'content': content, 'tokens': tokens, 'chunk_postfix': postfix,
            'chunk_number': chunk_num, 'type': 'metadata_budget_chunk',
            'filename_postfix': postfix, 'content_budget': content_budget,
            'metadata_budget': metadata_budget}


def _make_simple_chunk(content: str, tokens: int, postfix: str,
                       chunk_num: int) -> Dict[str, Any]:
    return {'content': content, 'tokens': tokens, 'chunk_postfix': postfix,
            'chunk_number': chunk_num, 'type': 'simple_size_based_chunk',
            'filename_postfix': postfix}

# ---------------------------------------------------------------------------
# Metadata-budget-aware chunking (primary)
# ---------------------------------------------------------------------------


def chunk_content_with_metadata_budget(
    content: str, vector_store_max_chunk_size: int,
    base_title: str = "", url: str = "", target_language: str = "Czech",
) -> List[Dict[str, Any]]:
    """Split content into A-Z files with exact token budget allocation.

    TOKEN BUDGET::

        content_budget  = (max_chunk_size - offset) * content_ratio
        metadata_budget = (max_chunk_size - offset) * (1 - content_ratio)
        required_chunks = ceil(total_tokens / content_budget)

    Args:
        content: Original markdown content to split.
        vector_store_max_chunk_size: Max tokens per file from vector store config.
        base_title: Base title for filename generation.
        url: Source URL for metadata.
        target_language: Target language for metadata generation.

    Returns:
        List of content chunks with [A-Z] naming.
    """
    cfg = get_config()
    logger.info("Content splitting: max_chunk=%d", vector_store_max_chunk_size)

    offset = cfg.DEFAULT_CONTENT_TOKEN_OFFSET
    working = vector_store_max_chunk_size - offset
    ratio = cfg.DEFAULT_CONTENT_RATIO
    cb = int(working * ratio)
    mb = working - cb

    if cb <= 0:
        logger.error("Content budget %d <= 0, defaulting to 100", cb)
        cb, mb = 100, vector_store_max_chunk_size - 100

    logger.info("Budget: content=%d (%.0f%%), meta=%d, offset=%d", cb, ratio*100, mb, offset)

    total_tok = count_tokens_approximate(content)
    req = max(1, (total_tok + cb - 1) // cb)
    logger.info("Total %d tok -> %d chunks (budget %d each)", total_tok, req, cb)

    chunks: List[Dict[str, Any]] = []
    lines = content.split('\n')
    cur, cur_tok, cn = "", 0, 0

    for line in lines:
        lt = count_tokens_approximate(line)

        if lt > cb:
            logger.warning("Line %d tok > budget %d, word-splitting", lt, cb)
            if cur.strip():
                pf = get_chunk_postfix(cn)
                chunks.append(_make_budget_chunk(cur.strip(), cur_tok, pf, cn+1, cb, mb))
                logger.info("Chunk %s: %d tok", pf, cur_tok)
                cur, cur_tok = "", 0; cn += 1

            words, seg = line.split(' '), ""
            for w in words:
                test = (seg + " " + w) if seg else w
                if count_tokens_approximate(test) > cb:
                    if seg.strip():
                        pf = get_chunk_postfix(cn)
                        chunks.append(_make_budget_chunk(
                            seg.strip(), count_tokens_approximate(seg), pf, cn+1, cb, mb))
                        logger.info("Chunk %s: %d tok (long-line)", pf, count_tokens_approximate(seg))
                        cn += 1
                    seg = w
                else:
                    seg = test
            if seg.strip():
                cur = seg.strip() + '\n'; cur_tok = count_tokens_approximate(cur)
            continue

        if cur_tok + lt > cb and cur.strip():
            pf = get_chunk_postfix(cn)
            chunks.append(_make_budget_chunk(cur.strip(), cur_tok, pf, cn+1, cb, mb))
            logger.info("Chunk %s: %d tok", pf, cur_tok)
            cur, cur_tok = line + '\n', lt; cn += 1
        else:
            cur += line + '\n'; cur_tok = count_tokens_approximate(cur)

    if cur.strip():
        pf = get_chunk_postfix(cn)
        chunks.append(_make_budget_chunk(cur.strip(), cur_tok, pf, cn+1, cb, mb))
        logger.info("Final chunk %s: %d tok", pf, cur_tok)

    logger.info("Budget chunking done: %d chunks (content=%d, meta=%d, total=%d)",
                len(chunks), cb, mb, vector_store_max_chunk_size)
    return chunks

# ---------------------------------------------------------------------------
# Legacy simple chunker (deprecated)
# ---------------------------------------------------------------------------


def chunk_large_content_simple(
    content: str, max_chunk_size: int, base_title: str = "", url: str = "",
) -> List[Dict[str, Any]]:
    """Legacy simple chunking -- kept for backward compatibility.

    .. deprecated::
        Use :func:`chunk_content_with_metadata_budget` instead.

    Args:
        content: Original markdown content to chunk.
        max_chunk_size: Maximum tokens per chunk.
        base_title: Base title for filename generation.
        url: Source URL for metadata.

    Returns:
        List of simple content chunks with [A-Z] naming.
    """
    warnings.warn(
        "chunk_large_content_simple is deprecated, use chunk_content_with_metadata_budget instead",
        DeprecationWarning, stacklevel=2,
    )
    logger.warning("DEPRECATED: chunk_large_content_simple()")
    logger.info("Legacy chunking for %s (max=%d)", url, max_chunk_size)

    chunks: List[Dict[str, Any]] = []
    lines = content.split('\n')
    cur, cur_tok, cn = "", 0, 0

    for line in lines:
        lt = count_tokens_approximate(line)

        if lt > max_chunk_size:
            logger.warning("Line %d tok > max %d, splitting", lt, max_chunk_size)
            if cur.strip():
                pf = get_chunk_postfix(cn)
                chunks.append(_make_simple_chunk(cur.strip(), cur_tok, pf, cn+1))
                logger.info("Chunk %s: %d tok", pf, cur_tok)
                cur, cur_tok = "", 0; cn += 1

            words, seg = line.split(' '), ""
            for w in words:
                if count_tokens_approximate(seg + " " + w) > max_chunk_size:
                    pf = get_chunk_postfix(cn)
                    chunks.append(_make_simple_chunk(
                        seg.strip(), count_tokens_approximate(seg), pf, cn+1))
                    logger.info("Chunk %s: %d tok (long-line)", pf, count_tokens_approximate(seg))
                    cn += 1; seg = w + " "
                else:
                    seg += w + " "
            if seg.strip():
                cur = seg.strip() + '\n'; cur_tok = count_tokens_approximate(cur)
            continue

        if cur_tok + lt > max_chunk_size and cur.strip():
            pf = get_chunk_postfix(cn)
            chunks.append(_make_simple_chunk(cur.strip(), cur_tok, pf, cn+1))
            logger.info("Chunk %s: %d tok", pf, cur_tok)
            cur, cur_tok = line + '\n', lt; cn += 1
        else:
            cur += line + '\n'; cur_tok = count_tokens_approximate(cur)

    if cur.strip():
        pf = get_chunk_postfix(cn)
        chunks.append(_make_simple_chunk(cur.strip(), cur_tok, pf, cn+1))
        logger.info("Final chunk %s: %d tok (limit %d)", pf, cur_tok, max_chunk_size)

    logger.info("Legacy chunking done: %d chunks", len(chunks))
    return chunks

# ---------------------------------------------------------------------------
# Section-based filename generation
# ---------------------------------------------------------------------------

_CLEAN_RE = re.compile(
    rf'[^\w\s\-\u2013{_CZECH_KEEP}]'
)


def generate_section_based_filename(
    section_title: str, chunk_number: int, base_title: str = "",
) -> str:
    """Generate a meaningful filename based on section content.

    Args:
        section_title: The section title from chunking.
        chunk_number: Chunk number for fallback naming.
        base_title: Base title for context (unused -- keeps filenames short).

    Returns:
        Sanitised filename without extension.
    """
    fallback = f"sekce_{chunk_number:02d}"
    if section_title and section_title.strip() and section_title != "Content Section":
        ct = section_title.strip()
        ct = re.sub(r'^#+\s*', '', ct)
        ct = re.sub(r'\*\*(.+?)\*\*', r'\1', ct)
        ct = re.sub(r'\*(.+?)\*', r'\1', ct)
        ct = _CLEAN_RE.sub('_', ct)
        ct = re.sub(r'\s+', '_', ct)
        ct = re.sub(r'_+', '_', ct).strip('_')
        if len(ct) > 80:
            ct = ct[:80]
        name = ct if ct else fallback
    else:
        name = fallback
    return sanitize_filename(name)
