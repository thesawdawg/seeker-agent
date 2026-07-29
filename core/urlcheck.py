"""
URL Validation for Display
--------------------------
Pre-checks URLs before they appear in break review reports so the researcher
is never presented with a link that is malformed or known-dead.

Two layers:

  is_well_formed(url)
      Structural check — scheme is http/https, host has a dot, no LLM
      placeholder strings ("url if known", "n/a", template braces).
      Fast, no network. Catches the common case where an LLM hallucinated
      a URL or emitted the prompt template literal.

  is_reachable(url, timeout)
      HEAD request with a GET fallback when HEAD is refused. Returns True only
      after a 2xx response. Malformed, dead, forbidden, unavailable, and
      unverified URLs are excluded from display.

The break report layer (core.breaks) validates historical links again before
rendering so legacy rows written by older, permissive checks cannot leak dead
or fabricated links into Break 1.
"""

import logging
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# Strings that LLMs sometimes emit instead of a real URL — either the
# prompt template literal leaked through, or the model wrote a placeholder.
_PLACEHOLDER_FRAGMENTS = (
    "url if known",
    "url if available",
    "n/a",
    "none",
    "null",
    "todo",
    "tbd",
    "placeholder",
    "your-url",
    "your_url",
    "example.com",
    "example.org",
    "doi.org/doi",
)


def is_well_formed(url: str) -> bool:
    """
    True if `url` is a structurally valid http(s) URL with a real-looking host.

    No network access — this is the cheap pre-filter that catches LLM
    hallucinations and prompt-template leakage before we even consider
    a HEAD request.
    """
    if not url or not url.strip():
        return False
    u = url.strip()
    lower = u.lower()

    # Catch template braces, angle brackets, and bare placeholders
    if "<" in u or "{" in u or lower in ("none", "null", "n/a", "todo", "tbd"):
        return False
    for frag in _PLACEHOLDER_FRAGMENTS:
        if frag in lower:
            return False

    try:
        parsed = urlparse(u)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # localhost is valid for local dev links; everything else needs a dot
    if host == "localhost":
        return True
    if "." not in host:
        return False
    return True


def check_url_status(url: str, timeout: float = 5.0) -> str:
    if not is_well_formed(url):
        return "invalid"
    headers = {"User-Agent": "PipelineResearchBot/1.0"}
    try:
        response = requests.head(
            url, timeout=timeout, allow_redirects=True, headers=headers,
        )
        if response.status_code in (403, 405):
            response = requests.get(
                url, timeout=timeout, allow_redirects=True,
                headers=headers, stream=True,
            )
        if 200 <= response.status_code < 300:
            return "redirected" if str(response.url or url) != url else "active"
        if response.status_code in (404, 410):
            return "dead"
        return "unreachable"
    except Exception as e:
        logger.debug(f"URL reachability check could not reach {url}: {e}")
        return "unreachable"


def is_reachable(url: str, timeout: float = 5.0) -> bool:
    """Return True only when an HTTP request confirms a live destination."""
    return check_url_status(url, timeout) in ("active", "redirected")


def validate_url(url: str, *, check_reachable: bool = False,
                 timeout: float = 5.0) -> bool:
    """
    Validate a URL for display.

    By default only checks format (is_well_formed). Set check_reachable=True
    to also issue a HEAD request.
    """
    if not is_well_formed(url):
        return False
    if check_reachable:
        return is_reachable(url, timeout=timeout)
    return True
