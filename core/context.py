"""
Context Builder
---------------
Assembles the accumulated pipeline context for each agent.
Each agent receives exactly what it needs — no more, no less.
"""

import json
import logging
from typing import Optional
from . import database as db

logger = logging.getLogger(__name__)


def _fmt(label: str, content: str) -> str:
    """Format a context section."""
    return f"\n\n=== {label.upper()} ===\n{content}"


# ---------------------------------------------------------------------------
# Truncation
#
# Every list below is capped, because a whole run's evidence does not fit in
# a model's context. What matters is *which* items survive the cap and
# whether anyone is told. Previously these were bare slices of an unordered
# query, so the surviving subset was arbitrary — and with ~90% of rated
# sources coming back Low, an arbitrary subset is mostly noise (review V1).
#
# Two rules now:
#   1. Read ranked (see database.RELEVANCE_ORDER and friends), so the cap
#      keeps the best rather than the first.
#   2. Say what was left out, in the prompt itself and on the step card, so
#      neither the agent nor the researcher mistakes a slice for the whole.
# ---------------------------------------------------------------------------

def _tail_note(kind: str, shown: int, total: int, breakdown: dict = None) -> str:
    """One line telling the reader the list above is partial."""
    if total <= shown:
        return ""
    hidden = total - shown
    detail = ""
    if breakdown:
        parts = [f"{n} {label}" for label, n in breakdown.items() if n]
        if parts:
            detail = f" — full set: {', '.join(parts)}"
    return (f"\n… and {hidden} further {kind} not shown "
            f"(showing the top {shown} of {total} by rank){detail}.")


def _note_truncation(kind: str, shown: int, total: int) -> None:
    """Record a visible warning when a cap actually bit."""
    if total <= shown:
        return
    try:
        from core import progress
        progress.warn(f"Context truncated: the agent saw the top {shown} of "
                      f"{total} {kind}; {total - shown} were not shown")
    except Exception:
        pass


def _sources_summary(sources: list[dict], max_items: int = 20,
                     kind: str = "sources") -> str:
    """Format a list of sources into readable text, flagging what was cut."""
    if not sources:
        return "None available."
    lines = []
    for s in sources[:max_items]:
        authors = json.loads(s.get("authors") or "[]") if s.get("authors") else []
        author_str = ", ".join(authors[:3]) + ("..." if len(authors) > 3 else "")
        rating = s.get("relevance_rating")
        rating_tag = f"[{rating}] " if rating else ("[unrated] " if "relevance_rating" in s else "")
        lines.append(
            f"- {rating_tag}[{s.get('year', 'n.d.')}] {s.get('title', 'Untitled')} "
            f"({author_str}) | {s.get('source_name', '')} | "
            f"{s.get('seminal_reason') or s.get('historical_reason') or s.get('relevance_reason', '')}"
        )
    out = "\n".join(lines)
    _note_truncation(kind, min(max_items, len(sources)), len(sources))
    return out + _tail_note(kind, min(max_items, len(sources)), len(sources))


# Caps for the unbounded summaries. These lists were previously rendered in
# full, which on a real run means 356 gaps in one prompt — past the context
# window of the local models this ships against (review V4). Ranked reads
# make a cap safe: the best survive it.
MAX_GAPS_IN_CONTEXT         = 40
MAX_IMPLICATIONS_IN_CONTEXT = 30
MAX_PROPOSALS_IN_CONTEXT    = 25
MAX_EVALUATIONS_IN_CONTEXT  = 25

# The Understanding Map is the deliverable, so it gets a wider view than the
# intermediate agents — but it is still a cap, and it is still reported.
MAP_SEMINAL      = 25
MAP_HISTORICAL   = 20
MAP_GAPS         = 25
MAP_IMPLICATIONS = 20


def _gaps_summary(gaps: list[dict], max_items: int = MAX_GAPS_IN_CONTEXT) -> str:
    if not gaps:
        return "No gaps identified yet."
    lines = []
    for g in gaps[:max_items]:
        lines.append(
            f"- [{g.get('gap_id')}] [{g.get('significance')}] "
            f"[{g.get('gap_type')}] {g.get('description')} "
            f"| Primary eval: {g.get('primary_evaluation')}"
        )
    shown = min(max_items, len(gaps))
    _note_truncation("gaps", shown, len(gaps))
    return "\n".join(lines) + _tail_note(
        "gaps", shown, len(gaps), _tally(gaps, "significance"))


