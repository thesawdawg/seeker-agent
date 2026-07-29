"""
Break Mechanics
---------------
Hard stops in the pipeline.
Each break:
  1. Produces a structured review document (saved to artifacts/)
  2. Waits for the human to upload an instruction document
  3. Reads the instruction document
  4. Checks for contradictions with prior agent outputs
  5. Returns instructions to inject into the next agent's context
"""

import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional
from . import database as db
from .urlcheck import is_well_formed, is_reachable

logger = logging.getLogger(__name__)

ARTIFACTS_DIR = Path(__file__).parent.parent / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Document production helpers
# ---------------------------------------------------------------------------

def _now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def _format_authors(authors_raw) -> str:
    """Format a JSON-encoded author list into a readable string."""
    if not authors_raw:
        return ""
    if isinstance(authors_raw, str):
        try:
            authors_raw = json.loads(authors_raw)
        except (json.JSONDecodeError, ValueError):
            return authors_raw
    if not isinstance(authors_raw, list) or not authors_raw:
        return ""
    names = [a if isinstance(a, str) else (a.get("name") or a.get("full_name") or str(a))
             for a in authors_raw]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} & {names[1]}"
    if len(names) <= 6:
        return ", ".join(names[:-1]) + f", & {names[-1]}"
    return ", ".join(names[:6]) + f", ... & {names[-1]}"


def _doi_url(doi: str) -> str:
    """Return a clickable DOI URL, or empty string."""
    d = (doi or "").strip()
    if not d:
        return ""
    if d.startswith("http"):
        return d
    return f"https://doi.org/{d}"


def _validate_source_for_display(source: dict,
                                 require_reachable: bool = False) -> dict:
    """
    Pre-check all URLs on a source before it appears in a break report.

    Returns a *copy* of `source` with invalid URLs blanked out and a
    `_url_issues` list describing what was removed, so both the markdown
    renderer and the web UI can show the researcher what happened.

    Provenance policy
    -----------------
    Links are unreliable unless directly extracted from provider response
    data or a tool call. The `url_origin` field records where a source's
    URLs came from:

      - "provider_api"   — Social agent, URLs extracted from OpenAlex /
                            Semantic Scholar / Consensus API responses.
      - "llm_synthesis"  — Grounder / Historian, URLs emitted by the LLM
                            during synthesis. These may be copied from
                            provider results, hallucinated, or mangled —
                            we cannot tell, so they are never shown.
      - "library_catalog"— Librarian, catalog_url from the Primo API.

    `active_link` and `doi` are shown only when `url_origin == "provider_api"`.
    `catalog_url` is always eligible (it is only ever written by the Librarian
    from a real API response) and is subject to the well-formed + dead-link
    checks below.

    Legacy rows without a `url_origin` field are treated as `llm_synthesis`
    (the safe default — hide rather than risk showing a fabricated link).

    Remaining checks (applied to URLs that pass the provenance gate)
    ---------------------------------------------------------------
    A URL is kept if it passes is_well_formed AND:
      - link_status is active / redirected / unreachable / flagged
        (unreachable is a transient network failure, not evidence the URL
        is invalid — review V2), OR
      - link_status is missing/unchecked and a live HEAD check does not
        return 404/410.

    A URL is removed if:
      - it fails is_well_formed (LLM placeholder, bad scheme, no host), OR
      - link_status is "dead" (server returned 404/410 at ingestion time), OR
      - link_status is missing/unchecked and a live HEAD check returns 404/410.

    The `require_reachable` parameter is retained for backward compatibility
    but is now redundant — provenance is the primary filter. When True,
    unchecked/missing link_status rows get a live HEAD check even on
    provider_api sources, providing an extra verification pass.
    """
    s = dict(source)
    issues: list[str] = []
    link_status = (s.get("link_status") or "").strip().lower()
    url_origin = (s.get("url_origin") or "").strip().lower()

    doi_url = _doi_url(s.get("doi") or "")
    active_link = (s.get("active_link") or "").strip()
    catalog_url = (s.get("catalog_url") or "").strip()

    # Provenance gate: active_link and doi are only eligible for display
    # when they came from a provider API response. LLM-synthesized URLs
    # (Grounder, Historian) and legacy rows without url_origin are hidden.
    llm_synthesized = url_origin != "provider_api"

    def _check(url: str, label: str, *, allow_llm: bool = False) -> str:
        """Return the URL if valid, else '' and record an issue."""
        if not url:
            return ""

        # Provenance gate for active_link / doi
        if not allow_llm and llm_synthesized:
            issues.append(
                f"{label}: link hidden (url_origin="
                f"{url_origin or 'missing'} — not from provider API)")
            return ""

        if not is_well_formed(url):
            issues.append(f"{label}: malformed URL removed ({url[:80]})")
            return ""
        if link_status == "dead":
            issues.append(f"{label}: dead link removed (404/410 at ingestion)")
            return ""
        # If link_status is unchecked or missing, do a live HEAD check
        if link_status in ("", "unchecked") or require_reachable:
            if not is_reachable(url, timeout=5.0):
                issues.append(f"{label}: dead link removed (404/410 on live check)")
                return ""
        return url

    valid_doi_url = _check(doi_url, "DOI")
    valid_active = _check(active_link, "URL")
    valid_catalog = _check(catalog_url, "Catalog", allow_llm=True)

    # Blank out invalid URLs so downstream renderers don't display them.
    # For DOI, blank the raw doi field if the constructed URL was invalid.
    if doi_url and not valid_doi_url:
        s["doi"] = ""
    if active_link and not valid_active:
        s["active_link"] = ""
    if catalog_url and not valid_catalog:
        s["catalog_url"] = ""

    s["_url_issues"] = issues
    return s


