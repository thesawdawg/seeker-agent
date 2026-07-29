"""
Pipeline state machine.

Covers the inversion that makes the pipeline drivable over HTTP: steps as
persisted records, parking at breaks without blocking, resuming, re-running a
completed step, and cascade invalidation of downstream work.

Agents are stubbed — this is about control flow, not agent behaviour.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated SQLite database plus a minimal config."""
    import core.database as db
    from core import db_backend, pipeline

    monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pipeline.db")
    db_backend.reset_backend()
    pipeline._schema_ready = False
    db.init_db()
    pipeline.init_steps_table()

    config = {"themes": [
        {"theme_id": "philosophy_of_mind", "label": "Philosophy of Mind",
         "keywords": [{"seed": "consciousness"}]},
        {"theme_id": "social_identity", "label": "Social Identity",
         "keywords": [{"seed": "identity"}]},
    ]}
    yield db, pipeline, config
    db_backend.reset_backend()
    pipeline._schema_ready = False


@pytest.fixture()
def stub_agents(monkeypatch):
    """Replace agent execution with a recorder."""
    from core import pipeline
    calls = []

    def fake_agent(step_name, run_id, problem, config):
        calls.append(step_name)

    def fake_mapper(run_id, problem, config):
        calls.append("concept_mapper")

    monkeypatch.setattr(pipeline, "_run_agent_step", fake_agent)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", fake_mapper)
    return calls


# ---------------------------------------------------------------------------
# Step records
# ---------------------------------------------------------------------------

def test_create_run_registers_all_steps(env):
    db, pipeline, _ = env
    run_id = pipeline.create_run("A problem")

    steps = pipeline.get_steps(run_id)
    assert [s["step_name"] for s in steps] == [s.name for s in pipeline.STEP_DEFS]
    assert all(s["status"] == "pending" for s in steps)
    assert [s["ordinal"] for s in steps] == sorted(s["ordinal"] for s in steps)


def test_ensure_steps_is_idempotent(env):
    db, pipeline, _ = env
    run_id = pipeline.create_run("A problem")
    pipeline.ensure_steps(run_id)
    pipeline.ensure_steps(run_id)
    assert len(pipeline.get_steps(run_id)) == len(pipeline.STEP_DEFS)


# ---------------------------------------------------------------------------
# Parking at breaks
# ---------------------------------------------------------------------------

def test_advance_parks_at_break0_without_blocking(env, stub_agents):
    """The whole point: reaching a break returns instead of waiting on stdin."""
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")

    state = pipeline.advance(run_id, config=config)

    assert state["awaiting_break"] == 0
    assert state["current_step"] == "break0"
    assert not state["complete"]
    assert stub_agents == ["concept_mapper"], "should stop before Grounder"
    assert db.get_run(run_id)["status"] == "awaiting_break0"


def test_advance_is_idempotent_while_parked(env, stub_agents):
    """Polling a parked run must not re-run work or move it forward."""
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")

    pipeline.advance(run_id, config=config)
    stub_agents.clear()
    state = pipeline.advance(run_id, config=config)

    assert state["awaiting_break"] == 0
    assert stub_agents == []


def test_full_run_stops_at_each_break_in_order(env, stub_agents):
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")

    seen = []
    for _ in range(10):
        state = pipeline.advance(run_id, config=config)
        if state["complete"]:
            break
        assert state["awaiting_break"] is not None
        seen.append(state["awaiting_break"])
        pipeline.submit_break(run_id, state["awaiting_break"], "CONFIRMED")

    assert seen == [0, 1, 2]
    assert pipeline.get_state(run_id)["complete"]
    assert db.get_run(run_id)["status"] == "completed"
    assert stub_agents == [
        "concept_mapper", "grounder", "social", "historian", "gaper",
        "librarian",
        "vision", "theorist", "rude", "synthesizer", "thinker", "scribe",
        "reporter",
    ]


