"""
Unattended operation and step activity.

A worker runs with no terminal and no human. Any source that wants a browser
login must be skipped, not waited on — a blocking OAuth prompt stalls the run
and, in a container, can never succeed.

Also covers the activity reporting that makes a long step legible: which
external service it is talking to right now.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Consensus must never block a worker
# ---------------------------------------------------------------------------

def test_importing_consensus_binds_no_port():
    """
    Regression: the module started an OAuth callback server at import time,
    so every process that merely imported it bound a port.
    """
    import core.consensus_mcp as consensus
    assert consensus._callback_server is None


def test_interactive_auth_is_off_by_default(monkeypatch):
    import core.consensus_mcp as consensus
    monkeypatch.delenv("SEEKER_CONSENSUS_INTERACTIVE", raising=False)
    assert consensus.interactive_auth_allowed() is False


def test_interactive_auth_still_needs_a_terminal(monkeypatch):
    """The env var alone must not enable it inside a container."""
    import core.consensus_mcp as consensus
    monkeypatch.setenv("SEEKER_CONSENSUS_INTERACTIVE", "1")
    monkeypatch.setattr(sys, "stdin", type("F", (), {"isatty": lambda self: False})())
    assert consensus.interactive_auth_allowed() is False


def test_search_returns_empty_without_credentials(monkeypatch):
    """
    Regression: this opened a browser and blocked for up to 300 seconds.
    It must return quickly and empty instead.
    """
    import core.consensus_mcp as consensus
    monkeypatch.delenv("SEEKER_CONSENSUS_INTERACTIVE", raising=False)
    monkeypatch.setattr(consensus, "have_tokens", lambda: False)
    monkeypatch.setattr(consensus, "_warned_unavailable", False)

    def explode(*a, **k):
        raise AssertionError("a browser must never be opened")

    monkeypatch.setattr(consensus.webbrowser, "open", explode)
    assert consensus.search_consensus("any query") == []


def test_search_never_raises(monkeypatch):
    """An optional source must not be able to fail a step."""
    import core.consensus_mcp as consensus
    monkeypatch.setattr(consensus, "have_tokens", lambda: True)
    monkeypatch.setattr(consensus, "interactive_auth_allowed", lambda: False)

    async def boom(*a, **k):
        raise RuntimeError("the MCP server exploded")

    monkeypatch.setattr(consensus, "_async_search", boom)
    assert consensus.search_consensus("any query") == []


def test_unavailable_warning_is_logged_once(monkeypatch, caplog):
    """A per-query warning would bury the rest of the log."""
    import core.consensus_mcp as consensus
    monkeypatch.delenv("SEEKER_CONSENSUS_INTERACTIVE", raising=False)
    monkeypatch.setattr(consensus, "have_tokens", lambda: False)
    monkeypatch.setattr(consensus, "_warned_unavailable", False)

    with caplog.at_level("WARNING"):
        for _ in range(5):
            consensus.search_consensus("query")

    warnings = [r for r in caplog.records if "Consensus" in r.getMessage()]
    assert len(warnings) == 1


def test_grounder_consensus_search_degrades(monkeypatch):
    """Grounder's wrapper must pass the empty result through, not raise."""
    import agents.grounder as grounder
    monkeypatch.setattr("core.consensus_mcp.search_consensus", lambda *a, **k: [])
    assert grounder._search_consensus("a query") == []


# ---------------------------------------------------------------------------
# Step activity
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    import core.database as db
    from core import db_backend, pipeline

    monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pipeline.db")
    db_backend.reset_backend()
    pipeline._schema_ready = False
    db.init_db()
    yield db, pipeline
    db_backend.reset_backend()
    pipeline._schema_ready = False


