"""
LLM Router
----------
Provider-agnostic. Any OpenAI-compatible endpoint works — Open-WebUI (the
default), OpenAI, Ollama's /v1 route, LM Studio, vLLM, llama.cpp, OpenRouter,
Together, Groq. Anthropic is supported as one provider among many, not as a
privileged primary.

Every agent calls the same seam:

    llm.call(prompt, system, agent_name="grounder", run_id=run_id)

Routing, the fallback chain, per-agent model roles and token limits all come
from the "llm" section of config.json rather than from constants in this file.

Per-run overrides (set at a human-in-the-loop break, so a researcher can change
models mid-pipeline) are registered with set_run_overrides() and take precedence
over config for that run only.
"""

import os
import time
import logging
import threading
from dataclasses import dataclass, field, replace
from typing import Optional, Any

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderConfig:
    """One reachable model endpoint."""
    name:     str
    kind:     str = "openai"          # wire format: "openai" | "anthropic"
    base_url: str = ""
    api_key:  str = ""
    models:   dict = field(default_factory=dict)   # {"primary": ..., "light": ...}

    def model_for_role(self, role: str) -> str:
        return (self.models or {}).get(role, "") or (self.models or {}).get("primary", "")

    @property
    def configured(self) -> bool:
        """A provider is usable once it has somewhere to send a request."""
        return bool(self.base_url)


@dataclass(frozen=True)
class AgentProfile:
    """How one agent should be run."""
    model_role:  str = "primary"
    max_tokens:  int = 8192
    temperature: float = 0.7
    model:       Optional[str] = None    # pin an exact model, ignoring model_role
    provider:    Optional[str] = None    # pin a provider, ignoring the chain


@dataclass(frozen=True)
class ChainStep:
    """One rung of the fallback ladder."""
    provider:   str
    model:      Optional[str] = None
    model_role: Optional[str] = None


@dataclass(frozen=True)
class LLMSettings:
    providers:       dict            # name -> ProviderConfig
    chain:           tuple           # ordered ChainStep
    agents:          dict            # name -> AgentProfile
    default_profile: AgentProfile = AgentProfile()
    timeout_seconds: int = 300
    max_retries:     int = 3
    retry_delay:     int = 5

    def profile_for(self, agent_name: str) -> AgentProfile:
        return self.agents.get((agent_name or "").lower(), self.default_profile)


# ---------------------------------------------------------------------------
# Loading config
# ---------------------------------------------------------------------------

def _resolve_provider(name: str, raw: dict) -> ProviderConfig:
    """Env vars win over config.json, so deployments override without editing files."""
    base_url = os.environ.get(raw.get("base_url_env", ""), "").strip() or raw.get("base_url", "")
    api_key  = os.environ.get(raw.get("api_key_env", ""), "").strip()  or raw.get("api_key", "")
    return ProviderConfig(
        name=name,
        kind=raw.get("kind", "openai"),
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        models=dict(raw.get("models") or {}),
    )


def load_settings(config: Optional[dict] = None) -> LLMSettings:
    """Build settings from config.json's "llm" section plus the environment."""
    if config is None:
        from core.utils import load_config
        config = load_config()

    raw = (config or {}).get("llm") or {}
    defaults = raw.get("defaults") or {}

    default_profile = AgentProfile(
        model_role=defaults.get("model_role", "primary"),
        max_tokens=int(defaults.get("max_tokens", 8192)),
        temperature=float(defaults.get("temperature", 0.7)),
    )

    providers = {
        name: _resolve_provider(name, spec)
        for name, spec in (raw.get("providers") or {}).items()
        if not name.startswith("_")
    }

    chain = []
    for step in raw.get("fallback_chain") or []:
        provider = step.get("provider")
        if provider in providers:
            chain.append(ChainStep(provider, step.get("model"), step.get("model_role")))
        elif provider:
            logger.warning(f"[LLM] fallback_chain references unknown provider '{provider}' — skipped")

    if not chain:
        fallback = raw.get("default_provider") or next(iter(providers), None)
        if fallback:
            chain.append(ChainStep(fallback))
            logger.warning(f"[LLM] No usable fallback_chain — defaulting to '{fallback}'")

    agents = {}
    for name, spec in (raw.get("agents") or {}).items():
        if name.startswith("_"):
            continue
        agents[name.lower()] = AgentProfile(
            model_role=spec.get("model_role", default_profile.model_role),
            max_tokens=int(spec.get("max_tokens", default_profile.max_tokens)),
            temperature=float(spec.get("temperature", default_profile.temperature)),
            model=spec.get("model"),
            provider=spec.get("provider"),
        )

    return LLMSettings(
        providers=providers,
        chain=tuple(chain),
        agents=agents,
        default_profile=default_profile,
        timeout_seconds=int(defaults.get("timeout_seconds", 300)),
        max_retries=int(defaults.get("max_retries", 3)),
        retry_delay=int(defaults.get("retry_delay_seconds", 5)),
    )


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