def test_submit_break_persists_and_releases(env, stub_agents):
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)

    pipeline.submit_break(run_id, 0, "ADD THEME: social_identity", source="web")

    stored = db.get_break_instructions(run_id, 0)
    assert stored["instructions"] == "ADD THEME: social_identity"
    assert stored["source"] == "web"
    assert db.get_run(run_id)["break0_done"] == 1
    assert pipeline.get_step(run_id, "break0")["status"] == "done"

    state = pipeline.advance(run_id, config=config)
    assert state["awaiting_break"] == 1, "should proceed to the next break"


def test_empty_break_answer_defaults_to_confirmed(env, stub_agents):
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)

    pipeline.submit_break(run_id, 0, "   ")
    assert db.get_break_instructions(run_id, 0)["instructions"] == "CONFIRMED"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def test_failed_step_halts_and_records_error(env, monkeypatch):
    db, pipeline, config = env

    def boom(step_name, run_id, problem, config):
        if step_name == "grounder":
            raise RuntimeError("search backend exploded")

    monkeypatch.setattr(pipeline, "_run_agent_step", boom)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)
    pipeline.submit_break(run_id, 0, "CONFIRMED")
    state = pipeline.advance(run_id, config=config)

    assert state["failed_steps"] == ["grounder"]
    step = pipeline.get_step(run_id, "grounder")
    assert step["status"] == "failed"
    assert "search backend exploded" in step["error"]
    assert db.get_run(run_id)["status"] == "failed:grounder"


def test_non_fatal_step_is_skipped_not_fatal(env, monkeypatch):
    """Social is a discovery layer — an outage must not sink the run."""
    db, pipeline, config = env
    reached = []

    def selective(step_name, run_id, problem, config):
        reached.append(step_name)
        if step_name == "social":
            raise RuntimeError("source API down")

    monkeypatch.setattr(pipeline, "_run_agent_step", selective)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)
    pipeline.submit_break(run_id, 0, "CONFIRMED")
    state = pipeline.advance(run_id, config=config)

    assert pipeline.get_step(run_id, "social")["status"] == "skipped"
    assert state["failed_steps"] == []
    assert "historian" in reached, "should carry on past Social"
    assert state["awaiting_break"] == 1


def test_resume_after_failure_retries_only_that_step(env, monkeypatch):
    db, pipeline, config = env
    attempts = {"n": 0}

    def flaky(step_name, run_id, problem, config):
        if step_name == "grounder":
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient")

    monkeypatch.setattr(pipeline, "_run_agent_step", flaky)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)
    pipeline.submit_break(run_id, 0, "CONFIRMED")
    pipeline.advance(run_id, config=config)
    assert pipeline.get_step(run_id, "grounder")["status"] == "failed"

    # A failed step is retried on the next advance, and the run moves on
    state = pipeline.advance(run_id, config=config)
    assert pipeline.get_step(run_id, "grounder")["status"] == "done"
    assert state["awaiting_break"] == 1
    assert pipeline.get_step(run_id, "break0")["status"] == "done", "break not re-asked"


def test_resume_never_steps_over_a_failed_step(env, monkeypatch):
    """
    A failed step must stay the next step. Treating it as finished would run
    the rest of the pipeline on missing data — silent, and hard to notice.
    """
    db, pipeline, config = env

    def always_fails(step_name, run_id, problem, config):
        if step_name == "grounder":
            raise RuntimeError("permanent")

    monkeypatch.setattr(pipeline, "_run_agent_step", always_fails)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)
    pipeline.submit_break(run_id, 0, "CONFIRMED")

    for _ in range(3):
        state = pipeline.advance(run_id, config=config)
        assert state["current_step"] == "grounder"
        assert not state["complete"]

    assert pipeline.get_step(run_id, "social")["status"] == "pending"
    assert pipeline.get_step(run_id, "grounder")["attempt"] >= 3


# ---------------------------------------------------------------------------
# Re-running steps
# ---------------------------------------------------------------------------

def test_downstream_steps_ordering(env):
    _, pipeline, _ = env
    downstream = pipeline.downstream_steps("gaper")
    assert downstream[0] == "librarian"
    assert downstream[1] == "break1"
    assert downstream[-1] == "reporter"
    assert "grounder" not in downstream


