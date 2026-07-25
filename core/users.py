"""
Users and Provider Credentials
------------------------------
Multi-user support, with the authentication seam deliberately narrow.

Today a user is identified by the API key they present for their model
provider (Open-WebUI by default): the key is validated against the provider,
fingerprinted, and that fingerprint identifies the account. No passwords, no
user store to maintain.

When SAML arrives it replaces `resolve_user` and nothing else. The rest of the
system only ever sees a user_id.

Provider API keys are stored encrypted (see core/crypto.py) and never returned
by the API — only a masked hint.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from core import database as db
from core import crypto
from core.utils import generate_id

logger = logging.getLogger(__name__)


USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       {ID} PRIMARY KEY,
    display_name  {KEY},
    auth_kind     {KEY} DEFAULT 'provider_key',  -- provider_key / saml / ...
    auth_ref      {KEY} NOT NULL,                -- key fingerprint, or SAML subject
    created_at    {TEXT} NOT NULL,
    last_seen_at  {TEXT},
    UNIQUE (auth_kind, auth_ref)
);

CREATE TABLE IF NOT EXISTS user_credentials (
    cred_id       {ID} PRIMARY KEY,
    user_id       {ID} NOT NULL,
    provider      {KEY} NOT NULL,
    base_url      {TEXT},
    api_key_enc   {LONGTEXT},
    key_hint      {KEY},
    models        {TEXT},                        -- JSON {"primary": ..., "light": ...}
    created_at    {TEXT} NOT NULL,
    updated_at    {TEXT},
    UNIQUE (user_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_user_creds_user ON user_credentials(user_id);
"""

_schema_ready = False


def init_users_tables():
    global _schema_ready
    if _schema_ready:
        return
    from core import db_backend
    db_backend.get_backend().init_schema(USERS_SCHEMA)
    _schema_ready = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_user(user_id: str) -> Optional[dict]:
    init_users_tables()
    rows = db.fetch("users", {"user_id": user_id})
    return rows[0] if rows else None


def find_by_auth(auth_ref: str, auth_kind: str = "provider_key") -> Optional[dict]:
    init_users_tables()
    rows = db.fetch("users", {"auth_kind": auth_kind, "auth_ref": auth_ref})
    return rows[0] if rows else None


def get_or_create(auth_ref: str, display_name: str = "",
                  auth_kind: str = "provider_key") -> dict:
    """Find a user by their auth reference, creating the account on first sight."""
    init_users_tables()
    existing = find_by_auth(auth_ref, auth_kind)
    if existing:
        db.update("users", {"last_seen_at": _now()},
                  {"user_id": existing["user_id"]})
        if display_name and not existing.get("display_name"):
            db.update("users", {"display_name": display_name},
                      {"user_id": existing["user_id"]})
        return get_user(existing["user_id"])

    user_id = generate_id("USR")
    db.insert("users", {
        "user_id":      user_id,
        "display_name": display_name or "researcher",
        "auth_kind":    auth_kind,
        "auth_ref":     auth_ref,
        "created_at":   _now(),
        "last_seen_at": _now(),
    })
    logger.info(f"New user registered: {user_id} ({auth_kind})")
    return get_user(user_id)


def set_display_name(user_id: str, name: str) -> bool:
    return db.update("users", {"display_name": name}, {"user_id": user_id})


# ---------------------------------------------------------------------------
# Provider credentials
# ---------------------------------------------------------------------------

def set_credentials(user_id: str, provider: str, base_url: str,
                    api_key: str, models: dict = None) -> dict:
    """
    Store a user's endpoint and key for one provider.

    Raises SecretUnavailable if encryption is not configured — we do not
    store provider keys in plaintext.
    """
    import json
    init_users_tables()

    encrypted = crypto.encrypt(api_key) if api_key else ""
    existing = get_credentials_row(user_id, provider)

    record = {
        "cred_id":     (existing or {}).get("cred_id") or generate_id("CRD"),
        "user_id":     user_id,
        "provider":    provider,
        "base_url":    (base_url or "").rstrip("/"),
        "api_key_enc": encrypted,
        "key_hint":    crypto.mask(api_key),
        "models":      json.dumps(models or {}),
        "created_at":  (existing or {}).get("created_at") or _now(),
        "updated_at":  _now(),
    }
    db.insert("user_credentials", record)
    logger.info(f"Credentials stored for {user_id} / {provider}")
    return public_credentials(record)


