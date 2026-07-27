"""
Auth backend base class.

Each backend handles its own login flow (which may be a form POST, a SAML
redirect, an OAuth callback, etc.) and calls ``manager.create_session()``
once it has authenticated a user. Session resolution, throttling, and
access control are backend-agnostic and live in the AuthManager.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from web.auth.manager import AuthManager


class AuthBackend(ABC):
    """Pluggable authentication backend.

    Subclasses implement ``register_routes`` to mount their login/callback
    endpoints on the FastAPI app. Inside those routes they call
    ``manager.create_session(user_id, ...)`` to issue a session token after
    successful authentication.
    """

    #: Short identifier used in /api/auth/methods and config.
    name: str = ""

    #: Human-readable label for the login UI.
    label: str = ""

    #: Whether this backend is ready to accept logins. A wireframed backend
    #: (e.g. SAML without configured IdP) returns False so the UI hides it.
    enabled: bool = True

    @abstractmethod
    def register_routes(self, app, manager: "AuthManager") -> None:
        """Register this backend's auth routes on the FastAPI app.

        Each backend owns its own URL paths under /api/auth/<name>/...
        The simple backend mounts POST /api/auth/login; the SAML backend
        mounts GET /api/auth/saml/login, POST /api/auth/saml/callback, etc.
        """
        ...

    def describe(self) -> dict:
        """Return a descriptor for /api/auth/methods."""
        return {"name": self.name, "label": self.label, "enabled": self.enabled}
