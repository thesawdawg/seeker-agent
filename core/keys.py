"""
API Key Manager
---------------
Loads API keys from .env file and environment variables.
Provides keys to all source handlers.
Warns clearly when a required key is missing.

In a multi-user (web) deployment, a worker binds the run's owner with
set_current_user() before advancing; the accessors below then check that
user's stored source credentials first, falling back to env vars. The CLI
and tools never bind a user, so they keep using env vars exactly as before
(review U2).
"""

import contextvars
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_PATH = Path(__file__).parent.parent / ".env"

# Per-run owner, set by the worker so source handlers pick up the user's
# stored academic-source keys before falling back to env vars (review U2).
_current_user: contextvars.ContextVar = contextvars.ContextVar(
    "seeker_keys_user", default="")


def _load_env():
    """Load .env file into os.environ if not already set."""
    if not ENV_PATH.exists():
        logger.debug(f"[Keys] No .env file found at {ENV_PATH} — using environment only")
        return
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key   = key.strip()
                value = value.strip()
                # Only set if not already in environment
                if key and value and key not in os.environ:
                    os.environ[key] = value


# Load on import
_load_env()


def set_current_user(user_id: str) -> None:
    """Bind subsequent key lookups to a user (worker-side, per run)."""
    _current_user.set(user_id or "")


def clear_current_user() -> None:
    _current_user.set("")


def get(key: str, required: bool = False, source_name: str = "",
        source_id: str = "") -> str:
    """
    Get an API key by environment variable name.
    If required=True and key is missing, logs a clear warning.
    Returns empty string if not set.

    When a user is bound (web worker), checks that user's stored source
    credentials for ``source_id`` first, then falls back to env (review U2).
    """
    # Per-user stored key takes precedence over env in a multi-user deployment.
    user_id = _current_user.get()
    if user_id and source_id:
        try:
            from core import users
            stored = users.get_source_api_key(user_id, source_id)
            if stored:
                return stored
        except Exception:
            pass  # DB/crypto unavailable (tests) — fall through to env

    value = os.environ.get(key, "").strip()
    if not value and required:
        logger.warning(
            f"[Keys] {key} is not set — {source_name} will not work correctly.\n"
            f"  Add it to your .env file or environment.\n"
            f"  See .env.example for instructions."
        )
    return value


# ---------------------------------------------------------------------------
# Convenience accessors
#
# OpenAlex and NCBI email are NOT required in practice (review R6/R7): the
# OpenAlex handler falls back to a mailto parameter, and PubMed works at the
# lower 3 req/s tier without an email. The previous required=True warnings
# were misleading on first run.
# ---------------------------------------------------------------------------

def openalex() -> str:
    return get("OPENALEX_API_KEY", required=False, source_id="openalex",
               source_name="OpenAlex (optional — mailto used if absent)")

def ncbi_api_key() -> str:
    return get("NCBI_API_KEY", required=False, source_id="pubmed",
               source_name="PubMed/NCBI (optional — 3x rate limit boost)")

def ncbi_email() -> str:
    return get("NCBI_EMAIL", required=False, source_id="pubmed",
               source_name="PubMed/NCBI (email requested by ToS, not strictly required)")

def semantic_scholar() -> str:
    return get("SEMANTIC_SCHOLAR_API_KEY", required=False, source_id="semantic_scholar",
               source_name="Semantic Scholar (optional)")

def core() -> str:
    return get("CORE_API_KEY", required=False, source_id="core",
               source_name="CORE (optional — higher rate limits)")

def philpapers_id() -> str:
    return get("PHILPAPERS_API_ID", required=False, source_id="philpapers",
               source_name="PhilPapers (optional — OAI-PMH used as fallback)")

def philpapers_key() -> str:
    return get("PHILPAPERS_API_KEY", required=False, source_id="philpapers",
               source_name="PhilPapers (optional — OAI-PMH used as fallback)")

def anthropic() -> str:
    # No longer required — Anthropic is one optional provider among many.
    return get("ANTHROPIC_API_KEY", required=False, source_name="Anthropic Claude API")

def google_books() -> str:
    return get("GOOGLE_BOOKS_API_KEY", required=False, source_id="google_books",
               source_name="Google Books (optional — higher quota)")

def scopus_api_key() -> str:
    return get("SCOPUS_API_KEY", required=False, source_id="scopus",
               source_name="Scopus (optional — needs institutional IP/VPN)")

