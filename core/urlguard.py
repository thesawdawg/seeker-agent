"""
Outbound URL Guard
------------------
The provider base_url is supplied by the caller and then fetched by the
server — at login, when credentials are stored, and on every model call for
the life of a run. Unguarded, that is a server-side request forgery seam
reachable before authentication: point it at an internal service and the
error responses distinguish "refused" from "reachable" from "reachable but
not JSON", and a 200 that happens to contain `id` or `name` keys is returned
to the caller as a model list (review S1).

Guarding it is awkward because SEEKER's *documented default* is a local
provider — `http://localhost:3000/api` for Open-WebUI, `http://localhost:11434`
for Ollama. Blocking private ranges outright would break the primary use
case. So the policy is layered:

  always            reject non-HTTP schemes, credentials in the URL,
                    link-local/metadata, multicast and reserved ranges
  default           allow loopback and private ranges (local providers work)
  ALLOW_PRIVATE=0   reject them too — set this on any shared deployment
  ALLOWLIST set     reject anything whose host does not match

Residual risk worth naming: the host is resolved and checked here, and
resolved again by the HTTP client when the request goes out. A DNS entry
that changes between the two (rebinding) is not caught. Closing that needs
connection-level pinning; the allowlist is the mitigation until then.
"""

import ipaddress
import logging
import os
import socket
from fnmatch import fnmatch
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ALLOW_PRIVATE_ENV = "SEEKER_PROVIDER_ALLOW_PRIVATE"
ALLOWLIST_ENV     = "SEEKER_PROVIDER_ALLOWLIST"

# Never reachable as a model endpoint, and the highest-value SSRF targets:
# cloud instance metadata lives at 169.254.169.254 on every major provider.
_ALWAYS_BLOCKED = (
    ipaddress.ip_network("169.254.0.0/16"),      # link-local / metadata
    ipaddress.ip_network("fe80::/10"),           # link-local v6
    ipaddress.ip_network("224.0.0.0/4"),         # multicast
    ipaddress.ip_network("ff00::/8"),            # multicast v6
    ipaddress.ip_network("0.0.0.0/8"),           # "this network"
    ipaddress.ip_network("100.64.0.0/10"),       # carrier-grade NAT
    ipaddress.ip_network("192.0.0.0/24"),        # IETF protocol assignments
    ipaddress.ip_network("240.0.0.0/4"),         # reserved
)

_METADATA_HOSTS = {
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
}


class UnsafeURL(ValueError):
    """The URL is not an acceptable outbound destination."""


def _allow_private() -> bool:
    return os.environ.get(ALLOW_PRIVATE_ENV, "1").strip().lower() not in (
        "0", "false", "no", "off")


def _allowlist() -> list[str]:
    raw = os.environ.get(ALLOWLIST_ENV, "")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _resolve(host: str) -> list[ipaddress._BaseAddress]:
    """Every address the host resolves to. A literal IP resolves to itself."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeURL(f"could not resolve host {host!r}: {e}") from e
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not out:
        raise UnsafeURL(f"host {host!r} resolved to no usable address")
    return out


def _check_address(addr, host: str) -> None:
    # IPv4-mapped IPv6 (::ffff:169.254.169.254) would otherwise sidestep the
    # v4 checks below.
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped

    for net in _ALWAYS_BLOCKED:
        if addr.version == net.version and addr in net:
            raise UnsafeURL(
                f"{host} resolves to {addr}, which is a reserved or "
                f"link-local address and is never a valid provider endpoint")

    if _allow_private():
        return
    if addr.is_loopback or addr.is_private or addr.is_reserved:
        raise UnsafeURL(
            f"{host} resolves to the private address {addr}. This deployment "
            f"has {ALLOW_PRIVATE_ENV}=0, so only publicly routable provider "
            f"endpoints are accepted.")


def validate(url: str, *, what: str = "provider base_url") -> str:
    """
    Check that `url` is an acceptable outbound destination.

    Returns the normalised URL (trailing slash stripped), or raises UnsafeURL
    with a message safe to show the user — it describes their input, never
    what was found at the other end.
    """
    if not url or not url.strip():
        raise UnsafeURL(f"A {what} is required")

    url = url.strip().rstrip("/")
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL(
            f"{what} must start with http:// or https:// "
            f"(got {parsed.scheme or 'no scheme'!r})")
    if parsed.username or parsed.password:
        raise UnsafeURL(f"{what} must not embed credentials")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeURL(f"{what} has no host")
    if host in _METADATA_HOSTS:
        raise UnsafeURL(f"{host} is an instance-metadata endpoint, "
                        f"not a model provider")

    allowlist = _allowlist()
    if allowlist:
        if not any(fnmatch(host, pattern) for pattern in allowlist):
            raise UnsafeURL(
                f"{host} is not in this deployment's provider allowlist")
        # An operator naming a host has vouched for it, and the allowlist is
        # the strongest control available here. Skip the range checks rather
        # than second-guessing them — and skip resolution too, so a DNS blip
        # cannot lock everyone out of an endpoint that is explicitly trusted.
        return url

    for addr in _resolve(host):
        _check_address(addr, host)

    return url


def is_safe(url: str) -> bool:
    """Non-raising form, for call sites that only need to skip."""
    try:
        validate(url)
        return True
    except UnsafeURL:
        return False
