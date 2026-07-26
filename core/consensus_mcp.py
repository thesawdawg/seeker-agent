"""
Consensus MCP Client
--------------------
Calls the Consensus MCP server at https://mcp.consensus.app/mcp using the
OAuth 2.1 Authorization Code + PKCE flow via the official MCP Python SDK.

Consensus redirects every OAuth endpoint without a trailing slash to the
same URL with a trailing slash (/oauth/register, /oauth/token, etc.).
The MCP SDK does not follow these redirects in its single-yield flow.

Fix: subclass OAuthClientProvider and override _handle_oauth_metadata_response
(async version) to patch trailing slashes into all discovered endpoints
immediately after metadata discovery. We also perform client registration
ourselves (with follow_redirects=True) before the SDK sees it.

First run:  registers, then opens a browser to log in. Tokens saved to
            db/consensus_tokens.json and reused automatically.

Usage (standalone test):
    python3 core/consensus_mcp.py "effects of sleep on memory consolidation"

Usage (from pipeline):
    from core.consensus_mcp import search_consensus
    results = search_consensus("smallholder farming sustainability", limit=10)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
_HERE         = Path(__file__).parent.parent
TOKEN_FILE    = _HERE / "db" / "consensus_tokens.json"
MCP_URL       = "https://mcp.consensus.app/mcp"
REDIRECT_HOST = "localhost"
REDIRECT_PORT = 9753
REDIRECT_URI  = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"

# Trailing slash required on all Consensus OAuth endpoints
CONSENSUS_REGISTER_URL = "https://consensus.app/oauth/register/"


# ─── Serialization helper ─────────────────────────────────────────────────────

def _to_json_safe(model) -> dict:
    """Pydantic v2: use mode='json' to convert AnyUrl → plain strings."""
    return model.model_dump(mode="json", exclude_none=True)


def _ensure_trailing_slash(url: str | None) -> str | None:
    """Add trailing slash if URL is set and doesn't already have one."""
    if url and not url.endswith("/"):
        return url + "/"
    return url


# ─── Patched OAuth provider ───────────────────────────────────────────────────

class ConsensusOAuthProvider(OAuthClientProvider):
    """
    Fixes Consensus's trailing-slash redirects on OAuth endpoints.

    Consensus redirects /oauth/token → /oauth/token/ (308).
    The SDK builds the token URL via _get_token_endpoint() which reads
    directly from context.oauth_metadata.token_endpoint — a sync method
    called right before the token exchange request is built.

    We override _get_token_endpoint() to ensure a trailing slash.
    This is the correct interception point — version-stable and targeted.
    """

    def _get_token_endpoint(self) -> str:
        url = super()._get_token_endpoint()
        if url and not url.endswith("/"):
            url = url + "/"
            logger.debug("[Consensus] token_endpoint trailing slash added: %s", url)
        return url


# ─── Token storage ────────────────────────────────────────────────────────────

