"""
Stopping a run, changing models, resuming.

The reason to stop is almost always that the run is using the wrong model, so
the cancellation has to bite while a step is mid-flight — waiting for the
offending model to finish would defeat the point — and the resume has to
actually apply the new choice.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import core.database as db
    from core import cancellation, db_backend, pipeline

    from core import jobs

    monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pipeline.db")
    db_backend.reset_backend()
    # These flags are module-level, so a fresh database needs them cleared or
    # the tables are never created for it.
    pipeline._schema_ready = False
    jobs._schema_ready = False
    cancellation._cache.clear()
    db.init_db()
    yield db, pipeline
    db_backend.reset_backend()
    pipeline._schema_ready = False
    jobs._schema_ready = False
    cancellation._cache.clear()


@pytest.fixture()
def stub_agents(monkeypatch):
    from core import pipeline
    calls = []
    monkeypatch.setattr(pipeline, "_run_agent_step",
                        lambda n, r, p, c: calls.append(n))
    monkeypatch.setattr(pipeline, "_run_concept_mapper",
                        lambda r, p, c: calls.append("concept_mapper"))
    return calls


def reach_grounder(pipeline, config=None):
    """Start a run and get it to the point where Grounder is next."""
    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config or {"themes": []})
    pipeline.submit_break(run_id, 0, "CONFIRMED")
    return run_id


# ---------------------------------------------------------------------------
# The checkpoint
# ---------------------------------------------------------------------------

def test_check_is_inert_without_a_bound_run():
    from core import cancellation
    cancellation.check()          # must not raise


def test_check_raises_once_the_run_is_cancelling(env):
    from core import cancellation
    db, pipeline = env
    run_id = pipeline.create_run("A problem")

    token = cancellation.bind(run_id)
    try:
        cancellation.check()      # not cancelling yet
        pipeline.request_cancel(run_id)
        cancellation.forget(run_id)
        with pytest.raises(cancellation.RunCancelled):
            cancellation.check()
    finally:
        cancellation.release(token)


def test_a_database_error_does_not_manufacture_a_cancellation(env, monkeypatch):
    """A blip must not stop every running pipeline."""
    from core import cancellation
    db, pipeline = env
    run_id = pipeline.create_run("A problem")

    def boom(*a, **k):
        raise RuntimeError("database gone")

    monkeypatch.setattr(db, "get_run", boom)
    cancellation.forget(run_id)
    assert cancellation.is_cancelling(run_id) is False


# ---------------------------------------------------------------------------
# Stopping mid-step
# ---------------------------------------------------------------------------

def test_stop_interrupts_a_running_step(env, monkeypatch):
    """
    The agent is interrupted at its next LLM call, not after it finishes —
    which is what makes stopping useful when the model is the problem.
    """
    from core import cancellation, llm
    db, pipeline = env
    attempts = []

    def slow_agent(step_name, run_id, problem, config):
        # Stands in for an agent making repeated model calls
        for i in range(10):
            attempts.append(i)
            if i == 2:
                pipeline.request_cancel(run_id)
                cancellation.forget(run_id)
            cancellation.check()

    monkeypatch.setattr(pipeline, "_run_agent_step", slow_agent)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = reach_grounder(pipeline)
    state = pipeline.advance(run_id, config={"themes": []})

    # Iterations 0 and 1 run; iteration 2 requests the stop and is halted by
    # the checkpoint on the same pass. The remaining seven never happen.
    assert attempts == [0, 1, 2], "should stop at the checkpoint, not run all ten"
    assert state["status"] == "cancelled"
    assert state["failed_steps"] == [], "stopping is not a failure"


def test_interrupted_step_is_discarded_not_left_half_done(env, monkeypatch):
    """Resuming on top of a partial step would duplicate its output."""
    db, pipeline = env
    from core import cancellation

    def half_writes_then_stops(step_name, run_id, problem, config):
        db.upsert_source({"source_id": "SRC-PARTIAL", "title": "half written",
                          "type": "seminal", "run_id": run_id})
        pipeline.request_cancel(run_id)
        cancellation.forget(run_id)
        cancellation.check()

    monkeypatch.setattr(pipeline, "_run_agent_step", half_writes_then_stops)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = reach_grounder(pipeline)
    assert db.count("sources", {"run_id": run_id}) == 0
    pipeline.advance(run_id, config={"themes": []})

    assert db.count("sources", {"run_id": run_id}) == 0, "partial output kept"
    assert pipeline.get_step(run_id, "grounder")["status"] == "pending"


def test_completed_steps_survive_a_stop(env, monkeypatch):
    db, pipeline = env
    from core import cancellation

    def stop_at_grounder(step_name, run_id, problem, config):
        if step_name == "grounder":
            pipeline.request_cancel(run_id)
            cancellation.forget(run_id)
            cancellation.check()

    monkeypatch.setattr(pipeline, "_run_agent_step", stop_at_grounder)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = reach_grounder(pipeline)
    pipeline.advance(run_id, config={"themes": []})

    assert pipeline.get_step(run_id, "concept_mapper")["status"] == "done"
    assert pipeline.get_step(run_id, "break0")["status"] == "done"
    assert db.get_break_instructions(run_id, 0) is not None


def test_stop_bites_during_source_searches(env, monkeypatch):
    """
    Regression: checkpoints existed between steps and before model calls, but
    Social spends most of a step querying academic sources over HTTP. A stop
    sat pending through hundreds of queries until the step finished.

    progress.note() runs immediately before each source call, so it doubles as
    the checkpoint.
    """
    from core import cancellation, progress
    db, pipeline = env
    searched = []

    def searches_many_sources(step_name, run_id, problem, config):
        for i in range(50):
            # Exactly how the agents report a source query
            progress.note("openalex", "searching", f"query {i}")
            searched.append(i)
            if i == 3:
                pipeline.request_cancel(run_id)
                cancellation.forget(run_id)

    monkeypatch.setattr(pipeline, "_run_agent_step", searches_many_sources)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = reach_grounder(pipeline)
    state = pipeline.advance(run_id, config={"themes": []})

    assert len(searched) < 10, \
        f"stop should halt the search loop quickly, ran {len(searched)} queries"
    assert state["status"] == "cancelled"


def test_note_outside_a_run_still_never_raises():
    """The checkpoint must not affect CLI or tool use, where nothing is bound."""
    from core import progress
    progress.note("openalex", "searching", "a query")


def test_stop_with_nothing_running_is_immediate(env, stub_agents):
    db, pipeline = env
    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config={"themes": []})   # parks at break 0

    result = pipeline.request_cancel(run_id)
    assert result["immediate"] is True
    assert result["status"] == "cancelled"
    assert db.get_run(run_id)["status"] == "cancelled"


def test_stop_between_steps(env, monkeypatch):
    """A stop requested while idle takes effect before more work starts."""
    db, pipeline = env
    from core import cancellation
    ran = []

    monkeypatch.setattr(pipeline, "_run_agent_step",
                        lambda n, r, p, c: ran.append(n))
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = reach_grounder(pipeline)
    db.update_run_status(run_id, "cancelling")
    cancellation.forget(run_id)

    pipeline.advance(run_id, config={"themes": []})
    assert ran == [], "no step should start once a stop is pending"
    assert db.get_run(run_id)["status"] == "cancelled"


def test_stopping_a_finished_run_is_a_no_op(env, stub_agents):
    db, pipeline = env
    run_id = pipeline.create_run("A problem")
    for _ in range(4):
        state = pipeline.advance(run_id, config={"themes": []})
        if state["complete"]:
            break
        pipeline.submit_break(run_id, state["awaiting_break"], "CONFIRMED")

    result = pipeline.request_cancel(run_id)
    assert result["status"] == "completed"
    assert db.get_run(run_id)["status"] == "completed"


# ---------------------------------------------------------------------------
# The hard deadline
#
# Cooperative checkpoints cannot help when a step is blocked inside a socket
# read, which is exactly when someone wants to stop. A stop must therefore
# complete in bounded time whether or not the worker cooperates.
# ---------------------------------------------------------------------------

def test_stop_records_when_it_was_requested(env, stub_agents):
    db, pipeline = env
    run_id = reach_grounder(pipeline)
    pipeline.set_step_status(run_id, "grounder", "running")

    pipeline.request_cancel(run_id)
    assert db.get_run(run_id)["cancel_requested_at"], "no deadline to measure"
    assert pipeline.cancel_deadline_passed(run_id) is False


def test_deadline_is_not_enforced_early(env, stub_agents):
    db, pipeline = env
    run_id = reach_grounder(pipeline)
    pipeline.set_step_status(run_id, "grounder", "running")
    pipeline.request_cancel(run_id)

    assert pipeline.enforce_stop_deadline(run_id) is False
    assert db.get_run(run_id)["status"] == "cancelling"


def test_overdue_stop_is_forced(env, stub_agents, monkeypatch):
    """A worker wedged in a socket read never notices — force it anyway."""
    from core import jobs
    db, pipeline = env
    jobs.init_jobs_table()

    run_id = reach_grounder(pipeline)
    pipeline.set_step_status(run_id, "grounder", "running")
    job_id = jobs.enqueue(run_id)
    jobs.claim_next()

    pipeline.request_cancel(run_id)
    monkeypatch.setattr(pipeline, "STOP_GRACE_SECONDS", -1)   # deadline passed

    assert pipeline.enforce_stop_deadline(run_id) is True
    assert db.get_run(run_id)["status"] == "cancelled"
    assert pipeline.get_step(run_id, "grounder")["status"] == "pending"
    assert jobs.get_job(job_id)["status"] == "done", \
        "a wedged worker must not keep holding the job"


def test_forcing_is_idempotent(env, stub_agents, monkeypatch):
    db, pipeline = env
    run_id = reach_grounder(pipeline)
    pipeline.set_step_status(run_id, "grounder", "running")
    pipeline.request_cancel(run_id)
    monkeypatch.setattr(pipeline, "STOP_GRACE_SECONDS", -1)

    assert pipeline.enforce_stop_deadline(run_id) is True
    assert pipeline.enforce_stop_deadline(run_id) is False, \
        "already stopped — nothing left to force"


def test_forced_stop_discards_partial_output(env, stub_agents, monkeypatch):
    db, pipeline = env
    run_id = reach_grounder(pipeline)
    pipeline.set_step_status(run_id, "grounder", "running")
    db.upsert_source({"source_id": "SRC-WEDGED", "title": "written before the stop",
                      "type": "seminal", "run_id": run_id})

    pipeline.request_cancel(run_id)
    monkeypatch.setattr(pipeline, "STOP_GRACE_SECONDS", -1)
    pipeline.enforce_stop_deadline(run_id)

    assert db.count("sources", {"run_id": run_id}) == 0


def test_resume_clears_leftovers_from_a_wedged_worker(env, stub_agents):
    """
    A worker abandoned past the deadline can keep writing briefly. The step it
    was running is about to restart, so its output is cleared again on resume.
    """
    db, pipeline = env
    run_id = reach_grounder(pipeline)
    db.update_run_status(run_id, "cancelled")

    # A zombie writes after the forced stop
    db.upsert_source({"source_id": "SRC-LATE", "title": "written by a zombie",
                      "type": "seminal", "run_id": run_id})

    pipeline.resume(run_id)
    assert db.count("sources", {"run_id": run_id}) == 0, \
        "leftovers would otherwise be duplicated by the restarted step"


def test_worker_abandons_a_step_that_will_not_stop(env, monkeypatch):
    """The worker must become available again even if the step never returns."""
    import worker
    from core import jobs
    db, pipeline = env
    jobs.init_jobs_table()

    released = threading_event()

    def never_returns(step_name, run_id, problem, config):
        released.wait(30)          # stands in for a wedged socket read

    monkeypatch.setattr(pipeline, "_run_agent_step", never_returns)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)
    monkeypatch.setattr(worker, "_register_user_providers", lambda r: None)
    monkeypatch.setattr(pipeline, "STOP_GRACE_SECONDS", -1)

    run_id = reach_grounder(pipeline)
    job_id = jobs.enqueue(run_id)
    job = jobs.claim_next()

    # Ask it to stop; the step cannot notice
    import threading as _t
    _t.Timer(0.3, lambda: pipeline.request_cancel(run_id)).start()

    try:
        worker.process_job(job, {"themes": []})
    finally:
        released.set()

    assert db.get_run(run_id)["status"] == "cancelled"
    assert jobs.get_job(job_id)["status"] == "done", "worker freed its job"


def threading_event():
    import threading
    return threading.Event()


# ---------------------------------------------------------------------------
# Resuming with a different model
# ---------------------------------------------------------------------------

def test_resume_applies_the_new_model_to_the_restarted_step(env, monkeypatch):
    """The whole point: the step that was wrong runs again with a new model."""
    from core import cancellation, llm
    db, pipeline = env
    seen = []

    def capture(step_name, run_id, problem, config):
        if step_name == "grounder":
            plan = llm.get_client().describe_plan("grounder")
            seen.append(plan[0]["model"] if plan else None)
            if len(seen) == 1:          # stop on the first attempt only
                pipeline.request_cancel(run_id)
                cancellation.forget(run_id)
                cancellation.check()

    monkeypatch.setattr(pipeline, "_run_agent_step", capture)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = reach_grounder(pipeline)
    llm.set_run_providers(run_id, {"owui": llm.ProviderConfig(
        name="owui", kind="openai", base_url="http://host/api", api_key="k",
        models={"primary": "the-wrong-model"})})
    try:
        pipeline.advance(run_id, config={"themes": []})
        assert seen == ["the-wrong-model"]
        assert db.get_run(run_id)["status"] == "cancelled"

        pipeline.resume(run_id, {"grounder": {"model": "the-right-model"}})
        pipeline.advance(run_id, config={"themes": []})

        assert seen[1] == "the-right-model", "resume did not apply the new model"
    finally:
        llm.clear_run_providers(run_id)
        llm.clear_run_overrides(run_id)


def test_resume_restarts_the_discarded_step(env, monkeypatch):
    db, pipeline = env
    from core import cancellation
    ran = []

    def stop_once(step_name, run_id, problem, config):
        ran.append(step_name)
        if step_name == "grounder" and ran.count("grounder") == 1:
            pipeline.request_cancel(run_id)
            cancellation.forget(run_id)
            cancellation.check()

    monkeypatch.setattr(pipeline, "_run_agent_step", stop_once)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = reach_grounder(pipeline)
    pipeline.advance(run_id, config={"themes": []})

    pipeline.resume(run_id)
    state = pipeline.advance(run_id, config={"themes": []})

    assert ran.count("grounder") == 2, "the discarded step should run again"
    assert state["status"] != "cancelled"
    assert pipeline.get_step(run_id, "grounder")["status"] == "done"


def test_resume_clears_a_stale_running_row(env, stub_agents):
    """A worker killed mid-step leaves 'running'; resume must free it."""
    db, pipeline = env
    run_id = reach_grounder(pipeline)
    pipeline.set_step_status(run_id, "grounder", "running")
    db.update_run_status(run_id, "cancelled")

    pipeline.resume(run_id)
    assert pipeline.get_step(run_id, "grounder")["status"] == "pending"
    assert db.get_run(run_id)["status"] == "active"


def test_resume_persists_overrides_for_another_process(env, stub_agents):
    """The worker is a different process — the choice must go through the DB."""
    from core import llm
    db, pipeline = env
    run_id = reach_grounder(pipeline)
    db.update_run_status(run_id, "cancelled")

    pipeline.resume(run_id, {"historian": {"model": "chosen-on-resume"}})
    llm.clear_run_overrides(run_id)          # simulate a fresh worker

    assert pipeline.get_model_overrides(run_id)["historian"]["model"] \
        == "chosen-on-resume"


def test_run_can_be_stopped_and_resumed_repeatedly(env, monkeypatch):
    db, pipeline = env
    from core import cancellation
    stops = {"n": 0}

    def stop_twice(step_name, run_id, problem, config):
        if step_name == "grounder" and stops["n"] < 2:
            stops["n"] += 1
            pipeline.request_cancel(run_id)
            cancellation.forget(run_id)
            cancellation.check()

    monkeypatch.setattr(pipeline, "_run_agent_step", stop_twice)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = reach_grounder(pipeline)
    for _ in range(2):
        pipeline.advance(run_id, config={"themes": []})
        assert db.get_run(run_id)["status"] == "cancelled"
        pipeline.resume(run_id)

    state = pipeline.advance(run_id, config={"themes": []})
    assert stops["n"] == 2
    assert state["awaiting_break"] == 1, "run continued after the second resume"


# ---------------------------------------------------------------------------
# The worker's view
# ---------------------------------------------------------------------------

def test_a_job_whose_worker_vanished_is_reclaimed(env, monkeypatch):
    """
    Regression: a worker container killed mid-step left its job 'running' and
    the run stuck — a stop request could never take effect because nothing was
    executing to notice it. Workers now beat while holding a job, so the
    absence of a beat marks it abandoned.
    """
    from core import jobs
    db, pipeline = env
    jobs.init_jobs_table()

    run_id = reach_grounder(pipeline)
    job_id = jobs.enqueue(run_id)
    claimed = jobs.claim_next()
    assert claimed["job_id"] == job_id

    jobs.heartbeat(job_id)
    assert jobs.get_job(job_id)["heartbeat_at"], "a held job must beat"
    assert jobs.reap_stale(minutes=5) == 0, "a beating job is not stale"

    # The worker disappears: no further beats
    assert jobs.reap_stale(minutes=0) == 1
    assert jobs.get_job(job_id)["status"] == "queued"
    assert jobs.claim_next() is not None, "another worker can pick it up"


def test_worker_does_not_requeue_a_stopped_run(env, monkeypatch):
    """Requeuing would restart the work the researcher just stopped."""
    from core import cancellation, jobs
    import worker
    db, pipeline = env

    def stop_immediately(step_name, run_id, problem, config):
        pipeline.request_cancel(run_id)
        cancellation.forget(run_id)
        cancellation.check()

    monkeypatch.setattr(pipeline, "_run_agent_step", stop_immediately)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)
    monkeypatch.setattr(worker, "_register_user_providers", lambda r: None)

    run_id = reach_grounder(pipeline)
    jobs.init_jobs_table()
    job_id = jobs.enqueue(run_id)
    job = jobs.claim_next()

    worker.process_job(job, {"themes": []})

    assert jobs.get_job(job_id)["status"] == "done", "a stop is not a failure"
    assert db.get_run(run_id)["status"] == "cancelled"
