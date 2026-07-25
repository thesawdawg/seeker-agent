"""
Directive round-trip: browser widgets → backend parsers.

The web UI's widgets generate directive strings, and the backend parses them.
These are two separate pieces of code that must agree on one grammar, so this
feeds the exact strings the frontend emits (see the vocabulary in
web/static/app.js buildDirectives) into the real parsers and asserts they are
understood.

If someone changes the wording on either side, this fails.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import breaks  # noqa: E402

APP_JS = Path(__file__).parent.parent / "web" / "static" / "app.js"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import core.database as db
    from core import db_backend, pipeline

    monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pipeline.db")
    db_backend.reset_backend()
    pipeline._schema_ready = False
    db.init_db()
    yield db
    db_backend.reset_backend()
    pipeline._schema_ready = False


# ---------------------------------------------------------------------------
# The grammar the frontend emits
# ---------------------------------------------------------------------------

def test_frontend_emits_only_known_directives():
    """
    Every directive template in app.js must be one the backend documents.
    Catches a widget inventing a command the pipeline will silently ignore.
    """
    js = APP_JS.read_text()
    block = js[js.index("function buildDirectives"):]
    block = block[:block.index("\n}")]

    emitted = set(re.findall(r"`([A-Z][A-Z ]+[A-Z])[: ]", block))
    known = {"REMOVE THEME", "ADD THEME", "REMOVE GAP", "CORRECT GAP",
             "ADD GAP", "OVERRIDE SEMINAL", "OVERRIDE VERDICT",
             "SCRIBE OUTPUT"}
    assert emitted <= known, f"app.js emits unknown directives: {emitted - known}"
    assert "CONFIRMED" in block, "app.js must fall back to CONFIRMED"


# ---------------------------------------------------------------------------
# Backend understands what the widgets produce
# ---------------------------------------------------------------------------

def test_scribe_output_directive_parses():
    """Exactly the string the Break 2 output builder emits."""
    instructions = (
        "SCRIBE OUTPUT: blog_post | audience: general public\n"
        "SCRIBE OUTPUT: paper_section | audience: specialists"
    )
    requests = breaks.parse_scribe_requests(instructions)
    assert requests == [
        {"output_type": "blog_post", "audience": "general public"},
        {"output_type": "paper_section", "audience": "specialists"},
    ]


def test_confirmed_alone_yields_the_default_output():
    assert breaks.parse_scribe_requests("CONFIRMED") == [
        {"output_type": "research_brief", "audience": "researcher"}
    ]


def test_theme_directives_from_checkboxes_apply():
    all_themes = [
        {"theme_id": "philosophy_of_mind", "label": "Philosophy of Mind"},
        {"theme_id": "social_identity", "label": "Social Identity"},
        {"theme_id": "epistemology", "label": "Epistemology"},
    ]
    selected = [all_themes[0], all_themes[1]]

    # What unchecking one box and checking another produces
    instructions = "REMOVE THEME: philosophy_of_mind\nADD THEME: epistemology"
    result = breaks.apply_theme_directives(instructions, selected, all_themes)

    assert {t["theme_id"] for t in result} == {"social_identity", "epistemology"}


def test_gap_removal_is_detected_as_a_contradiction(env):
    """
    Removing a gap in the UI must reach the contradiction checker, so
    downstream agents are told the human overrode Gaper.
    """
    db = env
    db.create_run("RUN-RT-1", "A problem")
    db.insert_gap({"gap_id": "GAP-1", "run_id": "RUN-RT-1",
                   "description": "No longitudinal data",
                   "significance": "High", "gap_type": "unstudied"})

    notices = breaks.check_contradictions("REMOVE GAP GAP-1", "RUN-RT-1", 1)
    assert len(notices) == 1
    assert "GAP-1" in notices[0]
    assert "High" in notices[0]


def test_verdict_override_is_detected_as_a_contradiction(env):
    db = env
    db.create_run("RUN-RT-2", "A problem")
    db.insert_proposal({"proposal_id": "PRO-1", "run_id": "RUN-RT-2",
                        "proposal": "A proposal"})
    db.insert_evaluation({"evaluation_id": "EVAL-1", "run_id": "RUN-RT-2",
                          "proposal_id": "PRO-1", "verdict": "unfeasible",
                          "verdict_reason": "No instrument exists"})

    notices = breaks.check_contradictions(
        "OVERRIDE VERDICT EVAL-1: the instrument was published last year",
        "RUN-RT-2", 2)
    assert len(notices) == 1
    assert "EVAL-1" in notices[0]
    assert "unfeasible" in notices[0]


def test_a_full_widget_submission_parses_end_to_end(env):
    """
    Everything a Break 2 screen can emit at once, exactly as the UI joins it:
    directives first, free text last.
    """
    db = env
    db.create_run("RUN-RT-3", "A problem")
    db.insert_proposal({"proposal_id": "PRO-9", "run_id": "RUN-RT-3",
                        "proposal": "A proposal"})
    db.insert_evaluation({"evaluation_id": "EVAL-9", "run_id": "RUN-RT-3",
                          "proposal_id": "PRO-9", "verdict": "unfeasible",
                          "verdict_reason": "Too costly"})

    submission = "\n".join([
        "OVERRIDE VERDICT EVAL-9: funding was secured in March",
        "SCRIBE OUTPUT: research_brief | audience: collaborators",
        "SCRIBE OUTPUT: blog_post | audience: general public",
        "Prioritise the measurement question over the theoretical one.",
    ])

    requests = breaks.parse_scribe_requests(submission)
    assert [r["output_type"] for r in requests] == ["research_brief", "blog_post"]
    assert requests[0]["audience"] == "collaborators"

    notices = breaks.check_contradictions(submission, "RUN-RT-3", 2)
    assert len(notices) == 1
    assert "EVAL-9" in notices[0]


def test_free_text_alone_does_not_break_parsing(env):
    """Plain prose must not be mistaken for a directive."""
    submission = "Please weight recent empirical work more heavily than theory."
    assert breaks.parse_scribe_requests(submission) == [
        {"output_type": "research_brief", "audience": "researcher"}
    ]
    assert breaks.check_contradictions(submission, "RUN-RT-4", 1) == []
