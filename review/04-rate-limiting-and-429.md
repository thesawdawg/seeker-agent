# Area 4 — 429 Control & Keyless-Endpoint Handling

`core/rate_limiter.py` is a reasonable design — per-source min-delay, daily limit, exponential backoff with a 429 floor. But it has four structural problems: (1) `grounder.py` doesn't use it, (2) the module-level singleton is unsafe for concurrent workers, (3) `Retry-After` headers are ignored, and (4) daily limits are tracked per-run instead of globally. The keyless-endpoint handling is mostly correct (handlers skip silently) but is invisible to the user and to pre-flight checks.

## R1 — `grounder.py` bypasses the rate limiter entirely

**Severity: Critical · Effort: M (subsumed by O1)**

`grounder.py:220` — `time.sleep(3.5)` for Semantic Scholar. `grounder.py:293` — `time.sleep(1.0)` for Open Library. `grounder.py:576` — `time.sleep(0.2)` after OpenAlex. None of these go through `get_limiter(run_id)`. Consequences:

- A 429 from Semantic Scholar during Grounder is not retried (no `limiter.backoff`), not logged with the `[RateLimit]` prefix, and not counted against the run's call budget.
- Social's limiter and Grounder's `time.sleep` don't share state, so if Social already exhausted the per-source budget, Grounder still fires.
- The UI's activity log shows Grounder's source calls but the limiter's `print_run_summary` (`rate_limiter.py:186`) undercounts.

**Fix:** O1. Replace `grounder.py`'s `_search_*` functions with calls into `social.SOURCE_HANDLERS`.

## R2 — Module-level `_limiter` singleton is unsafe for `--scale worker=3`

**Severity: Critical · Effort: S**

`rate_limiter.py:206–212`:
```python
_limiter: Optional[RateLimiter] = None

def get_limiter(run_id: str = "") -> RateLimiter:
    global _limiter
    if _limiter is None or (run_id and _limiter.run_id != run_id):
        _limiter = RateLimiter(run_id)
    return _limiter
```
The README (`README.md:171`) advertises `docker compose up -d --scale worker=3` for concurrent runs. With three worker processes this is fine (each process has its own `_limiter`). But within **one** worker process, `pipeline.advance` runs on a single thread today, so this is also fine *for now*. The problem is:

1. If the worker ever runs steps concurrently (e.g. a future `--concurrent-runs` flag, or the ThreadPoolExecutor proposed in O2), two runs in the same process will **clobber each other's limiter** — run B's `get_limiter("RUN-B")` call replaces run A's limiter, and run A's subsequent `wait("openalex")` calls go through run B's limiter with the wrong counters.
2. Even today, `reset_limiter(run_id)` (`social.py:973,1026`) is called at the start of every Social run. If two Social runs overlap in one process, the second reset wipes the first's counters.

**Fix:** key the limiters by `run_id` in a dict, not a single global:
```python
_limiters: dict[str, RateLimiter] = {}
_limiters_lock = Lock()

def get_limiter(run_id: str = "") -> RateLimiter:
    if not run_id:
        run_id = "_default"
    with _limiters_lock:
        if run_id not in _limiters:
            _limiters[run_id] = RateLimiter(run_id)
        return _limiters[run_id]

def reset_limiter(run_id: str = ""):
    with _limiters_lock:
        _limiters.pop(run_id, None)
    return RateLimiter(run_id)
```
Add a `clear_limiter(run_id)` that the worker calls in `finally` (`worker.py:194–198`) alongside `clear_run_providers`/`clear_run_overrides`, so the dict doesn't grow unbounded.

## R3 — `Retry-After` header is ignored on 429

**Severity: High · Effort: S**

`rate_limiter.py:116–128` — `backoff()` computes wait as `BACKOFF_BASE ** attempt` capped at `BACKOFF_MAX`, with a 10s floor for 429. The actual `Retry-After` header (returned by Semantic Scholar, Crossref, OpenAlex, and most well-behaved APIs on 429/503) is never read. If Semantic Scholar says "wait 60 seconds," the limiter waits 10 and immediately gets another 429, burning a retry.

The handlers don't pass the response to `backoff` either — `social.py:56` calls `limiter.backoff(self.SOURCE_ID, attempt, 429)` with no response object.

**Fix:**
- Change `backoff` signature to `backoff(source_id, attempt, status_code=0, retry_after=None)`. If `retry_after` is provided, use it (capped at a configurable `BACKOFF_MAX`, default 300s — a 10-minute Retry-After is legitimate for a quota reset). Otherwise use the existing exponential formula.
- In the handlers, read `resp.headers.get("Retry-After")` before calling `backoff`. It may be seconds (string) or an HTTP date — parse both. `requests` exposes it as `resp.headers["Retry-After"]`.

## R4 — Daily limits are tracked per-run, not globally

**Severity: High · Effort: M**

`rate_limiter.py:107` checks `count >= daily_limit` against `self._call_counts[source_id]`, which is per-`RateLimiter` instance, which is per-`run_id`. OpenAlex's 100k/day is a global limit across all of a user's runs. A user who has run 10 pipelines today and used 95k OpenAlex calls has 5k left — but the 11th run's limiter starts its counter at 0 and happily fires 100k more, getting 429s after 5k with no idea why.

**Fix:** track daily call counts in the DB, not in memory. A `source_call_log` table: `(date, source_id, user_id, calls)`. `wait()` checks today's row before allowing a call; on success, increments it. This survives worker restarts and is shared across workers. The in-memory counter can stay as a fast-path cache (read-through).

For anonymous/keyless sources (OpenAlex mailto, arXiv, PhilArchive), the limit is per-IP, so key by `(date, source_id, user_id="anon")` or by the user's IP if available.

