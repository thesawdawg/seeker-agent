"""
Pipeline State Machine
----------------------
The pipeline as resumable steps rather than one long blocking function.

Each step is a row in `run_steps` with an explicit status. A driver asks
"what is the next incomplete step?", runs exactly that, records the result,
and loops. On reaching a break the run is parked as `awaiting_input` and the
driver returns — freeing the process.

That inversion is what lets the same pipeline be driven three ways:

  CLI      main.py runs the loop, answering breaks at the terminal
  worker   a background process runs the loop and exits at each break
  web      HTTP POSTs the break answer, then asks the run to advance

Nothing here blocks on stdin. Interactive prompting lives in main.py.

Status values
  pending         not started
  running         claimed by a driver
  awaiting_input  a break is waiting for a human
  done            finished
  failed          errored; `error` holds the message
  skipped         deliberately bypassed
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from core import database as db
from core import breaks
from core.utils import generate_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

STEPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_steps (
    step_id       {ID} PRIMARY KEY,
    run_id        {ID} NOT NULL,
    step_name     {KEY} NOT NULL,
    ordinal       {INT} NOT NULL,
    kind          {KEY} NOT NULL,      -- agent / break / system
    status        {KEY} DEFAULT 'pending',
    attempt       {INT} DEFAULT 0,
    started_at    {TEXT},
    finished_at   {TEXT},
    error         {LONGTEXT},
    UNIQUE (run_id, step_name)
);

CREATE INDEX IF NOT EXISTS idx_run_steps_run ON run_steps(run_id);

-- Per-agent model choices for one run, set at creation or changed at a break.
-- Persisted rather than held in memory because the worker that runs the
-- agents is a different process from the web app that receives the change.
CREATE TABLE IF NOT EXISTS run_model_overrides (
    run_id      {ID} PRIMARY KEY,
    overrides   {LONGTEXT} NOT NULL,   -- JSON {agent: {model, provider, ...}}
    updated_at  {TEXT}
);
"""

_schema_ready = False


def init_steps_table():
    global _schema_ready
    if _schema_ready:
        return
    from core import db_backend
    db_backend.get_backend().init_schema(STEPS_SCHEMA)
    _schema_ready = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Step definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepDef:
    name:    str
    kind:    str                      # agent / break / system
    label:   str
    # Tables to clear when this step is re-run, as (table, extra_where) pairs.
    # run_id is always added to the where clause.
    outputs: tuple = ()
    break_num: Optional[int] = None


STEP_DEFS: tuple[StepDef, ...] = (
    StepDef("concept_mapper", "system", "Concept Mapper",
            outputs=(("concept_expansions", {}),)),
    StepDef("break0", "break", "Break 0 — Theme Confirmation", break_num=0),
    StepDef("grounder", "agent", "Grounder",
            outputs=(("sources", {"type": "seminal"}), ("argument_tree", {}))),
    StepDef("social", "agent", "Social",
            outputs=(("sources", {"type": "current"}),
                     ("argument_tree", {"agent_origin": "social"}))),
    StepDef("historian", "agent", "Historian",
            outputs=(("sources", {"type": "historical"}),
                     ("argument_tree", {"agent_origin": "historian"}))),
    StepDef("gaper", "agent", "Gaper",
            outputs=(("gaps", {}), ("argument_tree", {"agent_origin": "gaper"}))),
    StepDef("break1", "break", "Break 1 — Ground Truth Validation", break_num=1),
    StepDef("vision", "agent", "Vision", outputs=(("implications", {}),)),
    StepDef("theorist", "agent", "Theorist", outputs=(("proposals", {}),)),
    StepDef("rude", "agent", "Rude", outputs=(("evaluations", {}),)),
    StepDef("synthesizer", "agent", "Synthesizer", outputs=(("syntheses", {}),)),
    StepDef("break2", "break", "Break 2 — Trajectory Evaluation", break_num=2),
    StepDef("thinker", "agent", "Thinker", outputs=(("directions", {}),)),
    StepDef("scribe", "agent", "Scribe", outputs=(("artifacts", {}),)),
)

