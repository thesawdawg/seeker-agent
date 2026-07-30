"""
Step Activity
-------------
What a step is doing right now, so a run that takes twenty minutes is not an
opaque spinner.

A step is coarse ("Grounder"); the interesting detail is which external
service it is talking to — Semantic Scholar, OpenAlex, Consensus, the model
provider — and that is exactly where runs stall or silently lose a source.

Agents call note() without knowing which run or step they belong to; the
driver binds that once per step, the same way core/llm.py binds the run. So
instrumenting a source handler is a one-line call with no plumbing.

    from core import progress
    progress.note("semantic_scholar", "searching", query)

The latest note is written to run_steps.activity and surfaces in
GET /api/runs/{id}/status.
"""

import contextvars
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_current: contextvars.ContextVar = contextvars.ContextVar(
    "seeker_progress", default=None)

ARTIFACTS_DIR = Path(__file__).parent.parent / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

# Append-only history of every note(), independent of run_steps.activity
# (which holds only the latest line per step and is overwritten on each
# call). This is what lets the UI show a scrolling feed with data previews
# instead of a single "current activity" string, and what the verbose log
# artifact is built from.
EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS step_events (
    event_id    {ID} PRIMARY KEY,
    run_id      {ID} NOT NULL,
    seq         {INT} NOT NULL,       -- monotonic per run_id; event_id itself is a random uuid, not orderable
    step_name   {KEY},
    service     {KEY} NOT NULL,
    action      {TEXT},
    detail      {TEXT},
    preview     {LONGTEXT},          -- truncated request/response payload
    created_at  {TEXT} NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_step_events_run ON step_events(run_id, seq);
"""

_events_schema_ready = False
_seq_lock = __import__("threading").Lock()
_seq_counters: dict = {}


def _ensure_events_table() -> None:
    global _events_schema_ready
    if _events_schema_ready:
        return
    from core import db_backend
    db_backend.get_backend().init_schema(EVENTS_SCHEMA)
    _events_schema_ready = True


def _next_seq(run_id: str) -> int:
    """A per-run sequence number, monotonic within this process.

    All note() calls for a given run happen inside the one worker process
    driving that run at any moment, so a process-local counter is enough —
    no DB round trip or cross-process coordination needed for the common
    case. Seeded from the existing max on first use so a resumed run (new
    worker process, same run_id) keeps counting up rather than colliding
    with seq numbers already written by a previous process.
    """
    with _seq_lock:
        if run_id not in _seq_counters:
            from core import database as db
            rows = db.query(
                "SELECT MAX(seq) AS m FROM step_events WHERE run_id = ?", (run_id,))
            _seq_counters[run_id] = (rows[0]["m"] or 0) if rows else 0
        n = _seq_counters[run_id] + 1
        _seq_counters[run_id] = n
        return n


def verbose_log_path(run_id: str) -> Path:
    return ARTIFACTS_DIR / f"{run_id}_verbose_log.md"

# Services a step may report. Names match config.json agent_sources, so the UI
# can line live activity up against the sources a step is configured to use.
SERVICE_LABELS = {
    "openalex":         "OpenAlex",
    "semantic_scholar": "Semantic Scholar",
    "arxiv":            "arXiv",
    "pubmed":           "PubMed",
    "core":             "CORE",
    "philpapers":       "PhilPapers",
    "philarchive":      "PhilArchive",
    "philsci":          "PhilSci",
    "jstor":            "JSTOR",
    "ssrn":             "SSRN",
    "base":             "BASE",
    "hal":              "HAL",
    "eric":             "ERIC",
    "nber":             "NBER",
    "persee":           "Persée",
    "scopus":           "Scopus",
    "primo":            "Primo (Ex Libris)",
    "consensus":        "Consensus",
    "openlibrary":      "Open Library",
    "google_books":     "Google Books",
    "crossref":         "Crossref",
    "web_search":       "Web search",
    "conceptnet":       "ConceptNet",
    "llm":              "Model provider",
    "database":         "Database",
}


def label_for(service: str) -> str:
    return SERVICE_LABELS.get(service, service.replace("_", " ").title())


def bind(run_id: Optional[str], step_name: Optional[str]):
    """Bind subsequent note() calls to a step. Returns a token for release()."""
    return _current.set({"run_id": run_id, "step": step_name} if run_id else None)


def release(token) -> None:
    # RuntimeError covers a token already spent, which is easy to cause with
    # an early return or break inside a try/finally.
    try:
        _current.reset(token)
    except (ValueError, LookupError, RuntimeError):
        _current.set(None)


def context() -> Optional[dict]:
    return _current.get()


def note(service: str, action: str = "", detail: str = "", preview: str = "") -> None:
    """
    Record what the current step is doing, and honour a pending stop.

    Safe to call from anywhere, including outside a run.

    preview — an optional glimpse of the data actually sent or received
    (a query, a handful of result titles, a response snippet). It is kept
    separate from `detail` because detail lands in the single-line activity
    shown at the top of the step, while preview is only surfaced in the full
    event feed and the verbose log artifact, where more room is available.

    This doubles as a cancellation checkpoint, and raises RunCancelled if the
    run has been asked to stop. That is deliberate: note() marks the moment
    before a slow external call, which is exactly where a stop should take
    effect. Agents like Social spend most of a step in source searches rather
    than model calls, so without this a stop would not bite until the step
    finished — hundreds of queries later.

    Recording is still best-effort and never raises on its own account; only
    the cancellation check can interrupt.
    """
    from core import cancellation
    cancellation.check()

    parts = [label_for(service)]
    if action:
        parts.append(action)
    text = " — ".join(parts)
    if detail:
        detail = detail.strip().replace("\n", " ")
        text += f": {detail[:120]}"

    ctx = _current.get()
    if not ctx:
        logger.info(f"[{service}] {action} {detail}".rstrip())
        return

    logger.info(f"[{ctx['step']}] {text}")
    now = datetime.now(timezone.utc).isoformat()
    preview = preview.strip().replace("\r", "") if preview else ""
    try:
        from core import database as db
        db.update(
            "run_steps",
            {"activity": text[:500], "activity_at": now},
            {"run_id": ctx["run_id"], "step_name": ctx["step"]},
        )
    except Exception as e:
        logger.debug(f"[progress] could not record activity: {e}")

    _record_event(ctx["run_id"], ctx["step"], service, action, detail, preview, now)


def _record_event(run_id: str, step_name: Optional[str], service: str, action: str,
                   detail: str, preview: str, at: str) -> None:
    """Append a row to the durable event feed and the verbose log artifact.

    Never raises — this is diagnostic plumbing, not the pipeline itself.
    """
    try:
        _ensure_events_table()
        from core import database as db
        from core.utils import generate_id
        db.insert("step_events", {
            "event_id":   generate_id("evt"),
            "run_id":     run_id,
            "seq":        _next_seq(run_id),
            "step_name":  step_name,
            "service":    service,
            "action":     action,
            "detail":     detail[:2000] if detail else None,
            "preview":    preview[:4000] if preview else None,
            "created_at": at,
        })
    except Exception as e:
        logger.debug(f"[progress] could not record step event: {e}")

    try:
        _append_verbose_log(run_id, step_name, service, action, detail, preview, at)
    except Exception as e:
        logger.debug(f"[progress] could not append verbose log: {e}")


def _append_verbose_log(run_id: str, step_name: Optional[str], service: str, action: str,
                         detail: str, preview: str, at: str) -> None:
    line = [f"- `{at}` **{step_name or '-'}** — {label_for(service)}"]
    if action:
        line.append(f" · {action}")
    if detail:
        line.append(f": {detail}")
    text = "".join(line) + "\n"
    if preview:
        indented = "\n".join(f"  > {p}" for p in preview.splitlines() if p.strip())
        text += indented + "\n"

    path = verbose_log_path(run_id)
    is_new = not path.exists()
    with path.open("a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# Verbose log — {run_id}\n\n")
        f.write(text)


def get_events(run_id: str, since_seq: int = 0, limit: int = 300) -> list[dict]:
    """Events for a run with seq > since_seq, oldest first, capped at limit.

    Used by the status/SSE endpoints to send only what the client hasn't
    seen yet, so a long run doesn't re-transmit its whole history every tick.
    """
    _ensure_events_table()
    from core import database as db
    rows = db.query(
        "SELECT * FROM step_events WHERE run_id = ? AND seq > ? "
        "ORDER BY seq ASC LIMIT ?",
        (run_id, since_seq, limit),
    )
    return rows


def clear() -> None:
    """Wipe the current step's activity — used when a step finishes."""
    ctx = _current.get()
    if not ctx:
        return
    try:
        from core import database as db
        db.update("run_steps", {"activity": None},
                  {"run_id": ctx["run_id"], "step_name": ctx["step"]})
    except Exception:
        pass


def warn(message: str) -> None:
    """
    Record a non-fatal warning on the current step (review E7).

    Agents call this when they have degraded data (JSON parse failure,
    empty LLM response, partial extraction) so the researcher is told the
    Understanding Map is partial rather than seeing nothing. Safe to call
    outside a run — the warning is only logged in that case.
    """
    ctx = _current.get()
    if not ctx:
        logger.warning(f"[progress] {message}")
        return
    try:
        from core import pipeline
        pipeline.record_step_warning(ctx["run_id"], ctx["step"], message)
    except Exception as e:
        logger.debug(f"[progress] could not record warning: {e}")