class LLMError(RuntimeError):
    """No provider in the chain could serve the request."""


# Providers already reported as missing a model, so the warning is not repeated
# for every agent that routes past them.
_warned_missing_model: set = set()


def _headers(provider: ProviderConfig) -> dict:
    if provider.kind == "anthropic":
        return {
            "x-api-key": provider.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    return {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }


def _post_openai(provider, model, prompt, system, profile, timeout) -> str:
    """OpenAI-compatible chat completions — Open-WebUI, OpenAI, Ollama /v1, vLLM, ..."""
    url = f"{provider.base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens": profile.max_tokens,
        "temperature": profile.temperature,
        "stream": False,
    }
    resp = requests.post(url, json=payload, headers=_headers(provider), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"{provider.name} returned no choices: {str(data)[:200]}")
    text = (choices[0].get("message") or {}).get("content")
    if not text:
        raise LLMError(f"{provider.name} returned an empty message")
    return text


def _post_anthropic(provider, model, prompt, system, profile, timeout) -> str:
    """Anthropic Messages API — a different wire format, same router."""
    url = f"{provider.base_url}/v1/messages"
    payload = {
        "model": model,
        "max_tokens": profile.max_tokens,
        "temperature": profile.temperature,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(url, json=payload, headers=_headers(provider), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    blocks = [b.get("text", "") for b in (data.get("content") or []) if b.get("type") == "text"]
    text = "".join(blocks)
    if not text:
        raise LLMError(f"{provider.name} returned no text content")
    return text


_TRANSPORTS = {"openai": _post_openai, "anthropic": _post_anthropic}


# ---------------------------------------------------------------------------
# Per-run overrides — how a break changes models mid-pipeline
# ---------------------------------------------------------------------------

_run_overrides: dict = {}
_run_providers: dict = {}
_overrides_lock = threading.Lock()


def set_run_providers(run_id: str, providers: dict) -> None:
    """
    Supply provider configs for one run, overlaying config.json.

    This is how a multi-user deployment runs the pipeline against the
    requesting user's own endpoint and API key: the worker loads their stored
    credentials and registers them here before advancing the run. Credentials
    never touch global state, so concurrent runs for different users cannot
    borrow each other's keys.
    """
    with _overrides_lock:
        current = dict(_run_providers.get(run_id) or {})
        current.update({name: cfg for name, cfg in (providers or {}).items() if cfg})
        _run_providers[run_id] = current
    logger.info(f"[LLM] Provider overlay set for run {run_id}: "
                f"{sorted((providers or {}).keys())}")


def get_run_providers(run_id: str) -> dict:
    with _overrides_lock:
        return dict(_run_providers.get(run_id) or {})


def clear_run_providers(run_id: str) -> None:
    with _overrides_lock:
        _run_providers.pop(run_id, None)


def set_run_overrides(run_id: str, overrides: dict) -> None:
    """
    Pin models for a single run, e.g. from a break submission:

        set_run_overrides(run_id, {"theorist": {"model": "qwen3:32b",
                                                "provider": "ollama"}})

    Applies to agents that have not run yet. Keys are agent names; recognised
    fields are model, provider, model_role, max_tokens and temperature.
    """
    with _overrides_lock:
        current = dict(_run_overrides.get(run_id) or {})
        for agent, spec in (overrides or {}).items():
            merged = dict(current.get(agent.lower()) or {})
            merged.update({k: v for k, v in (spec or {}).items() if v is not None})
            current[agent.lower()] = merged
        _run_overrides[run_id] = current
    logger.info(f"[LLM] Model overrides set for run {run_id}: {overrides}")


def get_run_overrides(run_id: str) -> dict:
    with _overrides_lock:
        return dict(_run_overrides.get(run_id) or {})


def clear_run_overrides(run_id: str) -> None:
    with _overrides_lock:
        _run_overrides.pop(run_id, None)


def _apply_overrides(profile: AgentProfile, agent_name: str, run_id: Optional[str]) -> AgentProfile:
    if not run_id:
        return profile
    spec = get_run_overrides(run_id).get((agent_name or "").lower())
    if not spec:
        return profile
    return replace(
        profile,
        model=spec.get("model", profile.model),
        provider=spec.get("provider", profile.provider),
        model_role=spec.get("model_role", profile.model_role),
        max_tokens=int(spec.get("max_tokens", profile.max_tokens)),
        temperature=float(spec.get("temperature", profile.temperature)),
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class LLMClient:
    def __init__(self, settings: Optional[LLMSettings] = None):
        _load_env()
        self.settings = settings or load_settings()

    # -- planning -----------------------------------------------------------

    def _plan(self, agent_name: str, run_id: Optional[str] = None) -> list:
        """
        Resolve the ordered list of (provider, model) attempts for an agent.
        A pinned provider on the agent replaces the chain; the chain is the
        fallback ladder otherwise.
        """
        profile = _apply_overrides(self.settings.profile_for(agent_name), agent_name, run_id)

        # A run's own providers win over config.json, and are tried first —
        # the user configured them precisely so their run uses them.
        run_providers = get_run_providers(run_id) if run_id else {}

        if profile.provider:
            steps = [ChainStep(profile.provider, profile.model, profile.model_role)]
        else:
            steps = list(self.settings.chain)
            known = {s.provider for s in steps}
            steps = ([ChainStep(name) for name in run_providers if name not in known]
                     + steps)

        attempts = []
        for step in steps:
            provider = run_providers.get(step.provider) or \
                self.settings.providers.get(step.provider)
            if not provider:
                continue
            if not provider.configured:
                logger.debug(f"[{agent_name}] Provider '{step.provider}' has no base_url — skipped")
                continue
            model = (
                step.model
                or (profile.model if not profile.provider or step.provider == profile.provider else None)
                or provider.model_for_role(step.model_role or profile.model_role)
            )
            if not model:
                role = step.model_role or profile.model_role
                # Warn once per provider+role, not once per agent that hits it.
                if (provider.name, role) not in _warned_missing_model:
                    _warned_missing_model.add((provider.name, role))
                    logger.warning(
                        f"[LLM] Provider '{provider.name}' has no model for role "
                        f"'{role}' — it will be skipped. Set one in config.json "
                        f"llm.providers.{provider.name}.models"
                    )
                continue
            attempts.append((provider, model, profile))
        return attempts

    def describe_plan(self, agent_name: str, run_id: Optional[str] = None) -> list[dict]:
        """What this agent would try, in order. Used by the UI and `main.py keys`."""
        return [
            {"provider": p.name, "kind": p.kind, "model": m,
             "max_tokens": prof.max_tokens, "temperature": prof.temperature}
            for p, m, prof in self._plan(agent_name, run_id)
        ]

    # -- execution ----------------------------------------------------------

    def _attempt(self, provider, model, prompt, system, profile, agent_name) -> Optional[str]:
        transport = _TRANSPORTS.get(provider.kind)
        if not transport:
            logger.error(f"[{agent_name}] Unknown provider kind '{provider.kind}' for {provider.name}")
            return None

        for attempt in range(1, self.settings.max_retries + 1):
            try:
                logger.info(
                    f"[{agent_name}] {provider.name} ({provider.kind}) — model: {model} "
                    f"— attempt {attempt}/{self.settings.max_retries}"
                )
                text = transport(provider, model, prompt, system, profile,
                                 self.settings.timeout_seconds)
                logger.info(f"[{agent_name}] {provider.name} success — {len(text)} chars returned")
                return text

            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                body = (e.response.text[:300] if e.response is not None else "")
                retryable = status == 429 or status >= 500
                logger.warning(f"[{agent_name}] {provider.name} HTTP {status}: {body}")
                if not retryable:
                    return None
                if attempt < self.settings.max_retries:
                    time.sleep(self.settings.retry_delay * attempt)

            except requests.Timeout:
                logger.warning(
                    f"[{agent_name}] {provider.name} timed out after "
                    f"{self.settings.timeout_seconds}s"
                )
                if attempt < self.settings.max_retries:
                    time.sleep(self.settings.retry_delay)

            except requests.ConnectionError as e:
                logger.warning(f"[{agent_name}] {provider.name} unreachable at {provider.base_url}: {e}")
                return None

            except Exception as e:
                logger.error(f"[{agent_name}] {provider.name} error: {e}")
                return None

        logger.error(f"[{agent_name}] {provider.name} exhausted all retries")
        return None

    def call(
        self,
        prompt: str,
        system: str = "You are a helpful research assistant.",
        agent_name: str = "unknown",
        run_id: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> str:
        """
        Route one completion, walking the fallback chain until one succeeds.

        provider — pin a single provider for this call, bypassing the chain.
        run_id   — apply any per-run model overrides set at a break.
        """
        if provider:
            attempts = [
                (p, m, prof) for p, m, prof in self._plan(agent_name, run_id)
                if p.name == provider
            ] or self._plan_single(provider, agent_name, run_id)
        else:
            attempts = self._plan(agent_name, run_id)

        if not attempts:
            raise LLMError(
                f"[{agent_name}] No usable LLM provider. Configure one in config.json "
                f"under llm.providers and set its api key in .env "
                f"(default: OPENWEBUI_BASE_URL + OPENWEBUI_API_KEY)."
            )

        for prov, model, profile in attempts:
            result = self._attempt(prov, model, prompt, system, profile, agent_name)
            if result:
                return result
            logger.info(f"[{agent_name}] Falling back past {prov.name}")

        tried = ", ".join(f"{p.name}:{m}" for p, m, _ in attempts)
        raise LLMError(f"[{agent_name}] All LLM providers failed. Tried: {tried}")

    def _plan_single(self, provider_name, agent_name, run_id):
        prov = (get_run_providers(run_id).get(provider_name) if run_id else None) \
            or self.settings.providers.get(provider_name)
        if not prov or not prov.configured:
            return []
        profile = _apply_overrides(self.settings.profile_for(agent_name), agent_name, run_id)
        model = profile.model or prov.model_for_role(profile.model_role)
        return [(prov, model, profile)] if model else []

    # -- discovery ----------------------------------------------------------

    def list_models(self, provider_name: Optional[str] = None) -> list[str]:
        """
        Ask a provider what models it serves — GET {base_url}/models.
        Backs the model picker in the web UI. Returns [] if unreachable.
        """
        name = provider_name or (self.settings.chain[0].provider if self.settings.chain else None)
        provider = self.settings.providers.get(name or "")
        if not provider or not provider.configured or provider.kind != "openai":
            return []
        try:
            resp = requests.get(f"{provider.base_url}/models",
                                headers=_headers(provider), timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            logger.warning(f"[LLM] Could not list models for {provider.name}: {e}")
            return []

        items = payload.get("data") if isinstance(payload, dict) else payload
        return sorted(
            {m.get("id") or m.get("name") for m in (items or []) if isinstance(m, dict)} - {None}
        )

    def health(self) -> list[dict]:
        """Per-provider reachability, for `main.py keys` and the UI."""
        report = []
        for name, provider in self.settings.providers.items():
            entry = {"provider": name, "kind": provider.kind,
                     "base_url": provider.base_url,
                     "has_api_key": bool(provider.api_key),
                     "configured": provider.configured}
            if provider.configured and provider.kind == "openai":
                models = self.list_models(name)
                entry["reachable"] = bool(models)
                entry["model_count"] = len(models)
            else:
                entry["reachable"] = None
                entry["model_count"] = None
            report.append(entry)
        return report


# ---------------------------------------------------------------------------
# .env loading — agents may import this before core.keys has run
# ---------------------------------------------------------------------------

def _load_env():
    from pathlib import Path
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and value and key not in os.environ:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_client: Optional[LLMClient] = None
_client_lock = threading.Lock()


def get_client() -> LLMClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = LLMClient()
        return _client


def reset_client() -> None:
    """Drop the cached client so the next call re-reads config and env."""
    global _client
    with _client_lock:
        _client = None


def call(
    prompt: str,
    system: str = "You are a helpful research assistant.",
    agent_name: str = "unknown",
    run_id: Optional[str] = None,
    provider: Optional[str] = None,
) -> str:
    """Convenience function — use this in agents."""
    return get_client().call(prompt, system, agent_name, run_id, provider)


def list_models(provider_name: Optional[str] = None) -> list[str]:
    return get_client().list_models(provider_name)


def health() -> list[dict]:
    return get_client().health()
