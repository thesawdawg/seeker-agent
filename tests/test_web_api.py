"""
HTTP API and worker.

Exercises the real FastAPI app against an isolated database. The model
provider is stood up as a local HTTP server so key validation, login and
model listing are genuinely tested rather than mocked.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

TEST_SECRET = "yhAe3sQ0m2t5rVQKpZ8xN1cJ7wS4bG6dY9uH0iL2kM0="


# ---------------------------------------------------------------------------
# A stand-in Open-WebUI
# ---------------------------------------------------------------------------

class FakeProvider:
    def __init__(self, valid_key="sk-valid-key"):
        self.valid_key = valid_key
        self.models = ["qwen3:32b", "llama3.2:3b"]
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self._server.server_port}/api"

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorised(self):
                return self.headers.get("Authorization") == f"Bearer {outer.valid_key}"

            def do_GET(self):
                if not self._authorised():
                    return self._send(401, {"error": "bad key"})
                if self.path.endswith("/models"):
                    return self._send(200, {"data": [{"id": m} for m in outer.models]})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._authorised():
                    return self._send(401, {"error": "bad key"})
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self._send(200, {"choices": [{"message": {"content": "ok"}}]})

        return Handler


@pytest.fixture()
def provider():
    p = FakeProvider()
    yield p
    p.stop()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """The real app, against a throwaway database and in-memory sessions."""
    import core.database as db
    from core import db_backend, jobs, pipeline, users
    from web import auth

    monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
    monkeypatch.setenv("SEEKER_SECRET_KEY", TEST_SECRET)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pipeline.db")

    db_backend.reset_backend()
    for module in (pipeline, users, jobs):
        for flag in ("_schema_ready", "_owner_schema_ready"):
            if hasattr(module, flag):
                setattr(module, flag, False)
    auth.reset_sessions()

    from web.app import app
    with TestClient(app) as c:
        yield c

    db_backend.reset_backend()
    auth.reset_sessions()


def sign_in(client, provider, key=None, name="Sawyer"):
    resp = client.post("/api/auth/login", json={
        "base_url": provider.base_url,
        "api_key": key or provider.valid_key,
        "display_name": name,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture()
def stub_agents(monkeypatch):
    from core import pipeline
    calls = []
    monkeypatch.setattr(pipeline, "_run_agent_step",
                        lambda n, r, p, c: calls.append(n))
    monkeypatch.setattr(pipeline, "_run_concept_mapper",
                        lambda r, p, c: calls.append("concept_mapper"))
    return calls


def drain(config=None):
    """Run the worker until the queue is empty."""
    from core import jobs
    import worker
    from core.utils import load_config
    processed = 0
    while True:
        job = jobs.claim_next()
        if not job:
            return processed
        worker.process_job(job, config or load_config())
        processed += 1


# ---------------------------------------------------------------------------
# Health and auth
# ---------------------------------------------------------------------------

def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["storage"] == "sqlite"
    assert body["secrets_configured"] is True


def test_login_rejects_a_bad_key(client, provider):
    resp = client.post("/api/auth/login", json={
        "base_url": provider.base_url, "api_key": "sk-wrong",
    })
    assert resp.status_code == 401


def test_login_reports_unreachable_provider_separately(client):
    """A dead endpoint is a different problem from a rejected key."""
    resp = client.post("/api/auth/login", json={
        "base_url": "http://127.0.0.1:1/api", "api_key": "sk-x",
    })
    assert resp.status_code == 502


def test_login_creates_user_and_session(client, provider):
    body = sign_in(client, provider)
    assert body["user_id"].startswith("USR-")
    assert body["session"]
    assert body["models"] == ["llama3.2:3b", "qwen3:32b"]

    me = client.get("/api/auth/me").json()
    assert me["display_name"] == "Sawyer"
    assert me["credentials"][0]["provider"] == "open-webui"


def test_same_key_returns_the_same_user(client, provider):
    first = sign_in(client, provider)
    second = sign_in(client, provider)
    assert first["user_id"] == second["user_id"]


def test_protected_routes_require_a_session(client):
    for method, path in [("get", "/api/runs"), ("get", "/api/auth/me"),
                         ("get", "/api/credentials")]:
        assert getattr(client, method)(path).status_code == 401


def test_logout_invalidates_the_session(client, provider):
    sign_in(client, provider)
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def test_api_key_is_never_returned(client, provider):
    """The stored key unlocks the user's provider account — it must not leak."""
    sign_in(client, provider)
    body = client.get("/api/credentials").json()
    serialised = json.dumps(body)
    assert provider.valid_key not in serialised
    assert body["credentials"][0]["key_hint"].startswith("sk-v")