def scopus_inst_token() -> str:
    return get("SCOPUS_INST_TOKEN", required=False, source_id="scopus",
               source_name="Scopus institutional token (optional — email datasupport@elsevier.com)")

def consensus_mcp_status() -> str:
    """Consensus uses MCP OAuth — check db/consensus_tokens.json for token status."""
    from pathlib import Path
    token_file = Path(__file__).parent.parent / "db" / "consensus_tokens.json"
    return "authenticated" if token_file.exists() else "not authenticated (run pipeline once to trigger OAuth)"


def print_key_status():
    """Print a clear table of which keys are set and which are missing."""
    checks = [
        ("OPENALEX_API_KEY",        "OpenAlex",         False, "openalex.org/settings/api (mailto used if absent)"),
        ("NCBI_API_KEY",            "PubMed (NCBI)",    False, "ncbi.nlm.nih.gov/account"),
        ("NCBI_EMAIL",              "PubMed email",     False, "any valid email (requested by ToS, not required)"),
        ("SEMANTIC_SCHOLAR_API_KEY","Semantic Scholar", False, "semanticscholar.org/product/api"),
        ("CORE_API_KEY",            "CORE",             False, "core.ac.uk/services/api"),
        ("PHILPAPERS_API_ID",       "PhilPapers ID",    False, "philpapers.org/utils/create_api_user.html"),
        ("PHILPAPERS_API_KEY",      "PhilPapers Key",   False, "philpapers.org/utils/create_api_user.html"),
        ("SCOPUS_API_KEY",          "Scopus",           False, "dev.elsevier.com → Create API Key"),
        ("SCOPUS_INST_TOKEN",        "Scopus Inst Token",False, "email datasupport@elsevier.com"),
        ("GOOGLE_BOOKS_API_KEY",    "Google Books",     False, "console.cloud.google.com → Books API"),
    ]
    print(f"\n  {'─'*60}")
    print(f"  API Key Status")
    print(f"  {'─'*60}")
    all_required_ok = True
    for env_var, name, required, url in checks:
        value = os.environ.get(env_var, "").strip()
        if value:
            masked = value[:4] + "..." + value[-4:] if len(value) > 8 else "****"
            status = f"✅ set ({masked})"
        elif required:
            status = f"❌ MISSING (required) → {url}"
            all_required_ok = False
        else:
            status = f"⚠️  not set (optional) → {url}"
        req_str = "[required]" if required else "[optional]"
        print(f"  {name:<22} {req_str:<12} {status}")
    print(f"  {'─'*60}")
    if not all_required_ok:
        print(f"  ⚠️  Some required keys are missing. Edit your .env file.")
        print(f"     Copy .env.example → .env and fill in the values.")
    else:
        print(f"  ✅ All required keys are set.")

    print_llm_status()


def print_llm_status():
    """Print LLM provider configuration and reachability."""
    from core import llm

    print(f"\n  {'─'*60}")
    print(f"  LLM Providers")
    print(f"  {'─'*60}")

    for entry in llm.health():
        if not entry["configured"]:
            state = "⚠️  no base_url"
        elif entry["reachable"] is None:
            state = "✅ configured (not probed)" if entry["has_api_key"] else "⚠️  no api key"
        elif entry["reachable"]:
            state = f"✅ reachable ({entry['model_count']} models)"
        else:
            state = "❌ unreachable"
        key_note = "" if entry["has_api_key"] else "  [no api key]"
        print(f"  {entry['provider']:<14} {entry['kind']:<10} {state}{key_note}")
        if entry["base_url"]:
            print(f"  {'':<14} {entry['base_url']}")

        provider = llm.get_client().settings.providers.get(entry["provider"])
        models = {k: v for k, v in (provider.models if provider else {}).items() if v}
        if models:
            print(f"  {'':<14} models: " +
                  ", ".join(f"{role}={name}" for role, name in models.items()))
        else:
            print(f"  {'':<14} models: none set — this provider will be skipped "
                  f"(config.json → llm.providers.{entry['provider']}.models)")

    print(f"  {'─'*60}")
    print(f"  Routing per agent")
    print(f"  {'─'*60}")
    client = llm.get_client()
    for agent in sorted(client.settings.agents):
        plan = client.describe_plan(agent)
        if plan:
            chain = " → ".join(f"{s['provider']}:{s['model']}" for s in plan)
            print(f"  {agent:<14} {chain}")
        else:
            print(f"  {agent:<14} ❌ no usable provider")
    print(f"  {'─'*60}")
    print(f"  {'─'*60}\n")