def test_rerun_cascades_and_purges_outputs(env, stub_agents):
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")

    for _ in range(4):
        state = pipeline.advance(run_id, config=config)
        if state["complete"]:
            break
        pipeline.submit_break(run_id, state["awaiting_break"], "CONFIRMED")

    # Outputs that a re-run must discard
    db.insert_gap({"gap_id": "GAP-1", "run_id": run_id,
                   "description": "a gap", "significance": "High"})
    db.insert_implication({"implication_id": "IMP-1", "run_id": run_id,
                           "implication": "an implication"})
    assert db.count("gaps", {"run_id": run_id}) == 1

    reset = pipeline.reset_step(run_id, "gaper")

    assert "gaper" in reset and "vision" in reset and "scribe" in reset
    assert "librarian" in reset, "librarian is downstream of gaper"
    assert "grounder" not in reset, "upstream work must survive"
    assert db.count("gaps", {"run_id": run_id}) == 0
    assert db.count("implications", {"run_id": run_id}) == 0
    assert pipeline.get_step(run_id, "gaper")["status"] == "pending"
    assert pipeline.get_step(run_id, "grounder")["status"] == "done"


def test_rerun_a_break_reopens_it(env, stub_agents):
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)
    pipeline.submit_break(run_id, 0, "ADD THEME: social_identity")
    pipeline.advance(run_id, config=config)

    pipeline.reset_step(run_id, "break0")

    assert db.get_break_instructions(run_id, 0) is None
    assert db.get_run(run_id)["break0_done"] == 0
    state = pipeline.advance(run_id, config=config)
    assert state["awaiting_break"] == 0, "the break should be asked again"


def test_rerun_only_leaves_downstream_alone(env, stub_agents):
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")
    for _ in range(4):
        state = pipeline.advance(run_id, config=config)
        if state["complete"]:
            break
        pipeline.submit_break(run_id, state["awaiting_break"], "CONFIRMED")

    pipeline.reset_step(run_id, "vision", cascade=False)

    assert pipeline.get_step(run_id, "vision")["status"] == "pending"
    assert pipeline.get_step(run_id, "theorist")["status"] == "done"


def test_rerun_unknown_step_raises(env):
    _, pipeline, _ = env
    run_id = pipeline.create_run("A problem")
    with pytest.raises(ValueError):
        pipeline.reset_step(run_id, "not_a_step")


# ---------------------------------------------------------------------------
# Break payloads — what the web UI renders
# ---------------------------------------------------------------------------

def test_break0_payload_lists_themes_with_selection(env, stub_agents):
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)

    payload = pipeline.break_payload(run_id, 0, config)

    assert payload["break_num"] == 0
    assert payload["answered"] is False
    ids = {t["theme_id"] for t in payload["fields"]["themes"]}
    assert ids == {"philosophy_of_mind", "social_identity"}
    assert any(d["command"].startswith("ADD THEME") for d in payload["directives"])
    assert Path(payload["document"]).exists()


def test_break1_payload_carries_gaps_and_sources(env, stub_agents):
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)
    pipeline.submit_break(run_id, 0, "CONFIRMED")
    pipeline.advance(run_id, config=config)

    db.insert_gap({"gap_id": "GAP-7", "run_id": run_id,
                   "description": "No longitudinal data", "significance": "High"})
    db.upsert_source({"source_id": "SRC-1", "title": "Mind, Self and Society",
                      "type": "seminal", "run_id": run_id, "year": 1934})

    payload = pipeline.break_payload(run_id, 1, config)
    assert [g["gap_id"] for g in payload["fields"]["gaps"]] == ["GAP-7"]
    assert [s["title"] for s in payload["fields"]["seminal"]] == ["Mind, Self and Society"]