def set_models(user_id: str, provider: str, models: dict) -> Optional[dict]:
    """
    Update only which models a provider serves for each role.

    Separate from set_credentials because the API key is never returned to
    the client, so the UI cannot re-submit it just to change a model.
    """
    import json
    row = get_credentials_row(user_id, provider)
    if not row:
        return None
    db.update("user_credentials",
              {"models": json.dumps(models or {}), "updated_at": _now()},
              {"user_id": user_id, "provider": provider})
    return public_credentials(get_credentials_row(user_id, provider))


def get_credentials_row(user_id: str, provider: str) -> Optional[dict]:
    init_users_tables()
    rows = db.fetch("user_credentials", {"user_id": user_id, "provider": provider})
    return rows[0] if rows else None


def list_credentials(user_id: str) -> list[dict]:
    """Every provider this user has configured, without the secrets."""
    init_users_tables()
    return [public_credentials(r)
            for r in db.fetch("user_credentials", {"user_id": user_id})]


def public_credentials(row: dict) -> dict:
    """The safe-to-return view — never includes the key."""
    import json
    models = row.get("models") or "{}"
    try:
        models = json.loads(models) if isinstance(models, str) else models
    except (json.JSONDecodeError, TypeError):
        models = {}
    return {
        "provider":   row.get("provider"),
        "base_url":   row.get("base_url"),
        "key_hint":   row.get("key_hint"),
        "models":     models,
        "updated_at": row.get("updated_at"),
    }


def delete_credentials(user_id: str, provider: str) -> bool:
    init_users_tables()
    return db.execute(
        "DELETE FROM user_credentials WHERE user_id = ? AND provider = ?",
        (user_id, provider),
    )


def provider_config(user_id: str, provider: str):
    """
    Build an llm.ProviderConfig from a user's stored credentials.

    Returns None when the user has not configured that provider.
    """
    import json
    from core import llm

    row = get_credentials_row(user_id, provider)
    if not row:
        return None

    api_key = crypto.decrypt(row.get("api_key_enc") or "")
    models = row.get("models") or "{}"
    try:
        models = json.loads(models) if isinstance(models, str) else models
    except (json.JSONDecodeError, TypeError):
        models = {}

    kind = "anthropic" if provider == "anthropic" else "openai"
    return llm.ProviderConfig(
        name=provider,
        kind=kind,
        base_url=(row.get("base_url") or "").rstrip("/"),
        api_key=api_key,
        models=models,
    )


# ---------------------------------------------------------------------------
# Run ownership
# ---------------------------------------------------------------------------

RUN_OWNER_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_owners (
    run_id     {ID} PRIMARY KEY,
    user_id    {ID} NOT NULL,
    provider   {KEY},
    created_at {TEXT} NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_owners_user ON run_owners(user_id);
"""

_owner_schema_ready = False


def init_run_owners():
    """
    Ownership is a side table rather than a column on `runs`, so existing
    CLI runs (which have no user) stay valid and unmodified.
    """
    global _owner_schema_ready
    if _owner_schema_ready:
        return
    from core import db_backend
    db_backend.get_backend().init_schema(RUN_OWNER_SCHEMA)
    _owner_schema_ready = True


def claim_run(run_id: str, user_id: str, provider: str = "") -> bool:
    init_run_owners()
    return db.insert("run_owners", {
        "run_id":     run_id,
        "user_id":    user_id,
        "provider":   provider,
        "created_at": _now(),
    })


def run_owner(run_id: str) -> Optional[dict]:
    init_run_owners()
    rows = db.fetch("run_owners", {"run_id": run_id})
    return rows[0] if rows else None


def owns_run(user_id: str, run_id: str) -> bool:
    owner = run_owner(run_id)
    return bool(owner and owner.get("user_id") == user_id)


def runs_for_user(user_id: str) -> list[str]:
    init_run_owners()
    rows = db.fetch("run_owners", {"user_id": user_id})
    return [r["run_id"] for r in rows]
