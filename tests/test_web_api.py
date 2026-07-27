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
    from core import db_backend, jobs, pipeline, users, utils
    from web import auth

    monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
    monkeypatch.setenv("SEEKER_SECRET_KEY", TEST_SECRET)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pipeline.db")

    # F12: copy config.json to a temp path so config-editing tests don't
    # clobber the real file.
    import shutil
    real_config = utils.CONFIG_PATH
    tmp_config = tmp_path / "config.json"
    if real_config.exists():
        shutil.copy2(real_config, tmp_config)
    monkeypatch.setattr(utils, "CONFIG_PATH", tmp_config)

    db_backend.reset_backend()
    for module in (pipeline, users, jobs):
        for flag in ("_schema_ready", "_owner_schema_ready"):
            if hasattr(module, flag):
                setattr(module, flag, False)
    auth.reset_sessions()
    # The login throttle (review S7) is per-process and keyed by client
    # address; every test signs in from the same one.
    auth.reset_login_rate()
    auth.reset_manager()
    utils.invalidate_config_cache()

    from web.app import app
    with TestClient(app) as c:
        yield c

    db_backend.reset_backend()
    auth.reset_sessions()
    auth.reset_login_rate()
    auth.reset_manager()
    utils.invalidate_config_cache()


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
# Password auth (username/password backend)
# ---------------------------------------------------------------------------

