"""
Provenance
----------
What a deliverable is actually built on, stated on the deliverable itself.

The README's promise is that "every claim traces to verifiable evidence".
The data to back that up is all in the database — source_health knows which
sources answered and which were skipped, the sources table knows what was
retained and how it was rated, reference_verifications knows which DOIs
resolved, llm_usage knows what the run cost — but none of it reached the
researcher holding the Understanding Map. They had to take the evidence base
on faith (review V6).

This renders it as a short section appended to each artifact. It is
deliberately unglamorous: counts, not prose. The point is that a reader can
tell at a glance whether the map in front of them rests on 340 sources
across nine themes or on 40 sources from two, and whether the things it
could not reach were incidental or central.

Everything here is best-effort. A provenance section is documentation about
a run, and failing to render it must never sink the artifact it describes.
"""

import logging
from typing import Optional

from core import database as db

logger = logging.getLogger(__name__)


def collect(run_id: str) -> dict:
    """Gather the numbers. Never raises — missing pieces come back empty."""
    facts: dict = {
        "run_id": run_id,
        "sources": {},
        "link_status": {},
        "relevance": {},
        "themes": {},
        "coverage": [],
        "usage": {},
        "citations": {},
        "warnings": [],
    }

    def _safe(label, fn, default):
        try:
            return fn()
        except Exception as e:                      # pragma: no cover - defensive
            logger.debug(f"[provenance] {label} unavailable: {e}")
            return default

    facts["sources"] = _safe(
        "source counts",
        lambda: db.count_by("sources", "type", {"run_id": run_id}), {})
    facts["link_status"] = _safe(
        "link status",
        lambda: db.count_by("sources", "link_status", {"run_id": run_id}), {})
    facts["relevance"] = _safe(
        "relevance",
        lambda: db.count_by("sources", "relevance_rating", {"run_id": run_id}), {})
    facts["coverage"] = _safe(
        "source health", lambda: db.get_source_health(run_id), [])
    facts["usage"] = _safe(
        "usage", lambda: db.get_llm_usage_summary(run_id), {})
    facts["warnings"] = _safe("warnings", lambda: _step_warnings(run_id), [])
    return facts


def _step_warnings(run_id: str) -> list[dict]:
    """Non-fatal warnings recorded against steps — truncation, unrated sources."""
    import json
    from core import pipeline

    out = []
    for step in pipeline.get_steps(run_id):
        raw = step.get("warnings") or "[]"
        try:
            entries = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except (json.JSONDecodeError, TypeError):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("message"):
                out.append({"step": step.get("label") or step.get("step_name"),
                            "message": entry["message"]})
    return out


def render_markdown(run_id: str, cited: Optional[list] = None) -> str:
    """
    The provenance section for an artifact.

    `cited` is the CitableSource manifest the artifact actually cites, when
    the caller has one — that is what makes the difference between "we
    collected 341 sources" and "this document cites 34 of them" visible.
    """
    try:
        return _render(collect(run_id), cited)
    except Exception as e:                          # pragma: no cover - defensive
        logger.warning(f"[provenance] could not render for {run_id}: {e}")
        return ""