def test_stored_key_is_encrypted_at_rest(client, provider):
    from core import users
    sign_in(client, provider)
    me = client.get("/api/auth/me").json()

    row = users.get_credentials_row(me["user_id"], "open-webui")
    assert row["api_key_enc"]
    assert provider.valid_key not in row["api_key_enc"]
    # ...and round-trips back to the original
    cfg = users.provider_config(me["user_id"], "open-webui")
    assert cfg.api_key == provider.valid_key


def test_model_roles_can_be_set_without_resending_the_key(client, provider):
    """
    Agents pick a role, not a model name, so a provider with no roles assigned
    has nothing for them to call. The key is never returned to the client, so
    roles must be settable on their own.
    """
    from core import users
    sign_in(client, provider)
    me = client.get("/api/auth/me").json()
    assert me["credentials"][0]["models"] == {}, "a new sign-in has no roles yet"

    resp = client.patch("/api/credentials/open-webui/models",
                        json={"models": {"primary": "qwen3:32b",
                                         "light": "llama3.2:3b"}})
    assert resp.status_code == 200
    assert resp.json()["credential"]["models"]["primary"] == "qwen3:32b"

    # ...and the roles reach the provider config the worker builds
    cfg = users.provider_config(me["user_id"], "open-webui")
    assert cfg.model_for_role("primary") == "qwen3:32b"
    assert cfg.model_for_role("light") == "llama3.2:3b"
    assert cfg.api_key == provider.valid_key, "the key must survive a roles update"


def test_model_roles_for_unknown_provider_is_404(client, provider):
    sign_in(client, provider)
    assert client.patch("/api/credentials/vllm/models",
                        json={"models": {"primary": "x"}}).status_code == 404


def test_configured_roles_make_the_run_plan_usable(client, provider):
    """The end the gap actually mattered at: an agent resolving a model."""
    from core import llm, users
    sign_in(client, provider)
    me = client.get("/api/auth/me").json()
    client.patch("/api/credentials/open-webui/models",
                 json={"models": {"primary": "qwen3:32b", "light": "llama3.2:3b"}})

    run_id = "RUN-PLAN-1"
    llm.set_run_providers(run_id, {
        "open-webui": users.provider_config(me["user_id"], "open-webui")})
    try:
        plan = llm.get_client().describe_plan("grounder", run_id)
        assert plan and plan[0]["provider"] == "open-webui"
        assert plan[0]["model"] == "qwen3:32b"
        assert llm.get_client().describe_plan("scribe", run_id)[0]["model"] == "llama3.2:3b"
    finally:
        llm.clear_run_providers(run_id)


def test_put_credentials_validates_before_storing(client, provider):
    sign_in(client, provider)
    resp = client.put("/api/credentials", json={
        "provider": "ollama", "base_url": provider.base_url,
        "api_key": "sk-wrong",
    })
    assert resp.status_code == 401
    assert all(c["provider"] != "ollama"
               for c in client.get("/api/credentials").json()["credentials"])


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def test_create_run_enqueues_without_running_it(client, provider, stub_agents):
    """The request must return immediately — the worker does the work."""
    sign_in(client, provider)
    resp = client.post("/api/runs", json={"problem": "How does identity form?"})
    assert resp.status_code == 201

    body = resp.json()
    assert body["run_id"].startswith("RUN-")
    assert body["job_id"]
    assert stub_agents == [], "the web process must not run agents"

    status = client.get(f"/api/runs/{body['run_id']}/status").json()
    assert status["queued"] is True