def _register(client, username="alice", password="testpass123",
              display_name="Alice"):
    resp = client.post("/api/auth/password/register", json={
        "username": username, "password": password,
        "display_name": display_name,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _pw_login(client, username="alice", password="testpass123"):
    resp = client.post("/api/auth/password/login", json={
        "username": username, "password": password,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_password_register_creates_user_and_session(client):
    body = _register(client)
    assert body["user_id"].startswith("USR-")
    assert body["session"]
    assert body["display_name"] == "Alice"
    me = client.get("/api/auth/me").json()
    assert me["display_name"] == "Alice"
    assert me["credentials"] == []  # no provider keys yet


def test_password_register_rejects_duplicate_username(client):
    _register(client, username="bob")
    resp = client.post("/api/auth/password/register", json={
        "username": "bob", "password": "anotherpass",
    })
    assert resp.status_code == 409


def test_password_login_succeeds_with_correct_credentials(client):
    _register(client, username="carol")
    body = _pw_login(client, username="carol")
    assert body["user_id"].startswith("USR-")
    assert body["session"]


def test_password_login_rejects_wrong_password(client):
    _register(client, username="dave", password="correctpass1")
    resp = client.post("/api/auth/password/login", json={
        "username": "dave", "password": "wrongpassword",
    })
    assert resp.status_code == 401
    assert "Invalid" in resp.json()["detail"]


def test_password_login_rejects_unknown_user(client):
    resp = client.post("/api/auth/password/login", json={
        "username": "nobody", "password": "whatever123",
    })
    assert resp.status_code == 401


def test_password_register_rejects_short_password(client):
    resp = client.post("/api/auth/password/register", json={
        "username": "eve", "password": "short",
    })
    assert resp.status_code == 422  # pydantic validation


def test_password_user_can_add_provider_credentials(client, provider):
    """A password-authenticated user can add provider API keys via the
    credentials endpoints — same account, multiple provider keys."""
    _register(client, username="frank")
    # Add provider credentials
    resp = client.put("/api/credentials", json={
        "provider": "open-webui",
        "base_url": provider.base_url,
        "api_key": provider.valid_key,
        "models": {},
    })
    assert resp.status_code == 200, resp.text
    # Verify the credentials are associated with this user
    me = client.get("/api/auth/me").json()
    assert len(me["credentials"]) == 1
    assert me["credentials"][0]["provider"] == "open-webui"


def test_password_user_is_isolated_from_provider_key_user(client, provider):
    """A password user and a provider-key user are different accounts."""
    _register(client, username="grace", display_name="Grace")
    # Sign in with provider key (different user)
    client.post("/api/auth/logout")
    sign_in(client, provider, name="ProviderUser")
    me = client.get("/api/auth/me").json()
    assert me["display_name"] == "ProviderUser"
    # The password user's runs should not be visible
    client.post("/api/auth/logout")
    _pw_login(client, username="grace")
    me = client.get("/api/auth/me").json()
    assert me["display_name"] == "Grace"


def test_auth_methods_lists_all_backends(client):
    resp = client.get("/api/auth/methods")
    assert resp.status_code == 200
    names = [m["name"] for m in resp.json()["methods"]]
    assert "simple" in names
    assert "password" in names
    assert "saml" in names


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
    assert status["progress"]["total"] == 15
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
# Built-in run templates (F4)
# ---------------------------------------------------------------------------

def test_builtin_templates_endpoint(client, provider, stub_agents):
    """F4: GET /api/templates/built-in returns templates from config.json."""
    sign_in(client, provider)
    resp = client.get("/api/templates/built-in")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {t["name"] for t in body["templates"]}
    # The three shipped defaults
    assert "Humanities" in names
    assert "CS / Quantitative" in names
    assert "Quick scan" in names
    # Each has a description and config
    for t in body["templates"]:
        assert t["built_in"] is True
        assert "description" in t
        assert "config" in t
        assert "source_overrides" in t["config"]


# ---------------------------------------------------------------------------
# Per-step artifact download (F6)
# ---------------------------------------------------------------------------

def test_step_artifacts_lists_and_reads_files(client, provider, stub_agents):
    """F6: GET /api/runs/{id}/step-artifacts lists per-step markdown files."""
    from pathlib import Path
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    # Write a fake per-step artifact
    artifacts_dir = Path(__file__).parent.parent / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{run_id}_grounder_foundations.md"
    fpath = artifacts_dir / fname
    fpath.write_text("# Grounder Foundations\n\nA test doc.", encoding="utf-8")
    try:
        resp = client.get(f"/api/runs/{run_id}/step-artifacts")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        files = body["files"]
        assert any(f["filename"] == fname for f in files)
        the_file = next(f for f in files if f["filename"] == fname)
        assert the_file["step_key"] == "grounder_foundations"
        assert "Foundations" in the_file["label"]
        assert the_file["size"] > 0

        # Read the file's contents
        resp2 = client.get(f"/api/runs/{run_id}/step-artifacts/{fname}")
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert "Grounder Foundations" in body2["content"]
        assert body2["step_key"] == "grounder_foundations"
    finally:
        fpath.unlink(missing_ok=True)


def test_step_artifacts_path_traversal_blocked(client, provider, stub_agents):
    """F6: filenames outside {run_id}_*.md are rejected."""
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    # Wrong prefix
    resp = client.get(f"/api/runs/{run_id}/step-artifacts/other_run_grounder.md")
    assert resp.status_code == 400
    # Non-md extension
    resp = client.get(f"/api/runs/{run_id}/step-artifacts/{run_id}_foo.txt")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Source blacklist (F8)
# ---------------------------------------------------------------------------

def test_blacklist_add_list_remove(client, provider, stub_agents):
    """F8: blacklist entries can be added, listed, and removed."""
    sign_in(client, provider)

    # Add a DOI entry
    resp = client.post("/api/blacklist", json={
        "match_type": "doi", "match_value": "10.1234/abc",
        "reason": "retracted",
    })
    assert resp.status_code == 200

    # Add a title substring entry
    resp = client.post("/api/blacklist", json={
        "match_type": "title_substring", "match_value": "predatory journal",
    })
    assert resp.status_code == 200

    # List
    resp = client.get("/api/blacklist")
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    types = {e["match_type"] for e in entries}
    assert "doi" in types
    assert "title_substring" in types
    # DOI should be normalized
    doi_entry = next(e for e in entries if e["match_type"] == "doi")
    assert doi_entry["match_value"] == "10.1234/abc"
    assert doi_entry["reason"] == "retracted"

    # Remove
    resp = client.request("DELETE", "/api/blacklist", json={
        "match_type": "doi", "match_value": "10.1234/abc",
    })
    assert resp.status_code == 200
    resp = client.get("/api/blacklist")
    entries = resp.json()["entries"]
    assert not any(e["match_type"] == "doi" for e in entries)


def test_blacklist_blocks_source_insertion(client, provider, stub_agents):
    """F8: a blacklisted source is dropped by upsert_source."""
    from core import database as core_db
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    # Get the user_id from the run owner
    from core import users
    owner = users.run_owner(run_id)
    user_id = owner["user_id"] if owner else "anon"

    # Blacklist a DOI
    core_db.add_to_blacklist(user_id, "doi", "10.5555/blocked", reason="predatory")

    # Try to insert a source with that DOI — should be dropped
    result = core_db.upsert_source({
        "source_id": "SRC-BLK-1", "run_id": run_id, "title": "Bad Paper",
        "doi": "10.5555/blocked", "source_name": "openalex", "type": "current",
    })
    assert result is False

    # A non-blacklisted source still inserts fine
    result = core_db.upsert_source({
        "source_id": "SRC-OK-1", "run_id": run_id, "title": "Good Paper",
        "doi": "10.5555/ok", "source_name": "openalex", "type": "current",
    })
    assert result is True


def test_blacklist_title_substring_match(client, provider, stub_agents):
    """F8: title_substring matching is case-insensitive."""
    from core import database as core_db
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    from core import users
    owner = users.run_owner(run_id)
    user_id = owner["user_id"] if owner else "anon"

    core_db.add_to_blacklist(user_id, "title_substring", "PREDATORY")

    result = core_db.upsert_source({
        "source_id": "SRC-BLK-2", "run_id": run_id,
        "title": "Some predatory journal article",
        "source_name": "openalex", "type": "current",
    })
    assert result is False


def test_blacklist_invalid_match_type(client, provider, stub_agents):
    """F8: invalid match_type is rejected by the API."""
    sign_in(client, provider)
    resp = client.post("/api/blacklist", json={
        "match_type": "invalid", "match_value": "foo",
    })
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Branch a run (F11)
# ---------------------------------------------------------------------------

def test_branch_run_clones_prefix(client, provider, stub_agents):
    """F11: branching clones completed steps into a new run, original untouched."""
    from core import database as core_db
    from core.argument_tree import TreeBuilder, init_tree_table

    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "What is identity?"}).json()["run_id"]
    drain()  # advances to break0 (concept_mapper done)

    # Manually mark grounder as done and insert some sources + tree nodes
    # so we have something to clone.
    from core import pipeline
    init_tree_table()
    tree = TreeBuilder(run_id)
    root = tree.create_root("What is identity?")
    q1 = tree.add_question(root, "What is social identity?", agent="grounder")
    c1 = tree.add_claim(q1, "Identity is socially constructed",
                        confidence=0.8, agent="grounder")
    tree.add_evidence(c1, source_id="SRC-ORIG-1", evidence_type="book",
                      relationship="establishes", snippet="Mead argues...",
                      agent="grounder")
    tree.close()

    core_db.upsert_source({
        "source_id": "SRC-ORIG-1", "run_id": run_id,
        "title": "Mind, Self, and Society", "doi": "10.1234/mead",
        "source_name": "openalex", "type": "seminal",
    })

    # Mark concept_mapper and grounder as done
    pipeline._mark_cloned_steps_done(run_id, ["concept_mapper", "grounder"])

    # Branch from grounder
    resp = client.post(f"/api/runs/{run_id}/branch", json={
        "branch_after_step": "grounder",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    new_run_id = body["new_run_id"]
    assert new_run_id != run_id

    # The new run should have cloned steps done up to grounder
    new_state = body["state"]
    new_steps = {s["step_name"]: s["status"] for s in new_state["steps"]}
    assert new_steps["concept_mapper"] == "done"
    assert new_steps["grounder"] == "done"
    assert new_steps["social"] == "pending"

    # The original run should be unchanged
    orig_state = client.get(f"/api/runs/{run_id}/status").json()
    orig_steps = {s["name"]: s["status"] for s in orig_state["steps"]}
    assert orig_steps["concept_mapper"] == "done"
    assert orig_steps["grounder"] == "done"

    # The new run should have the cloned source
    new_sources = core_db.fetch("sources", {"run_id": new_run_id})
    assert len(new_sources) == 1
    assert new_sources[0]["title"] == "Mind, Self, and Society"
    assert new_sources[0]["source_id"] != "SRC-ORIG-1"  # new ID

    # The new run should have the cloned tree
    new_tree = client.get(f"/api/runs/{new_run_id}/tree").json()
    assert new_tree["tree"] is not None
    assert new_tree["tree"]["content"] == "What is identity?"
    assert new_tree["stats"]["total_nodes"] >= 4


def test_branch_run_with_new_problem(client, provider, stub_agents):
    """F11: branching with a new problem statement uses it in the new run."""
    from core import database as core_db
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "Original problem"}).json()["run_id"]
    drain()

    from core import pipeline
    pipeline._mark_cloned_steps_done(run_id, ["concept_mapper"])

    resp = client.post(f"/api/runs/{run_id}/branch", json={
        "branch_after_step": "concept_mapper",
        "new_problem": "A different angle on the problem",
    })
    assert resp.status_code == 200
    new_run_id = resp.json()["new_run_id"]

    # The new run's problem should be the new one
    new_run = core_db.get_run(new_run_id)
    assert new_run["problem"] == "A different angle on the problem"

    # Original run's problem is unchanged
    orig_run = core_db.get_run(run_id)
    assert orig_run["problem"] == "Original problem"


def test_branch_run_rejects_incomplete_step(client, provider, stub_agents):
    """F11: cannot branch from a step that hasn't completed."""
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()  # only concept_mapper is done; break0 is awaiting_input

    # Try to branch from grounder, which is still pending
    resp = client.post(f"/api/runs/{run_id}/branch", json={
        "branch_after_step": "grounder",
    })
    assert resp.status_code == 400
    assert "not done" in resp.json()["detail"].lower()


def test_branch_run_rejects_unknown_step(client, provider, stub_agents):
    """F11: unknown step name returns 404."""
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()

    resp = client.post(f"/api/runs/{run_id}/branch", json={
        "branch_after_step": "nonexistent_step",
    })
    assert resp.status_code == 404


def test_branch_run_preserves_tree_source_references(client, provider, stub_agents):
    """F11: tree node source_ids are remapped to the cloned sources' new IDs."""
    from core import database as core_db
    from core.argument_tree import TreeBuilder, init_tree_table
    from core import pipeline

    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "Test problem"}).json()["run_id"]
    drain()

    init_tree_table()
    tree = TreeBuilder(run_id)
    root = tree.create_root("Test problem")
    q = tree.add_question(root, "Q1", agent="grounder")
    c = tree.add_claim(q, "C1", confidence=0.5, agent="grounder")
    tree.add_evidence(c, source_id="SRC-X", evidence_type="paper",
                      relationship="supports", snippet="...",
                      agent="grounder")
    tree.close()

    core_db.upsert_source({
        "source_id": "SRC-X", "run_id": run_id, "title": "Paper X",
        "doi": "10.1/x", "source_name": "openalex", "type": "seminal",
    })

    pipeline._mark_cloned_steps_done(run_id, ["concept_mapper", "grounder"])

    resp = client.post(f"/api/runs/{run_id}/branch", json={
        "branch_after_step": "grounder",
    })
    assert resp.status_code == 200
    new_run_id = resp.json()["new_run_id"]

    # The new run's tree should have evidence nodes whose source_ids point
    # to the new run's sources (not the old SRC-X).
    new_tree = client.get(f"/api/runs/{new_run_id}/tree").json()
    new_sources = core_db.fetch("sources", {"run_id": new_run_id})
    new_source_ids = {s["source_id"] for s in new_sources}
    assert "SRC-X" not in new_source_ids  # old ID not present

    # Find the evidence node and check its source_ids
    def find_evidence(node):
        if node["node_type"] == "evidence":
            return node
        for child in node.get("children", []):
            result = find_evidence(child)
            if result:
                return result
        return None

    ev = find_evidence(new_tree["tree"])
    assert ev is not None
    # The source_ids should reference the new source IDs
    for sid in ev["source_ids"]:
        assert sid in new_source_ids


