"""
What a deliverable says about itself, and what a run says before it starts.

Both are cases where the system knew something the researcher needed and
kept it: the evidence base behind an Understanding Map (review V6), and the
scale of a run before committing to it (review X2).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core import database as db, db_backend, pipeline, provenance


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pipeline.db")
    db_backend.reset_backend()
    pipeline._schema_ready = False
    db.init_db()
    run_id = pipeline.create_run("Does identity shape intelligence?")
    yield run_id
    db_backend.reset_backend()
    pipeline._schema_ready = False


def _populate(run_id, *, unrated=0, unreachable=0):
    for i in range(20):
        rating = "High" if i < 2 else ("Medium" if i < 6 else "Low")
        db.insert("sources", {
            "source_id": f"S{i}", "run_id": run_id, "type": "current",
            "title": f"Paper {i}", "relevance_rating": rating,
            "link_status": "unreachable" if i < unreachable else "active"})
    for i in range(unrated):
        db.insert("sources", {
            "source_id": f"U{i}", "run_id": run_id, "type": "seminal",
            "title": f"Unrated {i}", "relevance_rating": None,
            "link_status": "active"})


# ---------------------------------------------------------------------------
# Provenance (V6)
# ---------------------------------------------------------------------------

def test_provenance_reports_the_evidence_base(run):
    _populate(run)
    text = provenance.render_markdown(run)
    assert "## Provenance" in text
    assert "20 sources retained" in text
    assert "2 High" in text and "4 Medium" in text and "14 Low" in text


def test_provenance_names_sources_that_contributed_nothing(run):
    """
    The whole point: "scopus was skipped" is what tells a researcher the map
    is narrower than they assumed.
    """
    _populate(run)
    db.record_source_health(run, "openalex", "social", "ok", results_returned=18)
    db.record_source_health(run, "scopus", "social", "skipped", last_error="no key")
    db.record_source_health(run, "core", "social", "failed", last_error="breaker")

    text = provenance.render_markdown(run)
    assert "openalex" in text
    assert "skipped" in text and "scopus" in text
    assert "failed" in text and "core" in text
    assert "nothing from these sources is in this document" in text


def test_provenance_surfaces_unrated_sources(run):
    _populate(run, unrated=5)
    text = provenance.render_markdown(run)
    assert "5 sources could not be assessed" in text


def test_provenance_explains_unreachable_links_rather_than_hiding_them(run):
    _populate(run, unreachable=3)
    text = provenance.render_markdown(run)
    assert "3 unreachable" in text
    assert "kept and flagged, not discarded" in text


def test_provenance_repeats_the_truncation_warnings(run):
    _populate(run)
    pipeline.record_step_warning(
        run, "scribe",
        "Context truncated: the agent saw the top 25 of 341 sources")
    text = provenance.render_markdown(run)
    assert "Limits of this document" in text
    assert "top 25 of 341" in text


def test_provenance_reports_model_usage(run):
    _populate(run)
    db.record_llm_usage(run, "social", "open-webui", "llama3.2:3b", 40000, 3000)
    text = provenance.render_markdown(run)
    assert "43,000 tokens" in text
    assert "open-webui:llama3.2:3b" in text


def test_provenance_flags_unverified_citations(run):
    _populate(run)

    class Cite:
        def __init__(self, key, verified):
            self.cite_key, self.verified, self.title = key, verified, key

    text = provenance.render_markdown(
        run, cited=[Cite("mead1934", True), Cite("ghost2024", False)])
    assert "Cited:** 2" in text
    assert "Unverified citations" in text
    assert "ghost2024" in text


def test_provenance_never_raises_on_a_missing_run(run):
    """Documentation about a run must not be able to sink the artifact."""
    assert isinstance(provenance.render_markdown("RUN-DOES-NOT-EXIST"), str)


def test_provenance_survives_a_broken_database(run, monkeypatch):
    monkeypatch.setattr(db, "count_by",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    text = provenance.render_markdown(run)
    assert isinstance(text, str)
    assert "Provenance" in text


# ---------------------------------------------------------------------------
# Pre-run estimate (X2)
# ---------------------------------------------------------------------------

CONFIG = {
    "themes": [{"theme_id": f"T{i}"} for i in range(4)],
    "sources": {"openalex": {"enabled": True}, "arxiv": {"enabled": True},
                "scopus": {"enabled": False}},
    "agent_sources": {"social": ["openalex", "arxiv", "scopus"],
                      "social_limit": 5},
}


def test_estimate_counts_only_enabled_sources(run):
    est = provenance.estimate_run("", CONFIG)
    assert est["sources"] == ["arxiv", "openalex"]      # scopus is disabled
    assert est["source_lookups"] == 4 * 2 * 5


def test_estimate_honours_per_run_source_overrides(run):
    est = provenance.estimate_run("", CONFIG, {"arxiv": False})
    assert est["sources"] == ["openalex"]
    assert est["source_lookups"] == 4 * 1 * 5


def test_estimate_reflects_the_batching_saving(run):
    """
    Rating is batched, so each *additional* paper costs 1/BATCH_SIZE of a
    call, not a whole one (review V3). Comparing two configs isolates that
    marginal cost from the fixed per-agent overhead, which dominates at
    small scales and would mask it.
    """
    from agents.social import RATING_BATCH_SIZE

    small = provenance.estimate_run("", {**CONFIG, "agent_sources": {
        **CONFIG["agent_sources"], "social_limit": 10}})
    large = provenance.estimate_run("", {**CONFIG, "agent_sources": {
        **CONFIG["agent_sources"], "social_limit": 110}})

    extra_lookups = large["source_lookups"] - small["source_lookups"]
    extra_calls = large["estimated_calls"] - small["estimated_calls"]
    assert extra_lookups == 800
    # One call per BATCH_SIZE papers, not one per paper.
    assert extra_calls == extra_lookups // RATING_BATCH_SIZE


def test_estimate_says_when_it_is_guessing(run):
    est = provenance.estimate_run("USR-NOBODY", CONFIG)
    assert est["is_measured"] is False
    assert "no completed runs" in est["basis"]


def test_estimate_learns_from_the_users_own_history(run, monkeypatch):
    from core import users
    monkeypatch.setattr(users, "runs_for_user", lambda uid: [run])
    # 10 calls, 100k tokens → 10,000 tokens per call, well above the fallback.
    for _ in range(10):
        db.record_llm_usage(run, "grounder", "p", "m", 9000, 1000)

    est = provenance.estimate_run("USR-1", CONFIG)
    assert est["is_measured"] is True
    assert "your last 1 run" in est["basis"]
    assert est["estimated_tokens"] == est["estimated_calls"] * 10000


def test_estimate_never_divides_by_zero_on_an_empty_config(run):
    est = provenance.estimate_run("", {})
    assert est["source_lookups"] == 0
    assert est["estimated_calls"] >= 0
    assert est["themes"] >= 1


def test_break_time_is_excluded_from_the_duration_estimate(run):
    """
    A run answered the next morning must not teach the estimator that runs
    take fourteen hours.
    """
    db.update("run_steps",
              {"status": "done", "started_at": "2026-01-01T00:00:00+00:00",
               "finished_at": "2026-01-01T14:00:00+00:00"},
              {"run_id": run, "step_name": "break0"})
    db.update("run_steps",
              {"status": "done", "started_at": "2026-01-01T00:00:00+00:00",
               "finished_at": "2026-01-01T00:01:00+00:00"},
              {"run_id": run, "step_name": "grounder"})
    assert provenance._run_wall_seconds(run) == 60