def test_create_run_requires_credentials_for_the_provider(client, provider):
    sign_in(client, provider)
    resp = client.post("/api/runs", json={"problem": "A problem",
                                          "provider": "vllm"})
    assert resp.status_code == 400


def test_worker_advances_the_run_to_the_first_break(client, provider, stub_agents):
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]

    assert drain() == 1

    status = client.get(f"/api/runs/{run_id}/status").json()
    assert status["awaiting_break"] == 0
    assert status["current_step"] == "break0"
    assert status["running"] is False
    assert stub_agents == ["concept_mapper"]


def test_status_reports_step_level_progress(client, provider, stub_agents):
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    status = client.get(f"/api/runs/{run_id}/status").json()
    assert status["progress"]["total"] == 14
    names = [s["name"] for s in status["steps"]]
    assert names[:3] == ["concept_mapper", "break0", "grounder"]
    by_name = {s["name"]: s for s in status["steps"]}
    assert by_name["concept_mapper"]["status"] == "done"
    assert by_name["break0"]["status"] == "awaiting_input"
    assert by_name["grounder"]["status"] == "pending"


# ---------------------------------------------------------------------------
# Isolation between users
# ---------------------------------------------------------------------------

def test_a_user_cannot_see_another_users_run(client, provider, stub_agents):
    sign_in(client, provider, key="sk-valid-key", name="First")
    run_id = client.post("/api/runs", json={"problem": "Private problem"}).json()["run_id"]
    client.post("/api/auth/logout")

    provider.valid_key = "sk-second-key"
    sign_in(client, provider, key="sk-second-key", name="Second")

    assert client.get("/api/runs").json()["runs"] == []
    # 404 rather than 403 — the run's existence is not disclosed
    assert client.get(f"/api/runs/{run_id}").status_code == 404
    assert client.get(f"/api/runs/{run_id}/status").status_code == 404
    assert client.post(f"/api/runs/{run_id}/break/0",
                       json={"instructions": "CONFIRMED"}).status_code == 404


def test_runs_list_shows_only_your_own(client, provider, stub_agents):
    sign_in(client, provider)
    client.post("/api/runs", json={"problem": "First problem"})
    client.post("/api/runs", json={"problem": "Second problem"})
    runs = client.get("/api/runs").json()["runs"]
    assert len(runs) == 2
    assert {r["problem"] for r in runs} == {"First problem", "Second problem"}


# ---------------------------------------------------------------------------
# Breaks over HTTP
# ---------------------------------------------------------------------------

def test_break_payload_is_structured_for_widgets(client, provider, stub_agents):
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    payload = client.get(f"/api/runs/{run_id}/break/0").json()
    assert payload["break_num"] == 0
    assert payload["is_current"] is True
    assert payload["answered"] is False
    assert isinstance(payload["fields"]["themes"], list)
    assert any(d["command"].startswith("ADD THEME") for d in payload["directives"])
    assert "document" not in payload, "server file paths must not be exposed"


def test_submit_break_releases_the_run(client, provider, stub_agents):
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    resp = client.post(f"/api/runs/{run_id}/break/0",
                       json={"instructions": "CONFIRMED"})
    assert resp.status_code == 200
    assert resp.json()["job_id"]

    drain()
    status = client.get(f"/api/runs/{run_id}/status").json()
    assert status["awaiting_break"] == 1
    assert "grounder" in stub_agents


def test_widget_directives_and_free_text_are_combined(client, provider, stub_agents):
    """Structured widgets generate the same directive language as the CLI."""
    from core import database as core_db
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    client.post(f"/api/runs/{run_id}/break/0", json={
        "directives": ["REMOVE THEME: philosophy_of_mind",
                       "ADD THEME: social_identity"],
        "instructions": "Focus on post-1990 work.",
    })

    stored = core_db.get_break_instructions(run_id, 0)["instructions"]
    assert "REMOVE THEME: philosophy_of_mind" in stored
    assert "ADD THEME: social_identity" in stored
    assert "Focus on post-1990 work." in stored