class FileTokenStorage(TokenStorage):
    """Persists OAuth tokens and client info to db/consensus_tokens.json."""

    def __init__(self, path: Path = TOKEN_FILE):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except Exception:
                self._data = {}

    def _save(self):
        self._path.write_text(json.dumps(self._data, indent=2))

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._data.get("tokens")
        return OAuthToken(**raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._data["tokens"] = _to_json_safe(tokens)
        self._save()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._data.get("client_info")
        return OAuthClientInformationFull(**raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._data["client_info"] = _to_json_safe(client_info)
        self._save()


# ─── Pre-registration ─────────────────────────────────────────────────────────

async def _ensure_registered(storage: FileTokenStorage) -> None:
    """
    Register with Consensus ourselves (follow_redirects=True) so we get
    a real server-issued client_id. Stored in token file so the SDK skips
    its own registration step.
    """
    existing = await storage.get_client_info()
    if existing and existing.client_id:
        logger.debug("[Consensus] Already registered, client_id=%s", existing.client_id)
        return

    logger.info("[Consensus] Registering with Consensus OAuth server...")

    payload = {
        "redirect_uris":              [REDIRECT_URI],
        "client_name":                "SEEKER Research Pipeline",
        "grant_types":                ["authorization_code", "refresh_token"],
        "response_types":             ["code"],
        "token_endpoint_auth_method": "none",
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        resp = await client.post(
            CONSENSUS_REGISTER_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
        )

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"[Consensus] Registration failed: {resp.status_code} {resp.text[:300]}"
        )

    data = resp.json()
    client_id = data.get("client_id")
    if not client_id:
        raise RuntimeError(
            f"[Consensus] Registration response missing client_id: {data}"
        )

    client_info = OAuthClientInformationFull(
        client_id=client_id,
        client_secret=data.get("client_secret"),
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        client_name="SEEKER Research Pipeline",
    )
    await storage.set_client_info(client_info)
    logger.info("[Consensus] Registered. client_id=%s", client_id)


# ─── Local callback server ────────────────────────────────────────────────────

class _CallbackServer:
    """Catches the OAuth redirect on localhost:PORT (GET or POST)."""

    def __init__(self):
        self._result: tuple[str, str | None] | None = None
        self._event = threading.Event()

    def _parse_and_respond(self, path: str, body: str, handler) -> None:
        qs = parse_qs(urlparse(path).query)
        if not qs.get("code") and body:
            qs = parse_qs(body)
        self._result = (
            (qs.get("code")  or [""])[0],
            (qs.get("state") or [None])[0],
        )
        self._event.set()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.end_headers()
        handler.wfile.write(
            b"<html><body><h2>Authentication successful.</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )

    def _serve(self):
        import http.server
        server = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                server._parse_and_respond(self.path, "", self)
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length else ""
                server._parse_and_respond(self.path, body, self)

        httpd = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _Handler)
        httpd.timeout = 0.5
        while not self._event.is_set():
            httpd.handle_request()
        httpd.server_close()

    def start(self):
        threading.Thread(target=self._serve, daemon=True).start()

    def wait(self, timeout: float = 300.0) -> tuple[str, str | None]:
        if not self._event.wait(timeout=timeout):
            raise TimeoutError("OAuth callback not received within timeout")
        return self._result  # type: ignore[return-value]


# Started lazily, and only when interactive auth is permitted. Starting it at
# import time would bind a port in every worker and web process that merely
# imports this module.
_callback_server: "_CallbackServer | None" = None


class ConsensusAuthRequired(RuntimeError):
    """
    Consensus needs a browser login that this process must not perform.

    Servers and workers run unattended: opening a browser is impossible and
    blocking on a callback would stall the run. Callers treat this as
    "the source is unavailable" and carry on without it.
    """


class ConsensusRateLimited(RuntimeError):
    """The MCP server returned a 429 or rate-limit error (review R9).

    Raised instead of returning [] so the calling handler can record the
    failure with the rate limiter — otherwise the limiter never hears about
    the 429 and keeps allowing calls that will also fail.
    """


def interactive_auth_allowed() -> bool:
    """
    Whether this process may open a browser and wait for a human.

    Off by default. A worker or web process must never block on OAuth, so
    interactive login is opt-in and additionally requires a real terminal.
    Authenticate once with:

        python3 core/consensus_mcp.py --login

    which stores tokens in db/consensus_tokens.json for every process to reuse.
    """
    import sys
    if os.environ.get("SEEKER_CONSENSUS_INTERACTIVE", "").strip().lower() not in (
            "1", "true", "yes"):
        return False
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


async def _redirect_handler(url: str) -> None:
    if not interactive_auth_allowed():
        raise ConsensusAuthRequired(
            "Consensus requires a browser login, which this process cannot do. "
            "Run 'python3 core/consensus_mcp.py --login' once to store tokens, "
            "or remove 'consensus' from agent_sources in config.json."
        )
    global _callback_server
    if _callback_server is None:
        _callback_server = _CallbackServer()
        _callback_server.start()
    print("\n[Consensus] Opening browser for authentication...")
    print(f"  If the browser does not open, visit:\n  {url}\n")
    webbrowser.open(url)


async def _callback_handler() -> tuple[str, str | None]:
    if not interactive_auth_allowed() or _callback_server is None:
        raise ConsensusAuthRequired("Consensus browser login is not available here")
    print("[Consensus] Waiting for authentication callback...")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _callback_server.wait, 300.0)
    print("[Consensus] Authentication successful.\n")
    return result


# ─── Core search ─────────────────────────────────────────────────────────────

async def _async_search(
    query: str,
    limit: int = 10,
    year_min: int | None = None,
    year_max: int | None = None,
    study_types: list[str] | None = None,
    sjr_max: int | None = None,
    human_only: bool | None = None,
) -> list[dict]:

    storage = FileTokenStorage()
    await _ensure_registered(storage)

    client_metadata = OAuthClientMetadata(
        redirect_uris=[REDIRECT_URI],
        client_name="SEEKER Research Pipeline",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope="search",
    )

    auth_provider = ConsensusOAuthProvider(
        server_url=MCP_URL,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=_redirect_handler,
        callback_handler=_callback_handler,
    )

    http_client = httpx.AsyncClient(auth=auth_provider)

    arguments: dict[str, Any] = {"query": query}
    if year_min   is not None: arguments["year_min"]    = year_min
    if year_max   is not None: arguments["year_max"]    = year_max
    if study_types:            arguments["study_types"] = study_types
    if sjr_max    is not None: arguments["sjr_max"]     = sjr_max
    if human_only is not None: arguments["human"]       = human_only

    async with streamable_http_client(MCP_URL, http_client=http_client) as (
        read, write, _
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            try:
                response = await session.call_tool("search", arguments=arguments)
            except Exception as e:
                # Detect rate-limiting from the underlying HTTP transport
                # so the handler can record it with the rate limiter (review R9).
                msg = str(e).lower()
                if "429" in msg or "rate limit" in msg or "too many requests" in msg:
                    raise ConsensusRateLimited(f"MCP search returned 429: {e}")
                raise

    # Collect all text content from response blocks
    raw_text = ""
    for block in (response.content or []):
        if hasattr(block, "text"):
            raw_text += block.text
        elif isinstance(block, dict) and "text" in block:
            raw_text += block["text"]

    if not raw_text.strip():
        logger.warning("[Consensus] Empty response")
        return []

    # Try JSON first (future-proof if Consensus switches format)
    papers_dicts = []
    try:
        data = json.loads(raw_text)
        papers_dicts = data if isinstance(data, list) else data.get("papers", [])
    except json.JSONDecodeError:
        pass

    results = []

    if papers_dicts:
        # JSON format
        for p in papers_dicts:
            if not isinstance(p, dict):
                continue
            raw_authors = p.get("authors", [])
            authors = (
                [a.get("name", a) if isinstance(a, dict) else str(a) for a in raw_authors[:5]]
                if isinstance(raw_authors, list) else []
            )
            year = p.get("year")
            try:   year = int(year) if year else None
            except: year = None
            cited_by = p.get("citation_count", 0) or 0
            try:   cited_by = int(cited_by)
            except: cited_by = 0
            journal = p.get("journal", "")
            if isinstance(journal, dict):
                journal = journal.get("title", "")
            results.append({
                "title":       p.get("title", ""),
                "authors":     authors,
                "year":        year,
                "source_name": "consensus",
                "doi":         p.get("doi", ""),
                "abstract":    (p.get("abstract") or p.get("takeaway") or "")[:1000],
                "active_link": p.get("url", ""),
                "cited_by":    cited_by,
                "journal":     journal,
                "study_type":  p.get("study_type", ""),
            })
    else:
        # Text format:
        # [N] [Title](url) (Authors, year, citations, Journal)
        #   Abstract text...
        import re
        # Split into paper blocks on lines starting with [number]
        blocks = re.split(r"(?=^\[\d+\])", raw_text, flags=re.MULTILINE)
        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # Header line: [N] [Title](url) (Author et al., year, N citations, Journal)
            header_match = re.match(
                r"^\[\d+\]\s+\[(.+?)\]\((https?://[^\)]+)\)\s*\(([^)]+)\)",
                block
            )
            if not header_match:
                continue

            title   = header_match.group(1).strip()
            url     = header_match.group(2).strip()
            meta    = header_match.group(3).strip()

            # Parse meta: "Author et al., year, N citations, Journal"
            meta_parts = [p.strip() for p in meta.split(",")]

            authors_str = meta_parts[0] if meta_parts else ""
            authors = [a.strip() for a in authors_str.split(" and ")] if authors_str else []

            year = None
            cited_by = 0
            journal = ""
            for part in meta_parts[1:]:
                part = part.strip()
                if re.match(r"^\d{4}$", part):
                    year = int(part)
                elif re.search(r"\d+\s+citation", part):
                    m = re.search(r"(\d+)", part)
                    cited_by = int(m.group(1)) if m else 0
                elif part and not re.match(r"^\d", part):
                    journal = part

            # Abstract is the rest of the block after the header line
            lines = block.splitlines()
            abstract_lines = []
            for line in lines[1:]:
                line = line.strip()
                if line:
                    abstract_lines.append(line)
            abstract = " ".join(abstract_lines)[:1000]

            results.append({
                "title":       title,
                "authors":     authors,
                "year":        year,
                "source_name": "consensus",
                "doi":         "",
                "abstract":    abstract,
                "active_link": url,
                "cited_by":    cited_by,
                "journal":     journal,
                "study_type":  "",
            })

    logger.info("[Consensus] '%s' → %d results via MCP", query, len(results))
    return results


def have_tokens() -> bool:
    """Whether a previous login left usable tokens on disk."""
    try:
        if not TOKEN_FILE.exists():
            return False
        return bool(json.loads(TOKEN_FILE.read_text()).get("tokens"))
    except Exception:
        return False


# Logged once per process — a per-query warning would bury the real output.
_warned_unavailable = False


def available() -> bool:
    """
    Whether Consensus can be used without human interaction.

    Unattended processes need stored tokens; only an interactive session may
    obtain them.
    """
    return have_tokens() or interactive_auth_allowed()


def search_consensus(query: str, limit: int = 10, **kwargs) -> list[dict]:
    """
    Synchronous entry point.

    Returns [] rather than raising or blocking when Consensus is unavailable,
    so it behaves like any other optional source.
    """
    global _warned_unavailable

    if not available():
        if not _warned_unavailable:
            _warned_unavailable = True
            logger.warning(
                "[Consensus] Skipped — no stored credentials and this process "
                "cannot open a browser. Run 'python3 core/consensus_mcp.py "
                "--login' once, or remove 'consensus' from agent_sources in "
                "config.json to stop attempting it."
            )
        return []

    try:
        return asyncio.run(_async_search(query, limit, **kwargs))
    except ConsensusAuthRequired as e:
        if not _warned_unavailable:
            _warned_unavailable = True
            logger.warning("[Consensus] %s", e)
        return []
    except ConsensusRateLimited:
        # Re-raise so the handler can record the failure with the rate
        # limiter (review R9) — the limiter needs to know about the 429.
        raise
    except Exception as e:
        # An optional source must never take the pipeline down with it
        logger.warning("[Consensus] Search failed (%s) — continuing without it",
                       type(e).__name__)
        logger.debug("[Consensus] %s", e, exc_info=True)
        return []


# ─── CLI test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if "--login" in sys.argv:
        # The one place a browser may open. Tokens land in
        # db/consensus_tokens.json and every other process reuses them.
        os.environ["SEEKER_CONSENSUS_INTERACTIVE"] = "1"
        if not sys.stdin.isatty():
            print("--login needs an interactive terminal.")
            sys.exit(1)
        print("\nAuthenticating with Consensus — a browser window will open.\n")
        try:
            asyncio.run(_async_search("authentication check", limit=1))
        except Exception as e:
            print(f"\nLogin failed: {e}")
            sys.exit(1)
        if have_tokens():
            print(f"\nTokens stored at {TOKEN_FILE}")
            print("Workers and the web app will now use Consensus.")
            sys.exit(0)
        print("\nNo tokens were stored — login did not complete.")
        sys.exit(1)

    query = " ".join(a for a in sys.argv[1:] if not a.startswith("-")) \
        or "effects of sleep on memory consolidation"
    print(f"\nSearching Consensus MCP: '{query}'\n{'─'*60}")
    results = asyncio.run(_async_search(query, limit=5))
    if not results:
        print("No results.")
    else:
        for i, r in enumerate(results, 1):
            print(f"\n[{i}] {r['title']}")
            print(f"     {', '.join(r['authors'][:3]) or '?'} · {r['year'] or '?'} · {r['journal'] or '?'}")
            print(f"     Cited: {r['cited_by']} · {r['active_link']}")
            if r["abstract"]:
                print(f"     {r['abstract'][:200]}...")
