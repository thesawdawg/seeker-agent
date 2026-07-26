"""
Rate Limiter
------------
Per-source rate limiting with progress display for all Social agent API calls.

Each source has:
  - min_delay: seconds to wait between calls (from official docs)
  - daily_limit: maximum calls per day (None = unlimited)
  - Exponential backoff on 429 / 5xx, honouring Retry-After when present
  - Circuit breaker: after N consecutive failures a source is short-circuited
    for a cooldown, so a down endpoint is not retried for every query

Progress bar shows:
  - Current source being queried
  - Calls made / remaining for this run
  - Wait countdown when throttling

The module keeps one RateLimiter per run_id in a dict (not a single global),
so concurrent runs in one worker process do not clobber each other's counters.
"""

import time
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Official rate limits per source (from documentation)
# ---------------------------------------------------------------------------

SOURCE_LIMITS = {
    # source_id: (min_delay_seconds, daily_limit, notes)
    "openalex":         (0.15,  100_000, "10 req/sec free; needs API key Feb 2026+"),
    "arxiv":            (3.0,   None,    "Official ToS: 1 req per 3 seconds strictly"),
    "pubmed":           (0.4,   None,    "NCBI: 3 req/sec without API key, 10 with key"),
    "semantic_scholar": (3.5,   None,    "100 req/5 min unauthenticated"),
    "core":             (0.6,   None,    "~2 req/sec on free tier"),
    "philpapers":       (2.0,   None,    "No official docs — conservative"),
    "philarchive":      (2.0,   None,    "PhilPapers open archive — OAI-PMH"),
    "philsci":          (2.0,   None,    "PhilSci-Archive Pittsburgh — OAI-PMH"),
    "scopus":           (1.0,   None,    "Elsevier Scopus — needs institutional IP or VPN"),
    "consensus":        (0.5,   None,    "Consensus semantic search — 200M+ papers"),
    "jstor":            (2.0,   None,    "Conservative — limited API access"),
    "ssrn":             (2.0,   None,    "Conservative"),
    "base":             (1.0,   None,    "BASE search API"),
    "hal":              (0.5,   None,    "HAL open API"),
    "eric":             (0.5,   None,    "IES ERIC API"),
    "nber":             (1.0,   None,    "NBER working papers"),
    "persee":           (1.0,   None,    "Persée French archive"),
    "crossref":         (1.0,   None,    "Crossref polite pool"),
    # Sources referenced by config.agent_sources that previously fell through
    # to the "default" entry — made explicit so the limits are visible/tunable
    # in one place. See review R11.
    "google_books":     (0.1,   None,    "Google Books — ~100 req/user/sec"),
    "open_library":     (1.0,   None,    "Open Library — no documented limit, be polite"),
    "web_search":       (0.0,   None,    "Anthropic server-side web_search tool — provider-side rate limit"),
    "default":          (1.0,   None,    "Unknown source — safe default"),
}


class SourceUnavailable(Exception):
    """Raised by wait() when the circuit breaker is tripped for a source.

    Handlers' existing ``except Exception`` blocks catch this and return ``[]``,
    so a down endpoint is skipped without burning the run's retry budget.
    """


class DailyLimitReached(Exception):
    """Raised by wait() when a source's daily call budget is exhausted.

    Kept distinct from SourceUnavailable so callers can tell "skipped because
    the endpoint is down" from "skipped because we hit the daily quota".
    """


# ---------------------------------------------------------------------------
# Backoff / circuit-breaker tuning
#
# These are defaults; RateLimiter.__init__ accepts overrides so a config
# ``rate_limiting`` section (see review R5) can tune them per source.
# ---------------------------------------------------------------------------

DEFAULT_MAX_RETRIES      = 3
DEFAULT_BACKOFF_BASE     = 2.0    # seconds — doubles each retry
DEFAULT_BACKOFF_MAX      = 30.0   # cap for exponential backoff
DEFAULT_FLOOR_429        = 10.0   # 429 always waits at least this long
DEFAULT_RETRY_AFTER_MAX  = 300.0  # cap for honoured Retry-After header
DEFAULT_BREAKER_FAILS    = 5      # consecutive failures before tripping
DEFAULT_BREAKER_COOLDOWN = 60.0   # seconds the breaker stays tripped


def _parse_retry_after(value: str) -> Optional[float]:
    """Retry-After may be seconds (string) or an HTTP-date. Returns seconds or None."""
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    # HTTP-date form per RFC 7231
    for fmt in ("%a, %d %b %Y %H:%M:%S GMT", "%A, %d-%b-%y %H:%M:%S GMT"):
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(value)
            if dt is not None:
                return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Rate limiter class
# ---------------------------------------------------------------------------

