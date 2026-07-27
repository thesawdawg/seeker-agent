"""
Login throttling — shared across all auth backends.

Unauthenticated login endpoints are a credential-stuffing / scanning
surface. urlguard bounds *where* a probe can point; this bounds how fast
it can be asked (review S7).
"""

import os
import time
from typing import Optional

from fastapi import HTTPException, status

LOGIN_MAX_ATTEMPTS = int(os.environ.get("SEEKER_LOGIN_MAX_ATTEMPTS", "10"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("SEEKER_LOGIN_WINDOW_SECONDS", "60"))

_login_attempts: dict[str, list[float]] = {}


def check_login_rate(client_ip: str) -> None:
    """Raise 429 when one address has tried too often in the window."""
    if LOGIN_MAX_ATTEMPTS <= 0:
        return
    now = time.time()
    cutoff = now - LOGIN_WINDOW_SECONDS
    recent = [t for t in _login_attempts.get(client_ip, []) if t > cutoff]
    if len(recent) >= LOGIN_MAX_ATTEMPTS:
        _login_attempts[client_ip] = recent
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many sign-in attempts. Try again in "
            f"{LOGIN_WINDOW_SECONDS} seconds.")
    recent.append(now)
    _login_attempts[client_ip] = recent
    # Keep the table from growing without bound on a busy or hostile host.
    if len(_login_attempts) > 4096:
        for ip in [k for k, v in _login_attempts.items()
                   if not any(t > cutoff for t in v)]:
            _login_attempts.pop(ip, None)


def reset_login_rate() -> None:
    """Test hook."""
    _login_attempts.clear()
