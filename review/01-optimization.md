# Area 1 — Optimization of API Calls, Page Loads, and Workflows

## O1 — `grounder.py` duplicates every source handler in `social.py` and bypasses the rate limiter

**Severity: Critical · Effort: M**

`agents/grounder.py:166–410` defines `_search_openalex`, `_search_semantic_scholar`, `_search_google_books`, `_search_open_library`, `_search_web`, `_search_consensus`. Each is a near-copy of the corresponding `SourceHandler` subclass in `agents/social.py` (OpenAlexHandler, SemanticScholarHandler, …) but stripped down:

- **No rate limiter.** `_search_semantic_scholar` does `time.sleep(3.5)` (`grounder.py:220`); the social handler routes through `limiter.wait("semantic_scholar")` which enforces the same 3.5s but also tracks call counts, daily limits, and prints progress. Grounder's calls are invisible to the limiter.
- **No retry/backoff.** Each handler is a single `try/except Exception → return []`. A single 429 or transient 5xx loses the whole query. The social handlers retry 3× with `limiter.backoff()`.
- **No progress instrumentation.** Grounder calls `progress.note(source, "searching", query)` (`grounder.py:574,581,587,…`) so the UI does show activity, but the per-source call counts and the "waiting Ns" countdown that the limiter prints are absent.
- **Two agents hitting the same endpoint in one run don't coordinate.** Social's `RateLimiter` is keyed by `run_id` (`rate_limiter.py:208`), so social and grounder *would* share a counter if grounder used it. Today they don't, so a run can fire Semantic Scholar calls from grounder at 3.5s intervals and then again from social at 3.5s intervals — double the polite rate, no shared backoff when one of them gets a 429.

**Fix:** delete `grounder.py:166–410` and import the `SOURCE_HANDLERS` registry from `social.py`. Grounder's `run()` already filters by `_allowed` from `config.agent_sources.grounder`; it can call `SOURCE_HANDLERS["openalex"].search(query, keywords, limit, run_id=run_id)` directly. The `_process_results` closure in `grounder.py:524` already takes a `source_label` and `evidence_type`, so the shape is compatible.

This one change closes findings **O1, O4 (partial), O5, E2, E4, R1, R2** below.

## O2 — Source searches are strictly sequential within a step

**Severity: High · Effort: M**

Both `social.py:_collect_for_theme` (`social.py:817`) and `grounder.py:run` (`grounder.py:494`) iterate `for source in sources:` and call `handler.search(...)` one at a time. A theme with 8 configured sources at ~3s polite delay each is ~24s of pure waiting per theme, plus the actual request latency. A 5-theme run is ~2 minutes of serial sleep before any LLM work starts.

The rate limiter is the *reason* these can't be naively parallelized — but the limiter is already thread-safe (`rate_limiter.py:78` takes a per-source `Lock`), so a `ThreadPoolExecutor` over the source list is safe *and* still respects per-source min-delays. The only coordination needed is that `progress.note()` and `db.upsert_source()` are called from the right thread; `progress.note` uses a `ContextVar` (`progress.py:29`) which is thread-local by default, so each worker thread needs `progress.bind()` set, or the activity note needs to be collected and reported back to the main thread.

**Fix:** in `_collect_for_theme`, replace the `for src in sources:` loop with `ThreadPoolExecutor(max_workers=min(4, len(sources)))` submitting one task per source. Each task calls `handler.search(...)`, returns `(source_id, results)`. The main thread then runs the relevance-rating + link-check + DB-insert loop (which is cheap and can stay serial). Expected speedup: 2–4× on multi-source themes.

## O3 — `rate_relevance` makes one LLM call per result, serially

**Severity: High · Effort: S**

`social.py:849` calls `rate_relevance(title, abstract, problem, theme_label)` for **every** result returned by every source. A theme that returns 8 results × 8 sources = 64 LLM calls just for relevance rating, each one a round-trip to the model. These are serial and each waits for the previous to complete.

**Fix (pick one):**
- **Batch:** change `RATING_SYSTEM` to accept a list of `{title, abstract}` pairs and return a JSON array of verdicts. One LLM call per (source, theme) instead of per result. This is the high-value fix — relevance rating is a classification task, models handle batches of 10–20 trivially.
- **Drop entirely for the first pass:** the rating is only used to populate `relevance_rating`/`relevance_reason` columns. Downstream agents (grounder, historian) don't read these fields — they re-synthesize from the raw sources. The rating could be deferred to an optional post-run tool, like `tools/eval_references.py` already is.
- **Parallelize:** if kept per-result, run the calls through a `ThreadPoolExecutor` (the LLM router is thread-safe; it holds no per-call state).

## O4 — `_check_link` runs a synchronous HEAD request per source, inside the collection loop

