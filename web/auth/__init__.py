"""
Authentication package — pluggable multi-backend auth.

Public API (backward-compatible with the old web/auth.py module):

    from web import auth

    auth.resolve_user        — FastAPI dependency for protected routes
    auth.require_run_access  — run ownership check
    auth.require_admin       — admin check
    auth.login               — provider-key login (simple backend)
    auth.logout              — session logout
    auth.check_login_rate    — login throttling
    auth.validate_provider_key — provider key validation
    auth.sessions            — session store accessor
    auth.reset_sessions      — test hook
    auth.reset_login_rate    — test hook
    auth.SESSION_HEADER      — session header name
    auth.SESSION_TTL_SECONDS — session TTL

New API for multi-backend support:

    auth.get_manager()       — the AuthManager singleton
    auth.reset_manager()     — test hook
    auth.AuthManager         — the manager class
"""

import logging

from fastapi import Header, Request

from web.auth.manager import AuthManager, get_manager, reset_manager
from web.auth.sessions import (
    SESSION_HEADER,
    SESSION_TTL_SECONDS,
    reset_sessions,
    sessions,
)
from web.auth.throttle import check_login_rate, reset_login_rate

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
__all__ = [
    # Manager
    "AuthManager",
    "get_manager",
    "reset_manager",
    # Sessions
    "SESSION_HEADER",
    "SESSION_TTL_SECONDS",
    "sessions",
    "reset_sessions",
    # Throttle
    "check_login_rate",
    "reset_login_rate",
    # Convenience re-exports (delegate to the manager singleton)
    "resolve_user",
    "require_run_access",
    "require_admin",
    "logout",
    "login",
    "validate_provider_key",
]


# ---------------------------------------------------------------------------
# Delegate functions — these call the manager singleton so that
# `from web import auth; auth.resolve_user` keeps working in every route
# that already uses `Depends(auth.resolve_user)`.
# ---------------------------------------------------------------------------

def resolve_user(request: Request,
                 x_seeker_session: str = Header(default="")) -> dict:
    """FastAPI dependency — identify the caller or reject."""
    return get_manager().resolve_user(request, x_seeker_session)


def require_run_access(user: dict, run_id: str) -> None:
    return get_manager().require_run_access(user, run_id)


def require_admin(user: dict) -> None:
    return get_manager().require_admin(user)


def logout(token: str) -> None:
    get_manager().logout(token)


def login(base_url: str, api_key: str, provider: str = "open-webui",
          display_name: str = "", models: dict = None) -> dict:
    """
    Provider-key login — delegates to the SimpleBackend.

    Kept for backward compatibility with code that calls auth.login()
    directly. The canonical path is POST /api/auth/login, which the
    SimpleBackend's route handler owns.
    """
    from web.auth.backends.simple import LoginRequest, SimpleBackend
    backend = SimpleBackend()
    req = LoginRequest(
        base_url=base_url, api_key=api_key, provider=provider,
        display_name=display_name, models=models or {},
    )
    return backend._login(req, get_manager())


def validate_provider_key(base_url: str, api_key: str,
                          kind: str = "openai") -> list[str]:
    """Provider key validation — delegates to the simple backend."""
    from web.auth.backends.simple import validate_provider_key as _vpk
    return _vpk(base_url, api_key, kind)
