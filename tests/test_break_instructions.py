"""
Break instruction persistence.

Regression cover for the defect where a resumed run silently discarded the
researcher's steering input: instructions lived only in the markdown review
document, and breaks 0 and 1 resumed with the literal string "CONFIRMED".
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Point the database layer at a throwaway SQLite file for each test."""
    import core.database as db
    from core import db_backend

    monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pipeline.db")
    db_backend.reset_backend()
    db.init_db()
    yield db
    db_backend.reset_backend()


def test_instructions_round_trip(isolated_db):
    db = isolated_db
    db.create_run("RUN-TEST-0001", "Does persistence survive a resume?")

    written = "REMOVE GAP GAP-001\nADD GAP: something the pipeline missed"
    assert db.save_break_instructions("RUN-TEST-0001", 1, written, ["a contradiction"])

    stored = db.get_break_instructions("RUN-TEST-0001", 1)
    assert stored is not None
    assert stored["instructions"] == written
    assert stored["contradictions"] == ["a contradiction"]
    assert stored["source"] == "cli"


def test_missing_instructions_return_none(isolated_db):
    isolated_db.create_run("RUN-TEST-0002", "Nothing submitted here.")
    assert isolated_db.get_break_instructions("RUN-TEST-0002", 1) is None


def test_resubmission_replaces_rather_than_duplicates(isolated_db):
    db = isolated_db
    db.create_run("RUN-TEST-0003", "Break gets answered twice.")

    db.save_break_instructions("RUN-TEST-0003", 0, "CONFIRMED")
    db.save_break_instructions("RUN-TEST-0003", 0, "ADD THEME: philosophy_of_mind")

    assert db.count("break_instructions", {"run_id": "RUN-TEST-0003"}) == 1
    stored = db.get_break_instructions("RUN-TEST-0003", 0)
    assert stored["instructions"] == "ADD THEME: philosophy_of_mind"


def test_breaks_are_stored_independently(isolated_db):
    db = isolated_db
    db.create_run("RUN-TEST-0004", "All three breaks answered.")

    for n in (0, 1, 2):
        db.save_break_instructions("RUN-TEST-0004", n, f"instructions for break {n}")

    for n in (0, 1, 2):
        assert db.get_break_instructions("RUN-TEST-0004", n)["instructions"] == \
            f"instructions for break {n}"


def test_resume_recovers_from_database(isolated_db, monkeypatch, tmp_path):
    """The path main.py takes on resume — this is what used to return 'CONFIRMED'."""
    from core import breaks
    db = isolated_db
    monkeypatch.setattr(breaks, "ARTIFACTS_DIR", tmp_path / "artifacts")

    db.create_run("RUN-TEST-0005", "Resume me.")
    db.save_break_instructions(
        "RUN-TEST-0005", 1, "CORRECT GAP GAP-007: this framing is wrong", ["noted"]
    )

    recovered = breaks.resume_instructions("RUN-TEST-0005", 1)
    assert "CORRECT GAP GAP-007: this framing is wrong" in recovered
    assert "--- CONTRADICTION LOG ---" in recovered
    assert "noted" in recovered


def test_resume_falls_back_to_review_document(isolated_db, monkeypatch, tmp_path):
    """Runs that predate persistence still recover from their markdown doc."""
    from core import breaks
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(breaks, "ARTIFACTS_DIR", artifacts)

    isolated_db.create_run("RUN-TEST-0006", "Legacy run.")
    (artifacts / "RUN-TEST-0006_break2_review.md").write_text(
        "# Break 2\n\n**Your instructions:**\n\nSCRIBE OUTPUT: blog_post | audience: general public\n"
    )

    recovered = breaks.resume_instructions("RUN-TEST-0006", 2)
    assert "SCRIBE OUTPUT: blog_post" in recovered


def test_resume_degrades_to_confirmed_when_nothing_exists(isolated_db, monkeypatch, tmp_path):
    from core import breaks
    monkeypatch.setattr(breaks, "ARTIFACTS_DIR", tmp_path / "artifacts")
    isolated_db.create_run("RUN-TEST-0007", "Nothing to recover.")
    assert breaks.resume_instructions("RUN-TEST-0007", 0) == "CONFIRMED"