def _format_full_reference(source: dict) -> str:
    """
    Format a source row as a full reference with backlinks.

    Includes authors, year, title, DOI/URL, abstract excerpt, and library
    catalog availability so the researcher can locate and read the original
    work to validate claims.
    """
    parts = []
    authors = _format_authors(source.get("authors"))
    year = source.get("year") or "n.d."
    title = source.get("title") or ""
    if authors:
        parts.append(authors)
    parts.append(f"({year}).")
    if title:
        parts.append(f"**{title}**.")
    # Source/venue
    source_name = source.get("source_name") or ""
    if source_name:
        parts.append(f"*{source_name}*.")

    ref = " ".join(parts)

    # Backlinks
    links = []
    doi_url = _doi_url(source.get("doi") or "")
    active_link = source.get("active_link") or ""
    catalog_url = source.get("catalog_url") or ""
    if doi_url:
        links.append(f"DOI: [{doi_url}]({doi_url})")
    if active_link and active_link != doi_url:
        links.append(f"URL: [{active_link}]({active_link})")
    if catalog_url and catalog_url != active_link and catalog_url != doi_url:
        links.append(f"Catalog: [{catalog_url}]({catalog_url})")
    if links:
        ref += f"  \n  {' · '.join(links)}"

    # Library catalog availability (from Librarian step)
    availability_raw = source.get("availability") or ""
    if availability_raw:
        try:
            avail_list = json.loads(availability_raw) if isinstance(availability_raw, str) else availability_raw
            if isinstance(avail_list, list) and avail_list:
                avail_str = ", ".join(str(a) for a in avail_list)
                ref += f"  \n  **Catalog availability:** {avail_str}"
        except (json.JSONDecodeError, ValueError):
            pass

    # Abstract excerpt
    abstract = (source.get("abstract") or "").strip()
    if abstract:
        excerpt = abstract[:300] + ("…" if len(abstract) > 300 else "")
        ref += f"  \n  > {excerpt}"

    # URL validation warnings (from _validate_source_for_display)
    issues = source.get("_url_issues") or []
    if issues:
        ref += f"  \n  ⚠ *{'; '.join(issues)}*"

    return ref


