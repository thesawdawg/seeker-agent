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
    db_backend.ensure_columns("jobs", {"heartbeat_at": "{TEXT}"})
    _schema_ready = True


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

    for existing in db.fetch("jobs", {"run_id": run_id}):
        if existing.get("status") in ("queued", "running"):
            return existing["job_id"]

    job_id = generate_id("JOB")
    db.insert("jobs", {
        "job_id":       job_id,
        "run_id":       run_id,
        "job_type":     job_type,
        "status":       "queued",
        "attempts":     0,
        "max_attempts": max_attempts,
        "created_at":   _now(),
    })
    logger.info(f"[jobs] queued {job_id} for {run_id}")
    return job_id


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
    data = {"status": status, "finished_at": _now()}
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
        release(job["job_id"], error="worker went away")
    return len(stale)


def queue_depth() -> dict:
    init_jobs_table()
    return {status: db.count("jobs", {"status": status})
            for status in ("queued", "running", "done", "failed")}
