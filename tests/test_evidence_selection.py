"""
Which evidence reaches the writing agents.

The pipeline spends a model call per source to produce a relevance rating,
and Gaper/Vision/Theorist attach a significance/strength to everything they
emit. The context builder then caps each list. Before, the cap was a slice of
an unordered query — so the surviving subset was arbitrary, and with ~90% of
rated sources coming back Low, mostly noise (review V1).

These tests pin the two properties that make the cap defensible: it keeps the
best, and it says what it dropped.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core import context, database as db, db_backend, pipeline


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


def _source(run_id, n, rating, stype="current"):
    return {
        "source_id": f"SRC-{stype}-{n}", "run_id": run_id, "type": stype,
        "title": f"{rating or 'Unrated'} paper {n}", "year": 2000 + (n % 20),
        "relevance_rating": rating, "relevance_reason": "because",
        "source_name": "openalex", "authors": '["A. Author"]',
    }


# ---------------------------------------------------------------------------
# Ranked reads
# ---------------------------------------------------------------------------

def test_sources_come_back_best_first(run):
    # Insert worst-first, so insertion order is the opposite of rank order.
    for n, rating in enumerate(["Low", "Low", "Medium", "High", None]):
        db.insert("sources", _source(run, n, rating))

    ranked = db.get_sources_by_type("current", run, ranked=True)
    assert [r["relevance_rating"] for r in ranked] == \
        ["High", "Medium", "Low", "Low", None]


def test_unrated_sources_sort_last_not_middle(run):
    """An unrated source is not a mid-ranked one (review V5)."""
    db.insert("sources", _source(run, 1, None))
    db.insert("sources", _source(run, 2, "Low"))
    ranked = db.get_sources_by_type("current", run, ranked=True)
    assert ranked[-1]["relevance_rating"] is None


def test_ranked_limit_keeps_the_top(run):
    for n, rating in enumerate(["Low"] * 20 + ["High"]):
        db.insert("sources", _source(run, n, rating))
    top = db.get_sources_by_type("current", run, ranked=True, limit=1)
    assert top[0]["relevance_rating"] == "High"


def test_gaps_come_back_by_significance(run):
    for n, sig in enumerate(["Low", "High", "Medium"]):
        db.insert_gap({"gap_id": f"GAP-{n}", "run_id": run,
                       "description": f"gap {n}", "significance": sig})
    ranked = db.get_gaps(run, ranked=True)
    assert [g["significance"] for g in ranked] == ["High", "Medium", "Low"]


def test_implications_come_back_by_strength(run):
    for n, strength in enumerate(["Speculative", "Strong", "Moderate"]):
        db.insert_implication({"implication_id": f"IMP-{n}", "run_id": run,
                               "implication": f"imp {n}", "strength": strength})
    ranked = db.get_implications(run, ranked=True)
    assert [i["strength"] for i in ranked] == ["Strong", "Moderate", "Speculative"]


def test_proposals_come_back_by_promise(run):
    for n, promise in enumerate(["Low", "High"]):
        db.insert_proposal({"proposal_id": f"PRO-{n}", "run_id": run,
                            "proposal": f"prop {n}", "promise_rating": promise})
    ranked = db.get_proposals(run, ranked=True)
    assert [p["promise_rating"] for p in ranked] == ["High", "Low"]


# ---------------------------------------------------------------------------
# Truncation is declared, not silent
# ---------------------------------------------------------------------------

def test_truncated_source_list_says_how_many_were_dropped():
    sources = [{"title": f"Paper {n}", "year": 2020, "authors": "[]",
                "relevance_rating": "Low", "source_name": "openalex"}
               for n in range(50)]
    text = context._sources_summary(sources, max_items=5)
    assert text.count("- ") == 5
    assert "45 further sources not shown" in text
    assert "top 5 of 50" in text


def test_a_complete_list_gets_no_tail_note():
    sources = [{"title": "Only paper", "year": 2020, "authors": "[]",
                "source_name": "openalex"}]
    assert "not shown" not in context._sources_summary(sources, max_items=5)


def test_gap_tail_note_reports_the_significance_breakdown():
    gaps = ([{"gap_id": f"G{n}", "significance": "High", "gap_type": "unstudied",
              "description": "d", "primary_evaluation": "unanswered"}
             for n in range(3)] +
            [{"gap_id": f"G{n}", "significance": "Low", "gap_type": "unstudied",
              "description": "d", "primary_evaluation": "unanswered"}
             for n in range(60)])
    text = context._gaps_summary(gaps, max_items=10)
    assert "53 further gaps not shown" in text
    assert "3 High" in text and "60 Low" in text


def test_summaries_are_capped_so_a_real_run_fits_a_context_window():
    """356 gaps in one prompt is past a 32k local model (review V4)."""
    gaps = [{"gap_id": f"G{n}", "significance": "Medium", "gap_type": "t",
             "description": "d" * 200, "primary_evaluation": "unanswered"}
            for n in range(356)]
    text = context._gaps_summary(gaps)
    assert text.count("\n- ") <= context.MAX_GAPS_IN_CONTEXT


def test_understanding_map_ranks_before_it_truncates(run):
    """
    End to end on the deliverable: the map's seminal list is capped at 25, so
    with 30 sources the 5 dropped must be the worst, not the last inserted.
    """
    for n in range(29):
        db.insert("sources", _source(run, n, "Low", stype="seminal"))
    db.insert("sources", _source(run, 99, "High", stype="seminal"))

    ctx = context.for_understanding_map(run, "Does identity shape intelligence?")
    assert "High paper 99" in ctx
    assert "5 further seminal works not shown" in ctx
