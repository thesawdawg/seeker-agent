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
from typing import Optional

logger = logging.getLogger(__name__)

_current: contextvars.ContextVar = contextvars.ContextVar(
    "seeker_progress", default=None)

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


def note(service: str, action: str = "", detail: str = "") -> None:
    """
    Record what the current step is doing, and honour a pending stop.

    Safe to call from anywhere, including outside a run.

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
    try:
        from core import database as db
        db.update(
            "run_steps",
            {"activity": text[:500], "activity_at": datetime.now(timezone.utc).isoformat()},
            {"run_id": ctx["run_id"], "step_name": ctx["step"]},
        )
    except Exception as e:
        logger.debug(f"[progress] could not record activity: {e}")


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