**Severity: Medium · Effort: S**

`social.py:857` calls `handler._check_link(r.get("active_link", ""))` for every result, immediately after the search returns. This is a `requests.head(url, timeout=10)` (`social.py:80`) — up to 10s per dead link, serially. A batch of 64 results where 10% have slow/dead URLs adds ~minute of blocking inside the step.

**Fix:** batch link checks into a `ThreadPoolExecutor` after the search loop, before the DB insert loop. Or defer link-checking to the existing `recheck_links()` background pass (`social.py:903`) and insert sources with `link_status="unverified"` — the dead-link archive logic at `social.py:879` already handles dead links later.

## O5 — Web UI re-fetches the heavy `/api/runs/{id}` endpoint on every poll

**Severity: Medium · Effort: S**

`app.js:856` — `renderOverview(status)` calls `api(\`/api/runs/${state.runId}\`)` on every poll tick to refresh the stat grid (sources/gaps/implications counts). The poll runs every 2s while work is moving (`POLL_ACTIVE_MS = 2000`, `app.js:18`). `/api/runs/{id}` (`web/app.py:297`) calls `pipeline.get_state` *plus* `db.count` for **7 tables** (`web/app.py:303–307`) on every call. For a 20-minute run that's ~600 × 7 = 4,200 count queries against the DB for a panel that changes a few times per step.

**Fix:** the counts only change when a step completes, so fetch them when `status.progress.done` changes (compare against `previous.progress.done` in `refreshStatus`, `app.js:424`) rather than on every tick. Alternatively, add `counts` to the lightweight `/api/runs/{id}/status` response, computed only when `done` has advanced since the last status poll (cache the last `done` count in the run row).

## O6 — `GET /api/runs` does N+1 state lookups

**Severity: Low · Effort: S**

`web/app.py:254–272` — `list_runs` calls `pipeline.get_state(run_id)` inside the loop for every run the user owns. `get_state` is not a single query; it builds the full step-state object. For a user with 50 runs this is 50 state-builds on every page load.

**Fix:** `list_runs` only uses `progress.done`, `progress.total`, and `awaiting_break` from the state — all of which are derivable from `runs.status` + a single `SELECT run_id, COUNT(*) ... GROUP BY run_id` over `run_steps`. One query instead of N.

## O7 — `philarchive`/`philsci` OAI-PMH handlers fetch the entire archive and filter client-side

**Severity: Medium · Effort: M**

`social.py:378` and `social.py:443` call `ListRecords` with no `set` or `from`/`until` filters, then iterate every record in the response checking `if any(t in searchable for t in query_terms)`. PhilArchive has ~115k records; PhilSci-Archive has ~30k. The handlers don't even page through `resumptionToken` — they get the first batch (typically 100–500 records) and stop, so they're both slow *and* incomplete.

**Fix:** OAI-PMH doesn't support full-text search, so the correct pattern is to (a) cache the full `ListRecords` dump once per day per archive into a local SQLite table, (b) run the query filter against the local cache. This is the same pattern `concept_mapper` uses for ConceptNet. Until that lands, at minimum implement `resumptionToken` paging so the handler actually searches the whole archive instead of the first page.

## O8 — `agents/historian.py` re-implements `_verify_link` (third copy)

**Severity: Low · Effort: S**

`historian.py:97` is a byte-for-byte copy of `grounder.py:417` and `social.py:75`. Three copies of the same 6-line function.

**Fix:** move to `core/utils.py` as `verify_link(url) -> str`, import everywhere.

## O9 — `core/references.py` uses `httpx.Client` correctly but doesn't share the rate limiter

**Severity: Low · Effort: S**

`references.py:508` opens an `httpx.Client` and calls Crossref/OpenAlex with a fixed `time.sleep(rate_limit_delay)` (`references.py:527,535`). This is separate from `core/rate_limiter.py`, so a Scribe step verifying 200 references against Crossref doesn't coordinate with a concurrent Social step hitting Crossref (if `crossref` were enabled in `agent_sources`).

**Fix:** route `references.py` through `get_limiter(run_id).wait("crossref")` / `.wait("openalex")` before each call, and `.backoff()` on 429/5xx. The limiter is transport-agnostic — it just tracks timestamps.

## O10 — `_warned_missing_model` is a module-level set that never resets

**Severity: Low · Effort: S**

`llm.py:173` — `_warned_missing_model: set = set()` is process-global. In a long-running worker, once provider X has been warned about role Y, the warning never fires again even if the operator fixes and re-breaks the config. Not a correctness issue, but it makes debugging "why is this provider being skipped?" harder after the first occurrence.

**Fix:** key it per `LLMClient` instance (`self._warned_missing_model = set()` in `__init__`), or clear it in `reset_client()`.