def test_break1_document_includes_full_references_with_backlinks(env, stub_agents):
    """Break 1 document must include DOI/URL backlinks and abstracts for
    seminal works so the researcher can read the original and validate claims."""
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)
    pipeline.submit_break(run_id, 0, "CONFIRMED")
    pipeline.advance(run_id, config=config)

    db.upsert_source({
        "source_id": "SRC-REF1", "title": "The Structure of Scientific Revolutions",
        "type": "seminal", "run_id": run_id, "year": 1962,
        "authors": ["Thomas Kuhn"],
        "doi": "10.1234/test.doi",
        "active_link": "https://arxiv.org/abs/2401.00001",
        "abstract": "A foundational work on paradigm shifts in science.",
        "seminal_reason": "Established the concept of paradigm shifts.",
        "source_name": "University of Chicago Press",
        "url_origin": "provider_api",
    })

    payload = pipeline.break_payload(run_id, 1, config)
    doc_path = Path(payload["document"])
    doc_text = doc_path.read_text()

    # Title and seminal reason must be present
    assert "The Structure of Scientific Revolutions" in doc_text
    assert "Established the concept of paradigm shifts" in doc_text

    # Full reference: authors
    assert "Thomas Kuhn" in doc_text

    # DOI backlink
    assert "https://doi.org/10.1234/test.doi" in doc_text

    # URL backlink
    assert "https://arxiv.org/abs/2401.00001" in doc_text

    # Abstract excerpt
    assert "A foundational work on paradigm shifts" in doc_text

    # Source/venue
    assert "University of Chicago Press" in doc_text


def test_break1_document_shows_catalog_availability(env, stub_agents):
    """Break 1 document includes library catalog availability from the
    Librarian step when catalog data is present on the source."""
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)
    pipeline.submit_break(run_id, 0, "CONFIRMED")
    pipeline.advance(run_id, config=config)

    db.upsert_source({
        "source_id": "SRC-CAT1", "title": "A Seminal Book",
        "type": "seminal", "run_id": run_id, "year": 1962,
        "authors": ["Test Author"],
        "seminal_reason": "Foundational work.",
        "catalog_url": "https://catalog.example.com/record/123",
        "availability": '["Online", "Physical"]',
        "catalog_checked": "2026-01-01T00:00:00Z",
    })

    payload = pipeline.break_payload(run_id, 1, config)
    doc_text = Path(payload["document"]).read_text()

    assert "A Seminal Book" in doc_text
    assert "https://catalog.example.com/record/123" in doc_text
    assert "Catalog availability" in doc_text
    assert "Online" in doc_text and "Physical" in doc_text


def test_librarian_skips_when_primo_not_configured(env, monkeypatch):
    """Librarian should complete gracefully when Primo is not configured."""
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")

    # Ensure Primo is not configured
    from core import primo
    monkeypatch.setattr(primo, "is_primo_configured", lambda: False)

    from agents.librarian import run as librarian_run
    # Should not raise
    librarian_run(f"PROBLEM:\nA problem", run_id)


def test_librarian_skips_when_mcp_toggle_disabled(env, monkeypatch):
    """Librarian should skip when the user has disabled the Primo MCP toggle."""
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")

    # Create a user and link them as the run owner
    from core import users
    users._schema_ready = False
    users._owner_schema_ready = False
    users.init_users_tables()
    users.init_run_owners()
    user = users.get_or_create("test-key-mcp-toggle", display_name="TestUser")
    users.claim_run(run_id, user["user_id"], provider="test")
    # Disable the primo MCP toggle
    users.set_mcp_toggle(user["user_id"], "primo", enabled=False)

    # Even if Primo is configured, the toggle should prevent the search
    from core import primo
    monkeypatch.setattr(primo, "is_primo_configured", lambda: True)

    # Add a source so the librarian would have something to search for
    db.upsert_source({
        "source_id": "SRC-TOGGLE1", "title": "A Test Book",
        "type": "seminal", "run_id": run_id, "year": 2020,
    })

    from agents.librarian import run as librarian_run
    librarian_run(f"PROBLEM:\nA problem", run_id)

    # The source should NOT have been enriched (catalog_checked should be None)
    sources = db.get_sources_by_type("seminal", run_id)
    assert len(sources) == 1
    assert sources[0].get("catalog_checked") is None
    assert sources[0].get("primo_record_id") is None


