"""
Authentication
--------------
One seam: `resolve_user`. Everything downstream sees only a user_id.

Today a caller authenticates with the API key for their model provider
(Open-WebUI by default). The key is validated by calling the provider's
/models endpoint; a valid key identifies the account by fingerprint, and a
session token is issued so later requests do not re-send the key.

To move to SAML, replace `resolve_user` with one that maps an assertion to
users.get_or_create(auth_ref=<saml subject>, auth_kind="saml"). No route,
model, or worker changes.

Sessions live in Redis when REDIS_URL is set, and in process memory
otherwise, so a single-process deployment needs no Redis.
"""

import json
import logging
import os
import secrets
import time
from typing import Optional

from fastapi import Header, HTTPException, Request, status

from core import crypto, users

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = int(os.environ.get("SEEKER_SESSION_TTL", "86400"))
SESSION_HEADER = "x-seeker-session"


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------

class _MemorySessions:
    """Fallback store. Fine for one process; Redis is required beyond that."""

    def __init__(self):
        self._data: dict[str, tuple[float, dict]] = {}

    def set(self, token: str, payload: dict, ttl: int) -> None:
        self._data[token] = (time.time() + ttl, payload)

    def get(self, token: str) -> Optional[dict]:
        entry = self._data.get(token)
        if not entry:
            return None
        expires, payload = entry
        if time.time() > expires:
            self._data.pop(token, None)
            return None
        return payload

    def delete(self, token: str) -> None:
        self._data.pop(token, None)


class _RedisSessions:
    def __init__(self, url: str):
        import redis
        self._redis = redis.Redis.from_url(url, decode_responses=True)

    def set(self, token: str, payload: dict, ttl: int) -> None:
        self._redis.setex(f"seeker:session:{token}", ttl, json.dumps(payload))

    def get(self, token: str) -> Optional[dict]:
        raw = self._redis.get(f"seeker:session:{token}")
        return json.loads(raw) if raw else None

    def delete(self, token: str) -> None:
        self._redis.delete(f"seeker:session:{token}")


_sessions = None


def sessions():
    global _sessions
    if _sessions is None:
        url = os.environ.get("REDIS_URL", "").strip()
        if url:
            try:
                _sessions = _RedisSessions(url)
                logger.info(f"Sessions in Redis: {url}")
            except Exception as e:
                logger.error(f"Redis unavailable ({e}) — using in-memory sessions")
                _sessions = _MemorySessions()
        else:
            logger.info("REDIS_URL unset — using in-memory sessions")
            _sessions = _MemorySessions()
    return _sessions


def reset_sessions():
    """Test hook."""
    global _sessions
    _sessions = None


# ---------------------------------------------------------------------------
# Provider key validation
# ---------------------------------------------------------------------------

def validate_provider_key(base_url: str, api_key: str,
                          kind: str = "openai") -> list[str]:
    """
    Confirm a key works by listing models, and return what it can reach.

    Raises HTTPException(401) on rejection and 502 when the provider is
    unreachable — those are different problems and the UI should say so.
    """
    import requests

    base_url = (base_url or "").rstrip("/")
    if not base_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "A provider base_url is required")

    headers = ({"x-api-key": api_key, "anthropic-version": "2023-06-01"}
               if kind == "anthropic"
               else {"Authorization": f"Bearer {api_key}"})

    try:
        resp = requests.get(f"{base_url}/models", headers=headers, timeout=15)
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Could not reach the provider at {base_url}: {e}")

    if resp.status_code in (401, 403):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "The provider rejected that API key")
    if resp.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Provider returned HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except Exception:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "Provider did not return JSON")

    items = payload.get("data") if isinstance(payload, dict) else payload
    models = sorted({m.get("id") or m.get("name")
                     for m in (items or []) if isinstance(m, dict)} - {None})
    return models


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login(base_url: str, api_key: str, provider: str = "open-webui",
          display_name: str = "", models: dict = None) -> dict:
    """
    Validate a provider key, identify the user, store their credentials, and
    issue a session token.
    """
    kind = "anthropic" if provider == "anthropic" else "openai"
    available_models = validate_provider_key(base_url, api_key, kind)

    user = users.get_or_create(
        auth_ref=crypto.fingerprint(api_key),
        display_name=display_name,
        auth_kind="provider_key",
    )

    try:
        users.set_credentials(user["user_id"], provider, base_url, api_key,
                              models or {})
    except crypto.SecretUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))

    token = secrets.token_urlsafe(32)
    sessions().set(token, {"user_id": user["user_id"], "provider": provider},
                   SESSION_TTL_SECONDS)

    return {
        "session":      token,
        "user_id":      user["user_id"],
        "display_name": user.get("display_name") or "",
        "provider":     provider,
        "models":       available_models,
        "expires_in":   SESSION_TTL_SECONDS,
    }


def logout(token: str) -> None:
    if token:
        sessions().delete(token)


# ---------------------------------------------------------------------------
# The dependency every protected route uses
# ---------------------------------------------------------------------------

def resolve_user(request: Request,
                 x_seeker_session: str = Header(default="")) -> dict:
    """
    Identify the caller, or reject.

    THE auth seam. A SAML integration replaces the body of this function and
    leaves every route untouched.
    """
    token = x_seeker_session or request.cookies.get("seeker_session", "")
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not signed in. POST /api/auth/login with your provider base_url "
            "and API key.",
        )

    payload = sessions().get(token)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Session expired — sign in again")

    user = users.get_user(payload.get("user_id", ""))
    if not user:
        sessions().delete(token)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown user")
    return user


def require_run_access(user: dict, run_id: str) -> None:
    """
    Confirm this user may see this run.

    Unowned runs (created by the CLI) are invisible to the web API rather
    than public — the safer default when ownership is unknown.
    """
    if not users.owns_run(user["user_id"], run_id):
        # 404, not 403: existence of another user's run is not disclosed
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
