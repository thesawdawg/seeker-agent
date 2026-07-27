# 1 — Correctness and reliability

Bugs that produce a wrong answer quietly, rather than an error loudly.

---

## C1 — Per-user source API keys never reach a source handler

**Severity: Critical · Effort: S**

`worker.py:169` binds the run's owner so handlers can use that user's stored Scopus/CORE/Semantic Scholar keys:

```python
from core import keys
keys.set_current_user(owner["user_id"])
```

`keys._current_user` is a `contextvars.ContextVar` (`core/keys.py:26`). Two lines later, `_advance_with_stop_deadline` runs the entire pipeline on a **new thread** (`worker.py:131`), and a new thread begins with an empty context — it does not inherit the parent's. Social then fans out to a `ThreadPoolExecutor` (`agents/social.py:1070`), losing it a second time.

Measured directly:

```
main thread:            keys user = 'USR-TEST'
worker-advance-thread   keys user = ''
social-search-pool      keys user = ''
```

So `keys.get(..., source_id="scopus")` never finds the stored key and always falls through to `os.environ`.

**What this means.** The `user_source_credentials` table, `PUT /api/source-credentials`, `DELETE /api/source-credentials`, the "Source keys" panel in the New Run screen, and the encryption applied to those keys are all dead in the deployed path. A user pastes their Scopus key into the UI, sees it accepted, and the run silently uses the operator's env var or no key at all. This was the entire point of review U2.

`tests/test_source_controls.py::test_keys_module_prefers_user_stored_key` passes because it calls `keys.get()` on the same thread that called `set_current_user`.

**Fix.** Carry the context across the thread hop:

```python
import contextvars
ctx = contextvars.copy_context()
thread = threading.Thread(target=lambda: ctx.run(run_it), daemon=True, ...)
```

and in `_collect_for_theme`, submit `ctx.run(_search_one, ...)` with a context captured on the calling thread. Add a regression test that asserts the value is visible *from inside a worker thread*, since a same-thread test cannot catch this class of bug.

---

## C2 — Social's rating pool loses the run binding: wrong provider, no model overrides

**Severity: Critical · Effort: S**

Same root cause, different victim. `agents/social.py:1120` rates and link-checks every result in a pool:

```python
with ThreadPoolExecutor(max_workers=min(8, len(titled) * 2)) as pool:
    ratings = list(pool.map(_rate, titled))
```

`_rate` → `rate_relevance` → `llm.call(prompt, RATING_SYSTEM, agent_name="social")` (`agents/social.py:915`), which resolves the run from `llm.current_run()` — another ContextVar, also empty in the pool.

The comment above the pool says *"The LLM router holds no per-call state (review O3)"*. It holds no per-**call** state, but it holds per-**run** state, and that is exactly what is lost.

**What this means in the multi-user deployment.** These are the highest-volume LLM calls in the whole pipeline — one per retrieved source, thousands per run. Without the run binding:

- `get_run_providers(run_id)` returns `{}`, so the user's own endpoint and API key are not used. The calls go to whatever `config.json` has globally — the operator's provider, billed to the operator, or nothing.
- Per-agent model overrides set at a break do not apply.
- `_record_usage` skips the row (`core/llm.py:288`), so `/api/runs/{id}/usage` under-reports token spend by the largest single component. The cost panel in the UI is wrong.

And when there is no global fallback provider, `llm.call` raises, `rate_relevance` swallows it (`agents/social.py:922`) and returns `("Medium", "Could not assess relevance")` for every paper. The run completes, the UI shows green, and the relevance signal is uniformly fabricated.

**Fix.** Same `contextvars.copy_context()` treatment. Additionally, `rate_relevance` should not disguise a router failure as a rating — see V5.

---

## C3 — The abandoned step thread keeps writing after its step is reset

**Severity: High · Effort: S**

`_advance_with_stop_deadline` (`worker.py:107-148`) stops *waiting* for a wedged step but cannot stop the step. The comment claims safety:

> It cannot corrupt the run, because the step it was working on has already been reset and its output discarded.

That holds only while the abandoned thread stays blocked. When its socket read finally returns, the thread resumes and continues the agent — inserting sources, tree nodes and health rows into the run it was abandoned from, *after* `finish_cancel` purged that step's outputs. If the user has since resumed, those writes land alongside the fresh attempt's writes. `sources` has no uniqueness constraint on `(run_id, doi)`, so the result is duplicated evidence attributed to one step.

**Fix.** Give the run a generation counter (bump it in `finish_cancel` and `resume`), stamp it into the pipeline context when the thread starts, and have `db.upsert_source` / `TreeBuilder._insert` drop writes carrying a stale generation. Cheaper interim measure: have `progress.note()` — already the cancellation checkpoint on every source call — also abort when the run's step row no longer shows this step as `running`.

---

## C4 — `branch_run` clones output tables from steps after the branch point

**Severity: High · Effort: M**