def _implications_summary(implications: list[dict],
                          max_items: int = MAX_IMPLICATIONS_IN_CONTEXT) -> str:
    if not implications:
        return "No implications identified yet."
    lines = []
    for i in implications[:max_items]:
        lines.append(
            f"- [{i.get('implication_id')}] [{i.get('strength')}] "
            f"[{i.get('implication_type')}] {i.get('implication')}"
        )
    shown = min(max_items, len(implications))
    _note_truncation("implications", shown, len(implications))
    return "\n".join(lines) + _tail_note(
        "implications", shown, len(implications),
        _tally(implications, "strength"))


def _proposals_summary(proposals: list[dict],
                       max_items: int = MAX_PROPOSALS_IN_CONTEXT) -> str:
    if not proposals:
        return "No proposals yet."
    lines = []
    for p in proposals[:max_items]:
        lines.append(
            f"- [{p.get('proposal_id')}] [{p.get('promise_rating')}] "
            f"[{p.get('proposal_type')}] {(p.get('proposal') or '')[:200]}..."
        )
    shown = min(max_items, len(proposals))
    _note_truncation("proposals", shown, len(proposals))
    return "\n".join(lines) + _tail_note(
        "proposals", shown, len(proposals), _tally(proposals, "promise_rating"))


def _evaluations_summary(evaluations: list[dict],
                         max_items: int = MAX_EVALUATIONS_IN_CONTEXT) -> str:
    if not evaluations:
        return "No evaluations yet."
    lines = []
    for e in evaluations[:max_items]:
        lines.append(
            f"- [{e.get('evaluation_id')}] Proposal {e.get('proposal_id')} → "
            f"[{e.get('verdict')}] {e.get('verdict_reason', '')}"
        )
    shown = min(max_items, len(evaluations))
    _note_truncation("evaluations", shown, len(evaluations))
    return "\n".join(lines) + _tail_note(
        "evaluations", shown, len(evaluations), _tally(evaluations, "verdict"))