def _produce_break0_doc(run_id: str, problem: str, selected_themes: list, excluded_themes: list) -> Path:
    """Produce Break 0 review document."""
    path = ARTIFACTS_DIR / f"{run_id}_break0_review.md"
    lines = [
        f"# Break 0 — Theme Confirmation",
        f"**Run ID:** {run_id}",
        f"**Problem:** {problem}",
        f"**Generated:** {_now_str()}",
        "",
        "---",
        "",
        "## Selected Themes",
        "The following themes were matched to your problem and pulled from the database:",
        "",
    ]
    for t in selected_themes:
        lines.append(f"- **{t['theme_id']}**: {t.get('label', '')} — Keywords: {', '.join([k.get('seed','') for k in t.get('keywords', [])])}")
    lines += [
        "",
        "## Excluded Themes",
        "The following themes were excluded with reasons:",
        "",
    ]
    for t in excluded_themes:
        lines.append(f"- **{t['theme_id']}**: {t.get('reason', 'Not relevant to problem')}")
    lines += [
        "",
        "---",
        "",
        "## Your Instructions",
        "Please review the above and provide your instructions below.",
        "You can:",
        "- Confirm the selection (write: `CONFIRMED`)",
        "- Override exclusions (write: `ADD THEME: <theme_id>`)",
        "- Remove a selected theme (write: `REMOVE THEME: <theme_id>`)",
        "- Add free-form instructions for the pipeline",
        "",
        "**Your instructions:**",
        "",
    ]
    path.write_text("\n".join(lines))
    logger.info(f"Break 0 document produced: {path}")
    return path


def _produce_break1_doc(run_id: str, problem: str) -> Path:
    """Produce Break 1 review document — Grounder + Historian + Gaper outputs."""
    seminal    = db.get_sources_by_type("seminal",    run_id)
    historical = db.get_sources_by_type("historical", run_id)
    gaps       = db.get_gaps(run_id)
    path = ARTIFACTS_DIR / f"{run_id}_break1_review.md"

    lines = [
        f"# Break 1 — Ground Truth Validation",
        f"**Run ID:** {run_id}",
        f"**Problem:** {problem}",
        f"**Generated:** {_now_str()}",
        "",
        "---",
        "",
        "## Seminal Works (Grounder)",
        f"*{len(seminal)} seminal works identified.*",
        "",
        "Full references with backlinks are provided so you can read the",
        "original works and validate the claims made about them.",
        "",
    ]
    for s in seminal[:30]:
        sv = _validate_source_for_display(s)
        sid = sv.get("source_id", "")
        lines.append(f"### [{sv.get('year','n.d.')}] {sv.get('title','')}")
        lines.append(f"*Source ID: {sid}*")
        lines.append("")
        lines.append(f"**Why seminal:** {sv.get('seminal_reason','')}")
        lines.append("")
        lines.append(_format_full_reference(sv))
        lines.append("")

    lines += ["", "---", "", "## Historical Map (Historian)", f"*{len(historical)} historical entries.*", ""]
    for s in historical[:30]:
        sv = _validate_source_for_display(s, require_reachable=True)
        lines.append(f"### [{sv.get('year','n.d.')}] {sv.get('title','')} [{sv.get('phase_tag','')}]")
        lines.append(f"**Why historical:** {sv.get('historical_reason','')}")
        lines.append("")
        lines.append(_format_full_reference(sv))
        lines.append("")

    lines += ["", "---", "", "## Gap Map (Gaper)", f"*{len(gaps)} gaps identified.*", ""]
    for g in gaps:
        lines.append(
            f"- **[{g.get('gap_id')}]** [{g.get('significance')}] [{g.get('gap_type')}]"
            f"\n  {g.get('description','')}"
            f"\n  *Primary evaluation: {g.get('primary_evaluation','')}*"
        )

    lines += [
        "",
        "---",
        "",
        "## Your Instructions",
        "Please review the above and provide your instructions.",
        "The pipeline will resume with your corrections injected into Vision.",
        "",
        "You can:",
        "- Confirm everything is correct (write: `CONFIRMED`)",
        "- Correct a gap (write: `CORRECT GAP <gap_id>: <your correction>`)",
        "- Remove a gap (write: `REMOVE GAP <gap_id>`)",
        "- Add a gap (write: `ADD GAP: <description>`)",
        "- Override a seminal work assessment (write: `OVERRIDE SEMINAL <source_id>: <your note>`)",
        "- Add free-form instructions for Vision and beyond",
        "",
        "**Your instructions:**",
        "",
    ]
    path.write_text("\n".join(lines))
    logger.info(f"Break 1 document produced: {path}")
    return path