STEP_BY_NAME = {s.name: s for s in STEP_DEFS}
STEP_ORDER = {s.name: i for i, s in enumerate(STEP_DEFS)}

# Steps whose failure should not abort the run. Social is a discovery layer;
# a source outage should not sink a pipeline that can proceed without it.
NON_FATAL = {"social"}


# ---------------------------------------------------------------------------
# Step records
# ---------------------------------------------------------------------------

def ensure_steps(run_id: str) -> None:
    """Create the step rows for a run if they do not exist yet."""
    init_steps_table()
    existing = {r["step_name"] for r in db.fetch("run_steps", {"run_id": run_id})}
    for i, step in enumerate(STEP_DEFS):
        if step.name in existing:
            continue
        db.insert("run_steps", {
            "step_id":   generate_id("STP"),
            "run_id":    run_id,
            "step_name": step.name,
            "ordinal":   i,
            "kind":      step.kind,
            "status":    "pending",
            "attempt":   0,
        })


def get_steps(run_id: str) -> list[dict]:
    """All step records for a run, in pipeline order."""
    init_steps_table()
    steps = db.fetch("run_steps", {"run_id": run_id})
    steps.sort(key=lambda s: s.get("ordinal", 0))
    for s in steps:
        s["label"] = STEP_BY_NAME[s["step_name"]].label if s["step_name"] in STEP_BY_NAME else s["step_name"]
    return steps


def get_step(run_id: str, step_name: str) -> Optional[dict]:
    rows = db.fetch("run_steps", {"run_id": run_id, "step_name": step_name})
    return rows[0] if rows else None


def set_step_status(run_id: str, step_name: str, status: str,
                    error: str = None) -> None:
    data = {"status": status}
    if status == "running":
        data["started_at"] = _now()
        data["error"] = None
    elif status in ("done", "failed", "skipped"):
        data["finished_at"] = _now()
    if error is not None:
        data["error"] = error[:4000]
    db.update("run_steps", data, {"run_id": run_id, "step_name": step_name})


# Statuses that mean a step still needs work. `failed` is included
# deliberately: leaving it out would let a resume step straight over a failed
# agent and run the rest of the pipeline on missing data.
INCOMPLETE = ("pending", "running", "awaiting_input", "failed")


def next_step(run_id: str) -> Optional[dict]:
    """
    The first step that still needs work.

    A step already `awaiting_input` is returned so the caller can see the run
    is parked at a break; `running` is returned so a crashed step is visible
    rather than silently skipped; `failed` is returned so a resume retries it.
    """
    for step in get_steps(run_id):
        if step["status"] in INCOMPLETE:
            return step
    return None


# ---------------------------------------------------------------------------
# Run state — what the CLI prints and the API serves
# ---------------------------------------------------------------------------

def get_state(run_id: str) -> dict:
    """A complete picture of a run: status, per-step progress, current break."""
    run = db.get_run(run_id)
    if not run:
        return {"run_id": run_id, "exists": False}

    steps = get_steps(run_id)
    current = next_step(run_id)

    awaiting = None
    if current and current["status"] == "awaiting_input":
        step_def = STEP_BY_NAME.get(current["step_name"])
        if step_def and step_def.break_num is not None:
            awaiting = step_def.break_num

    done = sum(1 for s in steps if s["status"] in ("done", "skipped"))
    failed = [s for s in steps if s["status"] == "failed"]

    return {
        "run_id":         run_id,
        "exists":         True,
        "problem":        run.get("problem", ""),
        "status":         run.get("status", ""),
        "created_at":     run.get("created_at"),
        "completed_at":   run.get("completed_at"),
        "steps":          steps,
        "current_step":   current["step_name"] if current else None,
        "running":        bool(current and current["status"] == "running"),
        "awaiting_break": awaiting,
        "progress":       {"done": done, "total": len(steps)},
        "failed_steps":   [s["step_name"] for s in failed],
        "complete":       current is None,
    }


