"""
References module
-----------------
Generates APA-formatted reference machinery for Scribe outputs.

Four responsibilities:
  1. Pull all sources for a run from the sources table → citable manifest.
  2. Assign stable short citation keys (AuthorYear, AuthorYearb on collision).
  3. Format each source as APA 7th edition.
  4. Verify that cited sources actually exist (Crossref for DOIs, OpenAlex
     as fallback, URL HEAD for the rest) and that the in-text citation
     semantically supports the claim it's attached to.

Used by Scribe's understanding_map path. Produces:
  - a manifest dict passed into the LLM prompt
  - post-generation validation of [CiteKey] markers against the manifest
  - a References section in APA, with dead-link flags
  - a companion .tex file with \\bibitem entries, ready to \\input elsewhere

No network calls happen at import time. All verification is opt-in via
verify_references(..., online=True).
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import httpx

from core import database as db
from core import llm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CitableSource:
    """A source from the run, prepared for citation."""
    source_id:    str
    cite_key:     str
    title:        str
    authors:      list[str]
    year:         Optional[int]
    doi:          Optional[str]      # raw DOI or URL, may be empty
    url:          Optional[str]
    abstract:     Optional[str]
    source_name:  str                # e.g. 'openalex', 'scopus'
    apa:          str = ""           # formatted APA string, computed
    theme_tags:   list[str] = field(default_factory=list)  # JSON array from sources table
    source_type:  str = ""           # 'current' | 'seminal' | 'historical'
    # verification outputs
    exists_online:   Optional[bool] = None
    verified_via:    Optional[str]  = None   # 'crossref' | 'openalex' | 'url_head' | None
    verification_note: str = ""


# ---------------------------------------------------------------------------
# APA formatting
# ---------------------------------------------------------------------------

_AUTHOR_SPLIT_RE = re.compile(r"\s*[;|]\s*")


def _parse_authors(raw) -> list[str]:
    """authors column may be JSON array, semicolon list, or plain string."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [a.strip() for a in raw if a and isinstance(a, str)]
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith("["):
            try:
                lst = json.loads(raw)
                return [a.strip() for a in lst if a and isinstance(a, str)]
            except json.JSONDecodeError:
                pass
        return [a.strip() for a in _AUTHOR_SPLIT_RE.split(raw) if a.strip()]
    return []


def _surname(author: str) -> str:
    """Best-effort surname extraction. Handles 'Last, First' and 'First Last'."""
    author = author.strip()
    if "," in author:
        return author.split(",", 1)[0].strip()
    parts = author.split()
    return parts[-1] if parts else author


def _first_initial(author: str) -> str:
    """First initial from 'Last, First' or 'First Last'."""
    author = author.strip()
    if "," in author:
        rest = author.split(",", 1)[1].strip()
        return rest[0].upper() if rest else ""
    parts = author.split()
    if len(parts) >= 2:
        return parts[0][0].upper()
    return ""


def _format_apa_authors(authors: list[str]) -> str:
    """
    APA 7th author list:
      1 author:  Freire, P.
      2 authors: Freire, P., & hooks, b.
      3-20:      Smith, A., Jones, B., & Lee, C.
      21+:       Smith, A., Jones, B., ... Lee, Z.
    """
    if not authors:
        return ""
    formatted = []
    for a in authors[:20]:
        surname = _surname(a)
        initial = _first_initial(a)
        if initial:
            formatted.append(f"{surname}, {initial}.")
        else:
            formatted.append(surname)
    if len(authors) == 1:
        return formatted[0]
    if len(authors) == 2:
        return f"{formatted[0]}, & {formatted[1]}"
    if len(authors) <= 20:
        return ", ".join(formatted[:-1]) + f", & {formatted[-1]}"
    # 21+: first 19, ellipsis, last
    head = ", ".join(formatted[:19])
    last = _format_apa_authors([authors[-1]])
    return f"{head}, ... {last}"


def _clean_doi(doi_raw: Optional[str]) -> Optional[str]:
    """Normalize DOI — accept raw DOI or URL form. Returns bare DOI."""
    if not doi_raw:
        return None
    d = doi_raw.strip()
    # strip URL prefixes
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.I)
    if d.startswith("10."):
        return d
    return None