def _produce_break2_doc(run_id: str, problem: str) -> Path:
    """Produce Break 2 review document — full synthesis for trajectory evaluation."""
    synthesis   = db.get_synthesis(run_id)
    proposals   = db.get_proposals(run_id)
    evaluations = db.get_evaluations(run_id)
    path = ARTIFACTS_DIR / f"{run_id}_break2_review.md"

    lines = [
        f"# Break 2 — Trajectory Evaluation",
        f"**Run ID:** {run_id}",
        f"**Problem:** {problem}",
        f"**Generated:** {_now_str()}",
        "",
        "---",
        "",
    ]

    if synthesis:
        narrative    = (synthesis.get("full_narrative", "") or "")
        trajectory   = (synthesis.get("trajectory_statement", "") or "")
        tensions_raw = synthesis.get("key_tensions", "")
        # Cap at readable lengths — full content always in database
        narrative_display  = narrative[:1500]  + ("...[truncated — full text in DB]" if len(narrative)  > 1500  else "")
        trajectory_display = trajectory[:800]  + ("...[truncated — full text in DB]" if len(trajectory) > 800   else "")
        tensions_display   = str(tensions_raw)[:600] + ("..." if len(str(tensions_raw)) > 600 else "")
        lines += [
            "## Sharpened Problem Statement",
            synthesis.get("sharpened_problem", ""),
            "",
            "---",
            "",
            "## Research Narrative",
            narrative_display,
            "",
            "---",
            "",
            "## Trajectory Statement",
            trajectory_display,
            "",
            "---",
            "",
            "## Key Tensions",
            tensions_display,
            "",
            "---",
            "",
        ]

    lines += ["## Feasibility Verdicts (Rude)", ""]
    for e in evaluations:
        p = next((p for p in proposals if p["proposal_id"] == e["proposal_id"]), {})
        proposal_text   = (p.get("proposal", "") or "")[:300]
        verdict_reason  = (e.get("verdict_reason", "") or "")[:400]
        weakest_link    = (e.get("weakest_empirical_link", "") or "")[:200]
        lines.append(
            f"- **[{e.get('proposal_id')}]** [{e.get('verdict')}]"
            f"\n  Proposal: {proposal_text}{'...' if len(p.get('proposal','') or '')>300 else ''}"
            f"\n  Reason: {verdict_reason}{'...' if len(e.get('verdict_reason','') or '')>400 else ''}"
            f"\n  Weakest link: {weakest_link}{'...' if len(e.get('weakest_empirical_link','') or '')>200 else ''}"
        )

    lines += [
        "",
        "---",
        "",
        "## Your Instructions",
        "Please evaluate the trajectory and provide your instructions.",
        "The pipeline will pass your instructions to Thinker and Scribe.",
        "",
        "You can:",
        "- Confirm the trajectory (write: `CONFIRMED`)",
        "- Override a verdict (write: `OVERRIDE VERDICT <evaluation_id>: <your reasoning>`)",
        "- Request specific output types from Scribe:",
        "  `SCRIBE OUTPUT: blog_post | audience: general public`",
        "  `SCRIBE OUTPUT: paper_section | audience: specialists`",
        "  `SCRIBE OUTPUT: research_brief | audience: collaborators`",
        "- Add free-form instructions for Thinker and Scribe",
        "",
        "**Your instructions:**",
        "",
    ]
    path.write_text("\n".join(lines))
    logger.info(f"Break 2 document produced: {path}")
    return path


# ---------------------------------------------------------------------------
# Instruction ingestion
# ---------------------------------------------------------------------------