# ---------------------------------------------------------------------------
# Config editor (F12)
# ---------------------------------------------------------------------------

def test_first_user_auto_promoted_to_admin(client, provider, stub_agents):
    """F12: the first user to sign in is automatically promoted to admin."""
    from core import users
    sign_in(client, provider)
    me = client.get("/api/auth/me").json()
    assert users.is_admin(me["user_id"])
    # The whoami endpoint should agree
    resp = client.get("/api/config/whoami")
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True


def test_config_get_returns_sections(client, provider, stub_agents):
    """F12: GET /api/config returns the editable sections."""
    sign_in(client, provider)
    resp = client.get("/api/config")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "sources" in body
    assert "themes" in body
    assert "agent_sources" in body
    assert "run_templates" in body
    assert body["is_admin"] is True
    # The sources section should have openalex
    assert "openalex" in body["sources"]


def test_config_update_sources(client, provider, stub_agents):
    """F12: PUT /api/config/sections/sources updates the sources section."""
    sign_in(client, provider)
    # Get current config
    cfg = client.get("/api/config").json()
    sources = cfg["sources"]
    # Disable arxiv
    if "arxiv" in sources:
        sources["arxiv"]["enabled"] = False

    resp = client.put("/api/config/sections/sources", json={"value": sources})
    assert resp.status_code == 200, resp.text

    # Verify it was saved — re-read
    cfg2 = client.get("/api/config").json()
    assert cfg2["sources"]["arxiv"]["enabled"] is False

    # Restore it
    sources["arxiv"]["enabled"] = True
    client.put("/api/config/sections/sources", json={"value": sources})