def _render(facts: dict, cited: Optional[list]) -> str:
    lines = ["## Provenance", "",
             "What this document is built on. Generated from the run's own "
             "records, not written by a model.", ""]

    # --- Evidence base ----------------------------------------------------
    sources = facts.get("sources") or {}
    total = sum(sources.values())
    if total:
        by_type = " · ".join(f"{n} {name}" for name, n in sorted(sources.items()))
        lines.append(f"**Evidence base:** {total} sources retained ({by_type})")

        rel = facts.get("relevance") or {}
        rated = [f"{rel[k]} {k}" for k in ("High", "Medium", "Low") if rel.get(k)]
        if rated:
            lines.append(f"**Relevance:** {' · '.join(rated)}")
        if rel.get("unrated"):
            lines.append(
                f"**Unrated:** {rel['unrated']} sources could not be assessed "
                f"— the model was unreachable for those calls, so they rank "
                f"last rather than in the middle")

        link = facts.get("link_status") or {}
        trouble = [f"{link[k]} {k}" for k in ("unreachable", "dead")
                   if link.get(k)]
        if trouble:
            lines.append(
                f"**Links:** {' · '.join(trouble)}. These sources are kept and "
                f"flagged, not discarded — a DOI resolves independently of "
                f"whatever the publisher is serving today.")
    else:
        lines.append("**Evidence base:** no sources recorded for this run.")
    lines.append("")

    # --- Citations --------------------------------------------------------
    if cited is not None:
        verified = sum(1 for c in cited if getattr(c, "verified", False))
        unverified = [c for c in cited if not getattr(c, "verified", False)]
        lines.append(f"**Cited:** {len(cited)} of {total or 'n/a'} sources"
                     + (f" · {verified} verified against Crossref/OpenAlex"
                        if verified else ""))
        if unverified:
            names = ", ".join(
                str(getattr(c, "cite_key", "") or getattr(c, "title", ""))[:40]
                for c in unverified[:8])
            more = f" (+{len(unverified) - 8} more)" if len(unverified) > 8 else ""
            lines.append(
                f"**Unverified citations:** {len(unverified)} could not be "
                f"confirmed to exist: {names}{more}. Check these before "
                f"relying on them.")
        lines.append("")

    # --- Source coverage --------------------------------------------------
    coverage = facts.get("coverage") or []
    if coverage:
        ok, degraded, failed, skipped = [], [], [], []
        for row in coverage:
            entry = f"{row.get('source_id')} ({row.get('results_returned', 0)})"
            bucket = {"ok": ok, "degraded": degraded,
                      "failed": failed, "skipped": skipped}.get(row.get("status"))
            if bucket is not None:
                bucket.append(entry)
        lines.append("**Source coverage:**")
        if ok:
            lines.append(f"- answered: {', '.join(sorted(ok))}")
        if degraded:
            lines.append(f"- answered with nothing to return: "
                         f"{', '.join(sorted(degraded))}")
        for label, bucket in (("failed", failed), ("skipped", skipped)):
            if bucket:
                lines.append(f"- **{label}**: {', '.join(sorted(bucket))} — "
                             f"nothing from these sources is in this document")
        lines.append("")

    # --- What was left out ------------------------------------------------
    warnings = facts.get("warnings") or []
    if warnings:
        lines.append("**Limits of this document:**")
        seen = set()
        for w in warnings:
            key = w["message"][:120]
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- {w['step']}: {w['message']}")
        lines.append("")

    # --- Cost -------------------------------------------------------------
    usage = facts.get("usage") or {}
    if usage.get("total_calls"):
        lines.append(
            f"**Model usage:** {usage['total_calls']} calls · "
            f"{usage.get('total_tokens', 0):,} tokens "
            f"({usage.get('total_prompt', 0):,} in / "
            f"{usage.get('total_completion', 0):,} out)")
        by_model = usage.get("by_model") or {}
        if by_model:
            lines.append("Models: " + ", ".join(sorted(by_model)))
        lines.append("")

    lines.append(f"*Run `{facts['run_id']}`.*")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Pre-run estimate (review X2)
#
# A run is a long, expensive, human-blocking commitment: three mandatory
# breaks, minutes to hours of wall clock, thousands of model calls. The New
# Run screen asked for a problem and a model and started. Everything needed
# to say roughly what that costs is already recorded — the pipeline shape,
# the configured sources, and this user's own llm_usage history.
#
# The estimate is deliberately coarse and says so. Its job is to stop someone
# discovering the scale of a run after committing to it, not to be a quote.
# ---------------------------------------------------------------------------

# Fallbacks for a user's first run, before there is any history to learn
# from. Derived from the shipped database's per-agent averages.
_FALLBACK_TOKENS_PER_CALL = 2600
_FALLBACK_CALLS_PER_SOURCE_BATCH = 1
_FALLBACK_SECONDS_PER_CALL = 9.0


