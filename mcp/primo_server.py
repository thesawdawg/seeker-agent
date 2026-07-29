#!/usr/bin/env python3
"""
Primo MCP Server
----------------
Exposes an Ex Libris / Clarivate Primo (or Primo VE) discovery layer to AI
agents through the public primoSearch REST API
(GET /primo/v1/search and /primo/v1/pnxs).

Documentation: https://developers.exlibrisgroup.com/primo/apis/

Primo APIs are institution-scoped: one API key is bound to a single
institution + environment, and every search must name the view (vid), tab
and scope configured in that institution's Primo Back Office.

Three ways to run:

  # 1. stdio — client launches the process (for mcpo bridge)
  python3 mcp/primo_server.py --api-key YOUR_KEY --vid INST:VIEW \
      --tab TAB --scope SCOPE --transport stdio

  # 2. Local HTTP — run once, connect multiple clients by URL
  python3 mcp/primo_server.py --api-key YOUR_KEY --vid INST:VIEW \
      --tab TAB --scope SCOPE --transport streamable-http --port 8013

  # 3. Via mcpo bridge (docker-compose)
  mcpo --port 8000 -- python3 /mcp/primo_server.py --transport stdio \
      --api-key YOUR_KEY --vid INST:VIEW --tab TAB --scope SCOPE

Environment variables (all optional, overridden by CLI flags):
    PRIMO_API_KEY, PRIMO_VID, PRIMO_TAB, PRIMO_SCOPE, PRIMO_INST,
    PRIMO_REGION, PRIMO_LANG, PRIMO_BASE_URL
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import re
import time
from typing import Any, Optional

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# ── CLI args ──────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Primo MCP server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--api-key", default=os.environ.get("PRIMO_API_KEY"),
                   help="Primo API key (or set PRIMO_API_KEY env var)")
    p.add_argument("--region", default=os.environ.get("PRIMO_REGION", "na"),
                   help="Gateway region: na eu ap ca cn")
    p.add_argument("--base-url", default=os.environ.get("PRIMO_BASE_URL"),
                   help="Full API gateway base URL (overrides --region)")
    p.add_argument("--vid", default=os.environ.get("PRIMO_VID"),
                   help="Default view id, e.g. INST:VIEW")
    p.add_argument("--tab", default=os.environ.get("PRIMO_TAB"),
                   help="Default tab name")
    p.add_argument("--scope", default=os.environ.get("PRIMO_SCOPE"),
                   help="Default scope name")
    p.add_argument("--inst", default=os.environ.get("PRIMO_INST"),
                   help="Institution code (on-premise Primo only)")
    p.add_argument("--lang", default=os.environ.get("PRIMO_LANG", "en"),
                   help="Default UI language")
    p.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8013")))
    p.add_argument("--transport", default=os.environ.get("MCP_TRANSPORT", "stdio"),
                   choices=["stdio", "sse", "streamable-http"])
    p.add_argument("--http-timeout", type=float, default=float(os.environ.get("HTTP_TIMEOUT", "30.0")))
    p.add_argument("--max-retries", type=int, default=int(os.environ.get("MAX_RETRIES", "3")))
    p.add_argument("--backoff-base", type=float, default=float(os.environ.get("BACKOFF_BASE", "1.0")))
    p.add_argument("--backoff-factor", type=float, default=float(os.environ.get("BACKOFF_FACTOR", "2.0")))
    p.add_argument("--jitter-max", type=float, default=float(os.environ.get("JITTER_MAX", "0.25")))
    return p.parse_args()


args = _parse_args()

# ── Config ────────────────────────────────────────────────────────────────────

REGION_HOSTS = {
    "na": "https://api-na.hosted.exlibrisgroup.com",
    "eu": "https://api-eu.hosted.exlibrisgroup.com",
    "ap": "https://api-ap.hosted.exlibrisgroup.com",
    "ca": "https://api-ca.hosted.exlibrisgroup.com",
    "cn": "https://api-cn.hosted.exlibrisgroup.com.cn",
}

API_KEY = args.api_key
DEFAULT_VID = args.vid
DEFAULT_TAB = args.tab
DEFAULT_SCOPE = args.scope
DEFAULT_INST = args.inst
DEFAULT_LANG = args.lang
HTTP_TIMEOUT = args.http_timeout
MAX_RETRIES = max(1, args.max_retries)
BACKOFF_BASE = max(0.0, args.backoff_base)
BACKOFF_FACTOR = max(1.0, args.backoff_factor)
JITTER_MAX = max(0.0, args.jitter_max)

BASE_URL = (args.base_url.rstrip("/") if args.base_url
            else REGION_HOSTS.get(args.region.lower(), REGION_HOSTS["na"]))

Q_FIELDS = ("any", "title", "creator", "sub", "usertag")
Q_PRECISIONS = ("contains", "exact", "begins_with")
SORT_OPTIONS = ("rank", "title", "author", "date", "date_d", "date_a")


# ── HTTP client with retry / backoff ──────────────────────────────────────────

def _should_retry(status_code: int) -> bool:
    return status_code in (429, 500, 502, 503, 504)


def _backoff_sleep_seconds(attempt: int) -> float:
    base = BACKOFF_BASE * (BACKOFF_FACTOR ** attempt)
    jitter = random.uniform(0.0, JITTER_MAX) if JITTER_MAX > 0 else 0.0
    return base + jitter


async def _get(url: str, params: dict) -> dict:
    """GET with exponential backoff. Returns response_json."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.get(url, params=params, headers={"Accept": "application/json"})
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (401, 403):
                    raise RuntimeError(
                        f"Primo API returned {resp.status_code} (unauthorized). "
                        f"Check the API key and that vid/scope/tab belong to its institution."
                    )
                if _should_retry(resp.status_code) and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(_backoff_sleep_seconds(attempt))
                    continue
                resp.raise_for_status()
            except httpx.TimeoutException:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(_backoff_sleep_seconds(attempt))
                    continue
                raise
            except httpx.ConnectError:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(_backoff_sleep_seconds(attempt))
                    continue
                raise
    raise RuntimeError(f"Primo API request failed after {MAX_RETRIES} retries")