def test_answering_the_wrong_break_is_rejected(client, provider, stub_agents):
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    resp = client.post(f"/api/runs/{run_id}/break/2",
                       json={"instructions": "CONFIRMED"})
    assert resp.status_code == 409


def test_answered_break_can_still_be_read_back(client, provider, stub_agents):
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()
    client.post(f"/api/runs/{run_id}/break/0",
                json={"directives": ["REMOVE THEME: philosophy_of_mind"]})

    payload = client.get(f"/api/runs/{run_id}/break/0").json()
    assert payload["answered"] is True
    assert "REMOVE THEME" in payload["instructions"]


def test_break0_preview_returns_results(client, provider, stub_agents, monkeypatch):
    """F1: Break 0 theme preview fires a lightweight OpenAlex + Semantic Scholar probe."""
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    # Stub the source handlers so no real network calls happen.
    from agents import social
    def _make_stub(source_id):
        class _StubHandler(social.SourceHandler):
            SOURCE_ID = source_id
            def search(self, query, keywords, limit=10, run_id=""):
                return [{"title": f"Stub paper {i} on {query[:20]}",
                         "authors": ["A. Researcher"], "year": 2021,
                         "doi": f"10.1234/stub{i}", "active_link": "https://example.org/stub"}
                        for i in range(limit)]
        return _StubHandler()
    monkeypatch.setattr(social, "SOURCE_HANDLERS", {
        "openalex": _make_stub("openalex"),
        "semantic_scholar": _make_stub("semantic_scholar"),
    })

    resp = client.post(f"/api/runs/{run_id}/break/0/preview?theme=philosophy_of_mind")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["theme_id"] == "philosophy_of_mind"
    assert body["sources_hit"] == 2
    assert body["total"] == 6  # 3 per source
    assert all("title" in r for r in body["results"])
    assert all("source" in r for r in body["results"])


def test_break0_preview_rejects_unknown_theme(client, provider, stub_agents):
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    resp = client.post(f"/api/runs/{run_id}/break/0/preview?theme=nonexistent_theme")
    assert resp.status_code == 404


def test_break0_preview_requires_theme_param(client, provider, stub_agents):
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    resp = client.post(f"/api/runs/{run_id}/break/0/preview")
    # FastAPI returns 422 for missing required query params
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Argument tree (F9)
# ---------------------------------------------------------------------------

def test_argument_tree_endpoint_returns_nested_tree(client, provider, stub_agents):
    """F9: GET /api/runs/{id}/tree returns the nested argument tree + stats."""
    from core.argument_tree import TreeBuilder, init_tree_table
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "What is identity?"}).json()["run_id"]
    drain()

    # Build a small tree directly
    init_tree_table()
    tree = TreeBuilder(run_id)
    root = tree.create_root("What is identity?")
    q1 = tree.add_question(root, "What is social identity?")
    c1 = tree.add_claim(q1, "Identity is socially constructed", confidence=0.8)
    tree.add_evidence(c1, source_id="SRC-001", evidence_type="book",
                      relationship="establishes", snippet="Mead argues...")
    tree.close()

    resp = client.get(f"/api/runs/{run_id}/tree")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["tree"]["node_type"] == "root"
    assert body["tree"]["content"] == "What is identity?"
    # Should have nested children
    assert len(body["tree"]["children"]) >= 1
    q_node = body["tree"]["children"][0]
    assert q_node["node_type"] == "question"
    assert len(q_node["children"]) >= 1
    c_node = q_node["children"][0]
    assert c_node["node_type"] == "claim"
    assert c_node["confidence"] == 0.8
    assert len(c_node["children"]) >= 1
    e_node = c_node["children"][0]
    assert e_node["node_type"] == "evidence"
    assert "SRC-001" in e_node["source_ids"]
    assert e_node["metadata"]["evidence_type"] == "book"

    # Stats
    stats = body["stats"]
    assert stats["total_nodes"] >= 4
    assert stats["by_type"]["root"] == 1
    assert stats["by_type"]["question"] == 1
    assert stats["by_type"]["claim"] == 1
    assert stats["by_type"]["evidence"] == 1
    assert "sources" in body