def _doi_url(doi: Optional[str]) -> Optional[str]:
    d = _clean_doi(doi)
    return f"https://doi.org/{d}" if d else None


def format_apa(source: CitableSource) -> str:
    """Format a CitableSource as an APA 7th entry (text string, no URL)."""
    authors_str = _format_apa_authors(source.authors)
    year = f"({source.year})" if source.year else "(n.d.)"
    title = (source.title or "").rstrip(".")
    # We don't know venue reliably from the sources table; we omit it rather
    # than invent one. If you later add a 'venue' column, plug it in here.
    parts = []
    if authors_str:
        parts.append(authors_str)
    parts.append(year + ".")
    if title:
        parts.append(f"{title}.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Citation keys
# ---------------------------------------------------------------------------

def _base_cite_key(authors: list[str], year: Optional[int]) -> str:
    if authors:
        surname = _surname(authors[0])
        surname = re.sub(r"[^A-Za-z]", "", surname) or "Anon"
    else:
        surname = "Anon"
    y = str(year) if year else "nd"
    return f"{surname}{y}"


def _assign_cite_keys(sources: list[CitableSource]) -> None:
    """Mutate sources in place, giving each a unique cite_key."""
    counts: dict[str, int] = {}
    # Sort for deterministic ordering
    sources.sort(key=lambda s: (s.authors[0] if s.authors else "", s.year or 0, s.title))
    for s in sources:
        base = _base_cite_key(s.authors, s.year)
        n = counts.get(base, 0)
        if n == 0:
            s.cite_key = base
        else:
            # 'Smith2020', 'Smith2020b', 'Smith2020c' ...
            s.cite_key = f"{base}{chr(ord('b') + n - 1)}"
        counts[base] = n + 1


# ---------------------------------------------------------------------------
# Manifest construction — pull all sources for a run
# ---------------------------------------------------------------------------

def build_manifest(run_id: str) -> list[CitableSource]:
    """Pull every source for a run and convert to CitableSource objects."""
    rows = db.get_sources_for_run(run_id) if hasattr(db, "get_sources_for_run") \
           else _fallback_get_sources(run_id)

    citables: list[CitableSource] = []
    for r in rows:
        # rows may be sqlite3.Row, DictCursor dict, or plain dict
        get = r.get if isinstance(r, dict) else (lambda k, _r=r: _r[k] if k in _r.keys() else None)
        title   = (get("title") or "").strip()
        if not title:
            continue
        authors = _parse_authors(get("authors"))
        year    = get("year")
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        doi_raw = get("doi") or ""
        url     = get("active_link") or ""
        theme_raw = get("theme_tags") or ""
        if isinstance(theme_raw, list):
            theme_tags = [str(t) for t in theme_raw if t]
        elif isinstance(theme_raw, str) and theme_raw.strip().startswith("["):
            try:
                theme_tags = [str(t) for t in json.loads(theme_raw) if t]
            except json.JSONDecodeError:
                theme_tags = []
        else:
            theme_tags = []
        citables.append(CitableSource(
            source_id   = get("source_id") or "",
            cite_key    = "",  # assigned below
            title       = title,
            authors     = authors,
            year        = year,
            doi         = doi_raw or None,
            url         = url or None,
            abstract    = get("abstract") or "",
            source_name = get("source_name") or "",
            theme_tags  = theme_tags,
            source_type = get("type") or "",
        ))
    _assign_cite_keys(citables)
    for c in citables:
        c.apa = format_apa(c)
    logger.info(f"[References] Built manifest of {len(citables)} citable sources for run {run_id}")
    return citables


def _fallback_get_sources(run_id: str) -> list[dict]:
    """Fallback if database.py doesn't expose a suitable helper."""
    from core import database as db
    return db.fetch("sources", {"run_id": run_id})


# ---------------------------------------------------------------------------
# LLM prompt helpers — give Scribe a compact view of the manifest
# ---------------------------------------------------------------------------

def format_manifest_for_prompt(manifest: list[CitableSource],
                                max_sources: int = 200) -> str:
    """Compact representation the LLM can consume."""
    lines = []
    for s in manifest[:max_sources]:
        authors = "; ".join(s.authors[:3]) if s.authors else "Anon"
        extra = f" ({len(s.authors)} authors)" if len(s.authors) > 3 else ""
        year = s.year or "n.d."
        abstract_snippet = (s.abstract or "").strip().replace("\n", " ")
        if len(abstract_snippet) > 200:
            abstract_snippet = abstract_snippet[:200] + "..."
        lines.append(
            f"[{s.cite_key}] {authors}{extra} ({year}). {s.title}"
            + (f"  Abstract: {abstract_snippet}" if abstract_snippet else "")
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Coverage matrix — per-theme, per-source counts for the Understanding Map
# ---------------------------------------------------------------------------

def build_coverage_matrix(manifest: list[CitableSource]) -> dict:
    """
    Build a per-theme, per-source-name count matrix from the manifest.

    Returns a dict shaped:
      {
        "total_sources": int,
        "themes": {
          "<theme>": {
            "total": int,
            "by_source": {"openalex": int, "semantic_scholar": int, ...},
            "by_type":  {"current": int, "seminal": int, "historical": int},
          }, ...
        },
        "by_source": {"openalex": int, ...},   # overall per-source totals
        "by_type":   {"current": int, ...},     # overall per-type totals
        "thin_themes": ["<theme>", ...],        # themes with < 3 sources
        "untagged": int,                        # sources with no theme_tags
      }

    Sources with no theme_tags contribute to "untagged" and to the overall
    per-source / per-type totals but not to any theme row.
    """
    themes: dict[str, dict] = {}
    by_source: dict[str, int] = {}
    by_type: dict[str, int] = {}
    untagged = 0

    for s in manifest:
        # overall per-source
        by_source[s.source_name] = by_source.get(s.source_name, 0) + 1
        # overall per-type
        if s.source_type:
            by_type[s.source_type] = by_type.get(s.source_type, 0) + 1

        tags = s.theme_tags or []
        if not tags:
            untagged += 1
            continue
        for tag in tags:
            tag = tag.strip()
            if not tag:
                continue
            row = themes.setdefault(tag, {
                "total": 0,
                "by_source": {},
                "by_type": {},
            })
            row["total"] += 1
            row["by_source"][s.source_name] = \
                row["by_source"].get(s.source_name, 0) + 1
            if s.source_type:
                row["by_type"][s.source_type] = \
                    row["by_type"].get(s.source_type, 0) + 1

    thin_themes = sorted(t for t, r in themes.items() if r["total"] < 3)

    return {
        "total_sources": len(manifest),
        "themes": themes,
        "by_source": by_source,
        "by_type": by_type,
        "thin_themes": thin_themes,
        "untagged": untagged,
    }


def format_coverage_for_prompt(matrix: dict, max_themes: int = 30) -> str:
    """Render the coverage matrix as a compact text block for the Scribe prompt."""
    if not matrix or matrix.get("total_sources", 0) == 0:
        return "(no sources retrieved — coverage unknown)"

    total = matrix["total_sources"]
    by_source = matrix["by_source"]
    by_type = matrix["by_type"]
    themes = matrix["themes"]
    thin = matrix["thin_themes"]
    untagged = matrix["untagged"]

    lines = [f"TOTAL SOURCES: {total}"]

    # overall per-source
    src_parts = [f"{name}={n}" for name, n in
                 sorted(by_source.items(), key=lambda kv: (-kv[1], kv[0]))]
    if src_parts:
        lines.append("BY SOURCE: " + ", ".join(src_parts))

    # overall per-type
    type_parts = [f"{t}={n}" for t, n in
                  sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))]
    if type_parts:
        lines.append("BY TYPE: " + ", ".join(type_parts))

    if untagged:
        lines.append(f"UNTAGGED (no theme): {untagged}")

    lines.append("")
    lines.append("PER-THEME COVERAGE:")
    # sort themes by total desc, then name
    sorted_themes = sorted(themes.items(), key=lambda kv: (-kv[1]["total"], kv[0]))
    for theme, row in sorted_themes[:max_themes]:
        src_parts = [f"{s}={n}" for s, n in
                     sorted(row["by_source"].items(), key=lambda kv: (-kv[1], kv[0]))]
        type_parts = [f"{t}={n}" for t, n in
                      sorted(row["by_type"].items(), key=lambda kv: (-kv[1], kv[0]))]
        marker = "  [THIN]" if row["total"] < 3 else ""
        lines.append(f"  {theme}: {row['total']} sources"
                     + (f"  ({', '.join(src_parts)})" if src_parts else "")
                     + (f"  [{', '.join(type_parts)}]" if type_parts else "")
                     + marker)

    if thin:
        lines.append("")
        lines.append("THIN-COVERAGE THEMES (treat claims about these with caution): "
                     + ", ".join(thin))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Post-generation: extract cite keys from rendered text