# ---------------------------------------------------------------------------
# Re-running a completed step
# ---------------------------------------------------------------------------

def downstream_steps(step_name: str) -> list[str]:
    """Steps that depend on this one — everything after it."""
    start = STEP_ORDER.get(step_name)
    if start is None:
        return []
    return [s.name for s in STEP_DEFS[start + 1:]]


def _purge_outputs(run_id: str, step_name: str) -> None:
    """Delete what a step produced, so a re-run does not stack on stale data."""
    step = STEP_BY_NAME.get(step_name)
    if not step:
        return

    for table, extra in step.outputs:
        where = {"run_id": run_id}
        where.update(extra)
        clauses = " AND ".join(f"{k} = ?" for k in where)
        db.execute(f"DELETE FROM {table} WHERE {clauses}", tuple(where.values()))

    if step.break_num is not None:
        db.execute("DELETE FROM break_instructions WHERE run_id = ? AND break_num = ?",
                   (run_id, step.break_num))
        db.update("runs", {f"break{step.break_num}_done": 0}, {"run_id": run_id})


def reset_step(run_id: str, step_name: str, cascade: bool = True) -> list[str]:
    """
    Mark a step (and by default everything downstream) pending again, clearing
    the outputs they produced.

    Returns the list of steps reset, so a caller can warn before discarding.
    """
    if step_name not in STEP_BY_NAME:
        raise ValueError(f"Unknown step: {step_name}")

    targets = [step_name] + (downstream_steps(step_name) if cascade else [])
    for name in targets:
        _purge_outputs(run_id, name)
        db.update("run_steps",
                  {"status": "pending", "started_at": None, "finished_at": None,
                   "error": None},
                  {"run_id": run_id, "step_name": name})

    db.update_run_status(run_id, "active")
    logger.info(f"[{run_id}] Reset steps: {', '.join(targets)}")
    return targets


# ---------------------------------------------------------------------------
# Break submission — the seam the web UI posts to
# ---------------------------------------------------------------------------

def submit_break(run_id: str, break_num: int, instructions: str,
                 source: str = "cli") -> dict:
    """
    Record a human's answer to a break and release the run.

    Safe to call from an HTTP handler: it touches only the database.
    """
    step_name = f"break{break_num}"
    if step_name not in STEP_BY_NAME:
        raise ValueError(f"Unknown break: {break_num}")

    instructions = (instructions or "").strip() or "CONFIRMED"
    contradictions = breaks.check_contradictions(instructions, run_id, break_num)

    db.save_break_instructions(run_id, break_num, instructions,
                               contradictions, source=source)
    db.mark_break_done(run_id, break_num)
    set_step_status(run_id, step_name, "done")

    logger.info(f"[{run_id}] Break {break_num} answered from {source} "
                f"({len(instructions)} chars, {len(contradictions)} contradiction(s))")
    return {"run_id": run_id, "break_num": break_num,
            "contradictions": contradictions}


def break_payload(run_id: str, break_num: int, config: dict = None) -> dict:
    """
    Everything a human needs to answer a break — structured for the web UI,
    and also what renders the markdown review document for the CLI.
    """
    return breaks.build_payload(run_id, break_num, config)


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------

def _selected_themes(run_id: str, config: dict) -> list[dict]:
    """
    Themes chosen by the concept mapper, honouring Break 0 adjustments.

    Read back from the database rather than carried in memory, so any driver
    can pick the run up at any point.
    """
    all_themes = config.get("themes", [])
    from core.concept_mapper import get_expansion

    expansion = get_expansion(run_id)
    theme_ids = None
    if expansion:
        import json
        raw = expansion.get("final_themes")
        try:
            theme_ids = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            theme_ids = None

    selected = ([t for t in all_themes if t.get("theme_id") in set(theme_ids)]
                if theme_ids else list(all_themes))

    # Apply ADD/REMOVE THEME directives from Break 0
    stored = db.get_break_instructions(run_id, 0)
    if stored:
        selected = breaks.apply_theme_directives(
            stored.get("instructions", ""), selected, all_themes
        )
    return selected or all_themes


