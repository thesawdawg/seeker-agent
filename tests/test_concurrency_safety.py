"""
Concurrency: the job queue, step claiming, and shared counters.

The README advertises `--scale worker=3`, which makes these the paths where
two processes meet. Each test below drives the real contention rather than
asserting the single-threaded happy path, because that is where the
read-then-write pairs looked correct (review C5, C6).
"""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core import database as db, db_backend, jobs, pipeline


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pipeline.db")
    db_backend.reset_backend()
    pipeline._schema_ready = False
    jobs._schema_ready = False
    db.init_db()
    jobs.init_jobs_table()
    yield
    # Symmetrical teardown: these flags are module-level, so leaving one set
    # means the *next* test's fresh database never gets its tables.
    db_backend.reset_backend()
    pipeline._schema_ready = False
    jobs._schema_ready = False


# ---------------------------------------------------------------------------
# One live job per run (C5)
# ---------------------------------------------------------------------------

def test_enqueue_is_idempotent(store):
    run_id = pipeline.create_run("A problem statement")
    assert jobs.enqueue(run_id) == jobs.enqueue(run_id)


def test_concurrent_enqueue_yields_exactly_one_live_job(store):
    """
    The check-then-insert pair could be interleaved by two requests — a
    double-clicked Submit was enough — and both jobs were then claimed by
    different workers, which drove the same run twice.
    """
    run_id = pipeline.create_run("A problem statement")
    barrier = threading.Barrier(8)

    def queue_it():
        barrier.wait()
        return jobs.enqueue(run_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: queue_it(), range(8)))

    live = [j for j in db.fetch("jobs", {"run_id": run_id})
            if j["status"] in ("queued", "running")]
    assert len(live) == 1
    assert set(ids) == {live[0]["job_id"]}


def test_a_finished_job_frees_the_run_for_the_next_one(store):
    run_id = pipeline.create_run("A problem statement")
    first = jobs.enqueue(run_id)
    jobs.finish(first, "done")
    second = jobs.enqueue(run_id)
    assert second and second != first


def test_a_requeued_job_still_holds_the_slot(store):
    """A job handed back to the queue is still live; it must not double up."""
    run_id = pipeline.create_run("A problem statement")
    first = jobs.enqueue(run_id)
    jobs.release(first, error="transient")
    assert jobs.enqueue(run_id) == first


# ---------------------------------------------------------------------------
# One driver per step (C5)
# ---------------------------------------------------------------------------

def test_only_one_caller_can_claim_a_step(store):
    run_id = pipeline.create_run("A problem statement")
    pipeline.ensure_steps(run_id)
    barrier = threading.Barrier(8)

    def claim():
        barrier.wait()
        return pipeline.claim_step(run_id, "concept_mapper")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: claim(), range(8)))

    assert sum(1 for r in results if r) == 1


def test_claiming_a_done_step_fails(store):
    run_id = pipeline.create_run("A problem statement")
    pipeline.ensure_steps(run_id)
    pipeline.set_step_status(run_id, "concept_mapper", "done")
    assert pipeline.claim_step(run_id, "concept_mapper") is False


def test_a_failed_step_can_be_reclaimed_on_resume(store):
    run_id = pipeline.create_run("A problem statement")
    pipeline.ensure_steps(run_id)
    pipeline.set_step_status(run_id, "grounder", "failed", error="boom")
    assert pipeline.claim_step(run_id, "grounder") is True
    assert pipeline.get_step(run_id, "grounder")["error"] is None


# ---------------------------------------------------------------------------
# Shared counters (C6)
# ---------------------------------------------------------------------------

def test_daily_call_count_does_not_lose_concurrent_increments(store):
    """
    The old read-then-write pair dropped increments under exactly the
    concurrency the global daily limit exists to police.
    """
    barrier = threading.Barrier(16)

    def bump():
        barrier.wait()
        db.increment_daily_calls("openalex", "USR-1")

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: bump(), range(16)))

    assert db.daily_call_count("openalex", "USR-1") == 16


def test_daily_call_counts_are_isolated_per_user_and_source(store):
    db.increment_daily_calls("openalex", "USR-1", n=3)
    db.increment_daily_calls("openalex", "USR-2", n=5)
    db.increment_daily_calls("arxiv", "USR-1", n=7)
    assert db.daily_call_count("openalex", "USR-1") == 3
    assert db.daily_call_count("openalex", "USR-2") == 5
    assert db.daily_call_count("arxiv", "USR-1") == 7


def test_source_health_tallies_accumulate_under_concurrency(store):
    run_id = pipeline.create_run("A problem statement")
    barrier = threading.Barrier(12)

    def report():
        barrier.wait()
        db.record_source_health(run_id, "openalex", "social", "ok",
                                results_returned=2, calls_made=1)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: report(), range(12)))

    row = db.get_source_health(run_id)[0]
    assert row["calls_made"] == 12
    assert row["results_returned"] == 24


def test_source_health_never_downgrades_a_failure(store):
    run_id = pipeline.create_run("A problem statement")
    db.record_source_health(run_id, "core", "social", "failed",
                            last_error="connection reset")
    db.record_source_health(run_id, "core", "social", "ok", results_returned=3)
    row = db.get_source_health(run_id)[0]
    assert row["status"] == "failed"
    assert row["last_error"] == "connection reset"
    assert row["results_returned"] == 3