## R5 — Backoff parameters are hardcoded constants, not configurable

**Severity: Medium · Effort: S**

`rate_limiter.py:53–55`:
```python
MAX_RETRIES      = 3
BACKOFF_BASE     = 2.0
BACKOFF_MAX      = 30.0
```
And `rate_limiter.py:123`: `wait = max(wait, 10.0)` for 429. None of these are configurable. A user on a strict API quota wants `BACKOFF_MAX = 120` and `MAX_RETRIES = 5`; a user on a fast local mirror wants `BACKOFF_BASE = 0.5` and `MAX_RETRIES = 1`.

**Fix:** move these into `config.json` under a new `rate_limiting` section, with per-source overrides:
```json
"rate_limiting": {
  "defaults": {"max_retries": 3, "backoff_base": 2.0, "backoff_max": 30.0, "floor_429": 10.0},
  "per_source": {
    "semantic_scholar": {"backoff_max": 120.0, "floor_429": 60.0}
  }
}
```
`RateLimiter.__init__` reads them; `SOURCE_LIMITS` stays as the source registry but the backoff tuning comes from config.

## R6 — `keys.openalex()` is marked `required=True` but OpenAlex doesn't require a key

**Severity: Medium · Effort: S**

`keys.py:62`:
```python
def openalex() -> str:
    return get("OPENALEX_API_KEY", required=True, source_name="OpenAlex (required since Feb 2026+")
```
The comment in `rate_limiter.py:31` says "needs API key Feb 2026+" — but as of the review date (July 2026), OpenAlex still works with just a `mailto` parameter (the handler at `social.py:109–110` does exactly this fallback). `required=True` logs a warning on every run that says "OPENALEX_API_KEY is not set — OpenAlex will not work correctly," which is false. The handler works fine without it.

**Fix:** change to `required=False`. The `mailto` fallback is already correct. Update the `print_key_status` table (`keys.py:104`) to mark OpenAlex as `[optional]` with note "mailto used if absent".

## R7 — `keys.ncbi_email()` is marked `required=True` but PubMed works without it

**Severity: Low · Effort: S**

`keys.py:68` — `ncbi_email()` is `required=True`. NCBI's ToS *asks* for an email but the API works without one (at the lower 3 req/s tier). The `required=True` warning is misleading on first run.

**Fix:** `required=False`, with the note "requested by NCBI ToS, not strictly required."

## R8 — No pre-flight "which sources will actually work" check in the web UI

**Severity: High · Effort: M**

`main.py keys` (the CLI) runs `print_key_status()` (`keys.py:102`) which shows exactly which sources have keys, which are missing, and which are required. The web UI has no equivalent. A user starting a run sees a source list (if U1 lands) but no indication that "Scopus will be skipped — no SCOPUS_API_KEY set" or "Consensus will be skipped — not authenticated."

**Fix:**
- New endpoint `GET /api/sources/health` returning per-source status: `{source: "scopus", enabled: true, has_key: false, reachable: null, note: "SCOPUS_API_KEY not set"}`. For keyless sources, `has_key: true` (n/a) and `reachable: true` (probe with a 1-result test query, cached for 5 min).
- UI: on the New Run screen, render each source with a status icon (✓ ready, ⚠ no key — will skip, ✗ unreachable). On the run's Sources tab (U3), show the same status after the run, with actual result counts.

## R9 — Consensus handler has no rate-limit coordination with the MCP client

**Severity: Medium · Effort: S**

`social.py:707–708` — ConsensusHandler calls `limiter.wait("consensus")` then `search_consensus(query, limit=limit)`. The MCP client (`core/consensus_mcp.py`) makes its own HTTP calls internally. If the MCP client gets a 429 from Consensus's backend, the limiter never hears about it — `search_consensus` raises, the handler catches it (`social.py:716`) and returns `[]`, no backoff is recorded.

**Fix:** have `search_consensus` raise a typed `RateLimitError` (or return a structured result) when it gets a 429 from the Consensus API, and have the handler call `limiter.backoff("consensus", attempt, 429, retry_after=...)` before retrying, the same way the HTTP handlers do.

## R10 — No 429 tracking across runs (quota exhaustion is invisible)

**Severity: Medium · Effort: M**

Related to R4. If run A exhausts Semantic Scholar's anonymous quota (100 req / 5 min), run B starting 30 seconds later will also get 429s on every call, retry 3× per query, fail, and return `[]` for every theme — with no signal that the quota is shared and exhausted. The user sees "Semantic Scholar returned 0 results" and assumes nothing matched.

**Fix:** the global daily-count table from R4 also enables a "quota exhausted" signal. When a source 429s with a `Retry-After` > 30s, mark `(date, source_id, user_id)` as `exhausted_until = now + retry_after` in the DB. `wait()` checks this before making a call and skips immediately with a clear log line: `[RateLimit] semantic_scholar quota exhausted until 14:32 UTC — skipping`. Surface in the UI's source health panel (U3).

## R11 — `SOURCE_LIMITS` registry is missing several configured sources

**Severity: Low · Effort: S**

`rate_limiter.py:29–50` defines limits for 17 sources. `config.json:1109` defines `sources` for 16 sources. `config.json:1186` (`agent_sources`) references sources like `google_books`, `open_library`, `web` that aren't in `SOURCE_LIMITS` — they fall through to the `"default": (1.0, None)` entry. That's safe but not tuned. Google Books allows 100 req/user/sec; Open Library has no documented limit but asks for politeness. The defaults are fine, but the registry should be explicit so the limits are visible and tunable in one place.

**Fix:** add entries for `google_books`, `open_library`, `web_search` to `SOURCE_LIMITS` with appropriate values and notes.
