"""
Password auth backend — username/password authentication.

This backend lets a user create an account with a username and password,
independent of any model provider. A password-authenticated user can then
add provider API keys via the credentials endpoints (PUT /api/credentials)
and use the pipeline just like a provider-key user.

A user may have multiple provider API keys associated with the same
account — the password identifies the user, and the provider credentials
are stored separately in the user_credentials table.

Endpoints:
    POST /api/auth/password/register  — create a new account
    POST /api/auth/password/login     — sign in with username + password

Password hashing uses PBKDF2-HMAC-SHA256 (stdlib, no extra dependency).
See core/crypto.hash_password / verify_password.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from core import users
from web.auth.backends.base import AuthBackend

logger = logging.getLogger(__name__)

# Minimum password length — enforced at registration.
MIN_PASSWORD_LENGTH = 8
# Minimum/maximum username length.
MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 64


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=MIN_USERNAME_LENGTH,
                          max_length=MAX_USERNAME_LENGTH,
                          description="Account username (3–64 chars)")
    password: str = Field(..., min_length=MIN_PASSWORD_LENGTH,
                          description=f"Password (min {MIN_PASSWORD_LENGTH} chars)")
    display_name: str = Field(default="",
                              description="Optional display name")


class PasswordLoginRequest(BaseModel):
    username: str
    password: str


class PasswordBackend(AuthBackend):
    """Username/password authentication."""

    name = "password"
    label = "Sign in with username"
    enabled = True

    def register_routes(self, app, manager) -> None:
        router = APIRouter()

        @router.post("/api/auth/password/register")
        def register(body: RegisterRequest, request: Request,
                     response: Response):
            """Create a new username/password account and start a session."""
            manager.check_login_rate(
                request.client.host if request.client else "unknown")

            try:
                user = users.create_password_user(
                    username=body.username,
                    password=body.password,
                    display_name=body.display_name,
                )
            except ValueError as e:
                raise HTTPException(status.HTTP_409_CONFLICT, str(e))
            except Exception as e:
                logger.error(f"Password registration failed: {e}")
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "Could not create account")

            session = manager.create_session(
                user_id=user["user_id"],
                provider="password",
                extra={"auth_backend": self.name},
            )
            response.set_cookie(
                "seeker_session", session["session"],
                max_age=session["expires_in"], httponly=True, samesite="lax",
            )
            return {
                **session,
                "display_name": user.get("display_name") or "",
                "username":     body.username,
            }

        @router.post("/api/auth/password/login")
        def password_login(body: PasswordLoginRequest, request: Request,
                           response: Response):
            """Sign in with username and password."""
            manager.check_login_rate(
                request.client.host if request.client else "unknown")

            user = users.verify_password_login(body.username, body.password)
            if not user:
                # Same message for unknown user and wrong password —
                # don't disclose which one it is.
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED,
                    "Invalid username or password")

            session = manager.create_session(
                user_id=user["user_id"],
                provider="password",
                extra={"auth_backend": self.name},
            )
            response.set_cookie(
                "seeker_session", session["session"],
                max_age=session["expires_in"], httponly=True, samesite="lax",
            )
            return {
                **session,
                "display_name": user.get("display_name") or "",
                "username":     body.username,
            }

        app.include_router(router)