def _wait_for_instruction_file(expected_path: Path, break_name: str) -> str:
    """
    Wait for the human to upload an instruction file.
    Polls every 10 seconds. Press Ctrl+C to abort.
    """
    print(f"\n{'='*60}")
    print(f"  {break_name} — PIPELINE PAUSED")
    print(f"{'='*60}")
    print(f"\nReview document saved to:\n  {expected_path}\n")
    print("When ready:")
    print(f"  1. Open the document above")
    print(f"  2. Fill in your instructions at the bottom")
    print(f"  3. Save the file")
    print(f"  4. Press ENTER here to continue\n")
    print("Or upload a separate instruction file and enter its path.")
    print("(Press Ctrl+C to abort the pipeline)\n")

    while True:
        try:
            user_input = input("Press ENTER when ready (or enter path to instruction file): ").strip()

            if user_input == "":
                # Read from the review document itself
                if expected_path.exists():
                    content = expected_path.read_text()
                    instructions = _extract_instructions(content)
                    if instructions:
                        logger.info(f"{break_name}: Instructions read from review document")
                        return instructions
                    else:
                        print("No instructions found in the document. Please fill in the instructions section.")
                else:
                    print(f"Document not found at {expected_path}")

            else:
                # Read from a separate file
                instruction_path = Path(user_input)
                if instruction_path.exists():
                    instructions = instruction_path.read_text().strip()
                    if instructions:
                        logger.info(f"{break_name}: Instructions read from {instruction_path}")
                        return instructions
                    else:
                        print("The file is empty. Please add your instructions.")
                else:
                    print(f"File not found: {instruction_path}")

        except KeyboardInterrupt:
            print("\n\nPipeline aborted by user.")
            sys.exit(0)


def _extract_instructions(document_content: str) -> str:
    """Extract the instructions section from a review document."""
    marker = "**Your instructions:**"
    if marker in document_content:
        parts = document_content.split(marker)
        if len(parts) > 1:
            instructions = parts[-1].strip()
            return instructions if instructions else ""
    return ""


def check_contradictions(instructions: str, run_id: str, break_num: int) -> list[str]:
    """Public alias — see _check_contradictions."""
    return _check_contradictions(instructions, run_id, break_num)


def _check_contradictions(instructions: str, run_id: str, break_num: int) -> list[str]:
    """
    Check if instructions contradict prior agent outputs.
    Returns a list of contradiction notices to inject into the next agent's context.
    """
    contradictions = []

    if break_num == 1:
        # Check gap overrides
        gaps = db.get_gaps(run_id)
        for gap in gaps:
            gap_id = gap.get("gap_id", "")
            if f"REMOVE GAP {gap_id}" in instructions:
                contradictions.append(
                    f"CONTRADICTION NOTICE: Your instruction removes {gap_id} "
                    f"which Gaper identified as a '{gap.get('significance')}' significance gap "
                    f"of type '{gap.get('gap_type')}'. "
                    f"Original: {gap.get('description','')[:100]}. "
                    f"This override is respected — downstream agents will not use this gap."
                )

    if break_num == 2:
        # Check verdict overrides
        evaluations = db.get_evaluations(run_id)
        for ev in evaluations:
            ev_id = ev.get("evaluation_id", "")
            if f"OVERRIDE VERDICT {ev_id}" in instructions:
                contradictions.append(
                    f"CONTRADICTION NOTICE: Your instruction overrides verdict for {ev_id} "
                    f"which Rude rated as '{ev.get('verdict')}'. "
                    f"Rude's reason: {ev.get('verdict_reason','')[:100]}. "
                    f"Your override is respected — Thinker and Scribe will proceed with your assessment."
                )

    return contradictions


# ---------------------------------------------------------------------------
# Public break interface
# ---------------------------------------------------------------------------

def answer_break_interactively(run_id: str, break_num: int,
                               config: dict = None) -> str:
    """
    Prompt for a break at the terminal and record the answer.

    CLI only — this blocks on stdin, so it must never be called from the web
    app or a worker. Those submit through core.pipeline.submit_break instead,
    which this function also uses, so both paths share one code path for
    persistence and contradiction checking.
    """
    from core import pipeline

    doc_path = produce_document(run_id, break_num, config)
    instructions = _wait_for_instruction_file(doc_path, f"BREAK {break_num}")
    result = pipeline.submit_break(run_id, break_num, instructions, source="cli")

    if result["contradictions"]:
        logger.info(f"Break {break_num}: {len(result['contradictions'])} contradiction(s) logged")
    return instructions