def test_config_update_themes(client, provider, stub_agents):
    """F12: PUT /api/config/sections/themes updates the themes section."""
    sign_in(client, provider)
    cfg = client.get("/api/config").json()
    original_themes = cfg["themes"]
    # Add a test theme
    new_themes = original_themes + [{
        "theme_id": "test_theme_f12",
        "label": "Test Theme",
        "keywords": [{"seed": "test_keyword", "expansion_depth": 1}],
    }]
    resp = client.put("/api/config/sections/themes", json={"value": new_themes})
    assert resp.status_code == 200, resp.text

    # Verify
    cfg2 = client.get("/api/config").json()
    theme_ids = {t["theme_id"] for t in cfg2["themes"]}
    assert "test_theme_f12" in theme_ids

    # Restore
    client.put("/api/config/sections/themes", json={"value": original_themes})


def test_config_rejects_non_editable_section(client, provider, stub_agents):
    """F12: PUT to a non-editable section is rejected."""
    sign_in(client, provider)
    resp = client.put("/api/config/sections/llm", json={"value": {}})
    assert resp.status_code == 400


def test_config_rejects_non_admin(client, provider, stub_agents):
    """F12: a non-admin user cannot access the config endpoints."""
    from core import users
    # Sign in as the first user (auto-admin)
    sign_in(client, provider, name="Admin")
    me = client.get("/api/auth/me").json()
    assert users.is_admin(me["user_id"])

    # Create a second user directly — they should NOT be admin
    second = users.get_or_create("non-admin-auth-ref", display_name="NonAdmin")
    assert not users.is_admin(second["user_id"])

    # Manually set the session to the second user
    from web import auth
    token = "test-non-admin-session"
    auth.sessions().set(token, {"user_id": second["user_id"]}, ttl=3600)
    client.cookies.set("seeker_session", token)

    resp = client.get("/api/config")
    assert resp.status_code == 403
    resp = client.put("/api/config/sections/sources", json={"value": {}})
    assert resp.status_code == 403
    resp = client.get("/api/config/whoami")
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is False


