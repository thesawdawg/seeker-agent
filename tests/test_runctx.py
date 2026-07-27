"""
Run context across threads.

These are the regressions the existing suite could not catch: every test that
exercised per-user keys or per-run routing did so on the calling thread,
where the ContextVars are trivially visible. The pipeline does not run on the
calling thread — worker.py hands it to a `threading.Thread`, and Social hands
source searches and relevance ratings to a `ThreadPoolExecutor`. A new thread
starts with an *empty* Context, so all three bindings were lost and every
consumer silently fell back (review C1, C2).

Each test here asserts from *inside* a worker thread. Asserting on the
calling thread is what let the bug ship.
"""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core import keys, llm, progress, runctx


@pytest.fixture(autouse=True)
def clean_context():
    run_token = llm.set_current_run(None)
    keys.set_current_user("")
    yield
    llm.reset_current_run(run_token)
    keys.clear_current_user()


def _probe():
    return runctx.snapshot()


# ---------------------------------------------------------------------------
# The trap itself — documented so nobody "simplifies" the wrappers away
# ---------------------------------------------------------------------------

def test_a_bare_thread_loses_every_binding():
    """The behaviour the wrappers exist to work around."""
    llm.set_current_run("RUN-BARE")
    keys.set_current_user("USR-BARE")

    seen = {}
    thread = threading.Thread(target=lambda: seen.update(_probe()))
    thread.start()
    thread.join()

    assert seen["run_id"] is None
    assert seen["user_id"] == ""


def test_a_bare_pool_loses_every_binding():
    llm.set_current_run("RUN-BARE")
    keys.set_current_user("USR-BARE")

    with ThreadPoolExecutor(max_workers=1) as pool:
        seen = pool.submit(_probe).result()

    assert seen["run_id"] is None
    assert seen["user_id"] == ""


# ---------------------------------------------------------------------------
# run_in_thread — the worker's thread hop (C1)
# ---------------------------------------------------------------------------

def test_run_in_thread_carries_run_and_user():
    llm.set_current_run("RUN-WORKER")
    keys.set_current_user("USR-WORKER")

    seen = {}
    thread = threading.Thread(
        target=runctx.run_in_thread(lambda: seen.update(_probe())))
    thread.start()
    thread.join()

    assert seen["run_id"] == "RUN-WORKER"
    assert seen["user_id"] == "USR-WORKER"


def test_run_in_thread_carries_the_progress_binding():
    token = progress.bind("RUN-P", "social")
    try:
        seen = {}
        thread = threading.Thread(
            target=runctx.run_in_thread(lambda: seen.update(_probe())))
        thread.start()
        thread.join()
    finally:
        progress.release(token)

    assert seen["step"] == "social"


def test_run_in_thread_propagates_exceptions():
    def boom():
        raise ValueError("from the thread")

    wrapped = runctx.run_in_thread(boom)
    captured = {}

    def target():
        try:
            wrapped()
        except Exception as e:      # noqa: BLE001 — that is the assertion
            captured["error"] = e

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    assert isinstance(captured.get("error"), ValueError)


# ---------------------------------------------------------------------------
# propagate — Social's pools (C2)
# ---------------------------------------------------------------------------

def test_propagate_carries_bindings_into_a_pool():
    llm.set_current_run("RUN-POOL")
    keys.set_current_user("USR-POOL")
    wrapped = runctx.propagate(_probe)

    with ThreadPoolExecutor(max_workers=2) as pool:
        seen = pool.submit(wrapped).result()

    assert seen["run_id"] == "RUN-POOL"
    assert seen["user_id"] == "USR-POOL"


def test_propagate_survives_concurrent_invocation():
    """
    The reason propagate re-binds values instead of reusing copy_context().

    A single contextvars.Context cannot be entered by two threads at once, so
    the obvious `ctx.run` wrapper raises under the concurrency Social
    actually uses.
    """
    llm.set_current_run("RUN-CONCURRENT")
    keys.set_current_user("USR-CONCURRENT")
    wrapped = runctx.propagate(_probe)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: wrapped(), range(64)))

    assert all(r["run_id"] == "RUN-CONCURRENT" for r in results)
    assert all(r["user_id"] == "USR-CONCURRENT" for r in results)


def test_propagate_restores_the_calling_context():
    llm.set_current_run("RUN-OUTER")
    wrapped = runctx.propagate(_probe)
    wrapped()
    assert llm.current_run() == "RUN-OUTER"


def test_propagate_does_not_leak_between_pool_tasks():
    """A reused pool thread must not inherit the previous task's binding."""
    llm.set_current_run("RUN-A")
    bound = runctx.propagate(_probe)

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(bound).result()["run_id"] == "RUN-A"
        # Same thread, an unwrapped call: it must see nothing.
        assert pool.submit(_probe).result()["run_id"] is None


# ---------------------------------------------------------------------------
# The consequence that actually mattered: keys.get() in a pool thread
# ---------------------------------------------------------------------------

def test_stored_source_key_is_visible_from_a_pool_thread(monkeypatch):
    """
    C1 end to end. A source handler calls keys.get() from a pool thread; with
    the binding lost it silently falls through to the env var, so a user's
    stored Scopus key was never used by a run.
    """
    monkeypatch.setattr("core.users.get_source_api_key",
                        lambda user_id, source_id: (
                            "sk-stored" if user_id == "USR-KEYS" else ""))
    monkeypatch.setenv("SCOPUS_API_KEY", "sk-from-env")

    keys.set_current_user("USR-KEYS")
    fetch = runctx.propagate(
        lambda: keys.get("SCOPUS_API_KEY", source_id="scopus"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert pool.submit(fetch).result() == "sk-stored"
        # Unwrapped, the same call gets the env var — the old behaviour.
        assert pool.submit(
            lambda: keys.get("SCOPUS_API_KEY", source_id="scopus")
        ).result() == "sk-from-env"
