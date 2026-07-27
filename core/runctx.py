"""
Run Context Across Threads
--------------------------
Three ContextVars carry per-run state:

    core.llm._current_run       which run's providers and model overrides apply
    core.keys._current_user     whose stored source API keys to use
    core.progress._current      which step live activity belongs to

A new thread starts with an **empty** Context. It does not inherit the one
its parent was running in. So anything handed to `threading.Thread` or to a
`ThreadPoolExecutor` silently loses all three: `llm.call()` falls back to
config.json instead of the user's own endpoint, `keys.get()` falls back to
env vars instead of the user's stored key, and `progress.note()` writes
nowhere. Nothing raises — the run just quietly uses the wrong credentials.

Two seams, both capturing on the *calling* thread:

    run_in_thread(fn)   for a single-use `threading.Thread` target
    propagate(fn)       for a callable submitted repeatedly to a pool

`run_in_thread` copies the whole Context, which is exact but single-use — a
Context cannot be entered by two threads at once, nor re-entered while
running. `propagate` therefore captures the three values instead and re-binds
them per invocation, which is safe for concurrent calls and for the thread
reuse a pool does.
"""

import contextvars
import functools
from typing import Callable


def run_in_thread(fn: Callable) -> Callable:
    """
    Wrap a `threading.Thread` target so it keeps the caller's context.

    Single use: the returned callable must be invoked exactly once, because
    it enters one copied Context. For pools, use `propagate`.
    """
    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def entered(*args, **kwargs):
        return ctx.run(fn, *args, **kwargs)

    return entered


def propagate(fn: Callable) -> Callable:
    """
    Wrap a callable submitted to a thread pool so it sees the caller's run.

    Captures the run binding, the credential owner and the live step at wrap
    time, and re-binds them inside every invocation. Safe to call
    concurrently and from reused pool threads, which `contextvars.copy_context`
    is not.
    """
    from core import keys, llm, progress

    run_id = llm.current_run()
    user_id = keys.current_user()
    step_ctx = progress.context()

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        run_token = llm.set_current_run(run_id)
        user_token = keys.set_current_user(user_id)
        prog_token = progress.bind(
            (step_ctx or {}).get("run_id"), (step_ctx or {}).get("step"))
        try:
            return fn(*args, **kwargs)
        finally:
            progress.release(prog_token)
            keys.reset_current_user(user_token)
            llm.reset_current_run(run_token)

    return inner


def snapshot() -> dict:
    """The three values, for logging and for tests that assert propagation."""
    from core import keys, llm, progress
    return {
        "run_id":  llm.current_run(),
        "user_id": keys.current_user(),
        "step":    (progress.context() or {}).get("step"),
    }