# ---------------------------------------------------------------------------

CITE_KEY_PATTERN = re.compile(r"\[([A-Za-z][A-Za-z0-9,\s]+)\]")


def _split_key_list(inner: str) -> list[str]:
    """Split a comma-separated key list from inside a single [..] bracket."""
    return [k.strip() for k in inner.split(",") if k.strip()]


def extract_cite_keys(text: str, valid_keys: set[str]) -> list[str]:
    """Find [Key] or [Key1, Key2] markers. Return unique valid keys in order."""
    found = []
    seen = set()
    for m in CITE_KEY_PATTERN.finditer(text):
        for k in _split_key_list(m.group(1)):
            # Must be a plausible cite key shape
            if not re.match(r"^[A-Za-z][A-Za-z0-9]+$", k):
                continue
            if k in valid_keys and k not in seen:
                seen.add(k)
                found.append(k)
    return found


def find_unknown_cite_keys(text: str, valid_keys: set[str]) -> list[str]:
    """
    Return key-shaped tokens inside [...] that are NOT in valid_keys.
    These are likely LLM hallucinations; caller should redact.
    """
    found = []
    seen = set()
    for m in CITE_KEY_PATTERN.finditer(text):
        for k in _split_key_list(m.group(1)):
            if not re.match(r"^[A-Za-z][A-Za-z0-9]+$", k):
                continue
            if k not in valid_keys and k not in seen:
                seen.add(k)
                found.append(k)
    # Filter common non-citation bracketed words
    return [k for k in found
            if len(k) >= 5
            and not k.isdigit()
            and k not in {"ibid", "unsupported", "no source in run"}]