def test_source_health_upgrades_to_a_worse_status(store):
    run_id = pipeline.create_run("A problem statement")
    db.record_source_health(run_id, "core", "social", "ok", results_returned=1)
    db.record_source_health(run_id, "core", "social", "failed",
                            last_error="timeout")
    assert db.get_source_health(run_id)[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# order_by is not an injection seam (O3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "created_at; DROP TABLE users",
    "created_at, (SELECT 1)",
    "1=1--",
    "created_at DESC; DELETE FROM runs",
])
def test_unsafe_order_by_is_rejected(store, bad):
    with pytest.raises(ValueError):
        db.order_clause(bad)


@pytest.mark.parametrize("good", ["created_at", "created_at DESC",
                                  "status ASC, created_at DESC"])
def test_valid_order_by_is_accepted(store, good):
    assert db.order_clause(good).startswith(" ORDER BY ")


def test_a_running_step_cannot_be_claimed(store):
    """
    The strictness that makes claim_step worth having: a step held by a live
    driver is off limits, even to a second driver that got a duplicate job.
    """
    run_id = pipeline.create_run("A problem statement")
    pipeline.ensure_steps(run_id)
    assert pipeline.claim_step(run_id, "concept_mapper") is True
    assert pipeline.claim_step(run_id, "concept_mapper") is False


def test_reaping_a_dead_worker_frees_its_step(store):
    """
    The other half of that strictness: a step abandoned by a killed worker
    would stall the run forever, so reaping the job must release the step.
    """
    run_id = pipeline.create_run("A problem statement")
    pipeline.ensure_steps(run_id)
    job_id = jobs.enqueue(run_id)
    db.update("jobs", {"status": "running", "claimed_by": "dead-worker",
                       "claimed_at": "2000-01-01T00:00:00+00:00",
                       "heartbeat_at": "2000-01-01T00:00:00+00:00"},
              {"job_id": job_id})
    pipeline.claim_step(run_id, "concept_mapper")

    assert jobs.reap_stale() == 1
    assert pipeline.get_step(run_id, "concept_mapper")["status"] == "pending"
    assert pipeline.claim_step(run_id, "concept_mapper") is True


# ---------------------------------------------------------------------------
# Disowning an abandoned driver (C3)
# ---------------------------------------------------------------------------

def test_an_abandoned_driver_stops_writing_once_disowned(store):
    """
    The worker abandons a step that will not stop, but the thread does not
    die — it is sitting in a socket read. When that returns it used to carry
    on running the step, writing into a run whose state had been reset,
    resumed, or handed to another worker. In the test suite the same zombie
    wrote into the *next* test's database (DB_PATH is a late-bound global),
    which showed up as "database is locked" on a brand-new file.
    """
    from core import cancellation

    wrote_after_disown = []
    resumed = threading.Event()
    disowned = threading.Event()
    entered = threading.Event()

    def wedged_step(step_name, run_id_, problem, config):
        from core import progress
        entered.set()
        disowned.wait(5)                 # stand in for a blocked socket read
        # This is the checkpoint every agent hits before an external call.
        progress.note("openalex", "searching")
        wrote_after_disown.append(step_name)   # must never be reached

    import core.pipeline as pl
    original = pl._run_agent_step
    original_mapper = pl._run_concept_mapper
    pl._run_concept_mapper = lambda *a: None
    try:
        # Get the run past concept_mapper and Break 0, so the next advance
        # actually enters an agent step rather than parking at the break.
        run_id = pipeline.create_run("A problem statement")
        pipeline.advance(run_id, config={"themes": []})
        pipeline.submit_break(run_id, 0, "CONFIRMED")
        pl._run_agent_step = wedged_step

        def drive():
            try:
                pipeline.advance(run_id, config={"themes": []})
            except Exception:
                pass
            resumed.set()

        thread = threading.Thread(target=drive, daemon=True)
        thread.start()
        assert entered.wait(10), "the driver never reached the agent step"

        # The worker gives up on it and disowns the thread.
        cancellation.new_epoch(run_id)
        disowned.set()
        resumed.wait(10)
    finally:
        pl._run_agent_step = original
        pl._run_concept_mapper = original_mapper

    assert wrote_after_disown == [], \
        "a disowned driver kept running the step past its checkpoint"


def test_a_live_driver_is_not_disowned_by_its_own_epoch(store):
    """The guard must not fire for the driver that legitimately owns the run."""
    from core import cancellation

    run_id = pipeline.create_run("A problem statement")
    token = cancellation.bind_epoch(run_id)
    try:
        assert cancellation.is_disowned(run_id) is False
        cancellation.new_epoch(run_id)
        assert cancellation.is_disowned(run_id) is True
    finally:
        cancellation.release_epoch(token)


def test_disowned_check_is_inert_outside_a_driver(store):
    """Nothing bound means nothing to disown."""
    from core import cancellation
    assert cancellation.is_disowned("RUN-NOBODY") is False
    cancellation.new_epoch("RUN-NOBODY")
    assert cancellation.is_disowned("RUN-NOBODY") is False
