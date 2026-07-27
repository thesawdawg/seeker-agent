# 4 — Optimization

Measurements below were taken against the SQLite backend by instrumenting `Backend.connect`.

---

## O1 — The SSE endpoint does blocking DB I/O inside `async def`

**Severity: High · Effort: M**

`run_events` (`web/app.py:479`) is an `async def` route. Its generator loop calls, every second:

```python
await asyncio.sleep(_SSE_POLL_SECONDS)
snap = _run_status_snapshot(run_id)     # ← synchronous DB access
```

`_run_status_snapshot` → `pipeline.get_state()` + `jobs.jobs_for_run()`, all synchronous `sqlite3` / `PyMySQL` calls with no thread offload. FastAPI runs `async def` handlers **on the event loop**, so each snapshot blocks the entire web process — every other request, every other SSE stream — for the duration of those queries.

Measured cost per snapshot on SQLite: **5 connections**, each a fresh `sqlite3.connect` plus `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON`. On MySQL the connection is thread-local and reused, but it is five network round-trips instead.

With *n* viewers watching runs, the loop performs 5*n* blocking queries per second and the stalls compound. This is a direct regression from the F3 change: the old 2-second polling ran through `def` handlers, which Starlette dispatches to a threadpool, so the blocking was contained.

**Fix.** Either make the route `def` and stream synchronously, or wrap the snapshot:

```python
snap = await asyncio.to_thread(_run_status_snapshot, run_id)
```

Then reduce what it costs: `_run_status_snapshot` needs `run_steps` and a job-status existence check — one query each, not the full `get_state` (see O6). Consider a `LISTEN`/pub-sub signal from the worker instead of DB polling, so an idle run costs nothing.

---

## O2 — `GET /api/runs` is N+1 across every run the user owns

**Severity: Medium · Effort: S**

```python
for run_id in users.runs_for_user(user["user_id"]):
    run = db.get_run(run_id)                      # 1 query
    summary = pipeline.get_run_summary(run_id)    # 1 query
```
(`web/app.py:305-309`)

Two queries per run, plus one for the ownership list. The shipped database has 34 runs for a single user, so opening the run list is ~69 queries and 69 SQLite connections. Sorting then happens in Python (`web/app.py:318`).

The `get_run_summary` helper was added by review O6 to avoid building full step objects — a real improvement that left the round-trip count untouched.

**Fix.** Three queries total, regardless of run count:

```sql
SELECT r.* FROM runs r JOIN run_owners o USING (run_id)
WHERE o.user_id = ? ORDER BY r.created_at DESC LIMIT ? OFFSET ?
```

plus one grouped aggregate over `run_steps` for the whole page's `run_id` set. Push the sort and the pagination into SQL (see X6).

---

## O3 — `db.fetch` cannot order, project, or paginate — so callers read whole tables

**Severity: Medium · Effort: M**

The generic helper (`core/database.py:429-445`) supports only equality filters and `LIMIT`. No `ORDER BY`, no column selection, no offset. Thirty-one call sites depend on it, and the missing capabilities show up as Python-side work over full row sets:

| Call site | What it does | What it should do |
|---|---|---|
| `get_llm_usage_summary` (`database.py:902`) | reads every `llm_usage` row, aggregates in Python | `GROUP BY agent_name, provider, model` |
| `is_source_blacklisted` (`database.py:1011`) | reads the user's whole blacklist, matches in Python | indexed lookup on the DOI/URL |
| `mark_previously_seen` (`database.py:592`) | reads all sources, issues one `UPDATE` per match | one `UPDATE ... WHERE (doi, title) IN (...)` |
| `get_source_health` (`database.py:1102`) | reads all rows, sorts in Python | `ORDER BY checked_at DESC` |
| `_maybe_auto_promote_first_user` (`users.py:107`) | reads every user to test for an admin | `WHERE is_admin = 1 LIMIT 1` |
| `jobs_for_run` (`jobs.py:119`) | reads all jobs for a run, sorts in Python | `ORDER BY created_at DESC LIMIT 1` |

The missing `ORDER BY` is also the mechanism behind V1 (arbitrary evidence selection) and S2 (arbitrary admin).

