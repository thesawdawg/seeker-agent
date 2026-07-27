"""
Outbound URL validation.

`POST /api/auth/login` takes a base_url from an unauthenticated caller and
makes the server fetch it. These tests pin what that is now allowed to reach
(review S1), and — just as importantly — that the documented local-provider
defaults still work, since blocking loopback outright would break the
primary use case.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core import urlguard


@pytest.fixture(autouse=True)
def default_policy(monkeypatch):
    monkeypatch.delenv(urlguard.ALLOW_PRIVATE_ENV, raising=False)
    monkeypatch.delenv(urlguard.ALLOWLIST_ENV, raising=False)


# ---------------------------------------------------------------------------
# Always blocked, whatever the policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data",     # AWS / Azure / DO metadata
    "http://169.254.169.254",
    "http://metadata.google.internal/computeMetadata/v1",
    "http://[::ffff:169.254.169.254]/latest",      # v4-mapped v6 bypass
])
def test_metadata_endpoints_are_always_blocked(url):
    with pytest.raises(urlguard.UnsafeURL):
        urlguard.validate(url)


def test_metadata_stays_blocked_even_when_private_is_allowed(monkeypatch):
    monkeypatch.setenv(urlguard.ALLOW_PRIVATE_ENV, "1")
    with pytest.raises(urlguard.UnsafeURL):
        urlguard.validate("http://169.254.169.254/latest/meta-data")


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://127.0.0.1:11211/",
    "ftp://example.com/",
    "//example.com/api",
    "not a url",
    "",
])
def test_non_http_schemes_are_rejected(url):
    with pytest.raises(urlguard.UnsafeURL):
        urlguard.validate(url)


def test_credentials_in_the_url_are_rejected():
    with pytest.raises(urlguard.UnsafeURL):
        urlguard.validate("http://user:pass@example.com/api")


# ---------------------------------------------------------------------------
# The documented local defaults must keep working
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://localhost:3000/api",        # Open-WebUI, the README default
    "http://127.0.0.1:11434/v1",        # Ollama
    "http://192.168.1.50:1234/v1",      # LM Studio on the LAN
])
def test_local_providers_are_allowed_by_default(url):
    assert urlguard.validate(url) == url.rstrip("/")


def test_trailing_slash_is_normalised():
    assert urlguard.validate("http://localhost:3000/api/") == \
        "http://localhost:3000/api"


# ---------------------------------------------------------------------------
# Locked-down mode, for a shared deployment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://localhost:3000/api",
    "http://127.0.0.1:8000/v1",
    "http://10.0.0.5/v1",
    "http://192.168.1.50:1234/v1",
    "http://172.16.4.4/v1",
])
def test_private_ranges_are_rejected_when_locked_down(monkeypatch, url):
    monkeypatch.setenv(urlguard.ALLOW_PRIVATE_ENV, "0")
    with pytest.raises(urlguard.UnsafeURL):
        urlguard.validate(url)


def test_public_hosts_still_pass_when_locked_down(monkeypatch):
    monkeypatch.setenv(urlguard.ALLOW_PRIVATE_ENV, "0")
    assert urlguard.validate("https://api.openai.com/v1") == \
        "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

def test_allowlist_permits_only_matching_hosts(monkeypatch):
    monkeypatch.setenv(urlguard.ALLOWLIST_ENV, "api.openai.com,*.corp.example")
    assert urlguard.validate("https://api.openai.com/v1")
    assert urlguard.validate("https://llm.corp.example/v1")
    with pytest.raises(urlguard.UnsafeURL):
        urlguard.validate("https://api.anthropic.com/v1")


def test_empty_allowlist_permits_anything_public(monkeypatch):
    monkeypatch.setenv(urlguard.ALLOWLIST_ENV, "")
    assert urlguard.validate("https://api.openai.com/v1")


def test_is_safe_does_not_raise():
    assert urlguard.is_safe("http://localhost:3000/api") is True
    assert urlguard.is_safe("http://169.254.169.254/") is False