def resume_instructions(run_id: str, break_num: int) -> str:
    """
    Recover a completed break's instructions when resuming a run.

    Order of preference:
      1. The database (authoritative — stored verbatim at submission time)
      2. The review document on disk (runs that predate persistence)
      3. "CONFIRMED" (nothing recoverable)
    """
    stored = db.get_break_instructions(run_id, break_num)
    if stored and stored.get("instructions"):
        instructions = stored["instructions"]
        contradictions = stored.get("contradictions") or []
        if contradictions:
            instructions += "\n\n--- CONTRADICTION LOG ---\n" + "\n".join(contradictions)
        logger.info(f"Break {break_num}: instructions recovered from database")
        return instructions

    doc_path = ARTIFACTS_DIR / f"{run_id}_break{break_num}_review.md"
    if doc_path.exists():
        instructions = _extract_instructions(doc_path.read_text())
        if instructions:
            logger.info(f"Break {break_num}: instructions recovered from {doc_path}")
            return instructions

    logger.warning(
        f"Break {break_num}: no stored instructions for {run_id} — "
        f"resuming with CONFIRMED. Human steering for this break is lost."
    )
    return "CONFIRMED"


# ---------------------------------------------------------------------------
# Structured payloads
#
# One source of truth for what a break shows. The CLI renders it to markdown;
# the web UI renders it to widgets. Both produce the same directive language,
# so a run answered in the browser and a run answered at the terminal are
# indistinguishable downstream.
# ---------------------------------------------------------------------------

def _theme_options(run_id: str, config: dict) -> list[dict]:
    """Every theme, flagged with whether the concept mapper activated it."""
    import json
    all_themes = (config or {}).get("themes", [])

    activated = None
    try:
        from core.concept_mapper import get_expansion
        expansion = get_expansion(run_id)
        if expansion:
            raw = expansion.get("final_themes")
            activated = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        logger.warning(f"Could not read concept expansion for {run_id}: {e}")

    activated_set = set(activated) if activated else None
    options = []
    for t in all_themes:
        tid = t.get("theme_id", "")
        options.append({
            "theme_id": tid,
            "label":    t.get("label", ""),
            "keywords": [k.get("seed", "") for k in t.get("keywords", [])],
            "selected": True if activated_set is None else tid in activated_set,
        })
    return options


def apply_theme_directives(instructions: str, selected: list[dict],
                           all_themes: list[dict]) -> list[dict]:
    """
    Apply `ADD THEME: <id>` / `REMOVE THEME: <id>` from Break 0 instructions.
    """
    by_id = {t.get("theme_id"): t for t in all_themes}
    chosen = {t.get("theme_id"): t for t in selected}

    for line in (instructions or "").splitlines():
        line = line.strip()
        upper = line.upper()
        if upper.startswith("ADD THEME:"):
            tid = line.split(":", 1)[1].strip()
            if tid in by_id:
                chosen[tid] = by_id[tid]
            else:
                logger.warning(f"ADD THEME references unknown theme: {tid}")
        elif upper.startswith("REMOVE THEME:"):
            tid = line.split(":", 1)[1].strip()
            chosen.pop(tid, None)

    return list(chosen.values())


