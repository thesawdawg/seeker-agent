"""
Branching a run, and what the link check is allowed to throw away.

Both are cases where the system quietly produced a wrong answer: a branch
inherited conclusions from steps it was about to re-run (review C4), and a
single unanswered HEAD request permanently erased a real paper (review V2).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import requests

from core import database as db, db_backend, pipeline


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pipeline.db")
    db_backend.reset_backend()
    pipeline._schema_ready = False
    db.init_db()
    yield
    db_backend.reset_backend()
    pipeline._schema_ready = False


def _completed_run() -> str:
    """A run with output from every stage, as if it had finished."""
    run_id = pipeline.create_run("Does identity shape intelligence?")
    pipeline.ensure_steps(run_id)
    db.insert("sources", {"source_id": "SRC-1", "run_id": run_id,
                          "type": "seminal", "title": "Foundational work"})
    db.insert_gap({"gap_id": "GAP-1", "run_id": run_id,
                   "description": "an unstudied gap", "significance": "High"})
    db.insert_implication({"implication_id": "IMP-1", "run_id": run_id,
                           "implication": "an implication", "strength": "Strong"})
    db.insert_proposal({"proposal_id": "PRO-1", "run_id": run_id,
                        "proposal": "a proposal", "promise_rating": "High"})
    db.insert_synthesis({"synthesis_id": "SYN-1", "run_id": run_id,
                         "full_narrative": "the narrative"})
    db.insert_direction({"direction_id": "DIR-1", "run_id": run_id,
                         "direction": "a direction"})
    for step in pipeline.STEP_DEFS:
        pipeline.set_step_status(run_id, step.name, "done")
    return run_id


# ---------------------------------------------------------------------------
# Branching (C4)
# ---------------------------------------------------------------------------

def test_branching_early_does_not_inherit_later_output(store):
    """
    Branch after grounder: the new run must start with grounder's sources and
    nothing else. Cloning every table regardless of the branch point meant
    Gaper re-ran on top of the parent's gaps and Theorist read proposals
    evaluated against evidence this branch never gathered.
    """
    source_run = _completed_run()
    new_run = pipeline.branch_run(source_run, "grounder")

    assert db.count("sources", {"run_id": new_run}) == 1      # grounder's own
    assert db.count("gaps", {"run_id": new_run}) == 0
    assert db.count("implications", {"run_id": new_run}) == 0
    assert db.count("proposals", {"run_id": new_run}) == 0
    assert db.count("syntheses", {"run_id": new_run}) == 0
    assert db.count("directions", {"run_id": new_run}) == 0


def test_branching_late_carries_the_whole_prefix(store):
    source_run = _completed_run()
    new_run = pipeline.branch_run(source_run, "synthesizer")

    assert db.count("gaps", {"run_id": new_run}) == 1
    assert db.count("implications", {"run_id": new_run}) == 1
    assert db.count("proposals", {"run_id": new_run}) == 1
    assert db.count("syntheses", {"run_id": new_run}) == 1
    # Thinker runs after synthesizer, so its output is not inherited.
    assert db.count("directions", {"run_id": new_run}) == 0


def test_branch_marks_the_prefix_done_and_the_rest_pending(store):
    source_run = _completed_run()
    new_run = pipeline.branch_run(source_run, "gaper")
    statuses = {s["step_name"]: s["status"]
                for s in pipeline.get_steps(new_run)}
    assert statuses["grounder"] == "done"
    assert statuses["gaper"] == "done"
    assert statuses["vision"] == "pending"
    assert statuses["scribe"] == "pending"


def test_branch_leaves_the_source_run_untouched(store):
    source_run = _completed_run()
    pipeline.branch_run(source_run, "grounder")
    assert db.count("gaps", {"run_id": source_run}) == 1
    assert db.count("proposals", {"run_id": source_run}) == 1


def test_clone_plan_covers_only_the_prefix():
    plan = pipeline._clone_plan(["concept_mapper", "break0", "grounder"])
    tables = {table for table, _, _ in plan}
    assert tables == {"concept_expansions", "sources"}
    # argument_tree and break_instructions have dedicated cloners
    assert "argument_tree" not in tables
    assert "break_instructions" not in tables


# ---------------------------------------------------------------------------
# Link checking (V2)
# ---------------------------------------------------------------------------

@pytest.fixture
def handler():
    from agents.social import SourceHandler
    return SourceHandler()


@pytest.mark.parametrize("exc", [
    requests.exceptions.Timeout("slow"),
    requests.exceptions.ConnectionError("reset"),
    requests.exceptions.SSLError("bad handshake"),
])
def test_an_unanswered_request_is_unreachable_not_dead(handler, monkeypatch, exc):
    """
    The distinction the whole finding turns on. Every exception used to mean
    'dead', and 'dead' meant the source was discarded — so a network wobble
    erased real papers.
    """
    monkeypatch.setattr(requests, "head",
                        lambda *a, **k: (_ for _ in ()).throw(exc))
    assert handler._check_link("https://example.com/paper") == "unreachable"


@pytest.mark.parametrize("code,expected", [
    (404, "dead"),
    (410, "dead"),
    (200, "active"),
    (403, "active"),      # publishers that refuse bots still have the paper
    (405, "active"),      # HEAD not allowed
    (500, "active"),      # a broken server is not a missing paper
])
def test_status_codes_map_to_the_right_verdict(handler, monkeypatch, code, expected):
    class Resp:
        status_code = code
        url = "https://example.com/paper"
    monkeypatch.setattr(requests, "head", lambda *a, **k: Resp())
    assert handler._check_link("https://example.com/paper") == expected


def test_a_redirect_is_reported_as_redirected(handler, monkeypatch):
    class Resp:
        status_code = 200
        url = "https://example.com/paper-moved"
    monkeypatch.setattr(requests, "head", lambda *a, **k: Resp())
    assert handler._check_link("https://example.com/paper") == "redirected"


def test_an_empty_url_is_unchecked_not_dead(handler):
    assert handler._check_link("") == "unchecked"


def test_doi_link_is_built_from_a_bare_doi():
    from agents.social import _doi_link
    assert _doi_link("10.1234/abc") == "https://doi.org/10.1234/abc"
    assert _doi_link("https://doi.org/10.1234/abc") == "https://doi.org/10.1234/abc"
    assert _doi_link("") == ""


def test_rating_failure_is_recorded_as_unrated_not_medium(monkeypatch):
    """
    A router failure written as 'Medium' was indistinguishable from a real
    judgement of Medium, and ranked as if it were one (review V5).
    """
    from agents import social
    monkeypatch.setattr(social.llm, "call",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("no provider")))
    rating, reason = social.rate_relevance("A title", "An abstract",
                                           "A problem", "A theme")
    assert rating is None
    assert "not assessed" in reason


def test_unparseable_rating_is_also_unrated(monkeypatch):
    from agents import social
    monkeypatch.setattr(social.llm, "call", lambda *a, **k: "I cannot comply.")
    rating, reason = social.rate_relevance("A title", "", "A problem", "A theme")
    assert rating is None
    assert "not assessed" in reason


def test_a_good_rating_still_comes_through(monkeypatch):
    from agents import social
    monkeypatch.setattr(
        social.llm, "call",
        lambda *a, **k: '{"rating": "High", "reason": "directly on point"}')
    assert social.rate_relevance("t", "a", "p", "th") == \
        ("High", "directly on point")
