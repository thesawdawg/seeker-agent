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

-- Per-user academic source API keys (review U2). A multi-user deployment
-- can't have every user's Scopus/CORE key in one .env file. Stored
-- encrypted, same as provider credentials, and never returned to the client.
CREATE TABLE IF NOT EXISTS user_source_credentials (
    cred_id       {ID} PRIMARY KEY,
    user_id       {ID} NOT NULL,
    source_id     {KEY} NOT NULL,                -- scopus / semantic_scholar / core / ...
    api_key_enc   {LONGTEXT},
    key_hint      {KEY},
    created_at    {TEXT} NOT NULL,
    updated_at    {TEXT},
    UNIQUE (user_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_user_creds_user ON user_credentials(user_id);
CREATE INDEX IF NOT EXISTS idx_user_source_creds ON user_source_credentials(user_id);

-- Saved config templates (review U10): model + source overrides a researcher
-- wants to reuse across runs.
CREATE TABLE IF NOT EXISTS user_templates (
    template_id   {ID} PRIMARY KEY,
    user_id       {ID} NOT NULL,
    name          {KEY} NOT NULL,
    config_json   {LONGTEXT} NOT NULL,             -- {model_overrides, source_overrides, ...}
    created_at    {TEXT} NOT NULL,
    UNIQUE (user_id, name)
);

-- Per-user preferences (key-value). Used for global toggles like MCP
-- connection enable/disable, which apply to all of a user's runs.
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id       {ID} NOT NULL,
    pref_key      {KEY} NOT NULL,
    pref_value    {TEXT},
    updated_at    {TEXT},
    UNIQUE (user_id, pref_key)
);
"""

_schema_ready = False


def init_users_tables():
    global _schema_ready
    if _schema_ready:
        return
    from core import db_backend
    db_backend.get_backend().init_schema(USERS_SCHEMA)
    # is_admin column (F12) — added after first ship. The first user to
    # register automatically becomes admin (a single-researcher install
    # means the first user is the operator). A specific admin can also be
    # pinned via the SEEKER_ADMIN_USER_ID env var.
    db_backend.ensure_columns("users", {"is_admin": "{INT} DEFAULT 0"})
    # password_hash column — for username/password auth (auth_kind="password").
    # The hash is stored here; auth_ref holds the username for lookup.
    db_backend.ensure_columns("users", {"password_hash": "{TEXT}"})
    _maybe_auto_promote_first_user()
    _maybe_promote_env_admin()
    _schema_ready = True


AUTO_PROMOTE_ENV = "SEEKER_AUTO_PROMOTE_FIRST_USER"


def _maybe_auto_promote_first_user():
    """
    Promote the sole user to admin on a single-researcher install.

    Two things were wrong before (review S2). The candidate came from an
    unordered `SELECT * FROM users` and was taken as `[0]`, which on MySQL is
    primary-key order over random `USR-<hex>` ids — so the promoted account
    was arbitrary, not the first to register, while the log line claimed
    otherwise. And it re-ran on every process start, so deleting the admin
    handed admin to another arbitrary user at the next boot.

    Now: only ever when there is exactly one account, ordered explicitly, and
    switchable off. Beyond one user an operator must name the admin with
    SEEKER_ADMIN_USER_ID — a config-write privilege should not be assigned by
    accident of registration order.
    """
    import os
    if os.environ.get(AUTO_PROMOTE_ENV, "1").strip().lower() in (
            "0", "false", "no", "off"):
        return
    if db.fetch("users", {"is_admin": 1}, limit=1):
        return

    total = db.count("users")
    if total == 0:
        return
    if total > 1:
        logger.warning(
            f"[F12] {total} users exist and none is an admin. Refusing to "
            f"pick one — set {AUTO_PROMOTE_ENV.replace('AUTO_PROMOTE_FIRST_USER', 'ADMIN_USER_ID')} "
            f"to the account that should administer this deployment.")
        return

    first = db.fetch("users", {}, limit=1, order_by="created_at ASC")[0]
    db.update("users", {"is_admin": 1}, {"user_id": first["user_id"]})
    logger.info(f"[F12] Promoted the only user {first['user_id']} to admin")


def _maybe_promote_env_admin():
    """Promote the user named in SEEKER_ADMIN_USER_ID, if set."""
    import os
    admin_id = os.environ.get("SEEKER_ADMIN_USER_ID", "").strip()
    if not admin_id:
        return
    user = get_user(admin_id)
    if user and not user.get("is_admin"):
        db.update("users", {"is_admin": 1}, {"user_id": admin_id})
        logger.info(f"[F12] Promoted {admin_id} to admin (env var)")


def is_admin(user_id: str) -> bool:
    """Whether this user is an operator (F12)."""
    init_users_tables()
    user = get_user(user_id)
    return bool(user and user.get("is_admin"))


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
    # F12: if this is the first user and no admin exists yet, promote them.
    _maybe_auto_promote_first_user()
    _maybe_promote_env_admin()
    return get_user(user_id)


def set_display_name(user_id: str, name: str) -> bool:
    return db.update("users", {"display_name": name}, {"user_id": user_id})


def list_all_users() -> list[dict]:
    """List all users — admin only. Returns safe fields (no password hashes)."""
    init_users_tables()
    rows = db.fetch("users", {}, order_by="created_at ASC")
    return [{
        "user_id":      r.get("user_id"),
        "display_name": r.get("display_name") or "",
        "auth_kind":    r.get("auth_kind") or "",
        "auth_ref":     r.get("auth_ref") or "",
        "is_admin":     bool(r.get("is_admin")),
        "created_at":   r.get("created_at") or "",
        "last_seen_at": r.get("last_seen_at") or "",
    } for r in rows]


# ---------------------------------------------------------------------------
# Password auth (auth_kind="password")
# ---------------------------------------------------------------------------

def create_password_user(username: str, password: str,
                         display_name: str = "") -> dict:
    """
    Register a new username/password account.

    Returns the new user dict. Raises ValueError if the username is taken.
    The username is stored as ``auth_ref`` with ``auth_kind="password"``;
    the password hash goes in the ``password_hash`` column.
    """
    init_users_tables()
    username = (username or "").strip()
    if not username:
        raise ValueError("Username must not be empty")
    if find_by_auth(username, "password"):
        raise ValueError(f"Username '{username}' is already taken")

    user_id = generate_id("USR")
    db.insert("users", {
        "user_id":       user_id,
        "display_name":  display_name or username,
        "auth_kind":     "password",
        "auth_ref":      username,
        "password_hash": crypto.hash_password(password),
        "created_at":    _now(),
        "last_seen_at":  _now(),
    })
    logger.info(f"New user registered: {user_id} (password, username={username})")
    _maybe_auto_promote_first_user()
    _maybe_promote_env_admin()
    return get_user(user_id)


def verify_password_login(username: str, password: str) -> Optional[dict]:
    """
    Look up a password user and verify the password.

    Returns the user dict on success, None on any failure (unknown user,
    wrong password, or not a password-kind account). Constant-time password
    comparison is handled by ``crypto.verify_password``.
    """
    init_users_tables()
    user = find_by_auth((username or "").strip(), "password")
    if not user:
        return None
    stored_hash = user.get("password_hash") or ""
    if not stored_hash:
        return None
    if not crypto.verify_password(password, stored_hash):
        return None
    # Update last_seen_at on successful login
    db.update("users", {"last_seen_at": _now()},
              {"user_id": user["user_id"]})
    return get_user(user["user_id"])


def set_password(user_id: str, password: str) -> bool:
    """Set or change the password for an existing user."""
    init_users_tables()
    return bool(db.update("users",
                          {"password_hash": crypto.hash_password(password)},
                          {"user_id": user_id}))


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

    # Re-check the stored URL before the worker starts sending this user's
    # prompts and API key to it. It was validated when it was stored, but a
    # row predating that check — or a policy tightened since — should not
    # become a live outbound destination (review S1).
    from core import urlguard
    base_url = (row.get("base_url") or "").rstrip("/")
    try:
        base_url = urlguard.validate(base_url)
    except urlguard.UnsafeURL as e:
        logger.error(f"Refusing stored provider endpoint for {user_id}/"
                     f"{provider}: {e}")
        return None

    kind = "anthropic" if provider == "anthropic" else "openai"
    return llm.ProviderConfig(
        name=provider,
        kind=kind,
        base_url=base_url,
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


def runs_page(user_id: str, limit: int = 50, offset: int = 0,
              status: str = "", search: str = "") -> tuple[list[dict], int]:
    """
    One page of a user's runs, newest first, with per-run step progress.

    Three queries regardless of how many runs the user owns. The previous
    shape was two queries *per run* plus a Python-side sort, so a researcher
    with 34 runs paid ~69 round-trips to open the list (review O2), and it
    returned all of them with no way to page or filter (review X6).

    Returns (rows, total_matching).
    """
    init_run_owners()
    where = ["o.user_id = ?"]
    params: list = [user_id]
    if status:
        where.append("r.status = ?")
        params.append(status)
    if search:
        where.append("LOWER(r.problem) LIKE ?")
        params.append(f"%{search.lower()}%")
    clause = " AND ".join(where)

    total_rows = db.query(
        f"SELECT COUNT(*) AS n FROM runs r "
        f"JOIN run_owners o ON o.run_id = r.run_id WHERE {clause}",
        tuple(params))
    total = int(total_rows[0]["n"]) if total_rows else 0

    runs = db.query(
        f"SELECT r.run_id, r.problem, r.status, r.created_at, r.completed_at "
        f"FROM runs r JOIN run_owners o ON o.run_id = r.run_id "
        f"WHERE {clause} ORDER BY r.created_at DESC "
        f"LIMIT {int(limit)} OFFSET {int(offset)}",
        tuple(params))
    if not runs:
        return [], total

    # Step progress for the whole page in one grouped read.
    run_ids = [r["run_id"] for r in runs]
    marks = ", ".join("?" for _ in run_ids)
    step_rows = db.query(
        f"SELECT run_id, step_name, status, ordinal FROM run_steps "
        f"WHERE run_id IN ({marks})", tuple(run_ids))
    by_run: dict[str, list[dict]] = {}
    for row in step_rows:
        by_run.setdefault(row["run_id"], []).append(row)

    from core.pipeline import summarise_steps
    for run in runs:
        run.update(summarise_steps(by_run.get(run["run_id"], [])))
    return runs, total


# ---------------------------------------------------------------------------
# Per-user academic source API keys (review U2)
# ---------------------------------------------------------------------------

def set_source_credentials(user_id: str, source_id: str, api_key: str) -> dict:
    """Store a user's API key for one academic source (Scopus, CORE, ...)."""
    init_users_tables()
    encrypted = crypto.encrypt(api_key) if api_key else ""
    existing = db.fetch("user_source_credentials",
                        {"user_id": user_id, "source_id": source_id})
    existing = existing[0] if existing else {}
    record = {
        "cred_id":     existing.get("cred_id") or generate_id("SCRD"),
        "user_id":     user_id,
        "source_id":   source_id,
        "api_key_enc": encrypted,
        "key_hint":    crypto.mask(api_key),
        "created_at":  existing.get("created_at") or _now(),
        "updated_at":  _now(),
    }
    db.insert("user_source_credentials", record)
    return public_source_credentials(record)


def get_source_credentials_row(user_id: str, source_id: str) -> Optional[dict]:
    init_users_tables()
    rows = db.fetch("user_source_credentials",
                    {"user_id": user_id, "source_id": source_id})
    return rows[0] if rows else None


def get_source_api_key(user_id: str, source_id: str) -> str:
    """Decrypt and return a user's key for a source, or '' if not stored."""
    row = get_source_credentials_row(user_id, source_id)
    if not row:
        return ""
    return crypto.decrypt(row.get("api_key_enc") or "")


def list_source_credentials(user_id: str) -> list[dict]:
    init_users_tables()
    return [public_source_credentials(r)
            for r in db.fetch("user_source_credentials", {"user_id": user_id})]


def public_source_credentials(row: dict) -> dict:
    return {
        "source_id":  row.get("source_id"),
        "key_hint":   row.get("key_hint"),
        "updated_at": row.get("updated_at"),
    }


def delete_source_credentials(user_id: str, source_id: str) -> bool:
    init_users_tables()
    return db.execute(
        "DELETE FROM user_source_credentials WHERE user_id = ? AND source_id = ?",
        (user_id, source_id),
    )


# ---------------------------------------------------------------------------
# Config templates (review U10)
# ---------------------------------------------------------------------------

def list_templates(user_id: str) -> list[dict]:
    init_users_tables()
    import json
    rows = db.fetch("user_templates", {"user_id": user_id})
    out = []
    for r in rows:
        try:
            cfg = json.loads(r.get("config_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            cfg = {}
        out.append({"name": r.get("name"), "config": cfg,
                     "created_at": r.get("created_at")})
    return out


def save_template(user_id: str, name: str, config: dict) -> None:
    init_users_tables()
    import json
    from core.utils import generate_id
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.insert("user_templates", {
        "template_id":  generate_id("TPL"),
        "user_id":      user_id,
        "name":         name,
        "config_json":  json.dumps(config),
        "created_at":   now,
    })


def delete_template(user_id: str, name: str) -> bool:
    init_users_tables()
    return db.execute(
        "DELETE FROM user_templates WHERE user_id = ? AND name = ?",
        (user_id, name),
    )


# ---------------------------------------------------------------------------
# User preferences (key-value) — MCP toggles, etc.
# ---------------------------------------------------------------------------

# MCP connection keys that can be toggled. Each maps to a human label and
# a description shown in the Settings UI. The pref_key stored in the DB is
# f"mcp.{conn_id}.enabled" with value "1" or "0". Default is enabled ("1")
# when no row exists — MCP connections are opt-out, not opt-in.
MCP_CONNECTIONS = [
    {
        "conn_id":   "consensus",
        "label":     "Consensus",
        "description": "AI-powered semantic search over 200M+ papers via MCP OAuth. "
                       "Used by Social and Grounder agents for discovery.",
    },
    {
        "conn_id":   "primo",
        "label":     "Primo (Ex Libris)",
        "description": "Library catalog search via the MCPO bridge. Used by the "
                       "Librarian agent to find catalog records and availability. "
                       "Requires PRIMO_API_KEY + PRIMO_VID in .env or Settings.",
    },
]


def get_preference(user_id: str, key: str, default: str = "") -> str:
    """Get a single user preference value."""
    init_users_tables()
    rows = db.fetch("user_preferences", {"user_id": user_id, "pref_key": key})
    if rows:
        return rows[0].get("pref_value") or default
    return default


def set_preference(user_id: str, key: str, value: str) -> None:
    """Set a single user preference value (upsert)."""
    init_users_tables()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.insert("user_preferences", {
        "user_id":    user_id,
        "pref_key":   key,
        "pref_value": value,
        "updated_at": now,
    })


def get_all_preferences(user_id: str) -> dict:
    """Get all preferences for a user as a {key: value} dict."""
    init_users_tables()
    rows = db.fetch("user_preferences", {"user_id": user_id})
    return {r.get("pref_key"): r.get("pref_value") or ""
            for r in rows}


def mcp_connection_enabled(user_id: str, conn_id: str) -> bool:
    """Check if an MCP connection is enabled for a user.

    Default is True (enabled) when no preference is set — MCP connections
    are opt-out, not opt-in. Set to "0" to disable.
    """
    val = get_preference(user_id, f"mcp.{conn_id}.enabled", "1")
    return val != "0"


def get_mcp_toggles(user_id: str) -> list[dict]:
    """Get all MCP connection toggles for a user, with current state."""
    prefs = get_all_preferences(user_id)
    out = []
    for conn in MCP_CONNECTIONS:
        key = f"mcp.{conn['conn_id']}.enabled"
        val = prefs.get(key, "1")
        out.append({
            "conn_id":     conn["conn_id"],
            "label":       conn["label"],
            "description": conn["description"],
            "enabled":     val != "0",
        })
    return out


def set_mcp_toggle(user_id: str, conn_id: str, enabled: bool) -> dict:
    """Enable or disable an MCP connection for a user."""
    init_users_tables()
    # Validate conn_id
    valid_ids = [c["conn_id"] for c in MCP_CONNECTIONS]
    if conn_id not in valid_ids:
        raise ValueError(f"Unknown MCP connection: {conn_id}")
    set_preference(user_id, f"mcp.{conn_id}.enabled", "1" if enabled else "0")
    return {"conn_id": conn_id, "enabled": enabled}