# ---------------------------------------------------------------------------
# SSE event stream (F3)
# ---------------------------------------------------------------------------

def test_sse_events_stream_status(client, provider, stub_agents):
    """F3: GET /api/runs/{id}/events returns an SSE stream with status events.

    We test against a completed run so the stream terminates after the
    done event — an open-ended stream would hang the TestClient.
    """
    from core import pipeline
    import core.database as cdb
    sign_in(client, provider)
    run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
    drain()  # advances to break0

    # Mark all steps done so the stream emits done and closes
    all_steps = [s.name for s in pipeline.STEP_DEFS]
    pipeline._mark_cloned_steps_done(run_id, all_steps)
    cdb.update_run_status(run_id, "completed")

    resp = client.get(f"/api/runs/{run_id}/events")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    text = resp.text
    assert "event: status" in text
    assert run_id in text
    assert "event: done" in text


def test_sse_events_requires_auth(client, provider, stub_agents):
    """F3: SSE endpoint requires authentication."""
    run_id = "RUN-FAKE-1234"
    resp = client.get(f"/api/runs/{run_id}/events")
    assert resp.status_code == 401


def test_sse_events_rejects_other_users_run(client, provider, stub_agents):
    """F3: SSE endpoint respects run ownership."""
    sign_in(client, provider, name="Owner")
    run_id = client.post("/api/runs", json={"problem": "My problem"}).json()["run_id"]
    drain()

    # Create a second user and try to access the run's events
    from core import users
    from web import auth
    second = users.get_or_create("other-auth-ref", display_name="Other")
    token = "test-sse-other-session"
    auth.sessions().set(token, {"user_id": second["user_id"]}, ttl=3600)
    client.cookies.set("seeker_session", token)

    resp = client.get(f"/api/runs/{run_id}/events")
    assert resp.status_code == 404  # 404, not 403 — existence is hidden


