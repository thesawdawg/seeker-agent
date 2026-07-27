"""
Job Queue
---------
A jobs table, claimed by worker processes. No broker.

The web app never executes pipeline work — it enqueues a job and returns.
A worker claims the job, advances the run until it parks at a break, fails,
or completes, then releases it. Because a break releases the worker, a run
waiting hours for a human occupies nothing.

Claiming uses SELECT ... FOR UPDATE SKIP LOCKED on MySQL, so several workers
can share the queue safely. SQLite (single-writer) takes an immediate
transaction instead, which is adequate for the single-worker local case.

Redis is used for sessions and caching elsewhere; it deliberately is not the
queue, so a Redis restart cannot lose queued work.
"""

import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Optional

from core import database as db
from core import db_backend
from core.utils import generate_id

logger = logging.getLogger(__name__)


JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       {ID} PRIMARY KEY,
    run_id       {ID} NOT NULL,
    job_type     {KEY} DEFAULT 'advance',
    status       {KEY} DEFAULT 'queued',   -- queued / running / done / failed
    claimed_by   {KEY},
    attempts     {INT} DEFAULT 0,
    max_attempts {INT} DEFAULT 3,
    error        {LONGTEXT},
    created_at   {TEXT} NOT NULL,
    claimed_at   {TEXT},
    heartbeat_at {TEXT},
    finished_at  {TEXT}
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id);
"""

# Set to the run_id while a job is queued or running, and NULL once it
# finishes. A unique index over it is what actually makes "one live job per
# run" true — NULLs do not collide on either backend, so any number of
# finished jobs coexist while a second live one cannot be inserted. Checking
# first and then inserting is not enough on its own: two requests can both
# pass the check before either writes (review C5).
ACTIVE_KEY_INDEX = "idx_jobs_active_run"

_schema_ready = False

# A job whose worker died stays 'running' until it is reaped. Workers beat
# while they hold a job, so absence of a beat means the worker is gone —
# which detects a killed container in minutes rather than half an hour.
STALE_AFTER_MINUTES = 5


def init_jobs_table():
    global _schema_ready
    if _schema_ready:
        return
    db_backend.get_backend().init_schema(JOBS_SCHEMA)
    # Added after jobs first shipped — see db_backend.ensure_columns
    db_backend.ensure_columns("jobs", {"heartbeat_at": "{TEXT}",
                                       "active_key": "{ID}"})
    # An existing deployment may already hold duplicate live jobs for a run;
    # backfill so the unique index can be created rather than failing forever.
    _backfill_active_keys()
    db_backend.ensure_unique_index("jobs", ACTIVE_KEY_INDEX, ["active_key"])
    _schema_ready = True


def _backfill_active_keys() -> None:
    """Give existing live jobs an active_key, keeping only the oldest per run."""
    try:
        rows = db.query(
            "SELECT job_id, run_id, created_at FROM jobs "
            "WHERE status IN ('queued', 'running') ORDER BY created_at")
    except Exception:
        return
    claimed: set[str] = set()
    for row in rows:
        run_id = row.get("run_id")
        if run_id in claimed:
            # A duplicate live job from before the unique index existed.
            db.update("jobs", {"status": "done", "active_key": None,
                               "error": "superseded — duplicate job for run"},
                      {"job_id": row["job_id"]})
            logger.warning(f"[jobs] retired duplicate live job {row['job_id']} "
                           f"for {run_id}")
            continue
        claimed.add(run_id)
        db.update("jobs", {"active_key": run_id}, {"job_id": row["job_id"]})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------

def enqueue(run_id: str, job_type: str = "advance",
            max_attempts: int = 3) -> Optional[str]:
    """
    Queue work for a run.

    Idempotent per run: if a job for this run is already queued or running,
    that one is returned. Polling clients and repeated break submissions
    therefore cannot pile up duplicate work.
    """
    init_jobs_table()

    live = _live_job(run_id)
    if live:
        return live["job_id"]

    job_id = generate_id("JOB")
    # insert_unique, not insert: an upsert on the active_key collision would
    # *replace* the job another process just queued instead of losing to it.
    queued = db.insert_unique("jobs", {
        "job_id":       job_id,
        "run_id":       run_id,
        "job_type":     job_type,
        "status":       "queued",
        "attempts":     0,
        "max_attempts": max_attempts,
        "created_at":   _now(),
        "active_key":   run_id,
    })
    if not queued:
        # Lost the race between the check above and this insert — whoever won
        # queued equivalent work, so return theirs.
        live = _live_job(run_id)
        if live:
            logger.debug(f"[jobs] {run_id} already queued as {live['job_id']}")
            return live["job_id"]
        logger.error(f"[jobs] could not queue work for {run_id}")
        return None
    logger.info(f"[jobs] queued {job_id} for {run_id}")
    return job_id


def _live_job(run_id: str) -> Optional[dict]:
    """The queued or running job for a run, if there is one."""
    for existing in db.fetch("jobs", {"run_id": run_id}):
        if existing.get("status") in ("queued", "running"):
            return existing
    return None


def get_job(job_id: str) -> Optional[dict]:
    init_jobs_table()
    rows = db.fetch("jobs", {"job_id": job_id})
    return rows[0] if rows else None


def jobs_for_run(run_id: str) -> list[dict]:
    init_jobs_table()
    jobs = db.fetch("jobs", {"run_id": run_id})
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return jobs


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------

def claim_next() -> Optional[dict]:
    """
    Atomically take the oldest queued job, or None.

    Two workers must never get the same job, hence the locking read.
    """
    init_jobs_table()
    backend = db_backend.get_backend()
    me = worker_name()

    if backend.name == "mysql":
        return _claim_mysql(me)
    return _claim_sqlite(me)


def _claim_mysql(me: str) -> Optional[dict]:
    with db_backend.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id FROM jobs WHERE status = 'queued' "
                "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
            row = cur.fetchone()
            if not row:
                return None
            job_id = row["job_id"] if isinstance(row, dict) else row[0]
            cur.execute(
                "UPDATE jobs SET status='running', claimed_by=%s, claimed_at=%s, "
                "attempts = attempts + 1 WHERE job_id = %s",
                (me, _now(), job_id),
            )
    return get_job(job_id)


def _claim_sqlite(me: str) -> Optional[dict]:
    # SQLite allows one writer; BEGIN IMMEDIATE takes the write lock up front
    # so two processes cannot both read the same queued row and claim it.
    conn = db_backend.get_backend().connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT job_id FROM jobs WHERE status = 'queued' "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        job_id = row["job_id"]
        conn.execute(
            "UPDATE jobs SET status='running', claimed_by=?, claimed_at=?, "
            "attempts = attempts + 1 WHERE job_id = ?",
            (me, _now(), job_id),
        )
        conn.execute("COMMIT")
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        logger.warning(f"[jobs] claim failed: {e}")
        return None
    finally:
        conn.close()
    return get_job(job_id)


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------

def heartbeat(job_id: str) -> None:
    """
    Signal that this job is still being worked on.

    Called periodically by the worker holding it. Without a recent beat the
    job is considered abandoned and returned to the queue.
    """
    db.update("jobs", {"heartbeat_at": _now()}, {"job_id": job_id})


def finish(job_id: str, status: str = "done", error: str = None) -> None:
    # Clearing active_key is what releases the run for the next enqueue; a
    # finished job must not keep holding the unique slot.
    data = {"status": status, "finished_at": _now(), "active_key": None}
    if error:
        data["error"] = error[:4000]
    db.update("jobs", data, {"job_id": job_id})


def release(job_id: str, error: str = None) -> None:
    """
    Hand a job back to the queue, or fail it once attempts are exhausted.

    Used when a step errors: a transient outage should be retried, but a
    permanently broken run must not be retried forever.
    """
    job = get_job(job_id)
    if not job:
        return
    attempts = job.get("attempts") or 0
    if attempts >= (job.get("max_attempts") or 3):
        finish(job_id, "failed", error or "max attempts exhausted")
        logger.warning(f"[jobs] {job_id} failed after {attempts} attempt(s)")
    else:
        db.update("jobs", {"status": "queued", "claimed_by": None,
                           "error": (error or "")[:4000]},
                  {"job_id": job_id})
        logger.info(f"[jobs] {job_id} requeued (attempt {attempts})")


def reap_stale(minutes: int = STALE_AFTER_MINUTES) -> int:
    """
    Requeue jobs whose worker died mid-run.

    Without this a crashed worker strands its runs permanently, which is the
    failure mode a DB-backed queue is supposed to avoid.
    """
    init_jobs_table()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    # A job is alive if it has beaten recently; fall back to the claim time
    # for jobs claimed before heartbeats existed.
    stale = [j for j in db.fetch("jobs", {"status": "running"})
             if (j.get("heartbeat_at") or j.get("claimed_at") or "") < cutoff]
    for job in stale:
        logger.warning(f"[jobs] reaping stale job {job['job_id']} "
                       f"(claimed by {job.get('claimed_by')})")
        _release_dead_steps(job["run_id"])
        release(job["job_id"], error="worker went away")
    return len(stale)


def _release_dead_steps(run_id: str) -> None:
    """
    Make a dead worker's in-flight step runnable again.

    Step claiming refuses to take a step that is already `running`, because
    that is how two drivers are kept off the same work. The cost of that
    strictness is that a step abandoned by a killed worker would stay
    `running` forever and stall the run — so recovery belongs here, where
    the worker has actually been established as gone.
    """
    from core import pipeline
    for step in pipeline.get_steps(run_id):
        if step["status"] != "running":
            continue
        logger.warning(f"[jobs] releasing {run_id}/{step['step_name']} — "
                       f"its worker went away")
        # Discard whatever the dead attempt wrote; the retry starts clean.
        pipeline.reset_step(run_id, step["step_name"], cascade=False)


def queue_depth() -> dict:
    """Job counts by status — one grouped query, not one COUNT per status.

    /api/health is also the container healthcheck, firing every 30s per
    container, so four round-trips for four numbers was worth collapsing
    (review O6).
    """
    init_jobs_table()
    depth = {status: 0 for status in ("queued", "running", "done", "failed")}
    for row in db.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
        depth[row.get("status") or "unknown"] = int(row.get("n") or 0)
    return depth