def build_payload(run_id: str, break_num: int, config: dict = None) -> dict:
    """
    Structured content for a break, plus the rendered review document.

    Shape is stable across breaks: `fields` carries the reviewable items,
    `directives` documents the commands a human may issue.
    """
    run = db.get_run(run_id) or {}
    problem = run.get("problem", "")
    stored = db.get_break_instructions(run_id, break_num)

    payload = {
        "run_id":      run_id,
        "break_num":   break_num,
        "problem":     problem,
        "generated":   _now_str(),
        "answered":    stored is not None,
        "instructions": (stored or {}).get("instructions", ""),
        "fields":      {},
        "directives":  [],
    }

    if break_num == 0:
        payload["title"] = "Break 0 — Theme Confirmation"
        payload["fields"]["themes"] = _theme_options(run_id, config or {})
        payload["directives"] = [
            {"command": "CONFIRMED", "description": "Accept the selection as-is"},
            {"command": "ADD THEME: <theme_id>", "description": "Include an excluded theme"},
            {"command": "REMOVE THEME: <theme_id>", "description": "Drop a selected theme"},
        ]

    elif break_num == 1:
        payload["title"] = "Break 1 — Ground Truth Validation"
        payload["fields"]["seminal"]    = [_validate_source_for_display(s)
                                           for s in db.get_sources_by_type("seminal", run_id)]
        payload["fields"]["historical"] = [_validate_source_for_display(s, require_reachable=True)
                                           for s in db.get_sources_by_type("historical", run_id)]
        payload["fields"]["gaps"]       = db.get_gaps(run_id)
        payload["directives"] = [
            {"command": "CONFIRMED", "description": "Everything is correct"},
            {"command": "CORRECT GAP <gap_id>: <text>", "description": "Revise a gap"},
            {"command": "REMOVE GAP <gap_id>", "description": "Drop a gap"},
            {"command": "ADD GAP: <description>", "description": "Add a missed gap"},
            {"command": "OVERRIDE SEMINAL <source_id>: <note>",
             "description": "Correct a seminal assessment"},
        ]

    elif break_num == 2:
        payload["title"] = "Break 2 — Trajectory Evaluation"
        proposals   = db.get_proposals(run_id)
        evaluations = db.get_evaluations(run_id)
        by_id = {p["proposal_id"]: p for p in proposals}
        payload["fields"]["synthesis"] = db.get_synthesis(run_id) or {}
        payload["fields"]["evaluations"] = [
            {**e, "proposal_text": (by_id.get(e.get("proposal_id"), {}) or {}).get("proposal", "")}
            for e in evaluations
        ]
        payload["fields"]["output_types"] = [
            "research_brief", "understanding_map", "blog_post",
            "paper_section", "literature_review",
        ]
        payload["directives"] = [
            {"command": "CONFIRMED", "description": "Accept the trajectory"},
            {"command": "OVERRIDE VERDICT <evaluation_id>: <reasoning>",
             "description": "Disagree with a feasibility verdict"},
            {"command": "SCRIBE OUTPUT: <type> | audience: <audience>",
             "description": "Request an output artifact"},
        ]

    else:
        raise ValueError(f"Unknown break: {break_num}")

    payload["document"] = str(produce_document(run_id, break_num, config))
    return payload


def produce_document(run_id: str, break_num: int, config: dict = None) -> Path:
    """Write the markdown review document for a break and return its path."""
    run = db.get_run(run_id) or {}
    problem = run.get("problem", "")

    if break_num == 0:
        options = _theme_options(run_id, config or {})
        selected = [{"theme_id": o["theme_id"], "label": o["label"],
                     "keywords": [{"seed": k} for k in o["keywords"]]}
                    for o in options if o["selected"]]
        excluded = [{"theme_id": o["theme_id"],
                     "reason": "Not activated by concept mapper"}
                    for o in options if not o["selected"]]
        return _produce_break0_doc(run_id, problem, selected, excluded)
    if break_num == 1:
        return _produce_break1_doc(run_id, problem)
    if break_num == 2:
        return _produce_break2_doc(run_id, problem)
    raise ValueError(f"Unknown break: {break_num}")


def parse_scribe_requests(instructions: str) -> list[dict]:
    """
    Parse SCRIBE OUTPUT directives from Break 2 instructions.
    Returns list of {output_type, audience} dicts.
    
    Format: SCRIBE OUTPUT: <output_type> | audience: <audience>
    """
    requests = []
    for line in instructions.split("\n"):
        line = line.strip()
        if line.upper().startswith("SCRIBE OUTPUT:"):
            parts = line[len("SCRIBE OUTPUT:"):].strip()
            output_type = parts.split("|")[0].strip()
            audience = "general"
            if "|" in parts:
                aud_part = parts.split("|")[1]
                if "audience:" in aud_part.lower():
                    audience = aud_part.lower().replace("audience:", "").strip()
            requests.append({"output_type": output_type, "audience": audience})
    
    # Default if no SCRIBE OUTPUT directives found
    if not requests:
        requests.append({"output_type": "research_brief", "audience": "researcher"})
    
    return requests