def _tally(rows: list[dict], column: str) -> dict:
    """Count rows by one column, so a tail note can say what was left out."""
    out: dict = {}
    for r in rows:
        key = r.get(column) or "unrated"
        out[key] = out.get(key, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Context builders per agent
# ---------------------------------------------------------------------------

def for_grounder(run_id: str, problem: str, social_sources: list[dict]) -> str:
    """Context for Grounder — problem + Social intelligence."""
    ctx = f"PROBLEM:\n{problem}"
    ctx += _fmt("Social Intelligence (current sources)", _sources_summary(social_sources))
    return ctx


def for_historian(run_id: str, problem: str) -> str:
    """Context for Historian — problem + seminal works from Grounder."""
    seminal = db.get_sources_by_type("seminal", run_id, ranked=True)
    social  = db.get_sources_by_type("current", run_id, ranked=True)
    ctx  = f"PROBLEM:\n{problem}"
    ctx += _fmt("Seminal Works (from Grounder)", _sources_summary(seminal))
    ctx += _fmt("Social Intelligence (current sources)", _sources_summary(social))
    return ctx


def for_gaper(run_id: str, problem: str, break1_instructions: str = None) -> str:
    """Context for Gaper — minimal. Gaper builds its own two-pass context
    internally from the DB. We just pass the problem and break instructions."""
    ctx = f"PROBLEM:\n{problem}"
    if break1_instructions:
        ctx += _fmt("Your Break 1 Instructions", break1_instructions)
    return ctx


def _tree_context(run_id: str, max_depth: int = 3, include_evidence: bool = True) -> str:
    """Get argument tree context if tree exists for this run."""
    try:
        from core.argument_tree import TreeBuilder
        tree = TreeBuilder(run_id)
        stats = tree.get_stats()
        if stats.get("total_nodes", 0) == 0:
            tree.close()
            return ""
        ctx = tree.to_context(max_depth=max_depth, include_evidence=include_evidence)
        tree.close()
        return ctx
    except Exception:
        return ""


def for_vision(run_id: str, problem: str, break1_instructions: str = None) -> str:
    """Context for Vision — tree + gaps + social + Break 1."""
    seminal    = db.get_sources_by_type("seminal",    run_id, ranked=True)
    historical = db.get_sources_by_type("historical", run_id, ranked=True)
    social     = db.get_sources_by_type("current",    run_id, ranked=True)
    gaps       = db.get_gaps(run_id, ranked=True)
    ctx  = f"PROBLEM:\n{problem}"
    tree_ctx = _tree_context(run_id, max_depth=3)
    if tree_ctx:
        ctx += _fmt("Argument Tree (structured claims + evidence)", tree_ctx)
    ctx += _fmt("Seminal Works (Grounder)", _sources_summary(seminal))
    ctx += _fmt("Historical Map (Historian)", _sources_summary(historical))
    ctx += _fmt("Gap Map (Gaper)", _gaps_summary(gaps))
    ctx += _fmt("Current Intelligence (Social)", _sources_summary(social))
    if break1_instructions:
        ctx += _fmt("Break 1 Instructions (Human)", break1_instructions)
    return ctx


def for_theorist(run_id: str, problem: str, break1_instructions: str = None) -> str:
    """Context for Theorist — tree + all prior outputs."""
    seminal     = db.get_sources_by_type("seminal",    run_id, ranked=True)
    historical  = db.get_sources_by_type("historical", run_id, ranked=True)
    social      = db.get_sources_by_type("current",    run_id, ranked=True)
    gaps        = db.get_gaps(run_id, ranked=True)
    implications = db.get_implications(run_id, ranked=True)
    ctx  = f"PROBLEM:\n{problem}"
    tree_ctx = _tree_context(run_id, max_depth=3)
    if tree_ctx:
        ctx += _fmt("Argument Tree", tree_ctx)
    ctx += _fmt("Seminal Works (Grounder)", _sources_summary(seminal))
    ctx += _fmt("Historical Map (Historian)", _sources_summary(historical))
    ctx += _fmt("Gap Map (Gaper)", _gaps_summary(gaps))
    ctx += _fmt("Implications Map (Vision)", _implications_summary(implications))
    ctx += _fmt("Current Intelligence (Social)", _sources_summary(social))
    if break1_instructions:
        ctx += _fmt("Break 1 Instructions (Human)", break1_instructions)
    return ctx


def for_rude(run_id: str, problem: str, break1_instructions: str = None) -> str:
    """Context for Rude — tree + proposals + dead ends + social."""
    historical = db.get_sources_by_type("historical", run_id, ranked=True)
    social     = db.get_sources_by_type("current",    run_id, ranked=True)
    proposals  = db.get_proposals(run_id, ranked=True)
    gaps       = db.get_gaps(run_id, ranked=True)
    ctx  = f"PROBLEM:\n{problem}"
    tree_ctx = _tree_context(run_id, max_depth=2, include_evidence=False)
    if tree_ctx:
        ctx += _fmt("Argument Tree (claims only — check proposals against this)", tree_ctx)
    ctx += _fmt("Proposals (Theorist)", _proposals_summary(proposals))
    ctx += _fmt("Historical Dead Ends (Historian)", _sources_summary(
        [s for s in historical if s.get("phase_tag") == "dead_end"]
    ))
    ctx += _fmt("Current Intelligence (Social)", _sources_summary(social))
    ctx += _fmt("Gap Map (Gaper)", _gaps_summary(gaps))
    if break1_instructions:
        ctx += _fmt("Break 1 Instructions (Human)", break1_instructions)
    return ctx


def for_synthesizer(run_id: str, problem: str, break1_instructions: str = None) -> str:
    """Context for Synthesizer — tree + everything."""
    seminal     = db.get_sources_by_type("seminal",    run_id, ranked=True)
    historical  = db.get_sources_by_type("historical", run_id, ranked=True)
    social      = db.get_sources_by_type("current",    run_id, ranked=True)
    gaps        = db.get_gaps(run_id, ranked=True)
    implications = db.get_implications(run_id, ranked=True)
    proposals   = db.get_proposals(run_id, ranked=True)
    evaluations = db.get_evaluations(run_id)
    ctx  = f"PROBLEM:\n{problem}"
    tree_ctx = _tree_context(run_id, max_depth=4)
    if tree_ctx:
        ctx += _fmt("Argument Tree (full — build narrative from this structure)", tree_ctx)
    ctx += _fmt("Seminal Works (Grounder)", _sources_summary(seminal))
    ctx += _fmt("Historical Map (Historian)", _sources_summary(historical))
    ctx += _fmt("Current Intelligence (Social)", _sources_summary(social))
    ctx += _fmt("Gap Map (Gaper)", _gaps_summary(gaps))
    ctx += _fmt("Implications Map (Vision)", _implications_summary(implications))
    ctx += _fmt("Proposals (Theorist)", _proposals_summary(proposals))
    ctx += _fmt("Feasibility Evaluations (Rude)", _evaluations_summary(evaluations))
    if break1_instructions:
        ctx += _fmt("Break 1 Instructions (Human)", break1_instructions)
    return ctx


def for_thinker(run_id: str, problem: str, break2_instructions: str = None) -> str:
    """Context for Thinker — tree + synthesis + pipeline."""
    synthesis   = db.get_synthesis(run_id)
    gaps        = db.get_gaps(run_id, ranked=True)
    implications = db.get_implications(run_id, ranked=True)
    proposals   = db.get_proposals(run_id, status="feasible", ranked=True)
    evaluations = db.get_evaluations(run_id)
    ctx  = f"PROBLEM:\n{problem}"
    tree_ctx = _tree_context(run_id, max_depth=2, include_evidence=False)
    if tree_ctx:
        ctx += _fmt("Argument Tree (claims structure)", tree_ctx)
    if synthesis:
        ctx += _fmt("Research Narrative (Synthesizer)", synthesis.get("full_narrative", ""))
        ctx += _fmt("Trajectory Statement", synthesis.get("trajectory_statement", ""))
        ctx += _fmt("Key Tensions", str(synthesis.get("key_tensions", "")))
    ctx += _fmt("Gap Map (Gaper)", _gaps_summary(gaps))
    ctx += _fmt("Implications Map (Vision)", _implications_summary(implications))
    ctx += _fmt("Viable Proposals (post-Rude)", _proposals_summary(proposals))
    if break2_instructions:
        ctx += _fmt("Break 2 Instructions (Human)", break2_instructions)
    return ctx


def for_understanding_map(run_id: str, problem: str) -> str:
    """
    Context for the Understanding Map — the core mandatory Scribe output.
    Provides the richest possible context: seminal works, historical timeline,
    gaps, implications, proposals, synthesis, and directions.
    """
    seminal     = db.get_sources_by_type("seminal",    run_id, ranked=True)
    historical  = db.get_sources_by_type("historical", run_id, ranked=True)
    gaps        = db.get_gaps(run_id, ranked=True)
    implications = db.get_implications(run_id, ranked=True)
    proposals   = db.get_proposals(run_id, ranked=True)
    evaluations = db.get_evaluations(run_id)
    synthesis   = db.get_synthesis(run_id)
    directions  = db.get_directions(run_id)

    ctx  = f"PROBLEM:\n{problem}\n"
    ctx += f"\nOUTPUT TYPE: understanding_map\n"

    # Argument tree — the intellectual backbone
    tree_ctx = _tree_context(run_id, max_depth=4, include_evidence=True)
    if tree_ctx:
        ctx += f"\n=== ARGUMENT TREE (full structure for reading curriculum) ===\n{tree_ctx}\n"

    # Seminal works — the core of the reading curriculum. Read ranked, so the
    # cap keeps the works the pipeline judged most relevant rather than
    # whichever rows the storage engine happened to return first (review V1).
    if seminal:
        ctx += "\n=== SEMINAL WORKS (for reading curriculum) ===\n"
        for s in seminal[:MAP_SEMINAL]:
            authors = json.loads(s.get("authors") or "[]") if s.get("authors") else []
            author_str = ", ".join(authors[:3])
            ctx += (
                f"\n- [{s.get('year','n.d.')}] {s.get('title','Untitled')}"
                f" — {author_str}"
                f"\n  Reason: {s.get('seminal_reason','')}"
                f"\n  Abstract: {(s.get('abstract','') or '')[:400]}"
            )
        ctx += _tail_note("seminal works", min(MAP_SEMINAL, len(seminal)),
                          len(seminal))
        _note_truncation("seminal works", min(MAP_SEMINAL, len(seminal)),
                         len(seminal))

    # Historical timeline — for the intellectual genealogy section. Ordered by
    # year here rather than by rank: a genealogy is a chronology.
    if historical:
        ctx += "\n\n=== HISTORICAL SOURCES (for genealogy narrative) ===\n"
        for s in sorted(historical, key=lambda x: x.get('year') or 9999)[:MAP_HISTORICAL]:
            authors = json.loads(s.get("authors") or "[]") if s.get("authors") else []
            ctx += (
                f"\n- [{s.get('year','n.d.')}] {s.get('title','')}"
                f" — {', '.join(authors[:2])}"
                f"\n  {s.get('historical_reason','')}"
            )
        ctx += _tail_note("historical sources",
                          min(MAP_HISTORICAL, len(historical)), len(historical))

    # Gaps — for unresolved core section and assessment questions
    if gaps:
        ctx += "\n\n=== GAPS (for unresolved core and assessment) ===\n"
        for g in gaps[:MAP_GAPS]:
            ctx += (
                f"\n- [{g.get('significance','')}] [{g.get('gap_type','')}]"
                f" {g.get('description','')}"
            )
        ctx += _tail_note("gaps", min(MAP_GAPS, len(gaps)), len(gaps),
                          _tally(gaps, "significance"))
        _note_truncation("gaps", min(MAP_GAPS, len(gaps)), len(gaps))

    # Implications — for conceptual map section
    if implications:
        ctx += "\n\n=== IMPLICATIONS (for conceptual map) ===\n"
        for i in implications[:MAP_IMPLICATIONS]:
            ctx += (
                f"\n- [{i.get('strength','')}] {i.get('implication','')}"
                f"\n  Hidden assumption: {i.get('assumption_note','') if i.get('hidden_assumption') else 'none flagged'}"
            )
        ctx += _tail_note("implications", min(MAP_IMPLICATIONS, len(implications)),
                          len(implications), _tally(implications, "strength"))

    # Synthesis — for territory overview and trajectory
    if synthesis:
        ctx += f"\n\n=== SYNTHESIS ===\n"
        ctx += f"Sharpened problem: {synthesis.get('sharpened_problem','')}\n"
        ctx += f"Trajectory: {synthesis.get('trajectory_statement','')}\n"
        ctx += f"Key tensions: {str(synthesis.get('key_tensions',''))[:600]}\n"
        ctx += f"Narrative: {(synthesis.get('full_narrative','') or '')[:1200]}\n"

    # Proposals + verdicts — for assessment questions about feasibility
    viable   = [p for p in proposals if p.get("status") == "feasible"]
    infeasible = [p for p in proposals if p.get("status") == "infeasible"]
    if viable or infeasible:
        ctx += "\n\n=== PROPOSALS AND VERDICTS (for assessment questions) ===\n"
        for p in (viable + infeasible)[:8]:
            ev = next((e for e in evaluations if e.get("proposal_id") == p.get("proposal_id")), {})
            ctx += (
                f"\n- [{ev.get('verdict','?')}] {p.get('proposal','')[:200]}"
                f"\n  Why: {(ev.get('verdict_reason','') or '')[:200]}"
            )

    # Thinker directions — for assessment questions about new directions
    if directions:
        ctx += "\n\n=== NEW DIRECTIONS (Thinker) ===\n"
        for d in directions[:8]:
            ctx += f"\n- [{d.get('distance_rating','')}] [{d.get('direction_type','')}] {d.get('direction','')}"

    return ctx


def for_scribe(
    run_id: str,
    problem: str,
    output_type: str,
    audience: str,
    break2_instructions: str = None
) -> str:
    """Context for Scribe — synthesis + directions + output spec."""
    synthesis  = db.get_synthesis(run_id)
    directions = db.get_directions(run_id)
    proposals  = db.get_proposals(run_id, status="feasible", ranked=True)
    gaps       = db.get_gaps(run_id, significance="High", ranked=True)
    ctx  = f"PROBLEM:\n{problem}"
    ctx += _fmt("Requested Output Type", output_type)
    ctx += _fmt("Intended Audience", audience)
    if synthesis:
        ctx += _fmt("Research Narrative (Synthesizer)", synthesis.get("full_narrative", ""))
        ctx += _fmt("Trajectory Statement", synthesis.get("trajectory_statement", ""))
    ctx += _fmt("New Directions (Thinker)", "\n".join(
        [f"- [{d.get('distance_rating')}] {d.get('direction')}" for d in directions]
    ))
    ctx += _fmt("Viable Proposals", _proposals_summary(proposals))
    ctx += _fmt("High Significance Gaps", _gaps_summary(gaps))
    if break2_instructions:
        ctx += _fmt("Break 2 Instructions (Human)", break2_instructions)
    return ctx


def for_reporter(run_id: str, problem: str) -> str:
    """Context for the Reporter agent — the final step that bundles all
    Scribe artifacts into a single combined HTML report.

    The Reporter reads most of its data directly from the database (every
    artifact file, source, gap, proposal, evaluation, synthesis, direction,
    and implication), so this context only carries the problem statement and
    a manifest of what artifacts exist. The agent does the heavy lifting
    itself to avoid bloating the LLM prompt with full file contents.
    """
    artifacts = db.get_artifacts(run_id)
    ctx = f"PROBLEM:\n{problem}"
    ctx += _fmt("Artifacts to Bundle", "\n".join(
        f"- {a.get('output_type')} ({a.get('format')}): {a.get('title', '')} "
        f"— {a.get('word_count', 0)} words"
        for a in artifacts
        if a.get("output_type") != "combined_report"
    ) or "No artifacts found.")
    return ctx
