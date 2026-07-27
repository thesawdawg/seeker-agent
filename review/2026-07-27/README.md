# SEEKER — Application Review, second pass

**Date:** 2026-07-27
**Scope:** Optimization, reliability, redundancy, workflow efficacy, true value of the research output, security, and UI/UX.
**Method:** Static review of the full tree at `36ae272`, plus runtime verification — the test suite was executed, contextvar propagation was measured empirically, DB round-trips per request were counted, and the shipped `db/pipeline.db` (34 real runs, 7,076 sources) was queried for evidence about output quality.

This pass is independent of `review/` (2026-07-26) and its fix commit `4814e8b`. Where that pass's findings landed, they are noted as landed. Where a fix introduced a new problem, that is called out.

## Contents

| File | Subject |
|---|---|
| `01-correctness-and-reliability.md` | Silent-wrong-answer bugs, concurrency races, state-machine defects |
| `02-security.md` | SSRF, privilege assignment, auth model, repository contents |
| `03-research-efficacy.md` | Whether the pipeline's output is worth what it costs to produce |
| `04-optimization.md` | API calls, DB access, event loop, page loads |
| `05-ux-and-ui.md` | Information the operator needs and cannot see; accessibility |
| `06-engineering-hygiene.md` | CI, dependencies, repository weight, dead code |

Findings are tagged **Severity** (Critical / High / Medium / Low) and **Effort** (S / M / L).

## Top-line summary

The architecture is genuinely good. The provider abstraction, the resumable step machine, the web/worker split, the cooperative-cancellation design with a hard deadline, and the hand-rolled markdown sanitiser are all better than what this class of project usually ships. The test suite is real: **209 passed, 14 skipped, 0 failed** once `cryptography` and `mcp` are correctly installed.

The problems are concentrated in three places.

**1. Three features are silently inert in the deployed path.** `worker.py` binds per-user state with `contextvars` and then runs the pipeline on a *new thread*, which starts with an empty context. Measured, not inferred:

```
main thread:            keys user = 'USR-TEST' | llm run = 'RUN-TEST'
worker-advance-thread   keys user = ''         | llm run = None
social-search-pool      keys user = ''         | llm run = None
```

The consequences are C1 and C2: per-user academic source API keys never reach a source handler (the entire `user_source_credentials` table, its two endpoints and its UI panel do nothing in production), and Social's relevance-rating calls — the highest-volume LLM calls in the pipeline — bypass the user's own provider credentials and model overrides. Both fail silently, and both are covered by tests that pass because the tests call in one thread.

**2. The pipeline discards its own best work at the context boundary.** Every source costs an LLM call to rate; every gap gets a `significance`; every implication gets a `strength`. Then `core/context.py` truncates with unordered slices — `seminal[:25]`, `gaps[:15]`, `implications[:12]` — against a `db.fetch()` helper that has no `ORDER BY` support at all. In the shipped database, 91% of rated sources are `Low`, so an unordered slice of 20 is overwhelmingly Low-relevance material. The Understanding Map is assembled from an arbitrary subset of the evidence, and nothing tells the researcher that 241 of 356 gaps were left out. This is the single largest threat to "true value of the results" (V1).

**3. An unauthenticated SSRF is the front door.** `POST /api/auth/login` takes a `base_url` from an anonymous caller and makes the server fetch it, with distinguishable errors for reachable/unreachable/non-JSON — a working internal port scanner requiring no credentials (S1). Once logged in, the stored URL becomes a destination the worker POSTs prompts to.

Also material: a random user — not the first — can be promoted to admin (S2); branching a run clones output tables it should not, so a branched run starts with a previous run's gaps and proposals already in it (C4); two concurrent enqueues for one run can put two workers on the same run with no lock (C5); and 17.2% of everything ever collected was thrown away by a `HEAD`-request liveness check that treats any timeout as "dead" (V2).

## Priority order

If only five things get done:

| | Finding | Why first |
|---|---|---|
| 1 | **C1 / C2** — contextvars lost across threads | Three shipped features do nothing; failures are silent |
| 2 | **S1** — unauthenticated SSRF via `base_url` | Pre-auth, reachable by anyone who can load the page |
| 3 | **V1** — ranked data truncated by unordered slices | Directly degrades the deliverable the project exists to produce |
| 4 | **V2** — link check discards 17% of sources | Same, and it is invisible to the researcher |
| 5 | **S2** — arbitrary user auto-promoted to admin | Config write access handed out non-deterministically |

## Status