def test_sse_status_signature_dedupes():
    """F3: _status_signature produces the same hash for identical states."""
    from web.app import _status_signature
    snap = {
        "status": "active", "current_step": "grounder", "running": True,
        "awaiting_break": None, "complete": False, "queued": False,
        "failed_steps": [],
        "steps": [
            {"name": "concept_mapper", "status": "done", "activity": None},
            {"name": "grounder", "status": "running", "activity": "OpenAlex — searching"},
        ],
    }
    assert _status_signature(snap) == _status_signature({**snap})
    # Different activity → different signature
    snap2 = {**snap, "steps": [
        {"name": "concept_mapper", "status": "done", "activity": None},
        {"name": "grounder", "status": "running", "activity": "arXiv — searching"},
    ]}
    assert _status_signature(snap) != _status_signature(snap2)
    # Different step status → different signature
    snap3 = {**snap, "steps": [
        {"name": "concept_mapper", "status": "done", "activity": None},
        {"name": "grounder", "status": "done", "activity": None},
    ]}
    assert _status_signature(snap) != _status_signature(snap3)


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
    assert names[0] == "grounder" and names[-1] == "reporter"
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


# ---------------------------------------------------------------------------
# Pre-run estimate (review X2)
# ---------------------------------------------------------------------------

def test_estimate_endpoint_requires_auth(client):
    # Weak on its own: /api/runs/{run_id} also 401s unauthenticated, so this
    # passes even when the literal path is being swallowed by it. The test
    # below is what actually pins the routing.
    assert client.get("/api/runs/estimate").status_code == 401


def test_estimate_endpoint_describes_the_run_ahead(client, provider):
    """
    Also the routing regression: FastAPI matches in declaration order, so if
    /api/runs/{run_id} is ever moved above this route it captures 'estimate'
    as a run_id and this comes back {"detail": "Run not found"}.
    """
    sign_in(client, provider)
    body = client.get("/api/runs/estimate").json()
    assert "detail" not in body, (
        "the literal /api/runs/estimate path was captured by "
        "/api/runs/{run_id} — declaration order matters"
    )
    for field in ("themes", "sources", "source_lookups", "estimated_calls",
                  "estimated_tokens", "estimated_seconds", "breaks", "basis",
                  "is_measured"):
        assert field in body, f"estimate is missing {field}"
    assert body["breaks"] == 3
    assert body["estimated_calls"] > 0


def test_estimate_shrinks_when_sources_are_turned_off(client, provider):
    """Turning a source off has to visibly change the number, or the card is
    decoration rather than a control."""
    sign_in(client, provider)
    full = client.get("/api/runs/estimate").json()
    if not full["sources"]:
        pytest.skip("no sources configured in this config.json")

    off = json.dumps({s: False for s in full["sources"]})
    trimmed = client.get(f"/api/runs/estimate?source_overrides={off}").json()
    assert trimmed["source_lookups"] == 0
    assert trimmed["estimated_calls"] < full["estimated_calls"]


def test_estimate_rejects_malformed_overrides(client, provider):
    sign_in(client, provider)
    resp = client.get("/api/runs/estimate?source_overrides=not-json")
    assert resp.status_code == 400


def test_estimate_flags_itself_as_a_guess_before_any_run(client, provider):
    sign_in(client, provider)
    body = client.get("/api/runs/estimate").json()
    assert body["is_measured"] is False
    assert "no completed runs" in body["basis"]
