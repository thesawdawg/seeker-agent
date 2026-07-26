"""
Pipeline Worker
---------------
Claims jobs from the database and advances runs. Runs as its own process
(its own container in the Docker stack), so restarting the web app never
interrupts a pipeline, and a crashed worker's jobs are reaped and retried
rather than stranded.

For each job it:
  1. loads the owning user's provider credentials and registers them for
     that run only, so the pipeline calls the user's own endpoint
  2. advances the run until it parks at a break, fails, or completes
  3. releases the job

A break parks the run and frees the worker immediately — a run waiting a
day for a human occupies nothing.

Usage:
  python3 worker.py                 # run until interrupted
  python3 worker.py --once          # drain the queue and exit
  python3 worker.py --interval 2    # seconds between polls
"""

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.keys import _load_env
_load_env()

from core import database as db
from core import jobs, llm, pipeline, users
from core.utils import load_config, setup_logging

logger = logging.getLogger("worker")

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    logger.info(f"Signal {signum} received — finishing current job then stopping")
    _stop = True


def _register_user_providers(run_id: str) -> None:
    """
    Point this run at its owner's provider credentials.

    A run started from the CLI has no owner and simply uses config.json.
    """
    owner = users.run_owner(run_id)
    if not owner:
        return

    configured = users.list_credentials(owner["user_id"])
    overlay = {}
    for cred in configured:
        try:
            cfg = users.provider_config(owner["user_id"], cred["provider"])
        except Exception as e:
            logger.error(f"[{run_id}] Could not load credentials for "
                         f"{cred['provider']}: {e}")
            continue
        if cfg and cfg.configured:
            overlay[cfg.name] = cfg

    if overlay:
        llm.set_run_providers(run_id, overlay)
        logger.info(f"[{run_id}] Using {owner['user_id']}'s providers: "
                    f"{sorted(overlay)}")
    else:
        logger.warning(f"[{run_id}] Owner {owner['user_id']} has no usable "
                       f"provider credentials — falling back to config.json")


HEARTBEAT_SECONDS = 30


def _start_heartbeat(job_id: str) -> threading.Event:
    """
    Beat while this job is held, so a killed worker is detected quickly.

    A step can run for many minutes without returning, so the beat lives on
    its own thread rather than being folded into the pipeline loop.
    """
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                jobs.heartbeat(job_id)
            except Exception as e:
                logger.debug(f"[{job_id}] heartbeat failed: {e}")

    threading.Thread(target=beat, daemon=True).start()
    return stop


def _advance_with_stop_deadline(run_id: str, config: dict):
    """
    Advance the run, but never stay wedged past a stop deadline.

    Cooperative checkpoints handle a stop in the normal case. They cannot help
    when the step is blocked inside a socket read — an unresponsive model
    endpoint or a slow academic API — because no Python runs to notice the
    request. So the step runs on its own thread, and once the grace period
    expires the worker stops waiting for it.

    The abandoned thread is a daemon: it dies when its HTTP call finally times
    out. It cannot corrupt the run, because the step it was working on has
    already been reset and its output discarded.

    Returns the run state, or None if the step was abandoned.
    """
    outcome = {}

    def run_it():
        try:
            outcome["state"] = pipeline.advance(run_id, config=config)
        except BaseException as e:            # reported to the caller below
            outcome["error"] = e

    thread = threading.Thread(target=run_it, daemon=True,
                              name=f"advance-{run_id}")
    thread.start()

    while True:
        thread.join(timeout=2.0)
        if not thread.is_alive():
            break
        if pipeline.enforce_stop_deadline(run_id):
            logger.warning(
                f"[{run_id}] Step did not stop within "
                f"{pipeline.STOP_GRACE_SECONDS}s — abandoning it. The worker is "
                f"free again; the abandoned call ends when it times out.")
            return None

    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("state")


def process_job(job: dict, config: dict) -> None:
    run_id = job["run_id"]
    job_id = job["job_id"]
    logger.info(f"[{job_id}] claimed — advancing {run_id}")

    jobs.heartbeat(job_id)
    stop_beat = _start_heartbeat(job_id)

    try:
        _register_user_providers(run_id)
        # Model choices were made in the web process — load them here
        pipeline.apply_model_overrides(run_id)
        state = _advance_with_stop_deadline(run_id, config)
        if state is None:
            # Abandoned past the stop deadline; the run is already at rest
            jobs.finish(job_id, "done", error="stopped by request (forced)")
            return

        if state["status"] in ("cancelling", "cancelled"):
            # A researcher stopping a run is a normal outcome, not a failure.
            # Requeuing it would immediately restart the work they just stopped.
            logger.info(f"[{job_id}] {run_id} stopped on request")
            jobs.finish(job_id, "done")
            return

        if state["failed_steps"]:
            # Let the queue decide whether this is worth another attempt
            step = state["failed_steps"][0]
            error = next((s.get("error") for s in state["steps"]
                          if s["step_name"] == step), "") or f"step {step} failed"
            jobs.release(job_id, error=f"{step}: {error}")
            return

        if state["awaiting_break"] is not None:
            logger.info(f"[{job_id}] {run_id} parked at break "
                        f"{state['awaiting_break']} — releasing worker")
        elif state["complete"]:
            logger.info(f"[{job_id}] {run_id} complete")

        jobs.finish(job_id, "done")

    except Exception as e:
        logger.error(f"[{job_id}] crashed: {e}", exc_info=True)
        jobs.release(job_id, error=str(e))
    finally:
        stop_beat.set()
        # Credentials are per-run and must not outlive the job in memory
        llm.clear_run_providers(run_id)
        llm.clear_run_overrides(run_id)


def main():
    parser = argparse.ArgumentParser(description="SEEKER pipeline worker")
    parser.add_argument("--once", action="store_true",
                        help="Drain the queue and exit")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Seconds between polls when the queue is empty")
    parser.add_argument("--reap-every", type=float, default=300.0,
                        help="Seconds between stale-job sweeps")
    args = parser.parse_args()

    setup_logging("worker")
    logging.getLogger().setLevel(logging.INFO)

    db.init_db()
    jobs.init_jobs_table()
    users.init_users_tables()
    users.init_run_owners()
    config = load_config()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info(f"Worker {jobs.worker_name()} started — storage: {db.backend_name()}")
    last_reap = 0.0

    while not _stop:
        now = time.monotonic()
        if now - last_reap > args.reap_every:
            reaped = jobs.reap_stale()
            if reaped:
                logger.info(f"Reaped {reaped} stale job(s)")
            last_reap = now

        job = jobs.claim_next()
        if job:
            process_job(job, config)
            continue

        if args.once:
            logger.info("Queue empty — exiting (--once)")
            break
        time.sleep(args.interval)

    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