def test_librarian_enriches_sources_with_catalog_data(env, monkeypatch):
    """Librarian should search Primo and update sources with catalog data."""
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")

    # Add a source to search for
    db.upsert_source({
        "source_id": "SRC-LIB1", "title": "The Structure of Scientific Revolutions",
        "type": "seminal", "run_id": run_id, "year": 1962,
        "doi": "10.1234/test",
    })

    # Mock Primo as configured and returning a result
    from core import primo
    monkeypatch.setattr(primo, "is_primo_configured", lambda: True)
    monkeypatch.setattr(primo, "find_source_in_primo", lambda **kwargs: {
        "record_id": "alma990001234",
        "title": "The Structure of Scientific Revolutions",
        "record_url": "https://catalog.example.com/record/990001234",
        "availability": ["Online", "Physical"],
        "isbn": "9780226458040",
        "issn": None,
    })

    from agents.librarian import run as librarian_run
    librarian_run(f"PROBLEM:\nA problem", run_id)

    # Verify the source was enriched
    sources = db.get_sources_by_type("seminal", run_id)
    assert len(sources) == 1
    s = sources[0]
    assert s.get("primo_record_id") == "alma990001234"
    assert s.get("catalog_url") == "https://catalog.example.com/record/990001234"
    assert "Online" in (s.get("availability") or "")
    assert s.get("catalog_checked") is not None


def test_payload_reports_a_previously_answered_break(env, stub_agents):
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)
    pipeline.submit_break(run_id, 0, "REMOVE THEME: social_identity")

    payload = pipeline.break_payload(run_id, 0, config)
    assert payload["answered"] is True
    assert payload["instructions"] == "REMOVE THEME: social_identity"


# ---------------------------------------------------------------------------
# Theme directives — the shared language between CLI and web
# ---------------------------------------------------------------------------

def test_apply_theme_directives(env):
    from core import breaks
    _, _, config = env
    all_themes = config["themes"]
    selected = [all_themes[0]]

    added = breaks.apply_theme_directives("ADD THEME: social_identity",
                                          selected, all_themes)
    assert {t["theme_id"] for t in added} == {"philosophy_of_mind", "social_identity"}

    removed = breaks.apply_theme_directives("REMOVE THEME: philosophy_of_mind",
                                           selected, all_themes)
    assert removed == []

    unknown = breaks.apply_theme_directives("ADD THEME: does_not_exist",
                                            selected, all_themes)
    assert {t["theme_id"] for t in unknown} == {"philosophy_of_mind"}


def test_break0_directives_reach_the_social_step(env, monkeypatch):
    """A Break 0 theme change must actually alter what Social searches."""
    from core import pipeline as pl
    db, pipeline, config = env
    captured = {}

    def capture(step_name, run_id, problem, config_):
        if step_name == "social":
            captured["themes"] = [
                t["theme_id"] for t in pl._selected_themes(run_id, config_)
            ]

    monkeypatch.setattr(pipeline, "_run_agent_step", capture)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)
    pipeline.submit_break(run_id, 0, "REMOVE THEME: social_identity")
    pipeline.advance(run_id, config=config)

    assert captured["themes"] == ["philosophy_of_mind"]


# ---------------------------------------------------------------------------
# Per-run LLM routing actually reaching the agents
#
# Agents call llm.call(prompt, system, agent_name=...) with no run_id. If the
# driver does not bind the run, a user's own provider and any model override
# are silently ignored and every agent falls through to config.json.
# ---------------------------------------------------------------------------

def test_advance_binds_the_run_for_llm_calls(env, monkeypatch):
    from core import llm, pipeline as pl
    db, pipeline, config = env
    seen = []

    def capture(step_name, run_id, problem, config_):
        # Exactly how agents call it — no run_id argument
        seen.append((step_name, llm.current_run()))

    monkeypatch.setattr(pipeline, "_run_agent_step", capture)
    monkeypatch.setattr(pipeline, "_run_concept_mapper",
                        lambda r, p, c: seen.append(("concept_mapper", llm.current_run())))

    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)
    pipeline.submit_break(run_id, 0, "CONFIRMED")
    pipeline.advance(run_id, config=config)

    assert seen, "no steps ran"
    for step_name, bound in seen:
        assert bound == run_id, f"{step_name} ran without the run bound"


