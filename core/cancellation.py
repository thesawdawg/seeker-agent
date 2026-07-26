"""
Cooperative Cancellation
------------------------
Stopping a run that is mid-step.

A worker cannot be killed safely — it holds a database connection, a claimed
job, and possibly a half-written argument tree. So cancellation is
cooperative: the web process records the request, and the worker notices it at
checkpoints and unwinds cleanly.

Checkpoints sit where waiting actually happens:

  * between pipeline steps
  * before each LLM attempt, which is where a long step spends its time

Without the second one, cancelling during a twenty-minute Grounder step would
not take effect until the step finished — useless when the reason for
cancelling is that the step is using the wrong model.

The status is read through a short-lived cache so a checkpoint costs nothing
when nothing has changed.
"""

import contextvars
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# How stale a cancellation check may be. Small enough to feel immediate,
# large enough that a chatty agent does not hammer the database.
CACHE_TTL_SECONDS = 2.0

CANCELLING = "cancelling"
CANCELLED = "cancelled"

_current: contextvars.ContextVar = contextvars.ContextVar(
    "seeker_cancel_run", default=None)

_cache: dict = {}


class RunCancelled(Exception):
    """Raised at a checkpoint when the run has been asked to stop."""

    def __init__(self, run_id: str):
        super().__init__(f"Run {run_id} was cancelled")
        self.run_id = run_id


def bind(run_id: Optional[str]):
    """Make check() apply to this run. Returns a token for release()."""
    _cache.pop(run_id, None)      # never start from a stale verdict
    return _current.set(run_id)


def release(token) -> None:
    try:
        _current.reset(token)
    except (ValueError, LookupError):
        _current.set(None)


def current_run() -> Optional[str]:
    return _current.get()


def is_cancelling(run_id: str) -> bool:
    """Whether this run has been asked to stop, cached briefly."""
    if not run_id:
        return False

    hit = _cache.get(run_id)
    now = time.monotonic()
    if hit and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]

    try:
        from core import database as db
        run = db.get_run(run_id)
        verdict = bool(run and run.get("status") in (CANCELLING, CANCELLED))
    except Exception as e:
        # A database blip must not manufacture a cancellation
        logger.debug(f"[cancel] could not read status for {run_id}: {e}")
        verdict = False

    _cache[run_id] = (now, verdict)
    return verdict


def check() -> None:
    """
    Raise RunCancelled if the bound run has been asked to stop.

    Call it wherever a long operation can be interrupted safely.
    """
    run_id = _current.get()
    if run_id and is_cancelling(run_id):
        raise RunCancelled(run_id)


def forget(run_id: str) -> None:
    """Drop the cached verdict — used after a resume."""
    _cache.pop(run_id, None)
