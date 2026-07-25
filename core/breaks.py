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

logger = logging.getLogger(__name__)

ARTIFACTS_DIR = Path(__file__).parent.parent / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Document production helpers
# ---------------------------------------------------------------------------

def _now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


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
    ]
    for s in seminal[:30]:
        lines.append(f"- [{s.get('year','n.d.')}] **{s.get('title','')}** — {s.get('seminal_reason','')}")

    lines += ["", "---", "", "## Historical Map (Historian)", f"*{len(historical)} historical entries.*", ""]
    for s in historical[:30]:
        lines.append(f"- [{s.get('year','n.d.')}] **{s.get('title','')}** [{s.get('phase_tag','')}] — {s.get('historical_reason','')}")

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
        payload["fields"]["seminal"]    = db.get_sources_by_type("seminal", run_id)
        payload["fields"]["historical"] = db.get_sources_by_type("historical", run_id)
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