def estimate_run(user_id: str, config: dict, source_overrides: dict = None,
                 theme_count: Optional[int] = None) -> dict:
    """
    Rough cost of a run before it starts.

    Learns from this user's completed runs where possible and falls back to
    static per-call figures otherwise. Always reports which of the two it
    used, so nobody mistakes a first-run guess for a measurement.
    """
    from agents.social import RATING_BATCH_SIZE

    config = config or {}
    overrides = {str(k).lower(): v for k, v in (source_overrides or {}).items()
                 if isinstance(v, bool)}

    sources_cfg = config.get("sources", {}) or {}
    agent_sources = config.get("agent_sources", {}) or {}
    social_sources = [
        s for s in (agent_sources.get("social") or [])
        if isinstance(s, str) and not s.startswith("_")
        and overrides.get(s, sources_cfg.get(s, {}).get("enabled", True))
    ]

    themes = theme_count if theme_count is not None else len(config.get("themes") or [])
    themes = max(themes, 1)
    per_source = int(agent_sources.get("social_limit")
                     or overrides.get("limit_per_source")
                     or config.get("limit_per_source") or 8)

    lookups = themes * len(social_sources) * per_source
    # Social rates in batches; every other agent is a handful of calls.
    rating_calls = -(-lookups // max(RATING_BATCH_SIZE, 1)) if lookups else 0
    agent_calls = sum(1 for s in _agent_step_names())
    total_calls = rating_calls + agent_calls

    history = _usage_history(user_id)
    if history["runs"] >= 1 and history["tokens_per_call"]:
        tokens_per_call = history["tokens_per_call"]
        seconds_per_call = history["seconds_per_call"] or _FALLBACK_SECONDS_PER_CALL
        basis = f"your last {history['runs']} run(s)"
    else:
        tokens_per_call = _FALLBACK_TOKENS_PER_CALL
        seconds_per_call = _FALLBACK_SECONDS_PER_CALL
        basis = "typical figures — you have no completed runs yet"

    return {
        "themes":            themes,
        "sources":           sorted(social_sources),
        "results_per_source": per_source,
        "source_lookups":    lookups,
        "estimated_calls":   total_calls,
        "estimated_tokens":  int(total_calls * tokens_per_call),
        "estimated_seconds": int(total_calls * seconds_per_call),
        "breaks":            3,
        "basis":             basis,
        "is_measured":       history["runs"] >= 1,
    }


def _agent_step_names() -> list[str]:
    from core import pipeline
    return [s.name for s in pipeline.STEP_DEFS if s.kind == "agent"]


def _usage_history(user_id: str) -> dict:
    """Per-call averages from this user's own completed runs."""
    empty = {"runs": 0, "tokens_per_call": 0, "seconds_per_call": 0.0}
    if not user_id:
        return empty
    try:
        from core import users
        run_ids = users.runs_for_user(user_id)
    except Exception:
        return empty
    if not run_ids:
        return empty

    total_tokens = total_calls = 0
    total_seconds = 0.0
    counted = 0
    for run_id in run_ids[-10:]:            # recent history only
        try:
            usage = db.get_llm_usage_summary(run_id)
        except Exception:
            continue
        if not usage.get("total_calls"):
            continue
        total_tokens += usage.get("total_tokens", 0)
        total_calls += usage["total_calls"]
        counted += 1
        total_seconds += _run_wall_seconds(run_id)

    if not total_calls:
        return empty
    return {
        "runs": counted,
        "tokens_per_call": total_tokens // total_calls,
        "seconds_per_call": (total_seconds / total_calls) if total_seconds else 0.0,
    }


def _run_wall_seconds(run_id: str) -> float:
    """
    Compute time for a run, excluding the hours it sat at a break.

    Summing step durations rather than created_at → completed_at is what
    keeps a run someone answered the next morning from poisoning the average.
    """
    from datetime import datetime
    from core import pipeline

    total = 0.0
    try:
        steps = pipeline.get_steps(run_id)
    except Exception:
        return 0.0
    for step in steps:
        started, finished = step.get("started_at"), step.get("finished_at")
        if not started or not finished or step.get("kind") == "break":
            continue
        try:
            delta = (datetime.fromisoformat(finished)
                     - datetime.fromisoformat(started)).total_seconds()
        except (TypeError, ValueError):
            continue
        if 0 < delta < 6 * 3600:            # ignore nonsense and wedged steps
            total += delta
    return total
