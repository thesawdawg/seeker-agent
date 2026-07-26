# Area 2 — Redundancy and Error Handling

The system has the *shape* of good retry logic in two places (`core/llm.py` and `core/rate_limiter.py`), but neither is configurable per source/agent, neither surfaces degradation to the user, and the agent-level handlers that bypass them (grounder, historian) have no retry at all. After all retries fail, every code path silently returns `[]` or `None` — the run continues with degraded data and the researcher is never told.

## E1 — `core/llm.py` treats `ConnectionError` as fatal, no retry

**Severity: High · Effort: S**

`llm.py:465–467`:
```python
except requests.ConnectionError as e:
    logger.warning(f"[{agent_name}] {provider.name} unreachable at {provider.base_url}: {e}")
    return None
```
A connection blip — DNS hiccup, the model container restarting, a transient network partition — instantly consumes one of the fallback-chain rungs with no retry, even though `max_retries` is configured for exactly this. `requests.Timeout` *is* retried (`llm.py:457–463`); `ConnectionError` should be too.

**Fix:** treat `ConnectionError` as retryable with the same backoff as Timeout. Only after `max_retries` attempts should the rung be abandoned.

## E2 — `core/llm.py` retry policy is global, not per-agent or per-provider

**Severity: Medium · Effort: M**

`llm.py:80–82` — `timeout_seconds`, `max_retries`, `retry_delay` are read from `llm.defaults` and apply to every agent on every provider. A 300s timeout is right for Grounder's 16k-token synthesis call but wasteful for Social's relevance-rating call (should be ~30s). A 5s retry delay is right for OpenAI but too aggressive for a local Ollama that's loading a 14B model on cold start (should be ~15s).

**Fix:** allow `llm.agents.<name>` and `llm.providers.<name>` to override `timeout_seconds`, `max_retries`, `retry_delay`. `AgentProfile` already carries `max_tokens`/`temperature`; add the three retry fields there with defaults from `LLMSettings`. `_attempt` reads them from the profile instead of `self.settings`.

## E3 — No circuit breaker — a down endpoint is retried for every query

**Severity: High · Effort: M**

When Semantic Scholar is down, the social agent retries 3× per query, fails, returns `[]`, then moves to the next theme and retries 3× again, fails again, returns `[]` again — for every theme × every query. A 5-theme run with 3 queries each wastes 45 failed retries (5 × 3 × 3) against an endpoint that returned 503 on the first call.

**Fix:** add a simple circuit breaker to `RateLimiter` — track consecutive failures per `source_id`, and after N (configurable, default 5) trip the breaker. While tripped, `wait()` raises `SourceUnavailable` (or returns a sentinel) and the handler returns `[]` immediately without making the call. Reset after a cooldown (default 60s). This is the standard pattern and the limiter is the right place for it since it already tracks per-source state.

## E4 — After retry failure, sources silently return `[]` — user is never told a source was skipped

**Severity: Critical · Effort: M**

This is the most important error-handling gap. Every source handler ends the same way:
- `social.py:73` — `_get` returns `None` after 3 retries → handler returns `[]`
- `social.py:255–259` — SemanticScholarHandler: `except Exception → return []`
- `grounder.py:207–209` — `_search_openalex`: `except Exception → return []`
- `historian.py:193–195` — JSON parse fails → `data = {"phases": [], ...}`

The run continues. Downstream agents (grounder, historian, gaper) synthesize from whatever made it through. The Understanding Map at the end might be missing an entire source's worth of evidence and the researcher has no signal that it happened. The activity log shows "Semantic Scholar — searching" but never "Semantic Scholar — failed after 3 retries, 0 results (endpoint returned 503)".

**Fix:**
1. Each handler should return a structured result, not just a list: `{results: [...], status: "ok"|"degraded"|"failed", error: "...", calls: N}`. Or raise a `SourceDegraded` exception that the agent catches and records.
2. The agent should write a `source_health` row per (run_id, source_id) with `status`, `error`, `results_returned`, `calls_made`, `retries`. This already has a natural home: the `sources` table has `source_name` and `run_id`, so a `SELECT source_name, COUNT(*) FROM sources WHERE run_id=? GROUP BY source_name` shows coverage — but only for successful inserts. A `source_health` table makes failures visible too.
3. Surface this in the UI (see `03-ui-config-and-progress.md`, finding U3) and in the Scribe's Understanding Map preamble: "This map was built from OpenAlex (42), arXiv (15), Semantic Scholar (0 — endpoint unreachable, retried 3×)". The researcher needs this to judge confidence.