`_CLONE_TABLES` (`core/pipeline.py:966-976`) lists `sources`, `gaps`, `implications`, `proposals`, `evaluations`, `syntheses`, `directions`, `break_instructions` — and `branch_run` copies **all of them unconditionally** (`core/pipeline.py:1029-1030`), regardless of where the branch point is:

```python
for table, id_col, extra_where in _CLONE_TABLES:
    _clone_table(table, id_col, source_run_id, new_run_id, extra_where)
```

The steps that produced those rows are then correctly marked `pending` (`_mark_cloned_steps_done` only marks the prefix). Nothing purges the cloned rows before those steps re-run.

So branching after `grounder` gives the new run a full set of the *original* run's gaps, implications, proposals, evaluations and syntheses. When Gaper runs, it inserts its own gaps on top. Vision and Theorist then read a context containing both — including proposals evaluated against evidence that no longer exists in this branch. A feature whose selling point is clean comparison between branches produces a branch contaminated with its parent's conclusions.

`break_instructions` is cloned twice — once by `_CLONE_TABLES` (all breaks, including ones after the branch point) and again by `_clone_breaks` (`core/pipeline.py:1221`), which is the version that respects the prefix. The unique constraint makes the second write win for the prefix, but breaks *after* the branch point survive from the first pass.

**Fix.** Make `_CLONE_TABLES` entries carry the producing step name, and clone only tables whose producing step is in `clone_step_names`. Remove `break_instructions` from `_CLONE_TABLES` and leave it to `_clone_breaks`. Add a test that branches after `grounder` and asserts `count(gaps) == 0` in the new run.

---

## C5 — Enqueue deduplication is read-then-write; two workers can drive one run

**Severity: High · Effort: M**

`jobs.enqueue` (`core/jobs.py:93-106`):

```python
for existing in db.fetch("jobs", {"run_id": run_id}):
    if existing.get("status") in ("queued", "running"):
        return existing["job_id"]
job_id = generate_id("JOB")
db.insert("jobs", {...})
```

Two requests interleaving between the check and the insert both create a job — and there is no unique index to stop them. The docstring promises "Idempotent per run", and both `submit_break` and `advance_run` enqueue, so a double-click on "Submit" is enough.

`claim_next` then correctly hands the two jobs to two different workers, and `pipeline.advance()` has **no run-level lock**. Both drivers call `next_step`, both see the same `pending` row, both set it `running`, both execute the agent. The README advertises `--scale worker=3`.

**Fix.** Two layers. Add `UNIQUE (run_id, status)` — or a partial/generated-column equivalent for MySQL — so a duplicate queued job cannot be inserted, and treat the insert failure as "already queued". Then make step claiming a compare-and-swap: `UPDATE run_steps SET status='running', claimed_by=? WHERE run_id=? AND step_name=? AND status IN ('pending','failed')`, and bail out when zero rows are affected.

---

## C6 — Lost-update races in the counters built for global rate limiting

**Severity: Medium · Effort: S**

`increment_daily_calls` (`core/database.py:1114-1134`) reads the current count, adds `n`, and writes it back. The comment says:

```python
# Read current, then upsert — the UNIQUE constraint makes this safe.
```

A unique constraint prevents duplicate *rows*; it does nothing for a read-modify-write. Two workers incrementing concurrently both read `N` and both write `N+1`. The whole point of review R4 was to make OpenAlex's 100k/day limit hold *across* workers, and the mechanism under-counts precisely when there is more than one worker.

`record_source_health` (`core/database.py:1085-1097`) accumulates `results_returned`, `calls_made` and `retries` the same way, so per-source coverage numbers in the UI drift low under Social's parallel fan-out.

**Fix.** Use an atomic statement — `INSERT ... ON DUPLICATE KEY UPDATE calls = calls + VALUES(calls)` on MySQL, `ON CONFLICT ... DO UPDATE SET calls = calls + excluded.calls` on SQLite. Both backends already have upsert paths in `db_backend.upsert_sql`; this needs an additive variant.

---

## C7 — The worker caches `config.json` at startup, so admin edits never reach it

**Severity: Medium · Effort: S**

`worker.py:240` loads the config once and passes the same dict into every `process_job` call for the process's lifetime. The F12 admin editor writes `config.json` atomically (`core/utils.py:57`) and reports success. The web process re-reads on each request, so the operator sees their change in the UI; the worker keeps using the boot-time copy until it is restarted.

An operator disables a misbehaving source in the Admin tab, sees it greyed out, and every subsequent run keeps querying it.

**Fix.** Reload per job with an mtime check, or have `save_config` bump a row in a `config_version` table that the worker consults before each job.

---

## C8 — Deprecated naive UTC timestamp in run IDs

**Severity: Low · Effort: S**

`core/utils.py:42` uses `datetime.utcnow()`, deprecated since Python 3.12 and scheduled for removal. Everywhere else the codebase correctly uses `datetime.now(timezone.utc)`. Since `requires-python = ">=3.11"` this is a warning today and a break later.

```python
ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
```
</content>