# ── Query building ────────────────────────────────────────────────────────────

def _build_q(query: str, field: str, precision: str) -> str:
    field = field if field in Q_FIELDS else "any"
    precision = precision if precision in Q_PRECISIONS else "contains"
    return f"{field},{precision},{query}"


def _build_qinclude(facets: list[tuple[str, str]]) -> str:
    if not facets:
        return ""
    return ",".join(f"{k}:{v}" for k, v in facets)


# ── Response formatting ───────────────────────────────────────────────────────

def _first(d: dict, k: str) -> Optional[str]:
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


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _extract_year(*candidates: str | None) -> int | None:
    for c in candidates:
        if not c:
            continue
        m = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", c)
        if m:
            return int(m.group(1))
    return None


def _format_doc(doc: dict) -> dict:
    """Extract a clean record from a Primo PNX doc."""
    pnx = doc.get("pnx", {}) or {}
    display = pnx.get("display", {}) or {}
    addata = pnx.get("addata", {}) or {}
    control = pnx.get("control", {}) or {}
    links = pnx.get("links", {}) or {}
    delivery = doc.get("delivery", {}) or {}

    return {
        "source": "primo",
        "record_id": _first(control, "recordid"),
        "title": _first(display, "title"),
        "authors": _all(addata, "au") or _all(display, "creator"),
        "contributors": _all(display, "contributor"),
        "year": _extract_year(_first(addata, "date"), _first(display, "creationdate")),
        "date": _first(addata, "date") or _first(display, "creationdate"),
        "publisher": _first(addata, "pub") or _first(display, "publisher"),
        "pub_place": _first(addata, "cop"),
        "doc_type": _first(display, "type"),
        "format": _first(display, "format"),
        "language": _first(display, "language") or _first(addata, "lang"),
        "isbn": _first(addata, "isbn"),
        "issn": _first(addata, "issn") or _first(addata, "eissn"),
        "doi": _first(addata, "doi"),
        "journal": _first(addata, "jtitle"),
        "is_part_of": _first(display, "ispartof"),
        "subjects": _all(display, "subject") or _all(addata, "subject"),
        "abstract": _first(addata, "abstract") or _first(display, "description"),
        "source_system": _first(control, "sourceid"),
        "source_record_id": _first(control, "sourcerecordid"),
        "link_to_resource": _first(links, "linktorsrc"),
        "openurl": _first(links, "openurl"),
        "thumbnail": _first(links, "thumbnail"),
        "availability": _all(delivery, "deliveryCategory") or _all(delivery, "availability"),
        "context": doc.get("context"),
        "record_url": doc.get("@id"),
    }


def _resolve_target(vid: str | None, scope: str | None, tab: str | None) -> tuple[str, str, str | None]:
    """Resolve view/scope/tab from per-call args falling back to server defaults."""
    v = vid or DEFAULT_VID
    s = scope or DEFAULT_SCOPE
    t = tab or DEFAULT_TAB
    missing = [n for n, val in (("vid", v), ("scope", s)) if not val]
    if missing:
        raise RuntimeError(
            f"Missing {', '.join(missing)}. Set them as server defaults "
            f"(--vid/--scope/--tab) or pass them to the tool."
        )
    return v, s, t


# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="primo",
    instructions=(
        "Ex Libris / Clarivate Primo discovery connector — search an institution's "
        "library catalog and discovery index, and fetch full PNX records. "
        "All queries run against one institution's configured view (vid/scope/tab)."
    ),
)