class RateLimiter:
    """
    Thread-safe per-source rate limiter with progress tracking.
    One instance per pipeline run — tracks all calls made.
    """

    def __init__(self, run_id: str = "", *, config: Optional[dict] = None,
                 user_id: str = ""):
        self.run_id = run_id
        self.user_id = user_id
        self._locks: dict[str, Lock] = {}
        self._last_call: dict[str, float] = {}
        self._call_counts: dict[str, int] = {}
        self._total_calls = 0
        self._start_time = time.time()

        # Circuit-breaker state, per source.
        self._breaker_lock = Lock()
        self._consecutive_fails: dict[str, int] = {}
        self._breaker_tripped_until: dict[str, float] = {}

        # Tunable backoff / breaker parameters, optionally from config.
        cfg = (config or {}).get("rate_limiting", {}) if isinstance(config, dict) else {}
        defaults = cfg.get("defaults", {}) if isinstance(cfg, dict) else {}
        self.max_retries     = int(defaults.get("max_retries", DEFAULT_MAX_RETRIES))
        self.backoff_base    = float(defaults.get("backoff_base", DEFAULT_BACKOFF_BASE))
        self.backoff_max     = float(defaults.get("backoff_max", DEFAULT_BACKOFF_MAX))
        self.floor_429       = float(defaults.get("floor_429", DEFAULT_FLOOR_429))
        self.retry_after_max = float(defaults.get("retry_after_max", DEFAULT_RETRY_AFTER_MAX))
        self.breaker_fails   = int(defaults.get("breaker_fails", DEFAULT_BREAKER_FAILS))
        self.breaker_cooldown = float(defaults.get("breaker_cooldown", DEFAULT_BREAKER_COOLDOWN))
        # Per-source overrides: {source_id: {backoff_max, floor_429, ...}}
        self._per_source = cfg.get("per_source", {}) if isinstance(cfg, dict) else {}

    def _tuning(self, source_id: str, key: str, default):
        per = self._per_source.get(source_id, {})
        if isinstance(per, dict) and key in per:
            return per[key]
        return default

    def _get_lock(self, source_id: str) -> Lock:
        if source_id not in self._locks:
            self._locks[source_id] = Lock()
        return self._locks[source_id]

    def _get_config(self, source_id: str) -> tuple[float, Optional[int]]:
        config = SOURCE_LIMITS.get(source_id, SOURCE_LIMITS["default"])
        return config[0], config[1]  # (min_delay, daily_limit)

    def _breaker_is_tripped(self, source_id: str) -> bool:
        with self._breaker_lock:
            until = self._breaker_tripped_until.get(source_id, 0)
            if until and time.time() < until:
                return True
            # Cooldown expired — allow calls again, reset the counter.
            if until:
                self._breaker_tripped_until.pop(source_id, None)
                self._consecutive_fails.pop(source_id, None)
            return False

    def wait(self, source_id: str, call_label: str = "") -> bool:
        """
        Block until it is safe to make a call to source_id.
        Respects min_delay between calls, the daily limit, and the circuit
        breaker. Prints a countdown if waiting more than 0.5s.

        Returns True if the caller may proceed, False if the daily limit was
        reached (the call should be skipped). Raises SourceUnavailable if the
        circuit breaker is tripped for this source.
        """
        # Circuit breaker — checked before the per-source lock so a tripped
        # source never blocks other sources' workers.
        if self._breaker_is_tripped(source_id):
            until = self._breaker_tripped_until.get(source_id, 0)
            logger.warning(
                f"[RateLimit] {source_id} circuit breaker tripped — skipping "
                f"(cooldown {until - time.time():.0f}s remaining)"
            )
            raise SourceUnavailable(source_id)

        # Global exhaustion marker (DB-backed, shared across runs/workers).
        # Only checked when this limiter has a user_id, i.e. it's running
        # inside a worker for an owned run — not in tests or the CLI collector.
        if self.user_id:
            try:
                from core import database as _db
                exhausted_until = _db.source_exhausted_until(source_id, self.user_id)
                if exhausted_until:
                    logger.warning(
                        f"[RateLimit] {source_id} quota exhausted until "
                        f"{exhausted_until} — skipping"
                    )
                    return False
            except Exception:
                pass  # DB unavailable (tests) — fall through to in-memory check

        min_delay, daily_limit = self._get_config(source_id)
        lock = self._get_lock(source_id)

        with lock:
            now = time.time()
            last = self._last_call.get(source_id, 0)
            elapsed = now - last
            remaining = min_delay - elapsed

            if remaining > 0:
                if remaining > 0.5:
                    self._print_wait(source_id, remaining, call_label)
                time.sleep(remaining)

            # Daily limit — check both the per-run in-memory counter (fast
            # path) and, when a user_id is set, the global DB-backed counter
            # so the limit is shared across all of a user's runs (review R4).
            count = self._call_counts.get(source_id, 0)
            if daily_limit:
                if count >= daily_limit:
                    logger.warning(f"[RateLimit] Daily limit reached for {source_id}: {daily_limit}")
                    return False
                if self.user_id:
                    try:
                        from core import database as _db
                        global_count = _db.daily_call_count(source_id, self.user_id)
                        if global_count >= daily_limit:
                            logger.warning(
                                f"[RateLimit] Global daily limit reached for "
                                f"{source_id}: {global_count}/{daily_limit}"
                            )
                            return False
                    except Exception:
                        pass

            # Record call — in-memory always, DB only when user_id is set.
            self._last_call[source_id] = time.time()
            self._call_counts[source_id] = count + 1
            self._total_calls += 1
            if self.user_id:
                try:
                    from core import database as _db
                    _db.increment_daily_calls(source_id, self.user_id)
                except Exception:
                    pass
            return True

    def record_success(self, source_id: str) -> None:
        """Mark a call as successful — resets the circuit-breaker failure count."""
        with self._breaker_lock:
            self._consecutive_fails.pop(source_id, None)

    def record_failure(self, source_id: str) -> None:
        """Mark a call as failed — may trip the circuit breaker."""
        with self._breaker_lock:
            fails = self._consecutive_fails.get(source_id, 0) + 1
            self._consecutive_fails[source_id] = fails
            threshold = int(self._tuning(source_id, "breaker_fails", self.breaker_fails))
            if fails >= threshold:
                cooldown = float(self._tuning(source_id, "breaker_cooldown", self.breaker_cooldown))
                self._breaker_tripped_until[source_id] = time.time() + cooldown
                logger.warning(
                    f"[RateLimit] {source_id} circuit breaker tripped after "
                    f"{fails} consecutive failures — cooldown {cooldown:.0f}s"
                )

    def backoff(self, source_id: str, attempt: int,
                status_code: Optional[int] = 0, retry_after: Optional[float] = None):
        """
        Exponential backoff after a failed request.
        Call this when you get a 429 or 5xx response, or a timeout.

        status_code: the HTTP status, or None for a timeout / connection error.
        retry_after: seconds from a Retry-After header, if present. When given,
                     it overrides the exponential formula (capped at
                     retry_after_max), so a server's "wait 60s" is respected.
        """
        # A failed call feeds the circuit breaker.
        self.record_failure(source_id)

        backoff_max = float(self._tuning(source_id, "backoff_max", self.backoff_max))
        floor_429   = float(self._tuning(source_id, "floor_429", self.floor_429))

        if retry_after is not None and retry_after > 0:
            wait = min(retry_after, self.retry_after_max)
            label = f"Retry-After {retry_after:.0f}s"
        else:
            wait = min(self.backoff_base ** attempt, backoff_max)
            if status_code == 429:
                wait = max(wait, floor_429)
            label = None

        if status_code == 429:
            logger.warning(f"[RateLimit] 429 on {source_id} — backing off {wait:.0f}s")
        elif status_code is None:
            logger.warning(f"[RateLimit] timeout on {source_id} — backing off {wait:.0f}s")
        elif status_code and status_code >= 500:
            logger.warning(f"[RateLimit] {status_code} on {source_id} — backing off {wait:.0f}s")
        elif status_code:
            logger.warning(f"[RateLimit] {status_code} on {source_id} — backing off {wait:.0f}s")
        self._print_wait(source_id, wait, label or f"backoff after {status_code}")
        time.sleep(wait)

    def _print_wait(self, source_id: str, seconds: float, label: str = ""):
        """Print a visible wait notice with countdown."""
        label_str = f" ({label})" if label else ""
        if seconds >= 2.0:
            # Countdown for long waits
            print(f"\r  ⏳ [{source_id}]{label_str} — waiting {seconds:.1f}s", end="", flush=True)
            start = time.time()
            while True:
                elapsed = time.time() - start
                left = seconds - elapsed
                if left <= 0:
                    break
                print(f"\r  ⏳ [{source_id}]{label_str} — waiting {left:.1f}s  ", end="", flush=True)
                time.sleep(0.2)
            print(f"\r  ✓ [{source_id}] ready{' '*30}", flush=True)
        else:
            # Short wait — just show a brief message
            print(f"\r  ⏳ [{source_id}] {seconds:.2f}s... ", end="", flush=True)

    def print_progress(
        self,
        source_id: str,
        current: int,
        total: int,
        label: str = ""
    ):
        """Print a progress line for the current source."""
        bar_width = 20
        filled = int(bar_width * current / total) if total > 0 else 0
        bar = "█" * filled + "░" * (bar_width - filled)
        pct = int(100 * current / total) if total > 0 else 0
        elapsed = time.time() - self._start_time
        total_made = self._call_counts.get(source_id, 0)
        label_str = f" {label}" if label else ""
        print(
            f"\r  [{source_id}] [{bar}] {pct}% ({current}/{total}){label_str}"
            f" | total calls: {self._total_calls} | {elapsed:.0f}s",
            end="", flush=True
        )

    def print_source_start(self, source_id: str, theme_id: str, query: str):
        """Print a header when starting a new source query."""
        delay, limit = self._get_config(source_id)
        count = self._call_counts.get(source_id, 0)
        limit_str = f"/{limit}" if limit else ""
        print(
            f"\n  → [{source_id}] theme={theme_id} | "
            f"calls: {count}{limit_str} | "
            f"delay: {delay}s | "
            f"query: {query[:60]}"
        )

    def print_source_done(self, source_id: str, results: int):
        """Print completion line for a source."""
        print(f"\r  ✓ [{source_id}] {results} results returned{' '*40}")

    def source_stats(self) -> dict[str, dict]:
        """Per-source counters — used by source-health reporting (review E4/U3)."""
        out = {}
        for src, count in self._call_counts.items():
            out[src] = {
                "calls": count,
                "consecutive_fails": self._consecutive_fails.get(src, 0),
                "breaker_tripped": bool(self._breaker_tripped_until.get(src, 0) and
                                        time.time() < self._breaker_tripped_until[src]),
            }
        return out

    def print_run_summary(self):
        """Print summary of all API calls made in this run."""
        elapsed = time.time() - self._start_time
        print(f"\n  {'─'*50}")
        print(f"  API Call Summary — run {self.run_id}")
        print(f"  {'─'*50}")
        print(f"  Total calls:  {self._total_calls}")
        print(f"  Time elapsed: {elapsed:.0f}s")
        print(f"\n  By source:")
        for src, count in sorted(self._call_counts.items(), key=lambda x: -x[1]):
            delay, limit = self._get_config(src)
            limit_str = f"/{limit}" if limit else "/∞"
            fails = self._consecutive_fails.get(src, 0)
            fail_str = f"  (fails: {fails})" if fails else ""
            print(f"    {src:<20} {count:>5} calls{limit_str}{fail_str}")
        print(f"  {'─'*50}\n")