def test_binding_is_cleared_after_advance(env, stub_agents):
    """A worker handles many runs; the binding must not leak between them."""
    from core import llm
    db, pipeline, config = env
    run_id = pipeline.create_run("A problem")
    pipeline.advance(run_id, config=config)
    assert llm.current_run() is None


def test_a_users_provider_reaches_an_agents_call(env, monkeypatch):
    """The end-to-end path M4 depends on, exercised through advance()."""
    from core import llm
    db, pipeline, config = env
    resolved = {}

    def capture(step_name, run_id, problem, config_):
        if step_name == "grounder":
            # No run_id passed — the plan must still find the user's provider
            resolved['plan'] = llm.get_client().describe_plan("grounder")

    monkeypatch.setattr(pipeline, "_run_agent_step", capture)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = pipeline.create_run("A problem")
    llm.set_run_providers(run_id, {"their-owui": llm.ProviderConfig(
        name="their-owui", kind="openai", base_url="http://their-host/api",
        api_key="sk-theirs", models={"primary": "their-model"})})
    try:
        pipeline.advance(run_id, config=config)
        pipeline.submit_break(run_id, 0, "CONFIRMED")
        pipeline.advance(run_id, config=config)

        plan = resolved.get('plan')
        assert plan, "grounder never ran"
        assert plan[0]["provider"] == "their-owui"
        assert plan[0]["model"] == "their-model"
    finally:
        llm.clear_run_providers(run_id)


def test_stored_model_override_reaches_an_agents_call(env, monkeypatch):
    """A model chosen at a break must apply without agents passing run_id."""
    from core import llm
    db, pipeline, config = env
    resolved = {}

    def capture(step_name, run_id, problem, config_):
        if step_name == "vision":
            resolved['plan'] = llm.get_client().describe_plan("vision")

    monkeypatch.setattr(pipeline, "_run_agent_step", capture)
    monkeypatch.setattr(pipeline, "_run_concept_mapper", lambda *a: None)

    run_id = pipeline.create_run("A problem")
    llm.set_run_providers(run_id, {"owui": llm.ProviderConfig(
        name="owui", kind="openai", base_url="http://host/api",
        api_key="k", models={"primary": "default-model"})})
    try:
        pipeline.advance(run_id, config=config)
        pipeline.submit_break(run_id, 0, "CONFIRMED")
        pipeline.set_model_overrides(run_id, {"vision": {"model": "chosen-at-break"}})
        # Simulate a fresh worker process: only the database carries the choice
        llm.clear_run_overrides(run_id)

        pipeline.advance(run_id, config=config)
        pipeline.submit_break(run_id, 1, "CONFIRMED")
        pipeline.advance(run_id, config=config)

        assert resolved.get('plan'), "vision never ran"
        assert resolved['plan'][0]["model"] == "chosen-at-break"
    finally:
        llm.clear_run_providers(run_id)
        llm.clear_run_overrides(run_id)


# ---------------------------------------------------------------------------
# State shape consumed by the CLI and (next) the HTTP API
# ---------------------------------------------------------------------------

def test_get_state_shape(env, stub_agents):
    db, pipeline, config = env
    run_id = pipeline.create_run("A research problem")
    pipeline.advance(run_id, config=config)

    state = pipeline.get_state(run_id)
    for key in ("run_id", "exists", "problem", "status", "steps", "current_step",
                "running", "awaiting_break", "progress", "failed_steps", "complete"):
        assert key in state, f"missing {key}"
    assert state["progress"]["total"] == len(pipeline.STEP_DEFS)
    assert state["progress"]["done"] >= 1
    assert all("label" in s for s in state["steps"])


def test_get_state_for_unknown_run(env):
    _, pipeline, _ = env
    assert pipeline.get_state("RUN-NOPE")["exists"] is False
