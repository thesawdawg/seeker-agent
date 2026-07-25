"""
Secret Storage
--------------
Symmetric encryption for the provider API keys users hand us.

Those keys belong to the user, not to us: they unlock that person's
Open-WebUI account. They are stored encrypted at rest and never returned
by the API — endpoints report only a masked hint.

Requires SEEKER_SECRET_KEY. If it is missing we refuse to store a key
rather than fall back to plaintext, because a silent downgrade here is how
credential leaks happen.

Generate one with:
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import base64
import hashlib
import hmac
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

ENV_SECRET = "SEEKER_SECRET_KEY"


class SecretUnavailable(RuntimeError):
    """No usable SEEKER_SECRET_KEY — refuse to store or read secrets."""


def _secret() -> str:
    value = os.environ.get(ENV_SECRET, "").strip()
    if not value:
        raise SecretUnavailable(
            f"{ENV_SECRET} is not set. Generate one with:\n"
            f'  python3 -c "from cryptography.fernet import Fernet; '
            f'print(Fernet.generate_key().decode())"\n'
            f"Storing provider API keys without it is refused."
        )
    return value


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise SecretUnavailable(
            "The 'cryptography' package is required to store provider API keys. "
            "Install it with: pip install 'cryptography>=42'"
        ) from e

    raw = _secret().encode()
    # Accept either a real Fernet key or any passphrase, derived to 32 bytes.
    try:
        return Fernet(raw)
    except Exception:
        derived = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        return Fernet(derived)


def available() -> bool:
    """Whether secrets can be stored in this process."""
    try:
        _fernet()
        return True
    except SecretUnavailable:
        return False


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except SecretUnavailable:
        raise
    except Exception as e:
        # Usually SEEKER_SECRET_KEY changed since the value was written
        raise SecretUnavailable(
            f"Could not decrypt a stored secret ({e}). "
            f"Has {ENV_SECRET} changed since it was saved?"
        ) from e


def fingerprint(value: str) -> str:
    """
    Stable, non-reversible identifier for an API key.

    Used to recognise a returning user without storing their key in a
    lookup-able form. Keyed by the server secret, so fingerprints do not
    transfer between deployments.
    """
    if not value:
        return ""
    try:
        key = _secret().encode()
    except SecretUnavailable:
        # Identity still works without encryption configured; only storage
        # of the key itself is refused.
        key = b"seeker-unkeyed-fingerprint"
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def mask(value: str) -> str:
    """A hint the UI can show without revealing the key."""
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}…{value[-4:]}"
