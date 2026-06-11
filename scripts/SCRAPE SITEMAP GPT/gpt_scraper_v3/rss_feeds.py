"""RSS/Atom/Events feed parsing for GPT Scraper V3.

Migrated from scrape_sitemap_GPT_v2.py (lines 6628-7205).
Fixes applied: M5 (type hints), M3 (no local re-imports).
Config access via get_config(); session via get_session().
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from gpt_scraper_v3.config import get_config
from gpt_scraper_v3.rate_limiter import get_rate_limiter
from gpt_scraper_v3.utilities import (
    get_session, is_url_blacklisted_by_path, normalize_url_query_params,
    strip_url_fragment,
)

logger = logging.getLogger(__name__)

try:
    import dateutil.parser as dateutil_parser
except ImportError:
    dateutil_parser = None  # type: ignore[assignment]


def parse_rss_feed(rss_url: str) -> List[Dict[str, Any]]:
    """Fetch *rss_url*, detect feed type, and delegate to the right parser."""
    cfg = get_config()
    logger.info("Parsing RSS feed: %s", rss_url)
    try:
        # Politeness rate limiter keyed by the feed URL (pacing across feeds is
        # intended when parse_rss_feed is called concurrently).
        with get_rate_limiter().acquire(rss_url):
            response = get_session().get(rss_url, timeout=cfg.REQUEST_TIMEOUT)
            response.raise_for_status()
            raw_content: bytes = response.content
        # Remove chrome-extension script tags that corrupt XML parsing
        if b"chrome-extension://" in raw_content or b"<script" in raw_content:
            content_str = raw_content.decode("utf-8", errors="ignore")
            content_str = re.sub(
                r"<script[^>]*>.*?</script>", "", content_str,
                flags=re.DOTALL | re.IGNORECASE,
            )
            content_str = re.sub(
                r"<script[^/>]*/>", "", content_str, flags=re.IGNORECASE,
            )
            raw_content = content_str.encode("utf-8")
            logger.info("Removed chrome extension script tags from XML content")
        soup = BeautifulSoup(raw_content, "xml")
        # Atom feed
        feed_tag = soup.find("feed")
        if feed_tag and isinstance(feed_tag, Tag) and feed_tag.get("xmlns") == "http://www.w3.org/2005/Atom":
            logger.info("Detected Atom feed format for: %s", rss_url)
            return parse_atom_feed(soup, rss_url)
        # RSS 2.0
        if soup.find("rss") or soup.find("channel"):
            logger.info("Detected RSS 2.0 feed format for: %s", rss_url)
            return parse_rss_2_0_feed(soup, rss_url)
        # Events XML
        if soup.find("events") and soup.find("event"):
            logger.info("Detected Events XML format for: %s", rss_url)
            return parse_events_feed(soup, rss_url)
        # Custom <response><item>
        if soup.find("response") and soup.find("item"):
            logger.info("Detected Custom <response><item> feed format for: %s", rss_url)
            logger.info("Applying HTML parser for robust item detection...")
            html_soup = BeautifulSoup(raw_content, "html.parser")
            return parse_custom_response_feed(html_soup, rss_url)
        # Fallback generic
        logger.info("Attempting generic XML parsing for: %s", rss_url)
        return parse_generic_feed(soup, rss_url)
    except Exception as e:
        logger.error("Error parsing RSS feed %s: %s", rss_url, str(e))
        return []


def parse_atom_feed(soup: BeautifulSoup, rss_url: str) -> List[Dict[str, Any]]:
    """Parse Atom feed format and return extracted URL info dicts."""
    cfg = get_config()
    extracted_urls: List[Dict[str, Any]] = []
    entries = soup.find_all("entry")
    logger.info("Found %d entries in Atom feed", len(entries))
    for entry in entries:
        try:
            link_elem = entry.find("link", {"rel": "alternate"})
            if not link_elem:
                link_elem = entry.find("link")
            if link_elem:
                url = link_elem.get("href")
                if url:
                    absolute_url = strip_url_fragment(urljoin(cfg.BASE_URL, url))
                    title_elem = entry.find("title")
                    title = title_elem.text.strip() if title_elem else "No title"
                    published_elem = (
                        entry.find("published") or entry.find("updated")
                        or entry.find("dc:date") or entry.find("dcterms:created")
                        or entry.find("dcterms:modified")
                    )
                    published = published_elem.text.strip() if published_elem else None
                    summary_elem = entry.find("summary")
                    summary = summary_elem.text.strip() if summary_elem else None
                    path = f"RSS: {rss_url} > {title}"
                    extracted_urls.append({
                        "url": absolute_url, "title": title, "path": path,
                        "published": published, "summary": summary,
                        "source_feed": rss_url,
                    })
                    logger.debug("Extracted from Atom: %s - %s", absolute_url, title)
        except Exception as e:
            logger.error("Error processing Atom entry: %s", str(e))
            continue
    return extracted_urls


def parse_rss_2_0_feed(soup: BeautifulSoup, rss_url: str) -> List[Dict[str, Any]]:
    """Parse RSS 2.0 feed format and return extracted URL info dicts."""
    cfg = get_config()
    extracted_urls: List[Dict[str, Any]] = []
    items = soup.find_all("item")
    logger.info("Found %d items in RSS 2.0 feed", len(items))
    for item in items:
        try:
            url: Optional[str] = None
            # 1. Standard <link> tag
            link_elem = item.find("link")
            if link_elem:
                url = link_elem.text.strip() if link_elem.text else link_elem.get("href")
            # 2. <guid> fallback
            if not url:
                guid_elem = item.find("guid")
                if guid_elem and guid_elem.text.strip().startswith("http"):
                    url = guid_elem.text.strip()
            # 3. <id> fallback
            if not url:
                id_elem = item.find("id")
                if id_elem and id_elem.text.strip().startswith("http"):
                    url = id_elem.text.strip()
            if not url:
                logger.debug("Skipping RSS 2.0 item: No valid URL in <link>, <guid>, or <id>.")
                continue
            absolute_url = strip_url_fragment(urljoin(cfg.BASE_URL, url))
            title_elem = item.find("title")
            title = title_elem.text.strip() if title_elem else "No title"
            published_elem = (
                item.find("pubDate") or item.find("dc:date")
                or item.find("dcterms:created") or item.find("dcterms:modified")
                or item.find("published") or item.find("createdDate")
            )
            published: Optional[str] = None
            if published_elem:
                published_text = published_elem.text.strip()
                time_match = re.search(r'<time\s+datetime=["\']([^"\']+)["\']', published_text)
                published = time_match.group(1) if time_match else published_text
            description_elem = item.find("description")
            description = description_elem.text.strip() if description_elem else None
            path = f"RSS: {rss_url} > {title}"
            extracted_urls.append({
                "url": absolute_url, "title": title, "path": path,
                "published": published, "description": description,
                "source_feed": rss_url,
            })
            logger.debug("Extracted from RSS 2.0: %s - %s", absolute_url, title)
        except Exception as e:
            logger.error("Error processing RSS 2.0 item: %s", str(e))
            continue
    return extracted_urls


def parse_events_feed(soup: BeautifulSoup, rss_url: str) -> List[Dict[str, Any]]:
    """Parse Events XML format -- extract structured event data and detail page URLs."""
    cfg = get_config()
    extracted_urls: List[Dict[str, Any]] = []
    events = soup.find_all("event")
    logger.info("Found %d events in Events XML feed", len(events))
    for event in events:
        try:
            details = event.find("details")
            if not details or not details.find("url"):
                logger.debug("Skipping event without details/url")
                continue
            url = details.find("url").text.strip()
            if not url:
                continue
            absolute_url = strip_url_fragment(urljoin(cfg.BASE_URL, url))
            # Event ID
            event_id_elem = event.find("id")
            event_id = event_id_elem.text.strip() if event_id_elem else "Unknown"
            # Event name (handle CDATA)
            name_elem = event.find("name")
            if name_elem:
                name = name_elem.get_text().strip() if name_elem.get_text() else "Unknown Event"
            else:
                name = f"Event {event_id}"
            # Date and time
            start_date = "Unknown"
            start_time = "Unknown"
            dates = event.find("dates")
            if dates and dates.find("date"):
                date_elem = dates.find("date")
                if date_elem.find("start_date"):
                    start_date = date_elem.find("start_date").text.strip()
                if date_elem.find("start_time"):
                    st_elem = date_elem.find("start_time")
                    start_time = st_elem.get_text().strip() if st_elem else "Unknown"
            # Organizer (handle CDATA)
            organizer = "Unknown"
            org_elem = event.find("organizer")
            if org_elem and org_elem.find("name"):
                org_name_elem = org_elem.find("name")
                organizer = org_name_elem.get_text().strip() if org_name_elem else "Unknown"
            # Place/location (handle CDATA)
            place = "Unknown"
            place_elem = event.find("place")
            if place_elem and place_elem.find("other"):
                place = place_elem.find("other").get_text().strip()
            # Event type
            event_type = "Unknown"
            types_elem = event.find("types")
            if types_elem and types_elem.find("type"):
                event_type = types_elem.find("type").text.strip()
            # Description (handle CDATA and embedded HTML)
            description: Optional[str] = None
            desc_elem = event.find("description")
            if desc_elem:
                description = desc_elem.get_text().strip()
                if description and len(description) > 500:
                    description = description[:500] + "..."
            # Enhanced title
            if start_date != "Unknown" and start_time != "Unknown":
                enhanced_title = f"{name} ({start_date} {start_time})"
            elif start_date != "Unknown":
                enhanced_title = f"{name} ({start_date})"
            else:
                enhanced_title = name
            # Event metadata
            event_metadata: Dict[str, Any] = {
                "event_id": event_id, "event_name": name,
                "start_date": start_date, "start_time": start_time,
                "organizer": organizer, "place": place,
                "event_type": event_type, "source_feed": rss_url,
            }
            # Enhanced description
            desc_parts: List[str] = []
            if description:
                desc_parts.append(f"Popis: {description}")
            if organizer != "Unknown":
                desc_parts.append(f"Organizator: {organizer}")
            if place != "Unknown":
                desc_parts.append(f"Misto: {place}")
            if event_type != "Unknown":
                desc_parts.append(f"Typ: {event_type}")
            combined_description = " | ".join(desc_parts) if desc_parts else description
            path = f"Events Feed: {rss_url} > {enhanced_title}"
            # Parse start_date as published date for timestamp comparison
            published_date: Optional[str] = None
            if start_date != "Unknown":
                try:
                    if "." in start_date:
                        day, month, year = start_date.split(".")
                        if start_time != "Unknown" and ":" in start_time:
                            published_date = f"{year}-{month.zfill(2)}-{day.zfill(2)}T{start_time}:00"
                        else:
                            published_date = f"{year}-{month.zfill(2)}-{day.zfill(2)}T00:00:00"
                    else:
                        published_date = start_date
                except Exception as e:
                    logger.warning("Could not parse event date %s: %s", start_date, str(e))
                    published_date = start_date
            extracted_urls.append({
                "url": absolute_url, "title": enhanced_title, "path": path,
                "published": published_date, "summary": combined_description,
                "description": combined_description, "event_metadata": event_metadata,
                "source_feed": rss_url,
            })
            logger.debug("Extracted from Events XML: %s - %s", absolute_url, enhanced_title)
            logger.debug("  Event ID: %s, Date: %s %s", event_id, start_date, start_time)
            logger.debug("  Organizer: %s, Place: %s, Type: %s", organizer, place, event_type)
        except Exception as e:
            logger.error("Error processing Events XML entry: %s", str(e))
            continue
    logger.info("Successfully extracted %d events from Events XML feed", len(extracted_urls))
    return extracted_urls


def parse_custom_response_feed(soup: BeautifulSoup, rss_url: str) -> List[Dict[str, Any]]:
    """Parse custom ``<response><item>`` feed format."""
    cfg = get_config()
    extracted_urls: List[Dict[str, Any]] = []
    items = soup.find_all("item")
    logger.info("Found %d items in custom response feed", len(items))
    for i, item in enumerate(items, 1):
        try:
            url: Optional[str] = None
            link_elem = item.find("link")
            # Method 1: get_text()
            if link_elem:
                url_text = link_elem.get_text(strip=True)
                if url_text and url_text.startswith("http"):
                    url = url_text
                    logger.debug("Item %d: Extracted URL via get_text(): %s", i, url)
            # Method 2: .string attribute
            if not url and link_elem:
                if link_elem.string and link_elem.string.strip().startswith("http"):
                    url = link_elem.string.strip()
                    logger.debug("Item %d: Extracted URL via .string: %s", i, url)
            # Method 3: .text property
            if not url and link_elem:
                try:
                    if link_elem.text and link_elem.text.strip().startswith("http"):
                        url = link_elem.text.strip()
                        logger.debug("Item %d: Extracted URL via .text: %s", i, url)
                except Exception:
                    pass
            # Method 4: NavigableString content directly
            if not url and link_elem:
                try:
                    for content in link_elem.contents:
                        if content and hasattr(content, "strip"):
                            content_str = str(content).strip()
                            if content_str.startswith("http"):
                                url = content_str
                                logger.debug("Item %d: Extracted URL via contents: %s", i, url)
                                break
                except Exception:
                    pass
            # Method 5: next sibling text node (html.parser treats <link> as void)
            if not url and link_elem:
                next_sibling = link_elem.next_sibling
                if next_sibling and hasattr(next_sibling, "strip"):
                    sibling_text = str(next_sibling).strip()
                    if sibling_text.startswith("http"):
                        url = sibling_text
                        logger.debug("Item %d: Extracted URL via next_sibling: %s", i, url)
            if not url:
                logger.warning(
                    "Item %d: No valid URL in <link> after all methods. Link element: %s",
                    i, link_elem,
                )
                continue
            absolute_url = strip_url_fragment(urljoin(cfg.BASE_URL, url))
            logger.debug("Item %d: Converted to absolute URL: %s", i, absolute_url)
            title_elem = item.find("title")
            title = title_elem.get_text(strip=True) if title_elem else "No title"
            # Publication date -- handles CDATA <time datetime="...">
            published_elem = item.find("published")
            published: Optional[str] = None
            if published_elem:
                published_text = published_elem.get_text(strip=True)
                time_match = re.search(r'<time\s+datetime=["\']([^"\']+)["\']', published_text)
                if time_match:
                    published = time_match.group(1)
                    logger.debug("Item %d: Extracted datetime from CDATA: %s", i, published)
                else:
                    published = published_text
                    logger.debug("Item %d: Using raw published text: %s", i, published)
            description_elem = item.find("description")
            description = description_elem.get_text(strip=True) if description_elem else None
            path = f"RSS: {rss_url} > {title}"
            extracted_urls.append({
                "url": absolute_url, "title": title, "path": path,
                "published": published, "description": description,
                "source_feed": rss_url,
            })
            logger.info("Item %d/%d: Extracted from Custom Response: %s - %s", i, len(items), absolute_url, title)
        except Exception as e:
            logger.error("Error processing custom response item %d: %s", i, str(e))
            continue
    logger.info("Successfully extracted %d URLs from custom response feed", len(extracted_urls))
    return extracted_urls


def parse_generic_feed(soup: BeautifulSoup, rss_url: str) -> List[Dict[str, Any]]:
    """Generic parser for unknown feed formats -- looks for any URLs."""
    cfg = get_config()
    extracted_urls: List[Dict[str, Any]] = []
    all_links: List[str] = []
    for elem in soup.find_all(["link", "a"]):
        href = elem.get("href")
        if href:
            all_links.append(href)
        elif elem.text and elem.text.strip().startswith("http"):
            all_links.append(elem.text.strip())
    # Also look for URLs in raw text content
    url_pattern = r"https?://[^\s<>\"']*"
    text_urls = re.findall(url_pattern, str(soup))
    all_links.extend(text_urls)
    # Filter and deduplicate
    seen_urls: set[str] = set()
    for url in all_links:
        try:
            url = url.strip()
            if not url or url in seen_urls:
                continue
            absolute_url = urljoin(cfg.BASE_URL, url)
            if (
                absolute_url.startswith(cfg.BASE_URL)
                and absolute_url not in cfg.BLACKLISTED_URLS
                and not is_url_blacklisted_by_path(absolute_url, cfg.BLACKLISTED_RELATIVE_PATHS)
                and absolute_url != rss_url
            ):
                seen_urls.add(url)
                extracted_urls.append({
                    "url": absolute_url, "title": f"Link from {rss_url}",
                    "path": f"RSS: {rss_url} > Generic Link",
                    "published": None, "source_feed": rss_url,
                })
                logger.debug("Extracted generic URL: %s", absolute_url)
        except Exception as e:
            logger.error("Error processing generic URL %s: %s", url, str(e))
            continue
    logger.info("Found %d URLs in generic feed parsing", len(extracted_urls))
    return extracted_urls


def process_rss_feeds(
    url_last_modified_map: Dict[str, Any],
    rss_last_run_timestamp: Optional[datetime],
    local_files_cache: Optional[Dict[str, Any]] = None,
    enable_resume: bool = False,
) -> List[Dict[str, Any]]:
    """Process all configured RSS feeds and extract URLs that pass filtering."""
    # Late imports to avoid circular dependencies
    from gpt_scraper_v3.url_processing import find_url_last_modified, should_process_url_with_resume
    from gpt_scraper_v3.xml_sitemap import get_last_run_timestamp

    cfg = get_config()
    logger.info("Processing %d RSS feeds", len(cfg.RSS_FEEDS))
    all_rss_urls: List[Dict[str, Any]] = []
    sitemap_last_run_timestamp = get_last_run_timestamp("sitemap")
    # F4: dedup items ACROSS feeds (spans the whole feed loop). Two feeds that
    # emit the same article (e.g. /rss and /rss/?21) collapse to one; first
    # occurrence wins.
    seen_rss_urls: Set[str] = set()
    cross_feed_dupes = 0

    # Parallelize ONLY the network fetch of each feed. ex.map preserves input
    # order, so feed_results[i] corresponds to cfg.RSS_FEEDS[i]; the per-feed
    # filtering loop below stays strictly serial in the original feed order so
    # cross-feed dedup (seen_rss_urls, first-occurrence-wins) is unchanged.
    feeds = list(cfg.RSS_FEEDS)
    with ThreadPoolExecutor(max_workers=cfg.AUX_PARALLEL_WORKERS) as ex:
        feed_results = list(ex.map(parse_rss_feed, feeds))

    for rss_url, feed_urls in zip(feeds, feed_results):
        logger.info("Processing RSS feed: %s", rss_url)
        filtered_urls: List[Dict[str, Any]] = []
        for url_info in feed_urls:
            url = url_info["url"]
            norm = normalize_url_query_params(url)
            if norm in seen_rss_urls:
                cross_feed_dupes += 1
                logger.debug("Cross-feed duplicate RSS item %s; skipping.", url)
                continue
            seen_rss_urls.add(norm)
            if url in cfg.BLACKLISTED_URLS or is_url_blacklisted_by_path(url, cfg.BLACKLISTED_RELATIVE_PATHS):
                logger.info("RSS URL %s is blacklisted. Skipping.", url)
                continue
            rss_published_date = url_info.get("published")
            xml_last_modified = find_url_last_modified(url, url_last_modified_map)
            if should_process_url_with_resume(
                url, xml_last_modified, rss_last_run_timestamp,
                local_files_cache, enable_resume,
                rss_published_date=rss_published_date,
                sitemap_last_run_timestamp=sitemap_last_run_timestamp,
            ):
                filtered_urls.append(url_info)
            else:
                logger.info("Skipping RSS URL %s (timestamp check or already processed locally).", url)
        all_rss_urls.extend(filtered_urls)
        logger.info("Extracted %d URLs from RSS feed: %s", len(filtered_urls), rss_url)
    logger.info("Total RSS URLs to process: %d", len(all_rss_urls))
    if cross_feed_dupes:
        logger.info(
            "Collapsed %d cross-feed duplicate RSS items (kept first occurrence)",
            cross_feed_dupes)
    return all_rss_urls