def _run_agent_step(step_name: str, run_id: str, problem: str, config: dict) -> None:
    """Dispatch one agent step. Raises on failure."""
    from core.context import (
        for_grounder, for_historian, for_gaper, for_vision, for_theorist,
        for_rude, for_synthesizer, for_thinker, for_scribe,
        for_understanding_map,
    )

    b1 = _instructions(run_id, 1)
    b2 = _instructions(run_id, 2)

    if step_name == "grounder":
        from agents.grounder import run as agent
        agent(for_grounder(run_id, problem, []), run_id)

    elif step_name == "social":
        from agents.social import run as agent
        agent(f"PROBLEM:\n{problem}", run_id,
              config=config, selected_themes=_selected_themes(run_id, config))

    elif step_name == "historian":
        from agents.historian import run as agent
        agent(for_historian(run_id, problem), run_id)

    elif step_name == "gaper":
        from agents.gaper import run as agent
        agent(for_gaper(run_id, problem), run_id)

    elif step_name == "vision":
        from agents.vision import run as agent
        agent(for_vision(run_id, problem, b1), run_id)

    elif step_name == "theorist":
        from agents.theorist import run as agent
        agent(for_theorist(run_id, problem, b1), run_id)

    elif step_name == "rude":
        from agents.rude import run as agent
        agent(for_rude(run_id, problem, b1), run_id)

    elif step_name == "synthesizer":
        from agents.synthesizer import run as agent
        agent(for_synthesizer(run_id, problem, b1), run_id)

    elif step_name == "thinker":
        from agents.thinker import run as agent
        agent(for_thinker(run_id, problem, b2), run_id)

    elif step_name == "scribe":
        from agents.scribe import run as agent
        # The understanding map is produced on every run
        agent(for_understanding_map(run_id, problem), run_id,
              output_type="understanding_map", audience="researcher")
        for req in breaks.parse_scribe_requests(b2):
            output_type, audience = req["output_type"], req["audience"]
            try:
                agent(for_scribe(run_id, problem, output_type, audience, b2),
                      run_id, output_type=output_type, audience=audience)
            except Exception as e:
                # One failed output type should not lose the others
                logger.warning(f"[{run_id}] Scribe failed for {output_type}: {e}")

    else:
        raise ValueError(f"No runner for step: {step_name}")


def _instructions(run_id: str, break_num: int) -> str:
    stored = db.get_break_instructions(run_id, break_num)
    if not stored:
        return "CONFIRMED"
    text = stored.get("instructions", "") or "CONFIRMED"
    contradictions = stored.get("contradictions") or []
    if contradictions:
        text += "\n\n--- CONTRADICTION LOG ---\n" + "\n".join(contradictions)
    return text


def _run_concept_mapper(run_id: str, problem: str, config: dict) -> None:
    from core.concept_mapper import expand as concept_expand
    try:
        concept_expand(problem, run_id, config)
    except Exception as e:
        # Non-fatal: Break 0 falls back to offering every theme
        logger.warning(f"[{run_id}] Concept mapper failed ({e}) — all themes offered")


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------

def advance(run_id: str, problem: str = None, config: dict = None,
            max_steps: int = None) -> dict:
    """
    Run steps until a break needs an answer, the run completes, or a step
    fails. Returns the run state.

    Never blocks on input. A break parks the run as `awaiting_input` and
    returns, which is what frees the worker for someone else's run.
    """
    from core.utils import load_config

    config = config if config is not None else load_config()
    run = db.get_run(run_id)
    if not run:
        raise ValueError(f"Unknown run: {run_id}")
    problem = problem or run.get("problem", "")

    ensure_steps(run_id)
    executed = 0

    # Bind LLM calls to this run for the duration. Agents call
    # llm.call(prompt, system, agent_name=...) without a run_id, so without
    # this the run's own providers and model overrides would never apply.
    from core import llm
    apply_model_overrides(run_id)
    run_token = llm.set_current_run(run_id)
    try:
        return _advance_loop(run_id, problem, config, max_steps)
    finally:
        llm.reset_current_run(run_token)