# ---------------------------------------------------------------------------
# Module-level registry — one limiter per run_id (review R2)
#
# The previous single ``_limiter`` global was unsafe for any future
# concurrent-runs-in-one-process mode: run B's get_limiter() call would
# replace run A's limiter and run A's subsequent wait() calls would go
# through run B's counters. Keying by run_id in a dict fixes that, and
# clear_limiter(run_id) lets the worker drop the entry once a run finishes
# so the dict does not grow unbounded.
# ---------------------------------------------------------------------------

_limiters: dict[str, RateLimiter] = {}
_limiters_lock = Lock()

_CONFIG_CACHE: Optional[dict] = None
_USER_ID_CACHE: dict[str, str] = {}   # run_id -> user_id, set by the worker

def configure(config: Optional[dict]) -> None:
    """Set the rate_limiting config used for new limiters.

    Existing limiters keep their tuning; only limiters created after this
    call pick up the new values. Called once at worker startup from
    load_config().
    """
    global _CONFIG_CACHE
    _CONFIG_CACHE = config

def set_run_user(run_id: str, user_id: str) -> None:
    """Record the owning user for a run, so the global daily limit (R4) applies."""
    if run_id:
        with _limiters_lock:
            _USER_ID_CACHE[run_id] = user_id or "anon"

def get_limiter(run_id: str = "") -> RateLimiter:
    key = run_id or "_default"
    with _limiters_lock:
        lim = _limiters.get(key)
        if lim is None:
            uid = _USER_ID_CACHE.get(key, "")
            lim = RateLimiter(run_id=key, config=_CONFIG_CACHE, user_id=uid)
            _limiters[key] = lim
        return lim

def reset_limiter(run_id: str = "") -> RateLimiter:
    """Replace the limiter for a run — used at the start of a fresh Social run."""
    key = run_id or "_default"
    with _limiters_lock:
        uid = _USER_ID_CACHE.get(key, "")
        _limiters[key] = RateLimiter(run_id=key, config=_CONFIG_CACHE, user_id=uid)
        return _limiters[key]

def clear_limiter(run_id: str = "") -> None:
    """Drop the limiter for a run. Called by the worker in its finally block."""
    key = run_id or "_default"
    with _limiters_lock:
        _limiters.pop(key, None)
        _USER_ID_CACHE.pop(key, None)
