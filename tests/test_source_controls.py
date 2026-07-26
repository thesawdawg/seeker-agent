"""
Per-run source overrides, per-user source credentials, and source health
endpoints (review U1 / U2 / U3 / R8).

These are the web-facing pieces that make the gathering steps configurable
and observable per run and per user, instead of only through config.json.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_web_api import FakeProvider, sign_in, drain, TEST_SECRET


# ---------------------------------------------------------------------------
# Rate limiter — circuit breaker, Retry-After, per-run dict (R2/R3/E3)
# ---------------------------------------------------------------------------

class TestRateLimiter:
    def test_get_limiter_is_per_run(self):
        from core import rate_limiter
        a = rate_limiter.get_limiter("RUN-A")
        b = rate_limiter.get_limiter("RUN-B")
        assert a is not b
        assert rate_limiter.get_limiter("RUN-A") is a  # cached
        rate_limiter.clear_limiter("RUN-A")
        assert rate_limiter.get_limiter("RUN-A") is not a

    def test_circuit_breaker_trips_after_consecutive_failures(self):
        from core.rate_limiter import RateLimiter, SourceUnavailable
        lim = RateLimiter(run_id="RUN-CB")
        # Trip the breaker
        for _ in range(lim.breaker_fails):
            lim.record_failure("test_src")
        # Now wait() should raise SourceUnavailable instead of sleeping
        with pytest.raises(SourceUnavailable):
            lim.wait("test_src")

    def test_record_success_resets_consecutive_failures(self):
        from core.rate_limiter import RateLimiter
        lim = RateLimiter(run_id="RUN-CB-RESET")
        for _ in range(lim.breaker_fails - 1):
            lim.record_failure("test_src")
        lim.record_success("test_src")
        # One more failure should NOT trip — the success reset the count
        lim.record_failure("test_src")
        assert not lim._breaker_is_tripped("test_src")


# ---------------------------------------------------------------------------
# Source overrides (U1)
# ---------------------------------------------------------------------------

class TestSourceOverrides:
    def test_set_and_get_source_overrides(self, tmp_path, monkeypatch):
        import core.database as db
        from core import db_backend, pipeline
        monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "p.db")
        db_backend.reset_backend()
        pipeline._schema_ready = False
        pipeline.set_source_overrides("RUN-X", {"scopus": True, "arxiv": False})
        stored = pipeline.get_source_overrides("RUN-X")
        assert stored == {"scopus": True, "arxiv": False}
        db_backend.reset_backend()
        pipeline._schema_ready = False

    def test_source_overrides_merge(self, tmp_path, monkeypatch):
        import core.database as db
        from core import db_backend, pipeline
        monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "p.db")
        db_backend.reset_backend()
        pipeline._schema_ready = False
        pipeline.set_source_overrides("RUN-M", {"scopus": True})
        pipeline.set_source_overrides("RUN-M", {"arxiv": False})
        stored = pipeline.get_source_overrides("RUN-M")
        assert stored == {"scopus": True, "arxiv": False}
        db_backend.reset_backend()
        pipeline._schema_ready = False

    def test_non_bool_values_are_dropped(self, tmp_path, monkeypatch):
        import core.database as db
        from core import db_backend, pipeline
        monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "p.db")
        db_backend.reset_backend()
        pipeline._schema_ready = False
        pipeline.set_source_overrides("RUN-N", {"scopus": "yes", "arxiv": False})
        stored = pipeline.get_source_overrides("RUN-N")
        assert stored == {"arxiv": False}
        db_backend.reset_backend()
        pipeline._schema_ready = False

    def test_apply_source_overrides_disables_in_config(self):
        from core import pipeline
        config = {
            "sources": {"scopus": {"enabled": True}, "arxiv": {"enabled": True}},
            "agent_sources": {"social": ["scopus", "arxiv", "openalex"]},
        }
        out = pipeline.apply_source_overrides("RUN-NO-SUCH", config)
        # No overrides stored -> config returned unchanged
        assert out["sources"]["scopus"]["enabled"] is True

    def test_apply_source_overrides_prunes_agent_sources(self, tmp_path, monkeypatch):
        import core.database as db
        from core import db_backend, pipeline
        monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "p.db")
        db_backend.reset_backend()
        pipeline._schema_ready = False
        pipeline.set_source_overrides("RUN-PRUNE", {"arxiv": False})
        config = {
            "sources": {"scopus": {"enabled": True}, "arxiv": {"enabled": True}},
            "agent_sources": {"social": ["scopus", "arxiv", "openalex"]},
        }
        out = pipeline.apply_source_overrides("RUN-PRUNE", config)
        assert out["sources"]["arxiv"]["enabled"] is False
        assert "arxiv" not in out["agent_sources"]["social"]
        assert "scopus" in out["agent_sources"]["social"]
        db_backend.reset_backend()
        pipeline._schema_ready = False


# ---------------------------------------------------------------------------
# Source credentials (U2)
# ---------------------------------------------------------------------------

class TestSourceCredentials:
    def test_set_and_get_source_api_key(self, tmp_path, monkeypatch):
        import core.database as db
        from core import db_backend, users
        monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
        monkeypatch.setenv("SEEKER_SECRET_KEY", "test-secret-key-for-encryption-0")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "p.db")
        db_backend.reset_backend()
        users._schema_ready = False
        users.set_source_credentials("USR1", "scopus", "sk-secret-key")
        assert users.get_source_api_key("USR1", "scopus") == "sk-secret-key"
        assert users.get_source_api_key("USR1", "core") == ""
        db_backend.reset_backend()
        users._schema_ready = False

    def test_source_api_key_is_encrypted_at_rest(self, tmp_path, monkeypatch):
        import core.database as db
        from core import db_backend, users
        monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
        monkeypatch.setenv("SEEKER_SECRET_KEY", "test-secret-key-for-encryption-1")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "p.db")
        db_backend.reset_backend()
        users._schema_ready = False
        users.set_source_credentials("USR1", "scopus", "sk-plaintext-key")
        row = users.get_source_credentials_row("USR1", "scopus")
        assert "sk-plaintext-key" not in (row.get("api_key_enc") or "")
        assert row.get("key_hint") != "sk-plaintext-key"
        db_backend.reset_backend()
        users._schema_ready = False

    def test_keys_module_prefers_user_stored_key(self, tmp_path, monkeypatch):
        import core.database as db
        from core import db_backend, keys, users
        monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
        monkeypatch.setenv("SEEKER_SECRET_KEY", "test-secret-key-for-encryption-2")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "p.db")
        monkeypatch.setenv("SCOPUS_API_KEY", "env-key")
        db_backend.reset_backend()
        users._schema_ready = False
        users.set_source_credentials("USR-K", "scopus", "user-stored-key")
        keys.set_current_user("USR-K")
        try:
            assert keys.scopus_api_key() == "user-stored-key"
        finally:
            keys.clear_current_user()
        # Without a bound user, env is used
        assert keys.scopus_api_key() == "env-key"
        db_backend.reset_backend()
        users._schema_ready = False


# ---------------------------------------------------------------------------
# Source health table (E4)
# ---------------------------------------------------------------------------

class TestSourceHealth:
    def test_record_and_get_source_health(self, tmp_path, monkeypatch):
        import core.database as db
        from core import db_backend
        monkeypatch.setenv("SEEKER_DB_BACKEND", "sqlite")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "p.db")
        db_backend.reset_backend()
        db.init_db()
        db.record_source_health("RUN-H", "scopus", "social",
                                status="ok", results_returned=5, calls_made=1)
        db.record_source_health("RUN-H", "arxiv", "social",
                                status="failed", last_error="timeout")
        rows = db.get_source_health("RUN-H")
        by_src = {r["source_id"]: r for r in rows}
        assert by_src["scopus"]["status"] == "ok"
        assert by_src["scopus"]["results_returned"] == 5
        assert by_src["arxiv"]["status"] == "failed"
        assert by_src["arxiv"]["last_error"] == "timeout"
        db_backend.reset_backend()


# ---------------------------------------------------------------------------
# Web endpoints for source controls (U1/U2/U3/R8)
# ---------------------------------------------------------------------------

class TestSourceEndpoints:
    def test_run_detail_includes_source_overrides(self, client, provider, stub_agents):
        sign_in(client, provider)
        run_id = client.post("/api/runs", json={
            "problem": "A problem",
            "source_overrides": {"scopus": True, "arxiv": False},
        }).json()["run_id"]
        detail = client.get(f"/api/runs/{run_id}").json()
        assert detail["source_overrides"] == {"scopus": True, "arxiv": False}

    def test_source_overrides_can_be_changed_at_a_break(self, client, provider, stub_agents):
        from core import pipeline
        sign_in(client, provider)
        run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
        drain()
        client.post(f"/api/runs/{run_id}/break/0", json={
            "instructions": "CONFIRMED",
            "source_overrides": {"scopus": False},
        })
        stored = pipeline.get_source_overrides(run_id)
        assert stored == {"scopus": False}

    def test_put_and_list_source_credentials(self, client, provider):
        sign_in(client, provider)
        resp = client.put("/api/source-credentials", json={
            "source_id": "scopus", "api_key": "sk-scopus-key"})
        assert resp.status_code == 200
        cred = resp.json()["credential"]
        assert cred["source_id"] == "scopus"
        assert cred["key_hint"] != "sk-scopus-key"
        listed = client.get("/api/source-credentials").json()["credentials"]
        assert any(c["source_id"] == "scopus" for c in listed)

    def test_delete_source_credentials(self, client, provider):
        sign_in(client, provider)
        client.put("/api/source-credentials", json={
            "source_id": "core", "api_key": "sk-core-key"})
        resp = client.delete("/api/source-credentials/core")
        assert resp.status_code == 200
        listed = client.get("/api/source-credentials").json()["credentials"]
        assert not any(c["source_id"] == "core" for c in listed)

    def test_sources_health_endpoint_reports_keyless_sources(self, client, provider):
        sign_in(client, provider)
        body = client.get("/api/sources/health").json()
        sources = {s["source_id"]: s for s in body["sources"]}
        # arxiv is keyless and should be reported as ready
        assert sources["arxiv"]["has_key"] is True
        assert sources["arxiv"]["note"] == "keyless"

    def test_sources_health_endpoint_reports_stored_key(self, client, provider):
        sign_in(client, provider)
        client.put("/api/source-credentials", json={
            "source_id": "scopus", "api_key": "sk-scopus-key"})
        body = client.get("/api/sources/health").json()
        scopus = next(s for s in body["sources"] if s["source_id"] == "scopus")
        assert scopus["has_key"] is True
        assert scopus["note"] == "stored"

    def test_run_sources_endpoint_returns_health_and_inserted(self, client, provider, stub_agents):
        import core.database as db
        sign_in(client, provider)
        run_id = client.post("/api/runs", json={"problem": "A problem"}).json()["run_id"]
        db.record_source_health(run_id, "openalex", "social",
                                status="ok", results_returned=3, calls_made=1)
        body = client.get(f"/api/runs/{run_id}/sources").json()
        health_by_src = {h["source_id"]: h for h in body["health"]}
        assert health_by_src["openalex"]["status"] == "ok"
        assert health_by_src["openalex"]["results_returned"] == 3
        assert "inserted" in body


# ---------------------------------------------------------------------------
# Fixtures reused from test_web_api
# ---------------------------------------------------------------------------

@pytest.fixture()
def provider():
    p = FakeProvider()
    yield p
    p.stop()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import core.database as db
    from core import db_backend, jobs, pipeline, users
    from web import auth
    from tests.test_web_api import TEST_SECRET
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


@pytest.fixture()
def stub_agents(monkeypatch):
    from core import pipeline
    calls = []
    monkeypatch.setattr(pipeline, "_run_agent_step",
                        lambda n, r, p, c: calls.append(n))
    monkeypatch.setattr(pipeline, "_run_concept_mapper",
                        lambda r, p, c: calls.append("concept_mapper"))
    return calls
