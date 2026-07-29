"""
Librarian Agent — Library Catalog Enrichment
---------------------------------------------
Searches a library catalog (Ex Libris Primo via MCPO bridge) for each
source gathered by Grounder, Social, and Historian. Enriches sources
with:

  - Catalog record URL (direct link to the institution's catalog)
  - Availability status (online, physical, full text, etc.)
  - ISBN/ISSN from the catalog record
  - Primo record ID (for future catalog lookups)

This is a deterministic step — no LLM calls needed. It reads all sources
from the database, searches Primo for each, and updates the source rows
with catalog metadata.

If Primo is not configured (no API key or MCPO bridge), the step completes
gracefully with a warning — the pipeline continues without catalog data.

Placement: after Gaper, before Break 1 — so the human reviewer sees
catalog availability when validating ground truth.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from core import database as db
from core import primo
from core import users

logger = logging.getLogger(__name__)

# Rate limiting — be polite to the Primo API / MCPO bridge
_DELAY_SECONDS = 0.5
_MAX_SOURCES = 100  # Cap to avoid hammering the catalog


def run(context: str, run_id: str, **kwargs) -> None:
    """
    Enrich sources with library catalog data from Primo.

    Args:
        context: Pipeline context (problem statement + prior outputs).
        run_id:  The current run ID.
    """
    logger.info(f"[Librarian] Starting for run {run_id}")

    # Check if the user has disabled the Primo MCP connection
    owner = users.run_owner(run_id)
    user_id = owner.get("user_id") if owner else None
    if user_id and not users.mcp_connection_enabled(user_id, "primo"):
        logger.info("[Librarian] Primo MCP connection disabled by user — skipping catalog enrichment.")
        return

    # Check if Primo is configured
    if not primo.is_primo_configured():
        logger.info("[Librarian] Primo not configured — skipping catalog enrichment. "
                    "Set PRIMO_API_KEY + PRIMO_VID + PRIMO_SCOPE in .env or user Settings to enable.")
        return

    # Gather all sources from the run
    all_sources: list[dict] = []
    for source_type in ("seminal", "current", "historical"):
        sources = db.get_sources_by_type(source_type, run_id, ranked=True)
        all_sources.extend(sources)

    if not all_sources:
        logger.info("[Librarian] No sources found for this run — nothing to look up.")
        return

    # Cap the number of sources to search
    sources_to_search = all_sources[:_MAX_SOURCES]
    logger.info(f"[Librarian] Searching Primo for {len(sources_to_search)} of "
                f"{len(all_sources)} sources")

    enriched = 0
    not_found = 0
    errors = 0

    for source in sources_to_search:
        source_id = source.get("source_id", "")
        title = source.get("title", "")
        doi = source.get("doi", "") or ""
        isbn = source.get("isbn", "") or ""

        if not title and not doi and not isbn:
            logger.debug(f"[Librarian] Skipping {source_id} — no title, DOI, or ISBN")
            continue

        # Skip if already checked recently
        existing_catalog_checked = source.get("catalog_checked")
        if existing_catalog_checked:
            logger.debug(f"[Librarian] {source_id} already checked at {existing_catalog_checked}")
            continue

        try:
            # Search Primo for this source
            record = primo.find_source_in_primo(
                title=title,
                doi=doi,
                isbn=isbn,
                max_results=3,
            )

            if record:
                # Enrich the source with catalog data
                update_data: dict = {
                    "source_id": source_id,
                    "title": title,  # Required for upsert
                    "type": source.get("type", "current"),
                    "run_id": run_id,
                    "primo_record_id": record.get("record_id", "") or "",
                    "catalog_url": record.get("record_url", "") or "",
                    "availability": json.dumps(record.get("availability", []) or []),
                    "catalog_checked": datetime.now(timezone.utc).isoformat(),
                }

                # Fill in ISBN/ISSN if the source doesn't have them
                if not source.get("isbn") and record.get("isbn"):
                    update_data["isbn"] = record["isbn"]
                if not source.get("issn") and record.get("issn"):
                    update_data["issn"] = record["issn"]

                # Preserve existing fields that upsert needs
                for field in ("authors", "year", "source_name", "doi", "abstract",
                              "active_link", "theme_tags", "intersection_tags",
                              "added_by", "date_collected", "last_checked",
                              "link_status", "seminal_reason", "historical_reason",
                              "phase_tag", "relevance_rating", "relevance_reason"):
                    if source.get(field) is not None:
                        update_data[field] = source[field]

                ok = db.upsert_source(update_data)
                if ok:
                    enriched += 1
                    avail = record.get("availability", [])
                    avail_str = ", ".join(avail) if avail else "unknown"
                    logger.info(f"[Librarian] Found in catalog: '{title[:60]}' "
                                f"— availability: {avail_str}")
                else:
                    errors += 1
            else:
                not_found += 1
                # Mark as checked even if not found, so we don't retry
                db.upsert_source({
                    "source_id": source_id,
                    "title": title,
                    "type": source.get("type", "current"),
                    "run_id": run_id,
                    "catalog_checked": datetime.now(timezone.utc).isoformat(),
                    # Preserve required fields
                    "authors": source.get("authors", ""),
                    "year": source.get("year"),
                    "source_name": source.get("source_name", ""),
                    "doi": doi,
                    "abstract": source.get("abstract", ""),
                    "active_link": source.get("active_link", ""),
                    "theme_tags": source.get("theme_tags", "[]"),
                    "added_by": source.get("added_by", ""),
                    "date_collected": source.get("date_collected", ""),
                    "link_status": source.get("link_status", "active"),
                })
                logger.debug(f"[Librarian] Not in catalog: '{title[:60]}'")

        except Exception as e:
            errors += 1
            logger.warning(f"[Librarian] Error looking up '{title[:60]}': {e}")
            continue

    logger.info(
        f"[Librarian] Complete for run {run_id}: "
        f"{enriched} found in catalog, {not_found} not found, {errors} errors"
    )
