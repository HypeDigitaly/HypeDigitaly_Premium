"""File saving module for GPT Scraper V3.

Metadata header creation and markdown file output with optional vector store
upload.  Migrated from scrape_sitemap_GPT_v2.py (lines 8133-8488).

Fixes applied:
  - H2: Removed duplicate event metadata block (copy-paste bug in V2)
  - M1: Replaced bare ``except:`` with specific exception types
  - M5: Type hints on all public function signatures
  - Path traversal protection in save_markdown_to_file()
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.utilities import (
    construct_final_filename,
    create_filename_from_url,
    count_tokens_approximate,
    sanitize_filename,
)
from gpt_scraper_v3.vector_store import (
    find_existing_files_by_url_cached,
    upload_and_add_to_vector_store,
)

logger = logging.getLogger(__name__)

# Optional dateutil import for RSS date parsing
try:
    from dateutil import parser as dateutil_parser
except ImportError:
    dateutil_parser = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Metadata header creation
# ---------------------------------------------------------------------------


def create_metadata_header(
    url: str,
    title: str,
    last_modified: Optional[Any] = None,
    path: Optional[str] = None,
    provider_used: Optional[str] = None,
    rss_metadata: Optional[Dict[str, Any]] = None,
    source_page_summary: Optional[str] = None,
    file_order: Optional[int] = None,
    total_files: Optional[int] = None,
    current_file_summary: Optional[str] = None,
    overlap_summary: Optional[str] = None,
) -> str:
    """Create a Markdown-formatted metadata header for the file.

    Args:
        url: Source URL of the content.
        title: Page title.
        last_modified: Last modification date from sitemap.
        path: Navigation path from sitemap.
        provider_used: API provider used (jina/firecrawl).
        rss_metadata: RSS-specific metadata (published, summary, source_feed).
        source_page_summary: Short 1-2 paragraph summary of the entire page.
        file_order: Current file position in sequence (e.g., 1, 2, 3...).
        total_files: Total number of files in sequence.
        current_file_summary: Summary of the current file in context of sequence.
        overlap_summary: Overlap summary for next file (not for last file).

    Returns:
        Formatted Markdown metadata header with table and emojis.
    """
    # Current processing time
    processing_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Format last modified date
    last_mod_str = "Unknown"
    if last_modified:
        if hasattr(last_modified, "strftime"):
            last_mod_str = last_modified.strftime("%Y-%m-%d %H:%M:%S")
        else:
            last_mod_str = str(last_modified)

    # Provider emoji mapping
    provider_emoji: Dict[str, str] = {
        "jina": "\U0001f916",
        "firecrawl": "\U0001f525",
    }
    provider_display = (
        f"{provider_emoji.get(provider_used or '', '\U0001f4c4')} "
        f"{provider_used or 'N/A'}"
    )

    # Create Markdown metadata section
    metadata_lines: List[str] = [
        "# \U0001f4c4 Metadata souboru",
        "",
        "## \U0001f517 **ZDROJOV\u00c1 URL:**",
        f"### **{url}**",
        "",
        "## \U0001f4dd **TITULEK:**",
        f"### **{title or 'N/A'}**",
        "",
        "## \U0001f9ed **CESTA/NAVIGACE KE ZDROJOV\u00c9 URL NA WEBU:**",
        f"### **{path or 'N/A'}**",
        "",
        "## \U0001f4c5 **DATUM POSLEDN\u00cd MODIFIKACE URL NA WEBU:**",
        f"### **{last_mod_str}**",
        "",
    ]

    # Add file order information if this is a chunked file
    if file_order is not None and total_files is not None:
        metadata_lines.extend([
            "\U0001f5c2\ufe0f **FILE ORDER INFORMATION:**",
            f"### **This is file {file_order} of {total_files} total files "
            f"from the original document**",
            "",
        ])

    # Add SOURCE PAGE SUMMARY if available
    if source_page_summary and source_page_summary.strip():
        metadata_lines.extend([
            "## \U0001f4ca **SOURCE PAGE SUMMARY:**",
            f"### **{source_page_summary.strip()}**",
            "",
        ])

    # Add CURRENT FILE SUMMARY if available (for chunked files)
    if current_file_summary and current_file_summary.strip():
        metadata_lines.extend([
            "## \U0001f4cb **CURRENT FILE SUMMARY:**",
            f"### **{current_file_summary.strip()}**",
            "",
        ])

    # Add PREVIOUS FILE / INFORMATION OVERLAP SUMMARY if available
    if overlap_summary and overlap_summary.strip():
        metadata_lines.extend([
            "## \U0001f504 **PREVIOUS FILE / INFORMATION OVERLAP SUMMARY:**",
            f"### **{overlap_summary.strip()}**",
            "",
        ])

    # Add RSS-specific metadata if available
    if rss_metadata:
        # Determine source type
        source_type = (
            "\U0001f4e1 RSS Feed"
            if rss_metadata.get("source_feed")
            else "\U0001f5fa\ufe0f Sitemap"
        )
        metadata_lines.extend([
            "## \U0001f4ca **TYP ZDROJE:**",
            f"### **{source_type}**",
            "",
        ])

        # Add RSS publication date if available
        if rss_metadata.get("published"):
            pub_date = rss_metadata["published"]
            # Try to parse and format the date if it's a string
            if isinstance(pub_date, str) and dateutil_parser:
                try:
                    parsed_date = dateutil_parser.parse(pub_date)
                    pub_date = parsed_date.strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, OverflowError):
                    # If parsing fails, use the original string
                    pass

            metadata_lines.extend([
                "## \U0001f4c5 **DATUM PUBLIKACE (RSS):**",
                f"### **{pub_date}**",
                "",
            ])

        # Add RSS source feed if available
        if rss_metadata.get("source_feed"):
            metadata_lines.extend([
                "## \U0001f4e1 **ZDROJOV\u00dd RSS FEED:**",
                f"### **{rss_metadata['source_feed']}**",
                "",
            ])

        # Add RSS summary/description if available
        if rss_metadata.get("summary") or rss_metadata.get("description"):
            summary = rss_metadata.get("summary") or rss_metadata.get("description")
            # Truncate if too long
            if summary and len(summary) > 200:
                summary = summary[:200] + "..."
            metadata_lines.extend([
                "## \U0001f4dd **POPIS/SHRNUT\u00cd (RSS):**",
                f"### **{summary}**",
                "",
            ])

        # Add Events XML specific metadata if available
        # FIX H2: This block appears ONLY ONCE (V2 had it duplicated)
        if rss_metadata.get("event_metadata"):
            event_data = rss_metadata["event_metadata"]
            metadata_lines.extend([
                "## \U0001f3aa **INFORMACE O UD\u00c1LOSTI:**",
                "",
            ])

            # Add event details in a structured format
            if event_data.get("event_id", "Unknown") != "Unknown":
                metadata_lines.append(
                    f"- **ID ud\u00e1losti**: {event_data['event_id']}"
                )
            if event_data.get("start_date", "Unknown") != "Unknown":
                date_time = f"{event_data['start_date']}"
                if event_data.get("start_time", "Unknown") != "Unknown":
                    date_time += f" {event_data['start_time']}"
                metadata_lines.append(f"- **Datum a \u010das**: {date_time}")
            if event_data.get("organizer", "Unknown") != "Unknown":
                metadata_lines.append(
                    f"- **Organiz\u00e1tor**: {event_data['organizer']}"
                )
            if event_data.get("place", "Unknown") != "Unknown":
                metadata_lines.append(
                    f"- **M\u00edsto**: {event_data['place']}"
                )
            if event_data.get("event_type", "Unknown") != "Unknown":
                metadata_lines.append(
                    f"- **Typ ud\u00e1losti**: {event_data['event_type']}"
                )

            metadata_lines.append("")

    else:
        # If no RSS metadata, indicate it's from sitemap
        metadata_lines.extend([
            "## \U0001f4ca **TYP ZDROJE:**",
            "### **\U0001f5fa\ufe0f Sitemap**",
            "",
        ])

    metadata_lines.extend([
        "---",
        "",
        "",  # Empty line before content
    ])

    return "\n".join(metadata_lines)


# ---------------------------------------------------------------------------
# File saving
# ---------------------------------------------------------------------------


def save_markdown_to_file(
    content: str,
    title: str,
    url: str,
    question_text: Optional[str] = None,
    upload_to_vector_store: bool = False,
    vector_store_id: Optional[str] = None,
    enable_deduplication: bool = True,
    chunking_strategy: Optional[Dict[str, Any]] = None,
    vector_store_cache: Optional[Dict[str, Any]] = None,
    last_modified: Optional[Any] = None,
    path: Optional[str] = None,
    provider_used: Optional[str] = None,
    rss_metadata: Optional[Dict[str, Any]] = None,
    lookup_url: Optional[str] = None,
    source_page_summary: Optional[str] = None,
    file_order: Optional[int] = None,
    total_files: Optional[int] = None,
    current_file_summary: Optional[str] = None,
    overlap_summary: Optional[str] = None,
    filename: Optional[str] = None,
) -> Optional[str]:
    """Save markdown content to a txt file with metadata header.

    Optionally uploads the saved file to an OpenAI Vector Store.  Handles both
    normal and chunked content with filename collision ``V[N]`` versioning.

    Returns:
        The filepath of the saved file, or ``None`` on error.
    """
    cfg = get_config()

    try:
        # Use lookup_url for vector store operations, default to url
        vector_store_url: str = lookup_url if lookup_url is not None else url

        # Use pre-constructed filename if provided, otherwise create from URL
        if filename:
            # Pre-constructed filename provided (from chunking logic)
            filename_without_ext = filename
            filename_with_ext = f"{filename}.txt"
            filepath = os.path.join(cfg.OUTPUT_DIR, filename_with_ext)
            logger.info(f"Using pre-constructed filename: {filename}")
        else:
            # Create base filename from URL path instead of title
            base_filename = create_filename_from_url(url, title, rss_metadata)

            # Check if this is a paginated file and extract page number
            is_paginated = False
            page_number: Optional[int] = None
            chunk_postfix: Optional[str] = None

            if "_PAGE" in title:
                is_paginated = True
                # Extract page number from title (e.g., "aktuality_PAGE2")
                try:
                    page_part = title.split("_PAGE")[-1]
                    # Handle cases like "PAGE2A" where there might be a
                    # chunk postfix
                    page_match = re.match(r"^(\d+)([A-Z]?).*$", page_part)
                    if page_match:
                        page_number = int(page_match.group(1))
                        if page_match.group(2):
                            chunk_postfix = page_match.group(2)
                    else:
                        page_number = (
                            int(page_part) if page_part.isdigit() else None
                        )
                except (ValueError, IndexError):
                    logger.warning(
                        "Could not extract page number from title: %s", title
                    )
                    page_number = None

            # Check if this has chunk postfix (for non-paginated chunked content)
            elif (
                title
                and len(title) > 0
                and title[-1].isupper()
                and title[-1].isalpha()
            ):
                potential_chunk = title[-1]
                if potential_chunk in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                    chunk_postfix = potential_chunk
                    # Remove chunk postfix from base filename creation
                    base_filename = create_filename_from_url(url, title[:-1])

            # Construct the final filename with all postfixes
            filename_without_ext = construct_final_filename(
                base_filename=base_filename,
                is_paginated=is_paginated,
                page_number=page_number,
                chunk_postfix=chunk_postfix,
                dedup_counter=None,  # Handled below if collision detected
            )

            # Add .txt extension
            filename_with_ext = f"{filename_without_ext}.txt"
            filepath = os.path.join(cfg.OUTPUT_DIR, filename_with_ext)

        # -- Path traversal protection --
        resolved = os.path.realpath(filepath)
        output_dir_resolved = os.path.realpath(cfg.OUTPUT_DIR)
        if not resolved.startswith(output_dir_resolved):
            raise ValueError(f"Path traversal detected: {filepath}")

        # Check for collisions (local filesystem AND Vector Store)
        collision_detected = False
        version_counter = 1

        # Check 1: Local filesystem collision
        local_collision = os.path.exists(filepath)
        if local_collision:
            logger.info(f"LOCAL FILE COLLISION detected: {filepath}")
            collision_detected = True

        # Check 2: Vector Store collision (if Vector Store is enabled)
        vector_collision = False
        if upload_to_vector_store and vector_store_id and enable_deduplication:
            if vector_store_cache is not None:
                existing_files = find_existing_files_by_url_cached(
                    vector_store_url, vector_store_cache
                )
                if existing_files:
                    logger.info(
                        "VECTOR STORE COLLISION detected: %d existing files "
                        "for %s",
                        len(existing_files),
                        vector_store_url,
                    )
                    vector_collision = True
                    collision_detected = True

        # Apply V[N] versioning ONLY if collision was detected
        if collision_detected:
            logger.info(
                "COLLISION DETECTED - Applying V[N] versioning "
                "(local: %s, vector: %s)",
                local_collision,
                vector_collision,
            )

            if filename:
                # Pre-constructed filename -- parse and add versioning
                original_filepath = filepath
                while os.path.exists(filepath):
                    name, ext = os.path.splitext(original_filepath)
                    filepath = f"{name}_V{version_counter}{ext}"
                    version_counter += 1
            else:
                # Traditional filename construction with V[N] versioning
                while os.path.exists(filepath):
                    filename_with_version = construct_final_filename(
                        base_filename=base_filename,
                        is_paginated=is_paginated,
                        page_number=page_number,
                        chunk_postfix=chunk_postfix,
                        dedup_counter=version_counter,
                    )
                    filepath = os.path.join(
                        cfg.OUTPUT_DIR, f"{filename_with_version}.txt"
                    )
                    version_counter += 1
        else:
            logger.info("NO COLLISION - Using original filename without versioning")

        # Create output directory if it does not exist
        os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)

        # Create metadata header with clean URL for display
        metadata_header = create_metadata_header(
            url,
            title,
            last_modified,
            path,
            provider_used,
            rss_metadata,
            source_page_summary,
            file_order,
            total_files,
            current_file_summary,
            overlap_summary,
        )

        # Insert QUESTION section if provided
        question_section = ""
        if question_text:
            question_section = (
                f"# **QUESTION:**\n\n{question_text}\n\n# **ANSWER:**\n\n"
            )

        # Combine metadata header + QUESTION + content
        full_content = metadata_header + question_section + content

        # Save content to file
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(full_content)

        logger.info(f"Saved markdown content to: {filepath}")
        print(f"Saved: {filepath}")

        # Upload to OpenAI Vector Store if requested
        if upload_to_vector_store and vector_store_id:
            upload_success = upload_and_add_to_vector_store(
                filepath,
                vector_store_id,
                vector_store_url,
                title,
                enable_deduplication,
                chunking_strategy,
                vector_store_cache,
            )
            if not upload_success:
                logger.warning(
                    "Failed to upload %s to vector store, but file was saved "
                    "locally",
                    filepath,
                )

        return filepath

    # FIX M1: Replace bare except: with specific exception types
    except (OSError, IOError, ValueError) as e:
        logger.error(
            "Error saving markdown to file for URL %s: %s", url, str(e)
        )
        return None
