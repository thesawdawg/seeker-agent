"""
Auth manager — orchestrates multiple auth backends.

The manager owns:
  - Session storage (Redis or in-memory) — backend-agnostic
  - Login throttling — shared across all backends
  - User resolution (resolve_user) — session-based, backend-agnostic
  - Access control (require_run_access, require_admin)

Each backend handles its own login flow and calls ``create_session()``
once it has authenticated a user. This separation means a SAML backend
does not need to know about provider keys, and the simple backend does
not need to know about SAML assertions.
"""

import logging
import secrets
from typing import Optional

from fastapi import Header, HTTPException, Request, status

from core import users
from web.auth.sessions import (
    SESSION_HEADER,
    SESSION_TTL_SECONDS,
    sessions,
)
from web.auth.throttle import check_login_rate

logger = logging.getLogger(__name__)


class AuthManager:
    """Pluggable auth manager supporting multiple backends."""

    def __init__(self):
        self._backends: dict[str, object] = {}
        self._backend_list: list[object] = []

    # -- Backend registration ----------------------------------------------

    def add_backend(self, backend) -> None:
        """Register an auth backend."""
        self._backends[backend.name] = backend
        self._backend_list.append(backend)
        logger.info(f"[Auth] Registered backend: {backend.name}")

    def get_backend(self, name: str):
        return self._backends.get(name)

    @property
    def backends(self) -> list:
        return self._backend_list

    def describe_methods(self) -> list[dict]:
        """Return descriptors for all registered backends (for /api/auth/methods)."""
        return [b.describe() for b in self._backend_list]

    # -- Route registration ------------------------------------------------

    def register_routes(self, app) -> None:
        """Mount each backend's routes on the FastAPI app."""
        for backend in self._backend_list:
            backend.register_routes(app, self)

    # -- Session management (called by backends after auth) ----------------

    def create_session(self, user_id: str, provider: str,
                       extra: dict = None) -> dict:
        """Issue a session token after a backend authenticates a user.

        Backends call this after validating credentials. Returns the session
        token and expiry for the backend to return to the client.
        """
        token = secrets.token_urlsafe(32)
        payload = {"user_id": user_id, "provider": provider}
        if extra:
            payload.update(extra)
        sessions().set(token, payload, SESSION_TTL_SECONDS)
        return {
            "session":    token,
            "user_id":    user_id,
            "expires_in": SESSION_TTL_SECONDS,
        }

    def logout(self, token: str) -> None:
        if token:
            sessions().delete(token)

    # -- Throttling (delegated to throttle module) -------------------------

    @staticmethod
    def check_login_rate(client_ip: str) -> None:
        check_login_rate(client_ip)

    # -- User resolution (the FastAPI dependency) --------------------------

    def resolve_user(self, request: Request,
                     x_seeker_session: str = Header(default="")) -> dict:
        """
        Identify the caller from their session, or reject.

        This is backend-agnostic — it only checks the session store.
        Any backend that called create_session() produced a valid session.
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

    # -- Access control ----------------------------------------------------

    @staticmethod
    def require_run_access(user: dict, run_id: str) -> None:
        """
        Confirm this user may see this run.

        Unowned runs (created by the CLI) are invisible to the web API rather
        than public — the safer default when ownership is unknown.
        """
        if not users.owns_run(user["user_id"], run_id):
            # 404, not 403: existence of another user's run is not disclosed
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")

    @staticmethod
    def require_admin(user: dict) -> None:
        """
        Confirm this user is an operator (F12).

        Admin-gated routes (config editing) return 403, not 404 — the config
        endpoint's existence is not secret, but only operators may use it.
        """
        if not users.is_admin(user["user_id"]):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Admin access required")


# ---------------------------------------------------------------------------
# Singleton — the default manager used by the app
# ---------------------------------------------------------------------------

_manager: Optional[AuthManager] = None


def get_manager() -> AuthManager:
    """Get or create the singleton AuthManager with default backends."""
    global _manager
    if _manager is None:
        _manager = AuthManager()
        # Always register the simple backend (provider-key auth)
        from web.auth.backends.simple import SimpleBackend
        _manager.add_backend(SimpleBackend())

        # Always register the password backend (username/password auth)
        from web.auth.backends.password import PasswordBackend
        _manager.add_backend(PasswordBackend())

        # Always register the SAML backend so its routes exist (they return
        # 503 when unconfigured). The backend's `enabled` property controls
        # whether it shows up in /api/auth/methods — it returns False when
        # the SAML env vars are not set.
        from web.auth.backends.saml import SAMLBackend
        _manager.add_backend(SAMLBackend())

    return _manager


def reset_manager() -> None:
    """Test hook — clears the singleton so the next get_manager() rebuilds it."""
    global _manager
    _manager = None