## E5 — `RateLimiter.wait()` silently no-ops when daily limit is hit

**Severity: Medium · Effort: S**

`rate_limiter.py:107–109`:
```python
if daily_limit and count >= daily_limit:
    logger.warning(f"[RateLimit] Daily limit reached for {source_id}: {daily_limit}")
    return
```
`return` without recording a call. The caller (`SourceHandler._get`, `social.py:50`) proceeds to make the HTTP request anyway — the daily-limit check is advisory, not enforced. The handler has no way to know the limiter said "stop".

**Fix:** have `wait()` return `bool` (or raise `DailyLimitReached`), and have `_get` check it:
```python
if not limiter.wait(self.SOURCE_ID):
    logger.info(f"[{self.SOURCE_ID}] daily limit reached — skipping")
    return None
```

## E6 — Grounder's source handlers have no retry at all

**Severity: High · Effort: S (subsumed by O1)**

`grounder.py:166–410` — every `_search_*` function is a single `try/except Exception → return []`. No retry on 429, no retry on 5xx, no retry on timeout. A single transient failure loses the query.

**Fix:** subsumed by O1 (consolidate onto `SourceHandler`). If O1 is deferred, at minimum wrap each handler in the same 3-retry loop `social.py:SourceHandler._get` uses.

## E7 — JSON parse failures degrade silently to empty data

**Severity: Medium · Effort: S**

`grounder.py:644–665`, `historian.py:188–195`, and similar in other agents: when the LLM returns malformed JSON, the code falls back to `data = {"phases": [], ...}` or `data = {}` and continues. The run completes "successfully" with an empty synthesis. The only signal is a `logger.warning` that the user never sees.

**Fix:** record a `step_warning` on the `run_steps` row (new column) when this happens, and surface it in the UI's live card: "Grounder completed but the model returned unparseable JSON — synthesis is empty. Re-run this step." The existing re-run infrastructure (`/api/runs/{id}/steps/{s}/rerun`) already lets the user retry.

## E8 — No retry on `requests.Timeout` in source handlers uses wrong backoff signature

**Severity: Low · Effort: S**

`social.py:66` — on timeout, the handler calls `limiter.backoff(self.SOURCE_ID, attempt, 0)`. `backoff`'s third arg is `status_code` (`rate_limiter.py:116`), and `0` is not a real status code. The log line ends up `[RateLimit] 0 on {source_id} — backing off Ns` which is misleading. Pass a sentinel like `status_code=None` and have `backoff` log "timeout" instead of the numeric code.

## E9 — `worker.py` releases a failed job back to the queue without retry-count tracking

**Severity: Medium · Effort: M**

`worker.py:175–180` — when a step fails, `jobs.release(job_id, error=...)` puts the job back. The worker loop will re-claim it (`worker.py:234`) and immediately retry the same failed step. There's no max-attempts cap on a job — a permanently broken step (e.g. a model that's been deleted) will be retried forever, every 2s, by every worker.

**Fix:** `jobs.release` should accept an `attempts` count, and `jobs.claim_next` should skip jobs that have exceeded `max_attempts` (configurable, default 3). Mark them as `failed` in the jobs table so the operator can see them.

## E10 — No graceful handling when ALL providers in the chain are exhausted

**Severity: Medium · Effort: S**

`llm.py:520–521` raises `LLMError("All LLM providers failed. Tried: ...")`. The agent catches this (e.g. `grounder.py:640–642`) and re-raises, the step fails, the run parks. This is correct behavior, but the error message that reaches the UI is just `step failed: All LLM providers failed. Tried: open-webui:qwen3:32b, ollama:llama3.2:3b` — no hint that the user can change models and resume. The "Stop and change model" button is on the live card, but only when the run is *running*, not when it's failed.

**Fix:** when a step fails with `LLMError`, the failed-step card in the UI (`app.js:567–576`) should show a "Change models and retry" button alongside "Retry this step", which opens the same model picker as the resume panel. The backend already supports this via `POST /api/runs/{id}/resume` with `model_overrides`.