def test_argument_tree_empty_run(client, provider, stub_agents):
    """F9: tree endpoint returns null tree and empty stats for a run with no tree."""
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    resp = client.get(f"/api/runs/{run_id}/tree")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tree"] == {} or body["tree"] is None
    assert body["stats"]["total_nodes"] == 0


def test_llm_usage_endpoint(client, provider, stub_agents):
    """F10: GET /api/runs/{id}/usage returns aggregated token usage."""
    from core import database as core_db
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    # Record some usage directly
    core_db.record_llm_usage(run_id, "grounder", "open-webui", "qwen3:32b",
                             prompt_tokens=500, completion_tokens=200)
    core_db.record_llm_usage(run_id, "grounder", "open-webui", "qwen3:32b",
                             prompt_tokens=300, completion_tokens=150)
    core_db.record_llm_usage(run_id, "scribe", "open-webui", "qwen3:32b",
                             prompt_tokens=1000, completion_tokens=800)

    resp = client.get(f"/api/runs/{run_id}/usage")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_calls"] == 3
    assert body["total_prompt"] == 1800
    assert body["total_completion"] == 1150
    assert body["total_tokens"] == 2950

    by_agent = body["by_agent"]
    assert by_agent["grounder"]["calls"] == 2
    assert by_agent["grounder"]["total"] == 1150
    assert by_agent["scribe"]["calls"] == 1
    assert by_agent["scribe"]["total"] == 1800

    by_model = body["by_model"]
    assert "open-webui:qwen3:32b" in by_model
    assert by_model["open-webui:qwen3:32b"]["calls"] == 3


def test_llm_usage_empty_run(client, provider, stub_agents):
    """F10: usage endpoint returns zeros for a run with no LLM calls."""
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    resp = client.get(f"/api/runs/{run_id}/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_calls"] == 0
    assert body["total_tokens"] == 0


# ---------------------------------------------------------------------------
# Cross-run source deduplication (F5)
# ---------------------------------------------------------------------------

def test_cross_run_source_dedup(client, provider, stub_agents):
    """F5: sources in a new run that match a previous run are flagged."""
    from core import database as core_db
    sign_in(client, provider)

    # First run — insert a source
    run1 = client.post("/api/runs", json={"problem": "First problem"}).json()["run_id"]
    drain()
    core_db.upsert_source({
        "source_id": "SRC-R1-1", "run_id": run1, "title": "Identity Theory",
        "doi": "10.1234/abc", "source_name": "openalex", "type": "current",
    })

    # Second run — references the first
    run2 = client.post("/api/runs", json={
        "problem": "Second problem", "previous_run_id": run1,
    }).json()["run_id"]
    drain()

    # Insert one matching + one new source in run2
    core_db.upsert_source({
        "source_id": "SRC-R2-1", "run_id": run2, "title": "Identity Theory",
        "doi": "10.1234/ABC",  # normalized: same as 10.1234/abc
        "source_name": "openalex", "type": "current",
    })
    core_db.upsert_source({
        "source_id": "SRC-R2-2", "run_id": run2, "title": "A Brand New Paper",
        "doi": "10.5678/xyz", "source_name": "semantic_scholar", "type": "current",
    })

    # Mark previously-seen
    prev_keys = core_db.get_previous_run_source_keys(run2)
    assert ("10.1234/abc", "identity theory") in prev_keys
    marked = core_db.mark_previously_seen(run2, prev_keys)
    assert marked == 1  # only SRC-R2-1 matches

    # Verify the flag was set
    run2_sources = core_db.fetch("sources", {"run_id": run2})
    src1 = next(s for s in run2_sources if s["source_id"] == "SRC-R2-1")
    assert int(src1["previously_seen"]) == 1
    src2 = next(s for s in run2_sources if s["source_id"] == "SRC-R2-2")
    assert int(src2["previously_seen"]) == 0

    # The /sources endpoint surfaces the count
    resp = client.get(f"/api/runs/{run2}/sources")
    assert resp.status_code == 200
    body = resp.json()
    assert body["previously_seen"] == 1
    assert body["previous_run_id"] == run1