def test_columns_added_after_the_table_existed(env):
    """
    Regression: activity/activity_at were added to run_steps in a later
    version. CREATE TABLE IF NOT EXISTS does nothing to an existing table, so
    the columns never appeared and every write to them failed silently.
    """
    from core import db_backend, progress
    db, pipeline = env

    # Recreate the table as it looked before the columns were introduced
    with db_backend.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS run_steps")
        cur.execute("""
            CREATE TABLE run_steps (
                step_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                step_name TEXT NOT NULL, ordinal INTEGER NOT NULL,
                kind TEXT NOT NULL, status TEXT DEFAULT 'pending',
                attempt INTEGER DEFAULT 0, started_at TEXT,
                finished_at TEXT, error TEXT)
        """)
    assert "activity" not in db_backend.existing_columns("run_steps")

    pipeline._schema_ready = False
    pipeline.init_steps_table()

    assert "activity" in db_backend.existing_columns("run_steps")
    assert "activity_at" in db_backend.existing_columns("run_steps")

    # ...and the column is genuinely writable
    run_id = pipeline.create_run("A problem")
    token = progress.bind(run_id, "grounder")
    try:
        progress.note("consensus", "searching")
    finally:
        progress.release(token)
    assert "Consensus" in pipeline.get_step(run_id, "grounder")["activity"]


def test_ensure_columns_is_idempotent(env):
    from core import db_backend
    added_first = db_backend.ensure_columns("run_steps", {"activity": "{TEXT}"})
    added_again = db_backend.ensure_columns("run_steps", {"activity": "{TEXT}"})
    assert added_first == [] and added_again == [], "column already present"


def test_note_outside_a_run_is_harmless():
    """Agents are also used from the CLI and tools, with nothing bound."""
    from core import progress
    progress.note("openalex", "searching", "a query")   # must not raise


def test_note_records_the_service_on_the_step(env):
    from core import progress
    db, pipeline = env
    run_id = pipeline.create_run("A problem")

    token = progress.bind(run_id, "grounder")
    try:
        progress.note("consensus", "searching", "AI academic integration history")
    finally:
        progress.release(token)

    step = pipeline.get_step(run_id, "grounder")
    assert "Consensus" in step["activity"]
    assert "searching" in step["activity"]
    assert step["activity_at"]


def test_activity_is_cleared_when_a_step_finishes(env):
    from core import progress
    db, pipeline = env
    run_id = pipeline.create_run("A problem")

    token = progress.bind(run_id, "grounder")
    try:
        progress.note("openalex", "searching")
        assert pipeline.get_step(run_id, "grounder")["activity"]
        progress.clear()
    finally:
        progress.release(token)

    assert pipeline.get_step(run_id, "grounder")["activity"] is None


def test_advance_binds_progress_for_each_step(env, monkeypatch):
    """Agents report activity without knowing which step they are."""
    from core import progress
    db, pipeline = env
    seen = {}

    def fake_agent(step_name, run_id, problem, config):
        ctx = progress.context()
        seen[step_name] = ctx and ctx["step"]
        progress.note("openalex", "searching")

    monkeypatch.setattr(pipeline, "_run_agent_step", fake_agent)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config={"themes": []})
    pipeline.submit_break(run_id, 0, "CONFIRMED")
    pipeline.advance(run_id, config={"themes": []})

    assert seen.get("grounder") == "grounder"


def test_binding_does_not_leak_after_advance(env, monkeypatch):
    """A worker handles many runs — activity context must not carry over."""
    from core import progress
    db, pipeline = env
    monkeypatch.setattr(pipeline, "_run_agent_step", lambda *a: None)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config={"themes": []})
    assert progress.context() is None


def test_service_labels_cover_configured_sources():
    """Every source a step can be configured with should have a human label."""
    from core import progress
    from core.utils import load_config

    config = load_config()
    configured = set()
    for step, sources in (config.get("agent_sources") or {}).items():
        if step.startswith("_") or not isinstance(sources, list):
            continue
        configured.update(s for s in sources if isinstance(s, str))

    unlabelled = [s for s in configured if s not in progress.SERVICE_LABELS]
    assert not unlabelled, f"sources with no display label: {sorted(unlabelled)}"