Everything rated Critical or High, plus the cheap Mediums, landed in
[#2](https://github.com/thesawdawg/seeker-agent/pull/2). The four items that
pass deferred there — V3, V6, X2, X4 — landed in the follow-up. Struck-through
rows below are closed; the rest stand.

Still open, in rough order of value: **V4** (a real token budget rather than
item caps), **O3** (converting the remaining `db.fetch` callers to the new
`order_by`/projection support), **X3** (a fuller coverage strip in the run
view), **S3/S4** (decoupling identity from the provider key, and a
`SEEKER_SECRET_KEY` rotation path), and **S6**'s open question of whether
`db/pipeline.db` needs removing from history as well as from the index.

## Full finding index

| ID | Severity | Effort | Finding |
|---|---|---|---|
| C1 | Critical | S | Per-user source API keys never reach handlers (contextvar lost on thread hop) |
| C2 | Critical | S | Social's rating/link pool loses the run binding — wrong provider, no overrides |
| C3 | High | S | Abandoned step thread keeps writing after the step is reset |
| C4 | High | M | `branch_run` clones output tables from steps after the branch point |
| C5 | High | M | `enqueue` dedup is read-then-write; two workers can drive one run |
| C6 | Medium | S | `increment_daily_calls` / `record_source_health` are lost-update races |
| C7 | Medium | S | Worker caches `config.json` at startup; admin edits never reach it |
| C8 | Low | S | `datetime.utcnow()` deprecated; naive timestamp in run IDs |
| S1 | High | M | Unauthenticated SSRF via `POST /api/auth/login` `base_url` |
| S2 | High | S | "First user" admin promotion picks an arbitrary row |
| S3 | Medium | M | Provider key is the password: rotation destroys the account |
| S4 | Medium | S | `SEEKER_SECRET_KEY` rotation orphans every user with no migration path |
| S5 | Medium | S | `/api/agents`, `/api/steps`, `/api/health` unauthenticated |
| S6 | Medium | S | `db/pipeline.db` (16 MB, 34 real runs) is committed to the repository |
| S7 | Low | S | No login throttling; `/break/0/preview` is an unmetered outbound proxy |
| S8 | Low | S | Admin config editor has no size limit, audit trail, or write lock |
| V1 | Critical | M | Context truncation ignores the rankings the pipeline paid to compute |
| V2 | High | M | `HEAD` liveness check silently discarded 17.2% of all sources |
| V3 | High | M | ~~One LLM call per source to produce a rating nothing downstream filters on~~ — **fixed**: batched, ~10x fewer calls |
| V4 | Medium | M | No token budgeting — context is sliced by characters, not tokens. Item caps landed; a real token budget has not |
| V5 | Medium | S | Relevance failures fall back to "Medium" indistinguishably from real ratings |
| V6 | Medium | M | ~~No evidence-coverage report on the final deliverable~~ — **fixed**: `core/provenance.py` |
| O1 | High | M | SSE endpoint does blocking DB I/O inside `async def` — stalls the event loop |
| O2 | Medium | S | `GET /api/runs` is N+1 across every run the user owns |
| O3 | Medium | M | `db.fetch` has no `ORDER BY`/projection; whole tables read to count or filter |
| O4 | Medium | S | `load_config()` re-reads and re-parses 36 KB of JSON per request |
| O5 | Medium | S | Blacklist re-read from the DB on every single source insert |
| O6 | Low | S | `get_state()` walks the step list twice; `queue_depth()` is four queries |
| O7 | Low | S | `progress.note()` writes to the DB on every source call |
| X1 | High | S | Pre-flight lies: `/api/sources/health` reports `stored` keys that are never used |
| X2 | High | M | ~~No cost or duration estimate before committing to a run~~ — **fixed**: `GET /api/runs/estimate` + New Run card |
| X3 | Medium | M | Truncation and discard are invisible in the UI |
| X4 | Medium | M | ~~No ARIA on the tab strips; toasts and progress not announced~~ — **fixed** (the source grid already used wrapping `<label>`; the review overstated that part) |
| X5 | Medium | S | Nothing explains that changing your provider key loses your account |
| X6 | Low | S | Run list is unpaginated and unfiltered |
| X7 | Low | S | ~~No `prefers-reduced-motion`; focus styling only on form fields~~ — **fixed** |
| H1 | High | S | No CI — a green 209-test suite that nothing runs |
| H2 | Medium | S | Test dependencies undeclared outside a `dev` extra; suite fails misleadingly |
| H3 | Medium | S | Artifacts, logs and exports from real runs are committed |
| H4 | Medium | S | Dependencies are floor-pinned only; no lockfile |
| H5 | Low | S | Dead code and stale comments after the M-series refactor |
</content>
</invoke>