def test_cross_run_no_previous_run(client, provider, stub_agents):
    """F5: a run with no previous_run_id has empty dedup keys."""
    from core import database as core_db
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    assert core_db.get_previous_run_id(run_id) is None
    assert core_db.get_previous_run_source_keys(run_id) == set()

    resp = client.get(f"/api/runs/{run_id}/sources")
    assert resp.status_code == 200
    body = resp.json()
    assert body["previously_seen"] == 0
    assert body["previous_run_id"] is None


def test_cross_run_title_only_match(client, provider, stub_agents):
    """F5: sources match on title when DOI is absent."""
    from core import database as core_db
    sign_in(client, provider)

    run1 = client.post("/api/runs", json={"problem": "First problem"}).json()["run_id"]
    drain()
    core_db.upsert_source({
        "source_id": "SRC-R1-2", "run_id": run1, "title": "The Politics of Identity!",
        "doi": "", "source_name": "openalex", "type": "current",
    })

    run2 = client.post("/api/runs", json={
        "problem": "Second problem", "previous_run_id": run1,
    }).json()["run_id"]
    drain()

    # Same title, different punctuation/case, no DOI
    core_db.upsert_source({
        "source_id": "SRC-R2-3", "run_id": run2, "title": "the politics of identity",
        "doi": "", "source_name": "semantic_scholar", "type": "current",
    })

    prev_keys = core_db.get_previous_run_source_keys(run2)
    assert ("", "the politics of identity") in prev_keys
    marked = core_db.mark_previously_seen(run2, prev_keys)
    assert marked == 1


# ---------------------------------------------------------------------------
# Mid-run model changes
# ---------------------------------------------------------------------------

def test_model_change_at_a_break_reaches_the_worker(client, provider, stub_agents):
    """
    Requirement: change models mid-pipeline. The choice is made in the web
    process but must apply in the worker process, so it goes through the
    database, not memory.
    """
    from core import llm, pipeline
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    client.post(f"/api/runs/{run_id}/break/0", json={
        "instructions": "CONFIRMED",
        "model_overrides": {"theorist": {"model": "qwen3:32b"}},
    })

    # Simulate the worker being a separate process with no in-memory state
    llm.clear_run_overrides(run_id)
    assert llm.get_run_overrides(run_id) == {}

    pipeline.apply_model_overrides(run_id)
    assert llm.get_run_overrides(run_id)["theorist"]["model"] == "qwen3:32b"

    assert client.get(f"/api/runs/{run_id}").json()["model_overrides"][
        "theorist"]["model"] == "qwen3:32b"


def test_model_overrides_merge_across_breaks(client, provider, stub_agents):
    from core import pipeline
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    client.post(f"/api/runs/{run_id}/break/0", json={
        "instructions": "CONFIRMED",
        "model_overrides": {"vision": {"model": "model-a"}}})
    drain()
    client.post(f"/api/runs/{run_id}/break/1", json={
        "instructions": "CONFIRMED",
        "model_overrides": {"theorist": {"model": "model-b"}}})

    stored = pipeline.get_model_overrides(run_id)
    assert stored["vision"]["model"] == "model-a"
    assert stored["theorist"]["model"] == "model-b"


def test_unknown_agent_in_overrides_is_ignored(client, provider, stub_agents):
    from core import pipeline
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={
        "problem": "A problem",
        "model_overrides": {"not_an_agent": {"model": "x"},
                            "vision": {"model": "good"}},
    }).json()["run_id"]

    stored = pipeline.get_model_overrides(run_id)
    assert "not_an_agent" not in stored
    assert stored["vision"]["model"] == "good"