# ---------------------------------------------------------------------------
# Semantic citation validation — does the source support the claim?
# ---------------------------------------------------------------------------

SEMANTIC_CHECK_SYSTEM = """You verify whether a cited source plausibly supports \
the claim it's attached to in a research document.

You will be shown:
  - A claim from a research document
  - The title and abstract of the cited source

Decide whether the source is PLAUSIBLY RELEVANT to the claim — not whether it
proves the claim, but whether a reader would accept it as a reasonable citation.

A source is plausibly relevant if:
  - It discusses the same topic, concept, event, or period as the claim
  - It takes a position, presents evidence, or provides context that the claim
    draws on

A source is NOT plausibly relevant if:
  - Its topic is clearly different (e.g. a philosophy-of-mind paper cited for
    a claim about war crimes)
  - It is in an unrelated discipline and contains no bridging content

Output ONLY JSON:
{
  "plausible": true | false,
  "confidence": "high" | "medium" | "low",
  "reason": "one sentence explaining the verdict"
}"""


def validate_citation_claims(claims_and_cites: list[dict],
                              manifest_by_key: dict[str, CitableSource]
                              ) -> list[dict]:
    """
    claims_and_cites: list of {"claim": "...", "cite_keys": ["Key1", "Key2"]}
    Returns the same shape with a validation verdict per (claim, key) pair.
    """
    results = []
    for entry in claims_and_cites:
        claim = entry["claim"]
        verdicts = []
        for key in entry.get("cite_keys", []):
            src = manifest_by_key.get(key)
            if not src:
                verdicts.append({"cite_key": key, "plausible": False,
                                 "confidence": "high",
                                 "reason": "cite_key not in manifest"})
                continue
            prompt = (
                f"CLAIM: {claim}\n\n"
                f"CITED SOURCE:\n"
                f"  Title: {src.title}\n"
                f"  Authors: {'; '.join(src.authors[:3]) or 'Anon'}\n"
                f"  Year: {src.year or 'n.d.'}\n"
                f"  Abstract: {(src.abstract or '')[:800]}\n\n"
                f"Is this source plausibly relevant to the claim?"
            )
            try:
                response = llm.call(prompt, SEMANTIC_CHECK_SYSTEM,
                                    agent_name="scribe")
                clean = re.sub(r"```(?:json)?|```", "", response).strip()
                verdict = json.loads(clean)
                verdict["cite_key"] = key
                verdicts.append(verdict)
            except Exception as e:
                logger.warning(f"[References] Semantic check failed for {key}: {e}")
                verdicts.append({"cite_key": key, "plausible": None,
                                 "confidence": "low",
                                 "reason": f"validation error: {e}"})
        results.append({"claim": claim, "verdicts": verdicts})
    return results


