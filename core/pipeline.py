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

import json
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
    activity      {TEXT},              -- what the step is doing right now
    activity_at   {TEXT},
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

-- Per-run source enable/disable overrides (review U1). A researcher can
-- drop arXiv for a humanities run or add Scopus when they have access,
-- without editing config.json. JSON: {source_id: bool} applied on top of
-- config.sources.<name>.enabled and config.agent_sources.<agent>.
CREATE TABLE IF NOT EXISTS run_source_overrides (
    run_id      {ID} PRIMARY KEY,
    overrides   {LONGTEXT} NOT NULL,   -- JSON {source_id: true/false}
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
    # Columns added after run_steps first shipped. CREATE TABLE IF NOT EXISTS
    # does nothing to an existing table, so they must be reconciled explicitly
    # or every write to them fails silently.
    db_backend.ensure_columns("run_steps", {
        "activity":    "{TEXT}",
        "activity_at": "{TEXT}",
        "warnings":    "{LONGTEXT}",  # JSON list of non-fatal warnings (review E7)
    })
    # When a stop was asked for, so its deadline can be measured
    db_backend.ensure_columns("runs", {"cancel_requested_at": "{TEXT}"})
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
    StepDef("reporter", "agent", "Reporter",
            outputs=(("artifacts", {"output_type": "combined_report"}),)),
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


def claim_step(run_id: str, step_name: str, from_statuses=("pending", "failed")) -> bool:
    """
    Take ownership of a step, atomically.

    A conditional UPDATE, so the database decides the winner: if two drivers
    reach the same pending step — which the job queue should prevent but
    cannot guarantee across a reap, a forced stop, or a manual advance — only
    the one whose UPDATE matches a row proceeds. Without this both would set
    the step running and both would execute the agent, doubling its output
    into the same run (review C5).

    Returns True if this caller now owns the step.
    """
    init_steps_table()
    ph = ", ".join("?" for _ in from_statuses)
    changed = db.execute_rowcount(
        f"UPDATE run_steps SET status = 'running', started_at = ?, error = NULL "
        f"WHERE run_id = ? AND step_name = ? AND status IN ({ph})",
        (_now(), run_id, step_name, *from_statuses),
    )
    return changed > 0


def record_step_warning(run_id: str, step_name: str, warning: str) -> None:
    """
    Record a non-fatal warning on a step (review E7).

    JSON parse failures, empty LLM responses, and other degraded-data cases
    used to only emit logger.warning — invisible to the researcher. This
    appends to a JSON list in run_steps.warnings that the UI surfaces on
    the step card so the researcher knows the Understanding Map is partial.
    """
    import json
    step = get_step(run_id, step_name)
    if not step:
        return
    raw = step.get("warnings") or "[]"
    try:
        warnings = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (json.JSONDecodeError, TypeError):
        warnings = []
    warnings.append({"at": _now(), "message": warning[:500]})
    db.update("run_steps", {"warnings": json.dumps(warnings[-20:])},
              {"run_id": run_id, "step_name": step_name})


# Statuses that mean a step still needs work. `failed` is included
# deliberately: leaving it out would let a resume step straight over a failed
# agent and run the rest of the pipeline on missing data.
INCOMPLETE = ("pending", "running", "awaiting_input", "failed")


def next_step(run_id: str, steps: list[dict] = None) -> Optional[dict]:
    """
    The first step that still needs work.

    A step already `awaiting_input` is returned so the caller can see the run
    is parked at a break; `running` is returned so a crashed step is visible
    rather than silently skipped; `failed` is returned so a resume retries it.

    Pass `steps` when the caller already has them — get_state does — to save
    a second read of the same rows (review O6).
    """
    for step in (steps if steps is not None else get_steps(run_id)):
        if step["status"] in INCOMPLETE:
            return step
    return None


# ---------------------------------------------------------------------------
# Run state — what the CLI prints and the API serves
# ---------------------------------------------------------------------------

def summarise_steps(steps: list[dict]) -> dict:
    """
    progress + awaiting_break from step rows the caller already has.

    Split out from get_run_summary so a list view can read every run's steps
    in one grouped query and summarise them here, instead of querying per run
    (review O2).
    """
    done = sum(1 for s in steps if s.get("status") in ("done", "skipped"))
    awaiting = None
    for s in sorted(steps, key=lambda x: x.get("ordinal", 0)):
        if s.get("status") == "awaiting_input":
            step_def = STEP_BY_NAME.get(s.get("step_name", ""))
            if step_def and step_def.break_num is not None:
                awaiting = step_def.break_num
            break
        if s.get("status") not in ("done", "skipped", "failed"):
            break
    return {
        "progress": {"done": done, "total": len(steps)},
        "awaiting_break": awaiting,
    }


def get_run_summary(run_id: str) -> dict:
    """Lightweight run status for a single run (review O6).

    Returns only progress.done, progress.total, and awaiting_break —
    without building the full step-state objects that get_state() assembles.
    """
    init_steps_table()
    return summarise_steps(db.fetch("run_steps", {"run_id": run_id}))


def get_state(run_id: str) -> dict:
    """A complete picture of a run: status, per-step progress, current break."""
    run = db.get_run(run_id)
    if not run:
        return {"run_id": run_id, "exists": False}

    steps = get_steps(run_id)
    current = next_step(run_id, steps)

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

# ---------------------------------------------------------------------------
# Stopping and resuming
#
# A run using the wrong model should not have to be waited out. Stopping is
# cooperative (see core/cancellation.py): the request is recorded here, and
# the worker unwinds at its next checkpoint.
# ---------------------------------------------------------------------------

# A stop must complete in bounded time. Cooperative checkpoints handle the
# common case, but a worker blocked inside a socket read cannot notice
# anything — so past this deadline the run is stopped without its cooperation.
STOP_GRACE_SECONDS = 60


def cancel_deadline_passed(run_id: str) -> bool:
    """Whether a stop request has outlived its grace period."""
    run = db.get_run(run_id)
    if not run or run.get("status") != "cancelling":
        return False
    requested = run.get("cancel_requested_at")
    if not requested:
        return True          # no timestamp recorded — do not wait forever
    try:
        started = datetime.fromisoformat(requested)
    except (TypeError, ValueError):
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds() > STOP_GRACE_SECONDS


def enforce_stop_deadline(run_id: str) -> bool:
    """
    Force a stop that has not landed within the grace period.

    Callable from the web process, so a run still comes to rest even when the
    worker holding it is wedged or gone. The interrupted step is discarded and
    its job released; a worker that later wakes up finds the run already
    stopped and does nothing further.
    """
    if not cancel_deadline_passed(run_id):
        return False

    running = [s for s in get_steps(run_id) if s["status"] == "running"]
    step_name = running[0]["step_name"] if running else None

    logger.warning(
        f"[{run_id}] Stop did not complete within {STOP_GRACE_SECONDS}s — "
        f"forcing it{' and discarding ' + step_name if step_name else ''}")
    finish_cancel(run_id, step_name)

    # Free the job so a wedged worker cannot keep holding it
    try:
        from core import jobs
        for job in jobs.jobs_for_run(run_id):
            if job.get("status") in ("queued", "running"):
                jobs.finish(job["job_id"], "done",
                            error="stopped by request (forced)")
    except Exception as e:
        logger.warning(f"[{run_id}] could not release job after forced stop: {e}")
    return True


def request_cancel(run_id: str) -> dict:
    """
    Ask a run to stop.

    If a worker is mid-step it notices at its next checkpoint — typically the
    next LLM attempt — and unwinds. If nothing is executing, the run is marked
    stopped immediately.
    """
    from core import cancellation

    run = db.get_run(run_id)
    if not run:
        raise ValueError(f"Unknown run: {run_id}")
    if run.get("status") == "completed":
        return {"run_id": run_id, "status": "completed",
                "note": "Run already finished"}

    ensure_steps(run_id)
    running = [s for s in get_steps(run_id) if s["status"] == "running"]

    db.update("runs", {"status": cancellation.CANCELLING,
                       "cancel_requested_at": _now()}, {"run_id": run_id})
    cancellation.forget(run_id)

    if not running:
        # Nothing to interrupt — park it stopped right away
        finish_cancel(run_id)
        return {"run_id": run_id, "status": cancellation.CANCELLED,
                "stopped_step": None, "immediate": True}

    logger.info(f"[{run_id}] Cancellation requested while {running[0]['step_name']} runs")
    return {"run_id": run_id, "status": cancellation.CANCELLING,
            "stopped_step": running[0]["step_name"], "immediate": False,
            "grace_seconds": STOP_GRACE_SECONDS}


def finish_cancel(run_id: str, step_name: str = None) -> None:
    """
    Record that a run has actually stopped.

    The interrupted step is reset rather than left half-done: it may have
    written some sources or tree nodes before stopping, and resuming on top of
    those would duplicate them.
    """
    from core import cancellation

    if step_name:
        # cascade=False — only the interrupted step is discarded, everything
        # already completed before it is kept.
        reset_step(run_id, step_name, cascade=False)

    db.update("runs", {"status": cancellation.CANCELLED,
                       "cancel_requested_at": None}, {"run_id": run_id})
    cancellation.forget(run_id)
    logger.info(f"[{run_id}] Stopped"
                + (f" — {step_name} will restart from the beginning" if step_name else ""))


def is_cancelling(run_id: str) -> bool:
    from core import cancellation
    return cancellation.is_cancelling(run_id)


def resume(run_id: str, model_overrides: dict = None) -> dict:
    """
    Restart a stopped run, optionally with different models.

    The step that was interrupted runs again from the beginning, using the new
    routing. Steps completed before the stop are untouched.
    """
    from core import cancellation

    run = db.get_run(run_id)
    if not run:
        raise ValueError(f"Unknown run: {run_id}")

    if model_overrides:
        set_model_overrides(run_id, model_overrides)

    ensure_steps(run_id)
    # A worker killed mid-step leaves the row 'running'; make it runnable again
    for step in get_steps(run_id):
        if step["status"] == "running":
            db.update("run_steps",
                      {"status": "pending", "started_at": None, "error": None},
                      {"run_id": run_id, "step_name": step["step_name"]})

    # A forced stop can leave a wedged worker writing for a little longer, so
    # clear the step about to restart rather than stacking on its leftovers.
    upcoming = next_step(run_id)
    if upcoming and STEP_BY_NAME.get(upcoming["step_name"], StepDef("", "", "")).kind != "break":
        _purge_outputs(run_id, upcoming["step_name"])

    db.update_run_status(run_id, "active")
    cancellation.forget(run_id)
    logger.info(f"[{run_id}] Resumed"
                + (f" with model changes: {sorted(model_overrides)}" if model_overrides else ""))
    return get_state(run_id)


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
        for_understanding_map, for_reporter,
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

    elif step_name == "reporter":
        from agents.reporter import run as agent
        agent(for_reporter(run_id, problem), run_id)

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

    # Bind LLM calls to this run for the duration. Agents call
    # llm.call(prompt, system, agent_name=...) without a run_id, so without
    # this the run's own providers and model overrides would never apply.
    from core import cancellation, llm
    apply_model_overrides(run_id)
    run_token = llm.set_current_run(run_id)
    cancel_token = cancellation.bind(run_id)
    try:
        return _advance_loop(run_id, problem, config, max_steps)
    finally:
        llm.reset_current_run(run_token)
        cancellation.release(cancel_token)


def _advance_loop(run_id: str, problem: str, config: dict,
                  max_steps: Optional[int]) -> dict:
    executed = 0

    while True:
        if max_steps is not None and executed >= max_steps:
            break

        # Stop before starting more work, not only mid-step
        from core import cancellation
        if cancellation.is_cancelling(run_id):
            finish_cancel(run_id)
            logger.info(f"[{run_id}] Stopped between steps")
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

        # `running` is deliberately *not* claimable: a step in that state
        # belongs to a live driver, and claiming it is exactly the
        # double-execution this guards against. A step left running by a
        # crashed worker is recovered one layer up — jobs.reap_stale resets
        # it when it reaps the dead worker's job, and resume() resets it when
        # a human restarts the run.
        if not claim_step(run_id, name):
            logger.info(
                f"[{run_id}] {name} is {step['status']} and could not be "
                f"claimed — another driver holds it. Leaving it to them.")
            break

        db.update("run_steps", {"attempt": (step.get("attempt") or 0) + 1},
                  {"run_id": run_id, "step_name": name})
        db.update_run_status(run_id, "active")
        logger.info(f"[{run_id}] ▶ {step_def.label}")

        from core import progress
        progress_token = progress.bind(run_id, name)
        try:
            if name == "concept_mapper":
                _run_concept_mapper(run_id, problem, config)
            else:
                _run_agent_step(name, run_id, problem, config)
            progress.clear()
            set_step_status(run_id, name, "done")
            logger.info(f"[{run_id}] ✓ {step_def.label}")
        except cancellation.RunCancelled:
            # Not a failure — the researcher asked it to stop
            logger.info(f"[{run_id}] {step_def.label} interrupted by a stop request")
            finish_cancel(run_id, name)
            break        # the finally below releases the progress token

        except Exception as e:
            logger.error(f"[{run_id}] ✗ {step_def.label} failed: {e}", exc_info=True)
            if name in NON_FATAL:
                set_step_status(run_id, name, "skipped", error=str(e))
                logger.warning(f"[{run_id}] {name} is non-fatal — continuing")
            else:
                set_step_status(run_id, name, "failed", error=str(e))
                db.update_run_status(run_id, f"failed:{name}")
                break
        finally:
            progress.release(progress_token)

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


# ---------------------------------------------------------------------------
# Per-run source overrides (review U1)
# ---------------------------------------------------------------------------

def set_source_overrides(run_id: str, overrides: dict) -> dict:
    """Record per-run source enable/disable choices. Merges with existing."""
    import json
    init_steps_table()
    cleaned = {str(k).lower(): bool(v)
               for k, v in (overrides or {}).items() if isinstance(v, bool)}
    if not cleaned:
        return get_source_overrides(run_id)
    merged = get_source_overrides(run_id)
    merged.update(cleaned)
    db.insert("run_source_overrides", {
        "run_id":     run_id,
        "overrides":  json.dumps(merged),
        "updated_at": _now(),
    })
    return merged


def get_source_overrides(run_id: str) -> dict:
    """Stored per-run source enable/disable choices."""
    import json
    init_steps_table()
    rows = db.fetch("run_source_overrides", {"run_id": run_id})
    if not rows:
        return {}
    try:
        return json.loads(rows[0].get("overrides") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def apply_source_overrides(run_id: str, config: dict) -> dict:
    """
    Merge a run's source overrides into the config dict the agents see.

    The worker calls this before advancing. Overrides are {source_id: bool};
    True forces a source on, False forces it off. A special key
    "limit_per_source" (int) sets the per-source result limit for this run
    (review U5). This updates both config.sources.<name>.enabled and prunes
    config.agent_sources.<agent> lists, so the agents' existing config reads
    pick up the change.
    """
    overrides = get_source_overrides(run_id)
    if not overrides:
        return config
    import copy
    cfg = copy.deepcopy(config)
    sources_cfg = cfg.setdefault("sources", {})
    # Work on a copy so the special key is not permanently removed from the
    # stored overrides dict (review U5).
    overrides = dict(overrides)
    run_limit = overrides.pop("limit_per_source", None)
    for source_id, enabled in overrides.items():
        sources_cfg.setdefault(source_id, {})["enabled"] = enabled
    # Also prune agent_sources lists so a disabled source is not even
    # attempted (the agents iterate agent_sources.<agent>).
    agent_sources = cfg.get("agent_sources", {})
    for agent, src_list in agent_sources.items():
        if isinstance(src_list, list):
            agent_sources[agent] = [
                s for s in src_list
                if overrides.get(s, sources_cfg.get(s, {}).get("enabled", True))
            ]
    # Apply the per-run source limit (review U5)
    if run_limit is not None:
        agent_sources["social_limit"] = int(run_limit)
        gl = agent_sources.setdefault("grounder_limits", {})
        for src in ("openalex", "semantic_scholar", "consensus",
                    "google_books", "open_library"):
            gl.setdefault(src, int(run_limit))
    return cfg


def create_run(problem: str, run_id: str = None, previous_run_id: str = None) -> str:
    """Register a new run and its steps."""
    from core.utils import generate_run_id
    run_id = run_id or generate_run_id()
    db.create_run(run_id, problem, previous_run_id=previous_run_id)
    ensure_steps(run_id)
    return run_id


# ---------------------------------------------------------------------------
# Branch a run (F11) — clone state up to a chosen step into a new run.
#
# This is the natural extension of reset_step: instead of discarding a step's
# output in place, it copies the prefix of a completed run into a new run_id
# so the original is preserved for comparison. The new run starts from the
# step after the branch point, with the cloned context already in place.
# ---------------------------------------------------------------------------

# The primary-key column for each run-scoped table, so a cloned row can be
# given a fresh id.
#
# Which tables get cloned is *not* a fixed list: it is derived from the
# StepDef.outputs of the steps in the branch prefix. Cloning a fixed list
# copied a run's gaps, proposals, evaluations and syntheses into a branch
# taken before those steps ran — and since the steps were then marked pending
# and nothing purged the rows, the branch re-ran them on top of the parent's
# conclusions (review C4). Deriving from STEP_DEFS keeps one source of truth
# for "what does this step produce".
_TABLE_ID_COL = {
    "sources":             "source_id",
    "concept_expansions":  "expansion_id",
    "gaps":                "gap_id",
    "implications":        "implication_id",
    "proposals":           "proposal_id",
    "evaluations":         "evaluation_id",
    "syntheses":           "synthesis_id",
    "directions":          "direction_id",
    "artifacts":           "artifact_id",
}

# Handled by dedicated cloners rather than the generic row copy.
_CLONE_SPECIAL = {"argument_tree", "break_instructions"}


def _clone_plan(clone_step_names: list[str]) -> list[tuple[str, str, dict]]:
    """(table, id_column, extra_where) for exactly the prefix's outputs."""
    plan: list[tuple[str, str, dict]] = []
    seen: set[tuple[str, str]] = set()
    for name in clone_step_names:
        step = STEP_BY_NAME.get(name)
        if not step:
            continue
        for table, extra in step.outputs:
            if table in _CLONE_SPECIAL:
                continue
            id_col = _TABLE_ID_COL.get(table)
            if not id_col:
                logger.debug(f"[F11] No id column known for {table} — skipped")
                continue
            key = (table, json.dumps(extra or {}, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            plan.append((table, id_col, dict(extra or {})))
    return plan


def branch_run(source_run_id: str, branch_after_step: str,
               new_problem: str = None, previous_run_id: str = None) -> str:
    """
    Clone a run's state up to and including branch_after_step into a new run.

    The new run's steps up to and including branch_after_step are marked
    'done'; everything after is 'pending'. The original run is untouched.

    Args:
        source_run_id:     the run to branch from
        branch_after_step: the last step to clone (must be done in the source)
        new_problem:       optional new problem statement (defaults to the
                           source run's problem)
        previous_run_id:   optional previous_run_id for the new run (F5)

    Returns:
        The new run_id.
    """
    import json

    if branch_after_step not in STEP_BY_NAME:
        raise ValueError(f"Unknown step: {branch_after_step}")

    source_state = get_state(source_run_id)
    if not source_state.get("exists"):
        raise ValueError(f"Source run {source_run_id} not found")

    # The branch point must be done — you can't branch from a step that
    # hasn't produced output yet.
    step_statuses = {s["step_name"]: s["status"] for s in source_state["steps"]}
    if step_statuses.get(branch_after_step) not in ("done", "skipped"):
        raise ValueError(
            f"Step {branch_after_step} is not done "
            f"(status: {step_statuses.get(branch_after_step)}). "
            f"Can only branch from a completed step."
        )

    # Determine which steps to clone (the prefix up to and including the
    # branch point) and which to leave pending.
    branch_ordinal = STEP_ORDER[branch_after_step]
    clone_step_names = [s.name for s in STEP_DEFS[:branch_ordinal + 1]]

    problem = new_problem or source_state.get("problem", "")
    new_run_id = create_run(problem, previous_run_id=previous_run_id)

    logger.info(f"[F11] Branching {source_run_id} after {branch_after_step} "
                f"into {new_run_id}")

    # 1. Clone the outputs of the prefix steps only — never the outputs of
    #    steps this branch is about to re-run (review C4).
    for table, id_col, extra_where in _clone_plan(clone_step_names):
        _clone_table(table, id_col, source_run_id, new_run_id, extra_where)

    # 2. Clone the argument tree. This needs special handling because
    #    parent_node_id references must be remapped to the new node IDs.
    _clone_argument_tree(source_run_id, new_run_id, branch_after_step)

    # 3. Clone break instructions for completed breaks in the prefix.
    _clone_breaks(source_run_id, new_run_id, clone_step_names)

    # 4. Copy model and source overrides.
    model_overrides = get_model_overrides(source_run_id)
    if model_overrides:
        set_model_overrides(new_run_id, model_overrides)
    source_overrides = get_source_overrides(source_run_id)
    if source_overrides:
        set_source_overrides(new_run_id, source_overrides)

    # 5. Mark cloned steps as done in the new run; leave the rest pending.
    _mark_cloned_steps_done(new_run_id, clone_step_names)

    # 6. Copy break{N}_done flags for completed breaks.
    for step_name in clone_step_names:
        step = STEP_BY_NAME[step_name]
        if step.break_num is not None:
            db.update("runs", {f"break{step.break_num}_done": 1},
                      {"run_id": new_run_id})

    logger.info(f"[F11] Branch {new_run_id} ready — "
                f"{len(clone_step_names)} steps cloned, "
                f"resuming from {STEP_DEFS[branch_ordinal + 1].name if branch_ordinal + 1 < len(STEP_DEFS) else 'end'}")
    return new_run_id


def _clone_table(table: str, id_col: str,
                 source_run_id: str, new_run_id: str,
                 extra_where: dict = None) -> int:
    """Copy all rows for a run from source to new, generating new IDs."""
    where = {"run_id": source_run_id}
    if extra_where:
        where.update(extra_where)
    try:
        rows = db.fetch(table, where)
    except Exception as e:
        # Table may not exist yet (e.g. concept_expansions if concept_mapper
        # hasn't been initialized in this test context). Skip gracefully.
        logger.debug(f"[F11] Could not fetch from {table}: {e}")
        return 0
    if not rows:
        return 0
    count = 0
    for r in rows:
        row = dict(r) if not isinstance(r, dict) else r.copy()
        # Generate a new ID for the cloned row
        old_id = row.get(id_col, "")
        row[id_col] = generate_id(id_col[:3].upper())
        row["run_id"] = new_run_id
        try:
            db.insert(table, row)
            count += 1
        except Exception as e:
            logger.warning(f"[F11] Could not clone {table} row {old_id}: {e}")
    return count


def _clone_argument_tree(source_run_id: str, new_run_id: str,
                         branch_after_step: str) -> int:
    """
    Clone argument_tree nodes from source to new run.

    Only nodes from steps up to and including the branch point are cloned
    (filtered by agent_origin). Parent-child relationships are preserved by
    building an old_id → new_id mapping.
    """
    import json

    # Determine which agents' nodes to clone based on the branch point.
    # The argument_tree is written by grounder, social, historian, and gaper.
    # If we're branching after grounder, only clone grounder's nodes.
    # If after social, clone grounder + social, etc.
    branch_ordinal = STEP_ORDER[branch_after_step]
    clone_agents = set()
    for step in STEP_DEFS[:branch_ordinal + 1]:
        if step.name in ("grounder", "social", "historian", "gaper"):
            clone_agents.add(step.name)
    # Root nodes have agent_origin="system" — always clone them (created by
    # grounder, but the column value is "system"). audit_note nodes are
    # created by historian — include them with historian.
    clone_agents.add("system")

    rows = db.query(
        "SELECT * FROM argument_tree WHERE run_id = ? ORDER BY depth, created_at",
        (source_run_id,),
    )
    if not rows:
        return 0

    id_map: dict[str, str] = {}
    count = 0
    for r in rows:
        row = dict(r) if not isinstance(r, dict) else r.copy()
        agent = row.get("agent_origin")
        node_type = row.get("node_type", "")

        # Filter: only clone nodes from agents that have run by the branch point.
        # Root nodes (agent_origin="system" or None) are always cloned.
        if agent and agent not in clone_agents:
            continue
        if node_type == "audit_note" and "historian" not in clone_agents:
            continue

        old_id = row["node_id"]
        new_id = generate_id("TND")
        id_map[old_id] = new_id

        # Remap parent reference
        old_parent = row.get("parent_node_id")
        row["parent_node_id"] = id_map.get(old_parent) if old_parent else None
        row["node_id"] = new_id
        row["run_id"] = new_run_id

        # Remap source_ids references (they point to source_ids in the sources
        # table, which were cloned with new IDs in _clone_table). We need a
        # source ID map for this — build it from the sources we already cloned.
        # This is handled below in a second pass for simplicity.

        try:
            db.insert("argument_tree", row)
            count += 1
        except Exception as e:
            logger.warning(f"[F11] Could not clone tree node {old_id}: {e}")

    # Second pass: remap source_ids in the cloned tree nodes.
    # Build a map of old_source_id → new_source_id from the sources table.
    _remap_tree_source_ids(source_run_id, new_run_id)

    return count


def _remap_tree_source_ids(source_run_id: str, new_run_id: str) -> None:
    """
    After cloning sources and tree nodes, remap the source_ids JSON arrays
    in the new run's tree nodes to point at the cloned source IDs.
    """
    import json

    # Build old → new source ID map by matching on title + doi + type.
    # The _clone_table function generated new IDs, so we can't match by ID;
    # we match by content.
    old_sources = db.fetch("sources", {"run_id": source_run_id})
    new_sources = db.fetch("sources", {"run_id": new_run_id})

    # Build a lookup from (title, doi, type) → new_source_id
    new_lookup = {}
    for s in new_sources:
        key = (s.get("title", ""), s.get("doi", ""), s.get("type", ""))
        new_lookup[key] = s.get("source_id")

    # Build old_source_id → new_source_id map
    id_map = {}
    for s in old_sources:
        key = (s.get("title", ""), s.get("doi", ""), s.get("type", ""))
        new_id = new_lookup.get(key)
        if new_id:
            id_map[s.get("source_id")] = new_id

    if not id_map:
        return

    # Update tree nodes in the new run
    new_nodes = db.query(
        "SELECT node_id, source_ids FROM argument_tree WHERE run_id = ?",
        (new_run_id,),
    )
    for r in new_nodes:
        raw = r.get("source_ids")
        if not raw:
            continue
        try:
            ids = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(ids, list):
            continue
        new_ids = [id_map.get(sid, sid) for sid in ids]
        if new_ids != ids:
            db.execute(
                "UPDATE argument_tree SET source_ids = ? WHERE node_id = ?",
                (json.dumps(new_ids), r["node_id"]),
            )


def _clone_breaks(source_run_id: str, new_run_id: str,
                  clone_step_names: list[str]) -> None:
    """Clone break_instructions for breaks in the cloned prefix."""
    break_nums = []
    for name in clone_step_names:
        step = STEP_BY_NAME.get(name)
        if step and step.break_num is not None:
            break_nums.append(step.break_num)
    if not break_nums:
        return
    for break_num in break_nums:
        try:
            rows = db.fetch("break_instructions", {
                "run_id": source_run_id, "break_num": break_num,
            })
        except Exception as e:
            logger.debug(f"[F11] Could not fetch break_instructions: {e}")
            continue
        for r in rows:
            row = dict(r) if not isinstance(r, dict) else r.copy()
            row["instruction_id"] = generate_id("BIN")
            row["run_id"] = new_run_id
            try:
                db.insert("break_instructions", row)
            except Exception as e:
                logger.warning(f"[F11] Could not clone break {break_num}: {e}")


def _mark_cloned_steps_done(new_run_id: str, clone_step_names: list[str]) -> None:
    """Mark the cloned prefix steps as done in the new run."""
    now = _now()
    for name in clone_step_names:
        step = STEP_BY_NAME.get(name)
        if step and step.kind == "break":
            # Breaks are marked done via break{N}_done flag, not step status.
            # The step itself stays as 'done' to indicate it was completed.
            db.update("run_steps",
                      {"status": "done", "finished_at": now},
                      {"run_id": new_run_id, "step_name": name})
        else:
            db.update("run_steps",
                      {"status": "done", "finished_at": now},
                      {"run_id": new_run_id, "step_name": name})