@mcp.tool()
async def search_catalog(
    query: str,
    field: str = "any",
    precision: str = "contains",
    max_results: int = 15,
    offset: int = 0,
    sort: str = "rank",
    resource_type: str | None = None,
    language: str | None = None,
    library: str | None = None,
    collection: str | None = None,
    availability: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    full_text_only: bool = False,
    return_facets: bool = False,
    vid: str | None = None,
    tab: str | None = None,
    scope: str | None = None,
) -> dict:
    """
    Search an Ex Libris Primo discovery layer (library catalog + discovery index).

    Args:
        query: Free-text search term(s).
        field: Search field — any, title, creator, sub (subject), usertag.
        precision: Match precision — contains, exact, begins_with.
        max_results: Records to return (1-50; the API caps a single page at 50).
        offset: Result offset for paging (max ~2000 recommended).
        sort: rank, title, author, date, date_d (newest), date_a (oldest).
        resource_type: Filter by resource type facet (e.g. books, articles, journals).
        language: Filter by language facet (e.g. eng, fre).
        library: Filter by holding library facet.
        collection: Filter by collection/domain facet.
        availability: Filter by availability facet (available, online_resources, physical_item).
        year_from: Lower bound creation year (inclusive).
        year_to: Upper bound creation year (inclusive).
        full_text_only: If true, only records with full text/availability.
        return_facets: If true, include facet buckets in the response.
        vid: Override the server's default view id (INST:VIEW).
        tab: Override the server's default tab.
        scope: Override the server's default scope.
    """
    v, s, t = _resolve_target(vid, scope, tab)

    inc: list[tuple[str, str]] = []
    if resource_type:
        inc.append(("facet_rtype", resource_type))
    if language:
        inc.append(("facet_lang", language))
    if library:
        inc.append(("facet_library", library))
    if collection:
        inc.append(("facet_domain", collection))
    if availability:
        inc.append(("facet_tlevel", availability))
    if year_from is not None or year_to is not None:
        start = str(year_from) if year_from is not None else "*"
        end = str(year_to) if year_to is not None else "*"
        inc.append(("facet_searchcreationdate", f"[{start} TO {end}]"))

    params: dict[str, Any] = {
        "vid": v,
        "scope": s,
        "q": _build_q(query, field, precision),
        "lang": DEFAULT_LANG,
        "offset": max(0, offset),
        "limit": max(1, min(max_results, 50)),
        "sort": sort if sort in SORT_OPTIONS else "rank",
        "pcAvailability": "false" if full_text_only else "true",
        "apikey": API_KEY,
    }
    if t:
        params["tab"] = t
    if DEFAULT_INST:
        params["inst"] = DEFAULT_INST
    qinc = _build_qinclude(inc)
    if qinc:
        params["qInclude"] = qinc

    data = await _get(f"{BASE_URL}/primo/v1/search", params)
    docs = data.get("docs", []) or []
    info = data.get("info", {}) or {}

    out: dict = {
        "total_found": info.get("total", info.get("totalResultsLocal", len(docs))),
        "returned": len(docs),
        "offset": params["offset"],
        "query_used": params["q"],
        "vid": v,
        "results": [_format_doc(d) for d in docs],
    }
    if return_facets:
        facets_raw = data.get("facets")
        if isinstance(facets_raw, list):
            out["facets"] = [
                {"name": f.get("name"),
                 "values": [{"value": v.get("value"), "count": v.get("count")}
                           for v in (f.get("values") or []) if isinstance(v, dict)]}
                for f in facets_raw if isinstance(f, dict)
            ]
    return out


@mcp.tool()
async def get_record(
    record_id: str,
    context: str = "L",
    vid: str | None = None,
    scope: str | None = None,
) -> dict:
    """
    Fetch a single Primo PNX record by its recordid.

    Args:
        record_id: Primo recordid (the control.recordid value, e.g. "alma990001234").
        context: "L" for a local institution record, "PC" for a Central Discovery
                 Index (CDI) record.
        vid: Override the server's default view id (INST:VIEW).
        scope: Override the server's default scope.
    """
    v, s, _ = _resolve_target(vid, scope, None)
    context = (context or "L").upper()

    params: dict[str, Any] = {"vid": v, "scope": s, "lang": DEFAULT_LANG, "apikey": API_KEY}
    if DEFAULT_INST:
        params["inst"] = DEFAULT_INST

    url = f"{BASE_URL}/primo/v1/pnxs/{context}/{record_id}"
    try:
        data = await _get(url, params)
    except RuntimeError as e:
        return {"total_found": 0, "returned": 0, "results": [],
                "error": f"Record not found in Primo: '{record_id}' ({e})"}

    doc = data
    if "docs" in data and isinstance(data["docs"], list):
        doc = data["docs"][0] if data["docs"] else None
    if not doc or "pnx" not in doc:
        return {"total_found": 0, "returned": 0, "results": [],
                "error": f"Record not found in Primo: '{record_id}'"}

    return {"total_found": 1, "returned": 1, "results": [_format_doc(doc)]}


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not API_KEY:
        logger.warning(
            "PRIMO_API_KEY not set — server will start but search/get_record "
            "calls will return errors. Set PRIMO_API_KEY to enable Primo searches."
        )
    if not DEFAULT_VID or not DEFAULT_SCOPE:
        logger.warning(
            "PRIMO_VID or PRIMO_SCOPE not set — search calls will fail until "
            "vid/scope are passed per-call or set as server defaults."
        )
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)