**Fix.** Extend the helper — `fetch(table, where=None, order_by=None, limit=None, offset=None, columns=None)` — and convert the worst offenders. `order_by` must be validated against a column allowlist, since it cannot be parameterised.

---

## O4 — `load_config()` re-reads and re-parses 36 KB of JSON on every request

**Severity: Medium · Effort: S**

`core/utils.py:46` opens and parses `config.json` (36 KB, ~1,100 lines of themes) with no caching. It is called by `/api/steps`, `/api/sources/health`, `/api/config`, `PUT /api/config/sections/*`, `break_payload` on every break fetch, and `pipeline.advance` on every job.

Trivially cacheable, and the invalidation signal already exists — `save_config` is the only writer.

**Fix.** Cache on `(path, st_mtime_ns)`; `save_config` uses `os.replace` so the mtime always changes. This also fixes C7 for the worker, which currently sits at the opposite extreme of never reloading.

---

## O5 — The blacklist is re-read from the database on every source insert

**Severity: Medium · Effort: S**

`upsert_source` (`core/database.py:660`) calls `users.run_owner(run_id)` and then `is_source_blacklisted`, which issues a fresh `SELECT * FROM source_blacklist` — potentially twice, because it falls back to the `anon` list when the user's is empty (`core/database.py:1013-1015`).

So each source insert costs 3–4 extra queries. Social inserts hundreds of sources per run.

**Fix.** Load the owner's blacklist once per run into a `{doi_set, url_patterns, title_patterns}` structure and pass it down, or memoise per `(user_id, run_id)` with a short TTL. The blacklist changes at human speed; re-reading it thousands of times per run buys nothing.

---

## O6 — Redundant work in the two hottest read paths

**Severity: Low · Effort: S**

`get_state()` (`core/pipeline.py:282`) calls `get_steps(run_id)` and then `next_step(run_id)`, which calls `get_steps(run_id)` again — the same query twice per call. Measured: **3 connections per `get_state()`**, one of which is pure duplication.

`queue_depth()` (`core/jobs.py:258`) runs four separate `COUNT(*)` queries — one per status — on every `/api/health`, which is also the container healthcheck firing every 30 seconds per container.

Measured totals: **3 per `get_state`, 4 per `/api/health`, 5 per status poll.**

**Fix.** Pass the already-fetched step list into `next_step`. Replace `queue_depth` with `SELECT status, COUNT(*) FROM jobs GROUP BY status`. Both are a few lines.

---

## O7 — `progress.note()` writes to the database on every source call

**Severity: Low · Effort: S**

`progress.note` issues an `UPDATE run_steps SET activity = ?, activity_at = ?` (`core/progress.py:119`) each time it is called — which is once per source per theme, plus once per LLM attempt via `_note_progress` (`core/llm.py:202`). On SQLite that is a new connection, two PRAGMAs, a write and a commit, on the write-locked path that the worker's real inserts also need.

The value is real — the live activity line is one of the best parts of the UI — but the write rate is higher than the display needs. The SSE stream reads it at 1 Hz; writes arriving faster than that are never seen.

**Fix.** Coalesce: keep the latest note in memory and flush at most once per second per step, always flushing the final note when the step ends. The cancellation check at the top of `note()` must stay unconditional — it is a correctness checkpoint, not display logic.

---

## Landed since the last review

Worth recording, because these were the previous pass's headline items and they are genuinely done:

- **O2 (parallel source search)** — `_collect_for_theme` now fans out across sources with a `ThreadPoolExecutor` while the limiter serialises per-source calls (`agents/social.py:1070`). The right design. (It does have the contextvar defect described in C2.)
- **O8 (Grounder consolidation)** — Grounder's duplicate handlers are gone; it calls the shared `SourceHandler` registry through `_run_shared_source` (`agents/grounder.py:401-450`), so it now gets the limiter, retries and the breaker.
- **E1 (ConnectionError retry)** — `core/llm.py:539` retries connection errors instead of treating them as fatal.
- **R10 (`Retry-After`)** — parsed and honoured, capped at `retry_after_max` (`core/rate_limiter.py:300`).
- **R2 (per-run limiters)** — the module-level singleton is now a keyed dict cleared by the worker (`core/rate_limiter.py:417`).
</content>
