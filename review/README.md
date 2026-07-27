# SEEKER — Application Review

> **A second review pass (2026-07-27) is in [`2026-07-27/`](2026-07-27/README.md).** It covers
> correctness, security, research efficacy and UI/UX, and records which findings from this pass
> landed in `4814e8b`.

**Date:** 2026-07-26
**Scope:** Optimization, redundancy/error handling, user configuration control & UI progress, 429 control across research endpoints (including keyless ones).
**Method:** Static review of `core/`, `agents/`, `web/`, `tools/`, `worker.py`, `main.py`, `config.json`, `.env.example`. Findings are evidence-backed; line numbers refer to the current tree.

This review intentionally does **not** revisit the architecture refactor already tracked in `app-design-output/recommendations.md` (M1–M6, largely landed). It focuses on the four areas you asked about, which that plan did not cover.

## Contents

| File | Subject |
|---|---|
| `01-optimization.md` | Area 1 — API calls, page loads, process/workflow efficiency |
| `02-error-handling-redundancy.md` | Area 2 — Retry logic, graceful degradation after retry failure |
| `03-ui-config-and-progress.md` | Area 3 — Exposed config controls & informative progress |
| `04-rate-limiting-and-429.md` | Area 4 — 429 control across endpoints, keyless-endpoint handling |
| `05-feature-expansion.md` | Recommended feature expansions for UX and research returns |

Each finding is tagged **Severity** (Critical / High / Medium / Low) and **Effort** (S / M / L).

## Top-line summary

The provider abstraction (`core/llm.py`), the resumable state machine, and the worker/web split are solid. The four areas you flagged are the right ones to look at next — they are the seams where the refactor landed fast on top of the original agent code without consolidating it:

1. **Optimization** — `agents/grounder.py` re-implements every source handler that `agents/social.py` already has, bypassing the rate limiter and the retry/backoff machinery. Source searches are fully sequential. The web UI re-fetches the heavy run-detail endpoint on every 2s poll.
2. **Error handling** — `core/llm.py` has good retry, but treats `ConnectionError` as fatal (no retry) and never surfaces degradation to the user: a source that returns `[]` because it was down is indistinguishable from one that returned `[]` because nothing matched. No circuit breaker.
3. **UI config control** — The UI exposes model selection and break directives but **zero** source/config control. `config.json` `agent_sources`, `sources.*.enabled`, per-source API keys, per-agent `max_tokens`/`temperature`, `limit_per_source` are all file edits. There is no pre-flight "these sources will be skipped because you have no keys" view in the web UI.
4. **429 control** — `core/rate_limiter.py` exists and is reasonable, but `grounder.py` does not use it (raw `time.sleep`), the module-level `_limiter` singleton is **not safe for the `--scale worker=3` deployment the README advertises** (concurrent runs clobber each other's limiter), `Retry-After` headers are ignored, and daily limits are tracked per-run rather than globally.

The single highest-impact fix is consolidating `grounder.py`'s duplicate source handlers onto the shared `SourceHandler` base class — it closes the rate-limit bypass, the missing-retry bypass, and the missing-progress-instrumentation gap in one change.