# ---------------------------------------------------------------------------
# Re-running steps
# ---------------------------------------------------------------------------

def test_rerun_impact_is_reported_before_acting(client, provider, stub_agents):
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()
    client.post(f"/api/runs/{run_id}/break/0", json={"instructions": "CONFIRMED"})
    drain()

    impact = client.get(f"/api/runs/{run_id}/steps/grounder/impact").json()
    names = [c["name"] for c in impact["cascade"]]
    assert names[0] == "grounder" and names[-1] == "scribe"
    assert impact["discard_count"] >= 1


def test_rerun_resets_and_requeues(client, provider, stub_agents):
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()
    client.post(f"/api/runs/{run_id}/break/0", json={"instructions": "CONFIRMED"})
    drain()

    resp = client.post(f"/api/runs/{run_id}/steps/grounder/rerun",
                       json={"cascade": True})
    assert resp.status_code == 200
    assert "scribe" in resp.json()["reset"]

    status = client.get(f"/api/runs/{run_id}/status").json()
    by_name = {s["name"]: s for s in status["steps"]}
    assert by_name["grounder"]["status"] == "pending"
    assert by_name["break0"]["status"] == "done", "upstream break survives"


def test_rerun_unknown_step_is_404(client, provider, stub_agents):
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    assert client.post(f"/api/runs/{run_id}/steps/nope/rerun",
                       json={}).status_code == 404


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def test_enqueue_is_idempotent_per_run(client, provider, stub_agents):
    from core import jobs
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]

    first = client.post(f"/api/runs/{run_id}/advance").json()["job_id"]
    second = client.post(f"/api/runs/{run_id}/advance").json()["job_id"]
    assert first == second, "polling must not pile up duplicate jobs"
    assert len([j for j in jobs.jobs_for_run(run_id)
                if j["status"] in ("queued", "running")]) == 1


def test_a_job_is_claimed_only_once(client, provider, stub_agents):
    from core import jobs
    sign_in(client, provider)
    client.post("/api/runs", json={"problem": "A problem"})

    first = jobs.claim_next()
    second = jobs.claim_next()
    assert first is not None
    assert second is None, "a queued job must not be claimed twice"


def test_stale_jobs_are_reaped(client, provider, stub_agents):
    """A worker that dies mid-run must not strand the run forever."""
    from core import jobs
    sign_in(client, provider)
    client.post("/api/runs", json={"problem": "A problem"})

    job = jobs.claim_next()
    assert jobs.get_job(job["job_id"])["status"] == "running"

    assert jobs.reap_stale(minutes=0) == 1
    assert jobs.get_job(job["job_id"])["status"] == "queued"


def test_failed_step_requeues_then_fails_the_job(client, provider, monkeypatch):
    from core import jobs, pipeline
    monkeypatch.setattr(pipeline, "_run_concept_mapper",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))

    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]

    for _ in range(4):
        if drain() == 0:
            break

    job = jobs.jobs_for_run(run_id)[0]
    assert job["status"] == "failed"
    assert job["attempts"] >= 3, "should retry before giving up"
    assert client.get(f"/api/runs/{run_id}/status").json()["failed_steps"] \
        == ["concept_mapper"]


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

def test_artifact_read_is_confined_to_the_artifacts_directory(client, provider,
                                                              stub_agents):
    """A stored path must not be able to address arbitrary files."""
    from core import database as core_db
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]

    core_db.insert_artifact({
        "artifact_id": "ART-EVIL", "run_id": run_id,
        "output_type": "research_brief", "file_path": "/etc/passwd",
    })
    body = client.get(f"/api/runs/{run_id}/artifacts/ART-EVIL").json()
    assert body["content"] == ""


def test_static_pipeline_shape_is_public(client):
    steps = client.get("/api/steps").json()["steps"]
    assert [s["name"] for s in steps][:2] == ["concept_mapper", "break0"]
    assert any(s["kind"] == "break" for s in steps)
