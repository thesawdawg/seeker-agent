"""
Primo Library Catalog Client
-----------------------------
Calls the MCPO bridge (which wraps the Primo MCP server) to search an
Ex Libris Primo library catalog for records and availability of sources.

If the MCPO bridge is unavailable, falls back to calling the Primo REST API
directly (requires PRIMO_API_KEY and PRIMO_VID in the environment).

Usage:
    from core.primo import search_primo, get_primo_record, is_primo_configured

    if is_primo_configured():
        results = search_primo("cognitive dissonance")
        for r in results:
            print(r["title"], r["availability"], r["record_url"])
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from core import keys

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

REGION_HOSTS = {
    "na": "https://api-na.hosted.exlibrisgroup.com",
    "eu": "https://api-eu.hosted.exlibrisgroup.com",
    "ap": "https://api-ap.hosted.exlibrisgroup.com",
    "ca": "https://api-ca.hosted.exlibrisgroup.com",
    "cn": "https://api-cn.hosted.exlibrisgroup.com.cn",
}

HTTP_TIMEOUT = 30.0


def _mcpo_url() -> str:
    """MCPO bridge URL (e.g. http://mcpo:8000). Empty if not configured."""
    return os.environ.get("MCPO_URL", "").strip().rstrip("/")


def _mcpo_api_key() -> str:
    return os.environ.get("MCPO_API_KEY", "seeker-mcpo").strip()


def _primo_api_key() -> str:
    """Get Primo API key — checks user-stored credentials first, then env."""
    return keys.get("PRIMO_API_KEY", required=False, source_id="primo",
                    source_name="Primo (Ex Libris library catalog)")


def _primo_vid() -> str:
    return os.environ.get("PRIMO_VID", "").strip()


def _primo_tab() -> str:
    return os.environ.get("PRIMO_TAB", "").strip()


def _primo_scope() -> str:
    return os.environ.get("PRIMO_SCOPE", "").strip()


def _primo_inst() -> str:
    return os.environ.get("PRIMO_INST", "").strip()


def _primo_region() -> str:
    return os.environ.get("PRIMO_REGION", "na").strip().lower()


def _primo_base_url() -> str:
    custom = os.environ.get("PRIMO_BASE_URL", "").strip().rstrip("/")
    if custom:
        return custom
    return REGION_HOSTS.get(_primo_region(), REGION_HOSTS["na"])


def is_primo_configured() -> bool:
    """Check if Primo is configured with enough credentials to search."""
    if _mcpo_url():
        return True
    return bool(_primo_api_key() and _primo_vid())


# ── MCPO bridge client ────────────────────────────────────────────────────────

def _mcpo_search(query: str, *, field: str = "any", max_results: int = 10,
                 resource_type: str | None = None) -> dict | None:
    """Search via the MCPO bridge (REST endpoint wrapping the MCP tool)."""
    base = _mcpo_url()
    if not base:
        return None

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            # MCPO exposes MCP tools as POST endpoints with JSON body
            resp = client.post(
                f"{base}/search_catalog",
                headers={"Authorization": f"Bearer {_mcpo_api_key()}",
                         "Content-Type": "application/json"},
                json={
                    "query": query,
                    "field": field,
                    "max_results": max_results,
                    **({"resource_type": resource_type} if resource_type else {}),
                },
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"[Primo] MCPO search returned {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        logger.warning(f"[Primo] MCPO bridge unavailable: {e}")
        return None


def _mcpo_get_record(record_id: str, context: str = "L") -> dict | None:
    """Get a record via the MCPO bridge."""
    base = _mcpo_url()
    if not base:
        return None

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.post(
                f"{base}/get_record",
                headers={"Authorization": f"Bearer {_mcpo_api_key()}",
                         "Content-Type": "application/json"},
                json={"record_id": record_id, "context": context},
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"[Primo] MCPO get_record returned {resp.status_code}")
            return None
    except Exception as e:
        logger.warning(f"[Primo] MCPO bridge unavailable for get_record: {e}")
        return None


# ── Direct Primo API client (fallback) ────────────────────────────────────────

def _direct_search(query: str, *, field: str = "any", max_results: int = 10,
                   resource_type: str | None = None) -> dict | None:
    """Search the Primo REST API directly (fallback when MCPO is down)."""
    api_key = _primo_api_key()
    vid = _primo_vid()
    if not api_key or not vid:
        return None

    scope = _primo_scope()
    if not scope:
        logger.warning("[Primo] PRIMO_SCOPE not set — cannot search directly")
        return None

    base = _primo_base_url()
    params: dict[str, Any] = {
        "vid": vid,
        "scope": scope,
        "q": f"{field},contains,{query}",
        "lang": "en",
        "offset": 0,
        "limit": max(1, min(max_results, 50)),
        "sort": "rank",
        "pcAvailability": "true",
        "apikey": api_key,
    }
    tab = _primo_tab()
    if tab:
        params["tab"] = tab
    inst = _primo_inst()
    if inst:
        params["inst"] = inst
    if resource_type:
        params["qInclude"] = f"facet_rtype:{resource_type}"

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.get(f"{base}/primo/v1/search", params=params,
                              headers={"Accept": "application/json"})
            if resp.status_code == 200:
                data = resp.json()
                docs = data.get("docs", []) or []
                info = data.get("info", {}) or {}
                return {
                    "total_found": info.get("total", len(docs)),
                    "returned": len(docs),
                    "results": [_format_primo_doc(d) for d in docs],
                }
            logger.warning(f"[Primo] Direct API returned {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        logger.warning(f"[Primo] Direct API search failed: {e}")
        return None


def _direct_get_record(record_id: str, context: str = "L") -> dict | None:
    """Get a record directly from the Primo API."""
    api_key = _primo_api_key()
    vid = _primo_vid()
    scope = _primo_scope()
    if not api_key or not vid or not scope:
        return None

    base = _primo_base_url()
    params: dict[str, Any] = {
        "vid": vid, "scope": scope, "lang": "en", "apikey": api_key,
    }
    inst = _primo_inst()
    if inst:
        params["inst"] = inst

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.get(f"{base}/primo/v1/pnxs/{context}/{record_id}",
                              params=params, headers={"Accept": "application/json"})
            if resp.status_code == 200:
                data = resp.json()
                doc = data
                if "docs" in data and isinstance(data["docs"], list):
                    doc = data["docs"][0] if data["docs"] else None
                if doc and "pnx" in doc:
                    return {"total_found": 1, "returned": 1,
                            "results": [_format_primo_doc(doc)]}
            return None
    except Exception as e:
        logger.warning(f"[Primo] Direct API get_record failed: {e}")
        return None


# ── PNX document formatting ──────────────────────────────────────────────────

def _first(d: dict, k: str) -> str | None:
    vals = d.get(k)
    if isinstance(vals, str):
        return vals
    if isinstance(vals, list) and vals:
        return str(vals[0])
    return None


def _all(d: dict, k: str) -> list[str]:
    vals = d.get(k)
    if isinstance(vals, str):
        return [vals]
    if isinstance(vals, list):
        return [str(v) for v in vals if v]
    return []


def _format_primo_doc(doc: dict) -> dict:
    """Extract a clean record from a Primo PNX doc."""
    pnx = doc.get("pnx", {}) or {}
    display = pnx.get("display", {}) or {}
    addata = pnx.get("addata", {}) or {}
    control = pnx.get("control", {}) or {}
    links = pnx.get("links", {}) or {}
    delivery = doc.get("delivery", {}) or {}

    return {
        "record_id": _first(control, "recordid"),
        "title": _first(display, "title"),
        "authors": _all(addata, "au") or _all(display, "creator"),
        "year": _first(addata, "date") or _first(display, "creationdate"),
        "publisher": _first(addata, "pub") or _first(display, "publisher"),
        "doc_type": _first(display, "type"),
        "format": _first(display, "format"),
        "isbn": _first(addata, "isbn"),
        "issn": _first(addata, "issn") or _first(addata, "eissn"),
        "doi": _first(addata, "doi"),
        "journal": _first(addata, "jtitle"),
        "subjects": _all(display, "subject") or _all(addata, "subject"),
        "abstract": _first(addata, "abstract") or _first(display, "description"),
        "link_to_resource": _first(links, "linktorsrc"),
        "openurl": _first(links, "openurl"),
        "availability": _all(delivery, "deliveryCategory") or _all(delivery, "availability"),
        "context": doc.get("context"),
        "record_url": doc.get("@id"),
    }


# ── Public API ────────────────────────────────────────────────────────────────

def search_primo(query: str, *, field: str = "any", max_results: int = 10,
                 resource_type: str | None = None) -> list[dict]:
    """
    Search the Primo library catalog.

    Tries the MCPO bridge first, falls back to direct Primo API calls.
    Returns a list of formatted result dicts, or an empty list on failure.
    """
    # Try MCPO bridge first
    data = _mcpo_search(query, field=field, max_results=max_results,
                        resource_type=resource_type)
    if data is None:
        # Fallback to direct API
        data = _direct_search(query, field=field, max_results=max_results,
                              resource_type=resource_type)
    if data is None:
        return []
    return data.get("results", []) or []


def get_primo_record(record_id: str, context: str = "L") -> dict | None:
    """
    Get a single Primo record by ID.

    Returns the formatted record dict, or None if not found.
    """
    data = _mcpo_get_record(record_id, context)
    if data is None:
        data = _direct_get_record(record_id, context)
    if data is None:
        return None
    results = data.get("results", []) or []
    return results[0] if results else None


def find_source_in_primo(title: str, doi: str = "", isbn: str = "",
                         max_results: int = 5) -> dict | None:
    """
    Search Primo for a specific source by title, DOI, or ISBN.

    Returns the best-matching Primo record, or None if no match found.
    The match is determined by title similarity.
    """
    # Try DOI search first (most precise)
    if doi:
        results = search_primo(doi, field="any", max_results=3)
        if results:
            return results[0]

    # Try ISBN
    if isbn:
        results = search_primo(isbn, field="any", max_results=3)
        if results:
            return results[0]

    # Fall back to title search
    if title:
        # Use a cleaned version of the title for better matching
        clean_title = title.strip()
        if len(clean_title) > 200:
            clean_title = clean_title[:200]
        results = search_primo(clean_title, field="title", max_results=max_results)
        if results:
            # Return the first result — Primo ranks by relevance
            return results[0]

    return None