# ---------------------------------------------------------------------------
# Online existence verification
# ---------------------------------------------------------------------------

CROSSREF_API = "https://api.crossref.org/works/"
OPENALEX_API = "https://api.openalex.org/works"

_VERIFY_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS reference_verifications (
    source_id     {ID} PRIMARY KEY,
    doi           {TEXT},
    url           {TEXT},
    exists_online {INT},      -- 1/0
    verified_via  {TEXT},
    note          {TEXT},
    checked_at    {TEXT}
);
"""

_verify_cache_ready = False


def _init_verify_cache():
    """Create the verification cache table once per process."""
    global _verify_cache_ready
    if _verify_cache_ready:
        return
    from core import db_backend
    db_backend.get_backend().init_schema(_VERIFY_CACHE_SCHEMA)
    _verify_cache_ready = True


def _cached_verification(source_id: str) -> Optional[dict]:
    from core import database as db
    _init_verify_cache()
    rows = db.fetch("reference_verifications", {"source_id": source_id})
    return rows[0] if rows else None


def _cache_verification(src: CitableSource) -> None:
    from datetime import datetime, timezone
    from core import database as db
    _init_verify_cache()
    db.insert("reference_verifications", {
        "source_id":     src.source_id,
        "doi":           _clean_doi(src.doi),
        "url":           src.url,
        "exists_online": int(bool(src.exists_online)) if src.exists_online is not None else None,
        "verified_via":  src.verified_via,
        "note":          src.verification_note,
        "checked_at":    datetime.now(timezone.utc).isoformat(),
    })


def _check_crossref(doi: str, client: httpx.Client) -> tuple[bool, str]:
    """Returns (exists, note). Crossref 200 means the DOI is registered."""
    try:
        r = client.get(CROSSREF_API + urllib.parse.quote(doi, safe=""),
                       timeout=10.0, follow_redirects=True)
        if r.status_code == 200:
            data = r.json()
            msg = data.get("message", {})
            title = (msg.get("title") or [""])[0]
            return True, f"crossref ok ({title[:60]})" if title else "crossref ok"
        if r.status_code == 404:
            return False, "crossref 404"
        return False, f"crossref {r.status_code}"
    except Exception as e:
        return False, f"crossref error: {e}"


def _check_openalex(title: str, client: httpx.Client) -> tuple[bool, str]:
    """Fallback: search OpenAlex by title."""
    try:
        r = client.get(OPENALEX_API, params={"search": title[:120], "per-page": 1},
                       timeout=10.0)
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            if results:
                matched = results[0].get("title", "")
                return True, f"openalex match: {matched[:60]}"
            return False, "openalex: no match"
        return False, f"openalex {r.status_code}"
    except Exception as e:
        return False, f"openalex error: {e}"


def _check_url_head(url: str, client: httpx.Client) -> tuple[bool, str]:
    try:
        r = client.head(url, timeout=8.0, follow_redirects=True)
        if r.status_code < 400:
            return True, f"url head {r.status_code}"
        # some servers don't support HEAD; try GET
        r = client.get(url, timeout=8.0, follow_redirects=True)
        if r.status_code < 400:
            return True, f"url get {r.status_code}"
        return False, f"url {r.status_code}"
    except Exception as e:
        return False, f"url error: {e}"


def verify_online(manifest: list[CitableSource],
                   use_cache: bool = True,
                   rate_limit_delay: float = 0.15) -> None:
    """
    Verify each source exists online. Mutates manifest in place.
    Order: Crossref (if DOI) → OpenAlex by title → URL HEAD.

    Crossref and OpenAlex calls route through the shared rate limiter
    (review O9) so they coordinate with concurrent Social/Grounder steps
    that hit the same sources.
    """
    from core.rate_limiter import get_limiter
    limiter = get_limiter("")  # no run_id — CLI/standalone use
    headers = {"User-Agent": "SEEKER/10.5 (mailto:research@example.org)"}
    with httpx.Client(headers=headers) as client:
        for src in manifest:
            if use_cache:
                cached = _cached_verification(src.source_id)
                if cached and cached.get("checked_at"):
                    src.exists_online = bool(cached["exists_online"])
                    src.verified_via  = cached.get("verified_via")
                    src.verification_note = cached.get("note") or ""
                    continue

            doi = _clean_doi(src.doi)
            verified = False
            via = None
            note = ""

            if doi:
                try:
                    limiter.wait("crossref")
                except Exception:
                    time.sleep(rate_limit_delay)  # limiter unavailable
                else:
                    ok, note = _check_crossref(doi, client)
                    if ok:
                        verified, via = True, "crossref"
                    limiter.record_success("crossref")

            if not verified and src.title:
                try:
                    limiter.wait("openalex")
                except Exception:
                    time.sleep(rate_limit_delay)
                else:
                    ok, note2 = _check_openalex(src.title, client)
                    if ok:
                        verified, via, note = True, "openalex", note2
                    else:
                        note = note + " | " + note2 if note else note2
                    limiter.record_success("openalex")

            if not verified and src.url:
                ok, note3 = _check_url_head(src.url, client)
                if ok:
                    verified, via, note = True, "url_head", note3
                else:
                    note = note + " | " + note3 if note else note3

            src.exists_online = verified
            src.verified_via  = via
            src.verification_note = note
            _cache_verification(src)

    ok_count = sum(1 for s in manifest if s.exists_online)
    logger.info(f"[References] Verified: {ok_count}/{len(manifest)} sources found online")


# ---------------------------------------------------------------------------
# Rendering — References section + .tex companion
# ---------------------------------------------------------------------------

def render_references_markdown(cited: list[CitableSource]) -> str:
    """Render a Markdown References section for the sources actually cited.

    Per design: dead-link sources keep their citation but the reference entry
    omits the URL. A small `[unverified online]` flag appears instead.
    """
    if not cited:
        return "## References\n\n_No citations in this document._\n"

    cited_sorted = sorted(cited, key=lambda s: s.cite_key.lower())
    lines = ["## References", ""]
    for s in cited_sorted:
        flag = ""
        if s.exists_online is False:
            flag = "  _[unverified online]_"
        lines.append(f"- **[{s.cite_key}]** {s.apa}{flag}")
    return "\n".join(lines) + "\n"


def render_references_tex(cited: list[CitableSource]) -> str:
    """Render a standalone .tex fragment with \\bibitem entries."""
    if not cited:
        return "% No citations in this document.\n"
    cited_sorted = sorted(cited, key=lambda s: s.cite_key.lower())
    lines = [
        "% References exported from SEEKER Understanding Map",
        "% Drop inside \\begin{thebibliography}{...} ... \\end{thebibliography}",
        "",
    ]
    for s in cited_sorted:
        # Escape special latex chars in the APA string
        safe_apa = _tex_escape(s.apa)
        flag = ""
        if s.exists_online is False:
            flag = " \\emph{[unverified online]}"
        lines.append(f"\\bibitem{{{s.cite_key}}} {safe_apa}{flag}")
    return "\n".join(lines) + "\n"


_TEX_ESCAPES = {
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}", "\\": r"\textbackslash{}",
}


def _tex_escape(text: str) -> str:
    return "".join(_TEX_ESCAPES.get(c, c) for c in text)
