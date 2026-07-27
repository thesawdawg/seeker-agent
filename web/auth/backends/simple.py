"""
Simple auth backend — provider API key authentication.

This is the original Seeker auth method: the caller presents the API key
for their model provider (Open-WebUI, OpenAI, Ollama, etc.). The key is
validated by calling the provider's /models endpoint; a valid key
identifies the account by fingerprint, and a session token is issued so
later requests do not re-send the key.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from core import crypto, users
from web.auth.backends.base import AuthBackend

logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    base_url: str = Field(..., description="Provider base URL, e.g. http://localhost:3000/api")
    api_key: str
    provider: str = "open-webui"
    display_name: str = ""
    models: dict = Field(default_factory=dict)


class SimpleBackend(AuthBackend):
    """Provider-key authentication — the default and original backend."""

    name = "simple"
    label = "Sign in with API key"
    enabled = True

    def register_routes(self, app, manager) -> None:
        router = APIRouter()

        @router.post("/api/auth/login")
        def login(body: LoginRequest, request: Request, response: Response):
            manager.check_login_rate(
                request.client.host if request.client else "unknown")
            result = self._login(body, manager)
            response.set_cookie(
                "seeker_session", result["session"],
                max_age=result["expires_in"], httponly=True, samesite="lax",
            )
            return result

        app.include_router(router)

    def _login(self, body: LoginRequest, manager) -> dict:
        """Validate a provider key, identify the user, store credentials,
        and issue a session token via the manager."""
        kind = "anthropic" if body.provider == "anthropic" else "openai"
        available_models = validate_provider_key(body.base_url, body.api_key, kind)

        user = users.get_or_create(
            auth_ref=crypto.fingerprint(body.api_key),
            display_name=body.display_name,
            auth_kind="provider_key",
        )

        try:
            users.set_credentials(user["user_id"], body.provider,
                                  body.base_url, body.api_key, body.models)
        except crypto.SecretUnavailable as e:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))

        session = manager.create_session(
            user_id=user["user_id"],
            provider=body.provider,
            extra={"auth_backend": self.name},
        )

        return {
            **session,
            "display_name": user.get("display_name") or "",
            "models": available_models,
        }


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

    from core import urlguard

    # This runs before authentication, so an unguarded fetch here is an
    # anonymous SSRF primitive (review S1).
    try:
        base_url = urlguard.validate(base_url)
    except urlguard.UnsafeURL as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    headers = ({"x-api-key": api_key, "anthropic-version": "2023-06-01"}
               if kind == "anthropic"
               else {"Authorization": f"Bearer {api_key}"})

    try:
        resp = requests.get(f"{base_url}/models", headers=headers, timeout=15)
    except Exception as e:
        # The reason is deliberately not echoed: "connection refused" versus
        # "timed out" is exactly the oracle that turns this endpoint into a
        # port scanner. It goes to the log, where the operator can see it.
        logger.info(f"Provider probe failed for {base_url}: {e}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Could not reach a provider at {base_url}")

    if resp.status_code in (401, 403):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "The provider rejected that API key")
    if resp.status_code >= 400:
        logger.info(f"Provider probe for {base_url} returned "
                    f"HTTP {resp.status_code}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Could not reach a provider at {base_url}")

    try:
        payload = resp.json()
    except Exception:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Could not reach a provider at {base_url}")

    items = payload.get("data") if isinstance(payload, dict) else payload
    models = sorted({m.get("id") or m.get("name")
                     for m in (items or []) if isinstance(m, dict)} - {None})
    return models