def _advance_loop(run_id: str, problem: str, config: dict,
                  max_steps: Optional[int]) -> dict:
    executed = 0

    while True:
        if max_steps is not None and executed >= max_steps:
            break

        step = next_step(run_id)
        if step is None:
            db.update_run_status(run_id, "completed")
            logger.info(f"[{run_id}] Pipeline complete")
            break

        name = step["step_name"]
        step_def = STEP_BY_NAME[name]

        # Park at an unanswered break and hand control back
        if step_def.kind == "break":
            if step["status"] != "awaiting_input":
                set_step_status(run_id, name, "awaiting_input")
                db.update_run_status(run_id, f"awaiting_break{step_def.break_num}")
                logger.info(f"[{run_id}] Parked at {name} — awaiting human input")
            break

        # A step left `running` means a previous driver died mid-step
        if step["status"] == "running":
            logger.warning(f"[{run_id}] Step {name} was left running — retrying")

        db.update("run_steps", {"attempt": (step.get("attempt") or 0) + 1},
                  {"run_id": run_id, "step_name": name})
        set_step_status(run_id, name, "running")
        db.update_run_status(run_id, "active")
        logger.info(f"[{run_id}] ▶ {step_def.label}")

        try:
            if name == "concept_mapper":
                _run_concept_mapper(run_id, problem, config)
            else:
                _run_agent_step(name, run_id, problem, config)
            set_step_status(run_id, name, "done")
            logger.info(f"[{run_id}] ✓ {step_def.label}")
        except Exception as e:
            logger.error(f"[{run_id}] ✗ {step_def.label} failed: {e}", exc_info=True)
            if name in NON_FATAL:
                set_step_status(run_id, name, "skipped", error=str(e))
                logger.warning(f"[{run_id}] {name} is non-fatal — continuing")
            else:
                set_step_status(run_id, name, "failed", error=str(e))
                db.update_run_status(run_id, f"failed:{name}")
                break

        executed += 1

    return get_state(run_id)


def set_model_overrides(run_id: str, overrides: dict) -> dict:
    """
    Record per-agent model choices for a run, and apply them in this process.

    Merges with anything already stored, so a break can adjust one agent
    without resetting the rest.
    """
    import json
    from core import llm

    init_steps_table()
    known = set(STEP_BY_NAME)
    cleaned = {k.lower(): v for k, v in (overrides or {}).items()
               if k.lower() in known and isinstance(v, dict)}
    if not cleaned:
        return get_model_overrides(run_id)

    merged = get_model_overrides(run_id)
    for agent, spec in cleaned.items():
        merged.setdefault(agent, {}).update(
            {k: v for k, v in spec.items() if v is not None}
        )

    db.insert("run_model_overrides", {
        "run_id":     run_id,
        "overrides":  json.dumps(merged),
        "updated_at": _now(),
    })
    llm.set_run_overrides(run_id, merged)
    return merged


def get_model_overrides(run_id: str) -> dict:
    """Stored per-agent model choices for a run."""
    import json
    init_steps_table()
    rows = db.fetch("run_model_overrides", {"run_id": run_id})
    if not rows:
        return {}
    try:
        return json.loads(rows[0].get("overrides") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def apply_model_overrides(run_id: str) -> dict:
    """
    Load a run's stored model choices into this process.

    The worker calls this before advancing, since the choices were made in
    the web process.
    """
    from core import llm
    overrides = get_model_overrides(run_id)
    if overrides:
        llm.set_run_overrides(run_id, overrides)
    return overrides


def create_run(problem: str, run_id: str = None) -> str:
    """Register a new run and its steps."""
    from core.utils import generate_run_id
    run_id = run_id or generate_run_id()
    db.create_run(run_id, problem)
    ensure_steps(run_id)
    return run_id
