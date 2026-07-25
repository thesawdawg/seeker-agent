"""
LLM router — provider abstraction, fallback chain, per-run overrides.

Transports are exercised against a real local HTTP server rather than mocks,
so the request/response shapes are actually verified.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import llm  # noqa: E402


# ---------------------------------------------------------------------------
# A stand-in for an OpenAI-compatible / Anthropic endpoint
# ---------------------------------------------------------------------------

class FakeProvider:
    """Serves /chat/completions, /v1/messages and /models with scripted results."""

    def __init__(self):
        self.status = 200
        self.reply = "hello from the fake provider"
        self.models = ["model-a", "model-b"]
        self.requests = []
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self._server.server_port}"

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.endswith("/models"):
                    self._send(200, {"data": [{"id": m} for m in outer.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({
                    "path": self.path,
                    "body": body,
                    "headers": dict(self.headers),
                })
                if outer.status != 200:
                    self._send(outer.status, {"error": "scripted failure"})
                elif self.path.endswith("/v1/messages"):
                    self._send(200, {"content": [{"type": "text", "text": outer.reply}]})
                else:
                    self._send(200, {"choices": [{"message": {"content": outer.reply}}]})

        return Handler


@pytest.fixture()
def fake():
    provider = FakeProvider()
    yield provider
    provider.stop()


@pytest.fixture()
def secondary():
    provider = FakeProvider()
    yield provider
    provider.stop()


def build_settings(providers, chain, agents=None):
    return llm.LLMSettings(
        providers={p.name: p for p in providers},
        chain=tuple(chain),
        agents=agents or {},
        default_profile=llm.AgentProfile(max_tokens=1000, temperature=0.5),
        timeout_seconds=10,
        max_retries=2,
        retry_delay=0,
    )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def test_loads_real_project_config():
    """The shipped config.json must produce a working routing plan."""
    from core.utils import load_config
    settings = llm.load_settings(load_config())

    assert "open-webui" in settings.providers
    assert settings.chain, "fallback_chain resolved to nothing"
    assert settings.profile_for("grounder").max_tokens == 16000
    assert settings.profile_for("social").model_role == "light"
    assert settings.profile_for("grounder").model_role == "primary"


def test_env_overrides_config_base_url(monkeypatch):
    monkeypatch.setenv("SEEKER_TEST_BASE", "http://example.invalid/api")
    provider = llm._resolve_provider("t", {
        "base_url": "http://ignored",
        "base_url_env": "SEEKER_TEST_BASE",
        "api_key_env": "SEEKER_TEST_KEY",
    })
    assert provider.base_url == "http://example.invalid/api"


def test_unset_provider_is_not_configured():
    provider = llm._resolve_provider("t", {"base_url": "", "api_key_env": "NOPE"})
    assert not provider.configured


def test_unknown_chain_provider_is_skipped():
    settings = llm.load_settings({"llm": {
        "providers": {"real": {"base_url": "http://x", "models": {"primary": "m"}}},
        "fallback_chain": [{"provider": "ghost"}, {"provider": "real"}],
    }})
    assert [s.provider for s in settings.chain] == ["real"]


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

def test_openai_transport_shape(fake):
    provider = llm.ProviderConfig("owui", "openai", fake.base_url, "secret-key",
                                  {"primary": "model-a"})
    client = llm.LLMClient(build_settings([provider], [llm.ChainStep("owui")]))

    assert client.call("the prompt", "the system prompt", "grounder") == fake.reply

    sent = fake.requests[0]
    assert sent["path"].endswith("/chat/completions")
    assert sent["headers"]["Authorization"] == "Bearer secret-key"
    assert sent["body"]["model"] == "model-a"
    assert sent["body"]["messages"] == [
        {"role": "system", "content": "the system prompt"},
        {"role": "user", "content": "the prompt"},
    ]
    assert sent["body"]["stream"] is False


def test_anthropic_transport_shape(fake):
    provider = llm.ProviderConfig("anthropic", "anthropic", fake.base_url, "sk-ant",
                                  {"primary": "claude-x"})
    client = llm.LLMClient(build_settings([provider], [llm.ChainStep("anthropic")]))

    assert client.call("p", "s", "grounder") == fake.reply

    sent = fake.requests[0]
    assert sent["path"].endswith("/v1/messages")
    assert sent["headers"]["x-api-key"] == "sk-ant"
    assert sent["headers"]["anthropic-version"] == "2023-06-01"
    assert sent["body"]["system"] == "s"          # system is top-level, not a message
    assert sent["body"]["messages"] == [{"role": "user", "content": "p"}]


def test_per_agent_token_limits_reach_the_wire(fake):
    provider = llm.ProviderConfig("owui", "openai", fake.base_url, "k", {"primary": "m"})
    settings = build_settings(
        [provider], [llm.ChainStep("owui")],
        agents={"grounder": llm.AgentProfile(max_tokens=16000, temperature=0.2)},
    )
    llm.LLMClient(settings).call("p", "s", "grounder")

    assert fake.requests[0]["body"]["max_tokens"] == 16000
    assert fake.requests[0]["body"]["temperature"] == 0.2


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------

def test_falls_through_to_next_provider(fake, secondary):
    fake.status = 500
    secondary.reply = "answered by the backup"

    primary = llm.ProviderConfig("primary", "openai", fake.base_url, "k", {"primary": "m1"})
    backup  = llm.ProviderConfig("backup", "openai", secondary.base_url, "k", {"primary": "m2"})
    client = llm.LLMClient(build_settings(
        [primary, backup], [llm.ChainStep("primary"), llm.ChainStep("backup")]
    ))

    assert client.call("p", "s", "grounder") == "answered by the backup"
    assert fake.requests, "primary should have been attempted"


def test_client_error_does_not_retry(fake):
    fake.status = 400
    provider = llm.ProviderConfig("owui", "openai", fake.base_url, "k", {"primary": "m"})
    client = llm.LLMClient(build_settings([provider], [llm.ChainStep("owui")]))

    with pytest.raises(llm.LLMError):
        client.call("p", "s", "grounder")
    assert len(fake.requests) == 1, "4xx must not be retried"


def test_server_error_retries(fake):
    fake.status = 503
    provider = llm.ProviderConfig("owui", "openai", fake.base_url, "k", {"primary": "m"})
    client = llm.LLMClient(build_settings([provider], [llm.ChainStep("owui")]))

    with pytest.raises(llm.LLMError):
        client.call("p", "s", "grounder")
    assert len(fake.requests) == 2, "5xx should exhaust max_retries"


def test_no_usable_provider_raises_actionable_error():
    empty = llm.ProviderConfig("owui", "openai", "", "", {})
    client = llm.LLMClient(build_settings([empty], [llm.ChainStep("owui")]))

    with pytest.raises(llm.LLMError, match="No usable LLM provider"):
        client.call("p", "s", "grounder")


def test_provider_without_model_for_role_is_skipped(fake):
    no_model = llm.ProviderConfig("nomodel", "openai", fake.base_url, "k", {})
    good     = llm.ProviderConfig("good", "openai", fake.base_url, "k", {"primary": "m"})
    client = llm.LLMClient(build_settings(
        [no_model, good], [llm.ChainStep("nomodel"), llm.ChainStep("good")]
    ))
    assert [s["provider"] for s in client.describe_plan("grounder")] == ["good"]


# ---------------------------------------------------------------------------
# Model roles and per-run overrides
# ---------------------------------------------------------------------------

def test_model_role_selects_model(fake):
    provider = llm.ProviderConfig("owui", "openai", fake.base_url, "k",
                                  {"primary": "big-model", "light": "small-model"})
    settings = build_settings([provider], [llm.ChainStep("owui")], agents={
        "grounder": llm.AgentProfile(model_role="primary"),
        "scribe":   llm.AgentProfile(model_role="light"),
    })
    client = llm.LLMClient(settings)

    assert client.describe_plan("grounder")[0]["model"] == "big-model"
    assert client.describe_plan("scribe")[0]["model"] == "small-model"


def test_run_override_changes_model_mid_pipeline(fake):
    """Requirement: a researcher swaps models at a break, for that run only."""
    provider = llm.ProviderConfig("owui", "openai", fake.base_url, "k", {"primary": "default-model"})
    client = llm.LLMClient(build_settings([provider], [llm.ChainStep("owui")]))
    run_id = "RUN-OVERRIDE-TEST"

    try:
        assert client.describe_plan("theorist", run_id)[0]["model"] == "default-model"

        llm.set_run_overrides(run_id, {"theorist": {"model": "qwen3:32b"}})

        assert client.describe_plan("theorist", run_id)[0]["model"] == "qwen3:32b"
        # other agents untouched
        assert client.describe_plan("grounder", run_id)[0]["model"] == "default-model"
        # other runs untouched
        assert client.describe_plan("theorist", "RUN-OTHER")[0]["model"] == "default-model"
    finally:
        llm.clear_run_overrides(run_id)

    assert client.describe_plan("theorist", run_id)[0]["model"] == "default-model"


def test_run_override_can_switch_provider(fake, secondary):
    a = llm.ProviderConfig("a", "openai", fake.base_url, "k", {"primary": "m-a"})
    b = llm.ProviderConfig("b", "openai", secondary.base_url, "k", {"primary": "m-b"})
    client = llm.LLMClient(build_settings([a, b], [llm.ChainStep("a")]))
    run_id = "RUN-PROVIDER-SWITCH"

    try:
        llm.set_run_overrides(run_id, {"vision": {"provider": "b"}})
        plan = client.describe_plan("vision", run_id)
        assert plan[0]["provider"] == "b"
        assert plan[0]["model"] == "m-b"
    finally:
        llm.clear_run_overrides(run_id)


def test_overrides_merge_rather_than_replace(fake):
    provider = llm.ProviderConfig("owui", "openai", fake.base_url, "k", {"primary": "m"})
    client = llm.LLMClient(build_settings([provider], [llm.ChainStep("owui")]))
    run_id = "RUN-MERGE"

    try:
        llm.set_run_overrides(run_id, {"rude": {"model": "m1"}})
        llm.set_run_overrides(run_id, {"rude": {"max_tokens": 4321}})
        plan = client.describe_plan("rude", run_id)[0]
        assert plan["model"] == "m1"
        assert plan["max_tokens"] == 4321
    finally:
        llm.clear_run_overrides(run_id)


def test_explicit_provider_argument_pins_the_call(fake, secondary):
    secondary.reply = "from the pinned provider"
    a = llm.ProviderConfig("a", "openai", fake.base_url, "k", {"primary": "m-a"})
    b = llm.ProviderConfig("b", "openai", secondary.base_url, "k", {"primary": "m-b"})
    client = llm.LLMClient(build_settings([a, b], [llm.ChainStep("a"), llm.ChainStep("b")]))

    assert client.call("p", "s", "grounder", provider="b") == "from the pinned provider"
    assert not fake.requests, "pinned call must not touch provider a"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_list_models(fake):
    fake.models = ["qwen3:32b", "llama3.2:3b"]
    provider = llm.ProviderConfig("owui", "openai", fake.base_url, "k", {"primary": "m"})
    client = llm.LLMClient(build_settings([provider], [llm.ChainStep("owui")]))

    assert client.list_models("owui") == ["llama3.2:3b", "qwen3:32b"]


def test_list_models_survives_unreachable_provider():
    provider = llm.ProviderConfig("dead", "openai", "http://127.0.0.1:1", "k", {"primary": "m"})
    client = llm.LLMClient(build_settings([provider], [llm.ChainStep("dead")]))
    assert client.list_models("dead") == []


def test_health_reports_per_provider(fake):
    live = llm.ProviderConfig("live", "openai", fake.base_url, "k", {"primary": "m"})
    dead = llm.ProviderConfig("dead", "openai", "", "", {})
    report = {e["provider"]: e for e in
              llm.LLMClient(build_settings([live, dead], [llm.ChainStep("live")])).health()}

    assert report["live"]["reachable"] is True
    assert report["live"]["has_api_key"] is True
    assert report["dead"]["configured"] is False


# ---------------------------------------------------------------------------
# The agent-facing seam must not have changed
# ---------------------------------------------------------------------------

def test_call_signature_is_backward_compatible():
    """All 17 existing agent call sites use (prompt, system, agent_name=...)."""
    import inspect
    params = list(inspect.signature(llm.call).parameters)
    assert params[:3] == ["prompt", "system", "agent_name"]
    assert "run_id" in params and "provider" in params
