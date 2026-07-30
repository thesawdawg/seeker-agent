"""
SEEKER HTTP API
---------------
FastAPI over the pipeline state machine.

The web process never runs pipeline work: it writes to the database and
enqueues a job. A worker claims the job and advances the run. That keeps
requests fast and lets long runs survive a web restart.

Progress is polled — GET /api/runs/{id}/status is cheap and returns
step-level state with a running indicator.

Run:
  uvicorn web.app:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.keys import _load_env
_load_env()

from core import breaks, crypto, database as db, jobs, llm, pipeline, progress, users
from core.utils import load_config
from web import auth

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.getLogger().setLevel(logging.INFO)
    db.init_db()
    jobs.init_jobs_table()
    users.init_users_tables()
    users.init_run_owners()
    # Register auth backend routes (simple, saml, etc.)
    auth.get_manager().register_routes(_app)
    logger.info(f"SEEKER API ready — storage: {db.backend_name()}")
    yield


app = FastAPI(
    title="SEEKER",
    description="Multi-agent deep research pipeline",
    version="2.0.0",
    lifespan=lifespan,
)

_origins = [o for o in os.environ.get("SEEKER_CORS_ORIGINS", "").split(",") if o]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CredentialRequest(BaseModel):
    provider: str
    base_url: str
    api_key: str
    models: dict = Field(default_factory=dict)


class CreateRunRequest(BaseModel):
    problem: str = Field(..., min_length=8)
    provider: str = "open-webui"
    model_overrides: dict = Field(
        default_factory=dict,
        description='Per-agent overrides, e.g. {"grounder": {"model": "qwen3:32b"}}',
    )
    source_overrides: dict = Field(
        default_factory=dict,
        description='Per-run source enable/disable, e.g. {"scopus": true, "arxiv": false} (review U1)',
    )
    previous_run_id: str = Field(
        default="",
        description='Optional: a previous run to compare against. Sources already in that run are flagged as "previously seen" (F5).',
    )


class BreakSubmission(BaseModel):
    instructions: str = ""
    directives: list[str] = Field(
        default_factory=list,
        description="Structured directives; joined with instructions",
    )
    model_overrides: dict = Field(
        default_factory=dict,
        description="Change models for the agents that have not run yet",
    )
    source_overrides: dict = Field(
        default_factory=dict,
        description="Per-run source enable/disable changes for the steps still to run",
    )


class SourceCredentialRequest(BaseModel):
    source_id: str = Field(..., description="e.g. scopus, semantic_scholar, core")
    api_key: str


class McpToggleRequest(BaseModel):
    conn_id: str = Field(..., description="MCP connection id, e.g. consensus, primo")
    enabled: bool


class ModelRolesRequest(BaseModel):
    models: dict = Field(
        default_factory=dict,
        description='Which model fills each role, e.g. {"primary": "qwen3:32b", "light": "llama3.2:3b"}',
    )


class ResumeRequest(BaseModel):
    provider: str = "open-webui"
    models: dict = Field(
        default_factory=dict,
        description='Provider role models, e.g. {"primary": "...", "light": "..."}',
    )
    model_overrides: dict = Field(
        default_factory=dict,
        description="Per-agent overrides for the steps still to run",
    )


class RerunRequest(BaseModel):
    cascade: bool = True


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "status":  "ok",
        "storage": db.backend_name(),
        "queue":   jobs.queue_depth(),
        "secrets_configured": __import__("core.crypto", fromlist=["x"]).available(),
    }


# ---------------------------------------------------------------------------
# Auth — login is handled by backend routes registered via AuthManager.
# Logout, /me, and method discovery live here (they are backend-agnostic).
# ---------------------------------------------------------------------------

@app.get("/api/auth/methods")
def auth_methods():
    """List available auth backends so the UI can render login options."""
    return {"methods": auth.get_manager().describe_methods()}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response,
           user: dict = Depends(auth.resolve_user)):
    token = request.headers.get(auth.SESSION_HEADER) or \
        request.cookies.get("seeker_session", "")
    auth.logout(token)
    response.delete_cookie("seeker_session")
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: dict = Depends(auth.resolve_user)):
    return {
        "user_id":      user["user_id"],
        "display_name": user.get("display_name") or "",
        "auth_kind":    user.get("auth_kind") or "",
        "is_admin":     bool(user.get("is_admin")),
        "credentials":  users.list_credentials(user["user_id"]),
    }


class UpdateProfileRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=100)


@app.put("/api/auth/me")
def update_profile(body: UpdateProfileRequest,
                   user: dict = Depends(auth.resolve_user)):
    """Update the current user's display name."""
    users.set_display_name(user["user_id"], body.display_name)
    return {"ok": True, "display_name": body.display_name}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


@app.put("/api/auth/password")
def change_password(body: ChangePasswordRequest,
                    user: dict = Depends(auth.resolve_user)):
    """Change the current user's password (password-auth users only)."""
    if user.get("auth_kind") != "password":
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Password change is only available for "
                            "username/password accounts")
    # Verify current password before allowing change
    stored_hash = user.get("password_hash") or ""
    if not stored_hash or not crypto.verify_password(
            body.current_password, stored_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Current password is incorrect")
    users.set_password(user["user_id"], body.new_password)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Provider credentials and models
# ---------------------------------------------------------------------------

@app.get("/api/credentials")
def list_credentials(user: dict = Depends(auth.resolve_user)):
    return {"credentials": users.list_credentials(user["user_id"])}


async def _check_provider_health(user_id: str, credential: dict,
                                 limiter: asyncio.Semaphore) -> dict:
    """Probe one stored provider without exposing its key or raw errors."""
    provider = credential["provider"]
    cfg = users.provider_config(user_id, provider)
    if not cfg or not cfg.configured:
        return {
            "provider": provider,
            "status": "unavailable",
            "message": "Connection is incomplete",
            "models_count": 0,
        }

    kind = "anthropic" if provider == "anthropic" else "openai"
    try:
        async with limiter:
            models = await asyncio.to_thread(
                auth.validate_provider_key, cfg.base_url, cfg.api_key, kind)
        return {
            "provider": provider,
            "status": "ok",
            "message": "Connected",
            "models_count": len(models),
        }
    except HTTPException as exc:
        if exc.status_code in (status.HTTP_401_UNAUTHORIZED,
                               status.HTTP_403_FORBIDDEN):
            health_status = "rejected"
            message = "API key rejected"
        elif exc.status_code == status.HTTP_400_BAD_REQUEST:
            health_status = "unavailable"
            message = "Invalid provider URL"
        else:
            health_status = "unavailable"
            message = "Provider unavailable"
        return {
            "provider": provider,
            "status": health_status,
            "message": message,
            "models_count": 0,
        }
    except Exception:
        logger.exception("Unexpected health-check failure for provider %s",
                         provider)
        return {
            "provider": provider,
            "status": "unavailable",
            "message": "Provider unavailable",
            "models_count": 0,
        }


@app.get("/api/providers/health")
async def provider_health(user: dict = Depends(auth.resolve_user)):
    """Check every model-provider connection stored by the current user."""
    credentials = users.list_credentials(user["user_id"])
    limiter = asyncio.Semaphore(5)
    checks = [
        _check_provider_health(user["user_id"], credential, limiter)
        for credential in credentials
    ]
    return {"providers": await asyncio.gather(*checks)}


@app.put("/api/credentials")
def put_credentials(body: CredentialRequest,
                    user: dict = Depends(auth.resolve_user)):
    """Add or replace credentials for one provider, after validating them."""
    kind = "anthropic" if body.provider == "anthropic" else "openai"
    models = auth.validate_provider_key(body.base_url, body.api_key, kind)
    stored = users.set_credentials(user["user_id"], body.provider,
                                   body.base_url, body.api_key, body.models)
    return {"credential": stored, "available_models": models}


@app.patch("/api/credentials/{provider}/models")
def patch_credential_models(provider: str, body: ModelRolesRequest,
                            user: dict = Depends(auth.resolve_user)):
    """
    Set which model fills each role for a provider.

    Agents choose a role ('primary' or 'light'), not a model name, so this is
    what makes a freshly configured provider actually usable. Separate from
    PUT /api/credentials because the API key is never returned to the client.
    """
    updated = users.set_models(user["user_id"], provider, body.models)
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"No credentials stored for provider '{provider}'")
    return {"credential": updated}


@app.delete("/api/credentials/{provider}")
def delete_credentials(provider: str, user: dict = Depends(auth.resolve_user)):
    users.delete_credentials(user["user_id"], provider)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Source credentials — per-user academic source API keys (review U2)
# ---------------------------------------------------------------------------

@app.get("/api/source-credentials")
def list_source_credentials(user: dict = Depends(auth.resolve_user)):
    return {"credentials": users.list_source_credentials(user["user_id"])}


@app.put("/api/source-credentials")
def put_source_credentials(body: SourceCredentialRequest,
                           user: dict = Depends(auth.resolve_user)):
    """Add or replace a user's API key for one academic source."""
    stored = users.set_source_credentials(user["user_id"], body.source_id, body.api_key)
    return {"credential": stored}


@app.delete("/api/source-credentials/{source_id}")
def delete_source_credentials(source_id: str, user: dict = Depends(auth.resolve_user)):
    users.delete_source_credentials(user["user_id"], source_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# MCP connection toggles — per-user enable/disable for MCP-based sources
# (Consensus, Primo, etc.). Stored in user_preferences.
# ---------------------------------------------------------------------------

@app.get("/api/mcp-toggles")
def get_mcp_toggles(user: dict = Depends(auth.resolve_user)):
    """List all MCP connections with their current enabled/disabled state."""
    return {"connections": users.get_mcp_toggles(user["user_id"])}


@app.put("/api/mcp-toggles")
def set_mcp_toggle(body: McpToggleRequest, user: dict = Depends(auth.resolve_user)):
    """Enable or disable an MCP connection for the current user."""
    try:
        result = users.set_mcp_toggle(user["user_id"], body.conn_id, body.enabled)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return result


# ---------------------------------------------------------------------------
# User settings — run defaults, appearance, display, notifications.
# Stored per account in the existing user_preferences table; the schema lives
# in core/user_settings.py so the UI can render itself from it.
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_user_settings(user: dict = Depends(auth.resolve_user)):
    """Current settings merged over defaults, plus the schema to render them."""
    from core import user_settings
    return {
        "settings": user_settings.get_settings(user["user_id"]),
        "groups":   user_settings.schema_for_ui(),
    }


@app.put("/api/settings")
def put_user_settings(body: dict, user: dict = Depends(auth.resolve_user)):
    """
    Partial update: {key: value}. Validated as a whole before anything is
    written, so one bad value cannot half-apply the request.
    """
    from core import user_settings
    payload = body.get("settings", body)
    try:
        updated = user_settings.update_settings(user["user_id"], payload)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return {"ok": True, "settings": updated}


# ---------------------------------------------------------------------------
# Admin — user management (F12)
# Admins can view users and see which providers/sources are configured,
# but cannot add, edit, or delete credentials for other users. Each user
# manages their own connections from the Settings page.
# ---------------------------------------------------------------------------

@app.get("/api/admin/users")
def admin_list_users(user: dict = Depends(auth.resolve_user)):
    """List all users — admin only."""
    auth.require_admin(user)
    return {"users": users.list_all_users()}


@app.get("/api/admin/users/{user_id}/credentials")
def admin_get_user_credentials(user_id: str,
                               user: dict = Depends(auth.resolve_user)):
    """View another user's provider connections — admin only, no secrets."""
    auth.require_admin(user)
    target = users.get_user(user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return {"credentials": users.list_credentials(user_id),
            "user": {"user_id": user_id,
                     "display_name": target.get("display_name") or "",
                     "auth_kind": target.get("auth_kind") or ""}}


@app.get("/api/admin/users/{user_id}/source-credentials")
def admin_get_user_source_credentials(
        user_id: str, user: dict = Depends(auth.resolve_user)):
    """View another user's source API keys — admin only, no secrets."""
    auth.require_admin(user)
    target = users.get_user(user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return {"credentials": users.list_source_credentials(user_id),
            "user": {"user_id": user_id,
                     "display_name": target.get("display_name") or ""}}


@app.get("/api/models")
def list_models(provider: str = "open-webui",
                user: dict = Depends(auth.resolve_user)):
    """Models the user's configured provider can serve — backs the pickers."""
    cfg = users.provider_config(user["user_id"], provider)
    if not cfg or not cfg.configured:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"No credentials stored for provider '{provider}'")
    kind = "anthropic" if provider == "anthropic" else "openai"
    return {"provider": provider, "models":
            auth.validate_provider_key(cfg.base_url, cfg.api_key, kind)}


@app.get("/api/agents")
def list_agents(user: dict = Depends(auth.resolve_user)):
    """
    Agent names and default routing, so the UI can offer per-agent models.

    Authenticated: unlike /api/steps, which publishes the pipeline's shape,
    this exposes configured model names and token limits — deployment
    configuration rather than public structure (review S5). The UI only calls
    it after sign-in.
    """
    client = llm.get_client()
    return {"agents": [
        {"name": name,
         "model_role": profile.model_role,
         "max_tokens": profile.max_tokens,
         "temperature": profile.temperature}
        for name, profile in sorted(client.settings.agents.items())
    ]}


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@app.get("/api/runs")
def list_runs(user: dict = Depends(auth.resolve_user),
              limit: int = 50, offset: int = 0,
              status: str = "", q: str = ""):
    """
    The caller's runs, newest first, paged and filterable.

    Three queries regardless of how many runs the user owns — the ordering,
    the filtering and the paging all happen in SQL (review O2, X6).

    limit/offset page the list; status filters exactly; q matches the problem
    text case-insensitively.
    """
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    rows, total = users.runs_page(user["user_id"], limit=limit, offset=offset,
                                  status=status, search=q)
    return {
        "runs": [
            {
                "run_id":         r["run_id"],
                "problem":        r.get("problem", ""),
                "status":         r.get("status", ""),
                "created_at":     r.get("created_at"),
                "progress":       r["progress"],
                "awaiting_break": r["awaiting_break"],
            }
            for r in rows
        ],
        "total":  total,
        "limit":  limit,
        "offset": offset,
    }


@app.post("/api/runs", status_code=status.HTTP_201_CREATED)
def create_run(body: CreateRunRequest, user: dict = Depends(auth.resolve_user)):
    """Start a run. Returns immediately; a worker picks it up."""
    cfg = users.provider_config(user["user_id"], body.provider)
    if not cfg or not cfg.configured:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            # User-facing: this is the message a researcher sees if they reach
            # Start run without a provider, so it names the screen, not the
            # endpoint.
            f"No model provider is connected yet. Open Settings → Providers "
            f"and connect '{body.provider}' before starting a run.",
        )

    run_id = pipeline.create_run(body.problem,
                                 previous_run_id=body.previous_run_id or None)
    users.claim_run(run_id, user["user_id"], body.provider)
    if body.model_overrides:
        _store_model_overrides(run_id, body.model_overrides)
    if body.source_overrides:
        pipeline.set_source_overrides(run_id, body.source_overrides)

    job_id = jobs.enqueue(run_id)
    logger.info(f"Run {run_id} created by {user['user_id']} (job {job_id})")
    return {"run_id": run_id, "job_id": job_id,
            "state": pipeline.get_state(run_id)}


# Declared before /api/runs/{run_id}: FastAPI matches in declaration
# order, so a literal segment under a parameterised prefix has to come
# first or the parameterised route captures it as a run_id.
@app.get("/api/runs/estimate")
def estimate_run(user: dict = Depends(auth.resolve_user),
                 source_overrides: str = "", themes: int = 0):
    """
    Roughly what a run will cost, before committing to it (review X2).

    A run is a long, expensive, human-blocking commitment and the New Run
    screen previously gave no sense of its scale. This learns from the
    caller's own completed runs where there are any, and says so when there
    are not — an estimate labelled as a guess is useful; one that looks like
    a measurement is not.

    source_overrides is an optional JSON object, the same shape the New Run
    form posts, so the estimate tracks the sources actually selected.
    """
    from core import provenance

    overrides = {}
    if source_overrides:
        try:
            parsed = json.loads(source_overrides)
            if isinstance(parsed, dict):
                overrides = parsed
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "source_overrides must be a JSON object")
    try:
        config = load_config()
    except FileNotFoundError:
        config = {}
    return provenance.estimate_run(
        user["user_id"], config, overrides,
        theme_count=themes if themes > 0 else None)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(auth.resolve_user)):
    auth.require_run_access(user, run_id)
    state = pipeline.get_state(run_id)
    if not state.get("exists"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    state["counts"] = {
        table: db.count(table, {"run_id": run_id})
        for table in ("sources", "gaps", "implications", "proposals",
                      "evaluations", "directions", "artifacts")
    }
    state["model_overrides"] = pipeline.get_model_overrides(run_id)
    state["source_overrides"] = pipeline.get_source_overrides(run_id)
    return state


@app.get("/api/runs/{run_id}/status")
def run_status(run_id: str, since_seq: int = 0,
               user: dict = Depends(auth.resolve_user)):
    """
    The polling endpoint. Deliberately small — the frontend hits this every
    couple of seconds, so it returns step state and nothing heavy.

    since_seq — only events after this sequence number are included, so a
    long-running step doesn't re-send its whole verbose history every poll.
    Pass 0 (default) to get the most recent slice.
    """
    auth.require_run_access(user, run_id)

    # A stop must land within its grace period even if the worker holding the
    # run is wedged or gone, so the deadline is enforced here rather than
    # relying on the worker to notice.
    if pipeline.enforce_stop_deadline(run_id):
        logger.info(f"[{run_id}] stop deadline enforced from the status endpoint")

    state = pipeline.get_state(run_id)
    if not state.get("exists"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")

    queued = [j for j in jobs.jobs_for_run(run_id)
              if j.get("status") in ("queued", "running")]
    return {
        "run_id":         run_id,
        "status":         state["status"],
        "progress":       state["progress"],
        "current_step":   state["current_step"],
        "running":        state["running"] or bool(queued),
        "awaiting_break": state["awaiting_break"],
        "complete":       state["complete"],
        "stop_grace_seconds": pipeline.STOP_GRACE_SECONDS,
        "failed_steps":   state["failed_steps"],
        "queued":         bool(queued),
        "steps": [
            {"name": s["step_name"], "label": s["label"], "status": s["status"],
             "error": s.get("error"), "started_at": s.get("started_at"),
             "finished_at": s.get("finished_at"),
             # What this step is talking to right now, e.g. "Consensus — searching"
             "activity": s.get("activity"), "activity_at": s.get("activity_at")}
            for s in state["steps"]
        ],
        "events": _serialize_events(
            progress.get_events(run_id, since_seq=since_seq),
            {s["step_name"]: s["label"] for s in state["steps"]},
        ),
    }


def _serialize_events(rows: list[dict], step_labels: dict) -> list[dict]:
    """Trim step_events rows to what the frontend needs, verbosely."""
    return [
        {
            "seq":       r["seq"],
            "step":      step_labels.get(r.get("step_name"), r.get("step_name")),
            "service":   progress.label_for(r["service"]),
            "action":    r.get("action") or "",
            "detail":    r.get("detail") or "",
            "preview":   r.get("preview") or "",
            "at":        r.get("created_at"),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# SSE event stream (F3) — replaces the 2s polling loop with server-pushed
# events. The endpoint polls the DB at 1s intervals internally and emits an
# event whenever step status, activity, or run-level state changes. It
# terminates when the run completes or the client disconnects.
# ---------------------------------------------------------------------------

import asyncio
from typing import AsyncGenerator


def _run_status_snapshot(run_id: str, since_seq: int = 0) -> Optional[dict]:
    """Build the same status dict as the GET /status endpoint, or None
    if the run no longer exists."""
    state = pipeline.get_state(run_id)
    if not state.get("exists"):
        return None
    queued = [j for j in jobs.jobs_for_run(run_id)
              if j.get("status") in ("queued", "running")]
    return {
        "run_id":         run_id,
        "status":         state["status"],
        "progress":       state["progress"],
        "current_step":   state["current_step"],
        "running":        state["running"] or bool(queued),
        "awaiting_break": state["awaiting_break"],
        "complete":       state["complete"],
        "stop_grace_seconds": pipeline.STOP_GRACE_SECONDS,
        "failed_steps":   state["failed_steps"],
        "queued":         bool(queued),
        "steps": [
            {"name": s["step_name"], "label": s["label"], "status": s["status"],
             "error": s.get("error"), "started_at": s.get("started_at"),
             "finished_at": s.get("finished_at"),
             "activity": s.get("activity"), "activity_at": s.get("activity_at")}
            for s in state["steps"]
        ],
        "events": _serialize_events(
            progress.get_events(run_id, since_seq=since_seq),
            {s["step_name"]: s["label"] for s in state["steps"]},
        ),
    }


def _status_signature(snap: dict) -> str:
    """A compact hash of the parts of the status that the UI renders.

    Two snapshots with the same signature produce identical UI, so the
    SSE stream only emits when this string changes. This avoids spamming
    the client with a full status payload every second when nothing moved.
    """
    parts = [
        snap["status"],
        snap["current_step"] or "",
        str(snap["running"]),
        str(snap["awaiting_break"]),
        str(snap["complete"]),
        str(snap["queued"]),
        ",".join(snap["failed_steps"]),
        "|".join(
            f"{s['name']}:{s['status']}:{s.get('activity') or ''}"
            for s in snap["steps"]
        ),
    ]
    return "\x1f".join(parts)


# SSE poll interval — the internal DB poll frequency. 1s is fast enough
# that activity notes appear promptly, while being far cheaper than the
# old 2s HTTP polling (one persistent connection vs. a new request every
# 2s, and no HTTP overhead per tick).
_SSE_POLL_SECONDS = 1.0
# Maximum stream lifetime — prevents a forgotten browser tab from holding
# a connection open forever. The client reconnects automatically.
_SSE_MAX_LIFETIME_SECONDS = 300  # 5 minutes


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request,
                     user: dict = Depends(auth.resolve_user)):
    """
    Server-Sent Events stream for a run (F3).

    Emits `status` events whenever the run's step state, activity, or
    completion changes. The client uses EventSource and falls back to
    polling if SSE is unavailable.

    The stream closes after _SSE_MAX_LIFETIME_SECONDS or when the run
    completes; the client reconnects automatically.
    """
    auth.require_run_access(user, run_id)

    async def event_stream() -> AsyncGenerator[str, None]:
        import time
        start = time.monotonic()
        last_sig = None
        last_activity_keys: set[str] = set()
        last_seq = 0

        # Send an initial event immediately so the client doesn't wait 1s
        # for its first status.
        snap = await asyncio.to_thread(_run_status_snapshot, run_id, last_seq)
        if snap is None:
            yield _sse_event("error", {"message": "Run not found"})
            return
        yield _sse_event("status", snap)
        last_sig = _status_signature(snap)
        last_activity_keys = _activity_keys(snap)
        last_seq = max([last_seq] + [e["seq"] for e in snap["events"]])

        # If the run is already complete, emit done and close
        if snap["complete"] and not snap["running"]:
            yield _sse_event("done", {"run_id": run_id})
            return

        while True:
            # Check for client disconnect
            if await request.is_disconnected():
                logger.debug(f"[SSE] Client disconnected from {run_id}")
                return

            # Check max lifetime
            if time.monotonic() - start > _SSE_MAX_LIFETIME_SECONDS:
                logger.debug(f"[SSE] Max lifetime reached for {run_id}")
                return

            # Sleep before next poll — yield control to the event loop
            # so other requests can be served.
            await asyncio.sleep(_SSE_POLL_SECONDS)

            # Offload to a thread: the snapshot is synchronous DB work, and
            # this is an `async def` generator, so doing it inline blocks the
            # event loop — every other request and every other SSE stream —
            # once per second per viewer (review O1). Only events newer than
            # last_seq are fetched, so a verbose step doesn't re-send its
            # whole history every second.
            snap = await asyncio.to_thread(_run_status_snapshot, run_id, last_seq)
            if snap is None:
                yield _sse_event("error", {"message": "Run not found"})
                return

            sig = _status_signature(snap)
            activity_keys = _activity_keys(snap)

            # Emit a status event if step state, activity, or granular events changed
            if sig != last_sig or activity_keys != last_activity_keys or snap["events"]:
                yield _sse_event("status", snap)
                last_sig = sig
                last_activity_keys = activity_keys
                if snap["events"]:
                    last_seq = max(e["seq"] for e in snap["events"])

            # Emit a done event and close when the run is complete
            if snap["complete"] and not snap["running"]:
                yield _sse_event("done", {"run_id": run_id})
                return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        },
    )


def _sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event string."""
    payload = json.dumps(data)
    return f"event: {event_type}\ndata: {payload}\n\n"


def _activity_keys(snap: dict) -> set[str]:
    """A set of '{step}|{activity}' strings for dedup."""
    return {f"{s['name']}|{s.get('activity') or ''}" for s in snap["steps"]}


@app.post("/api/runs/{run_id}/advance")
def advance_run(run_id: str, user: dict = Depends(auth.resolve_user)):
    """Nudge a run — queue work for it. Safe to call repeatedly."""
    auth.require_run_access(user, run_id)
    return {"job_id": jobs.enqueue(run_id), "state": pipeline.get_state(run_id)}


@app.get("/api/runs/{run_id}/sources")
def run_sources(run_id: str, user: dict = Depends(auth.resolve_user)):
    """
    Per-source health and coverage for a run (review U3).

    Combines the source_health table (which sources succeeded/failed and how
    many results each returned) with the actual sources inserted, so the
    researcher can judge whether the Understanding Map is trustworthy.
    """
    auth.require_run_access(user, run_id)
    health = db.get_source_health(run_id)
    # Count actual sources inserted per source_name
    rows = db.query(
        "SELECT source_name, COUNT(*) AS n FROM sources WHERE run_id = ? GROUP BY source_name",
        (run_id,),
    )
    inserted = {r["source_name"]: int(r["n"]) for r in rows}
    # F5: previously-seen count from cross-run dedup
    prev_rows = db.query(
        "SELECT COUNT(*) AS n FROM sources WHERE run_id = ? AND previously_seen = 1",
        (run_id,),
    )
    previously_seen = int(prev_rows[0]["n"]) if prev_rows else 0
    previous_run_id = db.get_previous_run_id(run_id)

    # Link and rating coverage, so the researcher can see what the evidence
    # base actually looks like rather than only how many rows landed
    # (review X3). A source whose landing page did not answer is kept and
    # flagged now, not discarded — this is where that shows up.
    link_status = db.count_by("sources", "link_status", {"run_id": run_id})
    relevance = db.count_by("sources", "relevance_rating", {"run_id": run_id})
    return {
        "run_id": run_id,
        "health": health,
        "inserted": inserted,
        "previously_seen": previously_seen,
        "previous_run_id": previous_run_id,
        "link_status": link_status,
        "relevance": relevance,
    }


@app.put("/api/runs/{run_id}/sources/override")
def update_source_override(run_id: str, body: dict,
                           user: dict = Depends(auth.resolve_user)):
    """
    Update source overrides mid-run (review U9).

    Lets a researcher disable a problematic source without stopping the
    whole run. The override is applied on the next step advance — an
    in-flight call is not interrupted.
    """
    auth.require_run_access(user, run_id)
    pipeline.set_source_overrides(run_id, body)
    return {"overrides": pipeline.get_source_overrides(run_id)}


@app.get("/api/sources/health")
def sources_health(user: dict = Depends(auth.resolve_user)):
    """
    Pre-flight: which sources are ready to use for this user (review R8).

    For each configured source, reports whether it is enabled, whether a key
    is available (env or the user's stored source credentials), and whether
    it is currently marked exhausted. Lets the New Run screen show
    "Scopus will be skipped — no key set" before the run starts.
    """
    config = load_config()
    sources_cfg = config.get("sources", {})
    agent_sources = config.get("agent_sources", {})
    # Sources referenced anywhere in agent_sources
    referenced: set[str] = set()
    for srcs in agent_sources.values():
        if isinstance(srcs, list):
            referenced.update(s for s in srcs if isinstance(s, str))

    # Map source_id -> env var name for the key check
    key_env = {
        "openalex": "OPENALEX_API_KEY",
        "pubmed": "NCBI_API_KEY",
        "semantic_scholar": "SEMANTIC_SCHOLAR_API_KEY",
        "core": "CORE_API_KEY",
        "philpapers": "PHILPAPERS_API_KEY",
        "scopus": "SCOPUS_API_KEY",
        "google_books": "GOOGLE_BOOKS_API_KEY",
    }
    # Sources that work without any key
    keyless = {"arxiv", "philarchive", "philsci", "open_library", "jstor",
               "ssrn", "base", "hal", "eric", "nber", "persee", "crossref", "web"}

    out = []
    for source_id in sorted(referenced | set(sources_cfg.keys())):
        enabled = sources_cfg.get(source_id, {}).get("enabled", True)
        env_var = key_env.get(source_id)
        has_key = False
        note = ""
        detail = ""
        if source_id in keyless:
            has_key = True
            note = "keyless"
            detail = "no key needed"
        elif env_var:
            env_set = bool(os.environ.get(env_var, "").strip())
            stored = bool(users.get_source_api_key(user["user_id"], source_id))
            has_key = env_set or stored
            # core.keys.get() checks the user's stored key *before* the env
            # var, so when both exist the stored one is what the run will
            # use. The pre-flight said "env" in that case — reporting a key
            # other than the one that will actually be sent (review X1).
            note = "stored" if stored else ("env" if env_set
                                            else "no key — will skip")
            detail = {
                "stored": "your stored key will be used",
                "env":    "the server's key will be used",
            }.get(note, "no key configured — this source will be skipped")
        else:
            note = "no key check defined"
            detail = "this source has no key requirement recorded"
        exhausted_until = db.source_exhausted_until(source_id, user["user_id"])
        out.append({
            "source_id": source_id,
            "enabled": enabled,
            "has_key": has_key,
            # `note` is a stable token the UI switches on; `detail` is the
            # sentence to show a human.
            "note": note,
            "detail": detail,
            "exhausted_until": exhausted_until,
        })
    return {"sources": out}


# ---------------------------------------------------------------------------
# Config templates — save/restore a tuned set of model + source overrides
# (review U10). Stored per-user so a researcher can apply a "Humanities deep
# scan" preset to every new run without re-entering it.
# ---------------------------------------------------------------------------

@app.get("/api/templates")
def list_templates(user: dict = Depends(auth.resolve_user)):
    """List the caller's saved config templates."""
    return {"templates": users.list_templates(user["user_id"])}


@app.get("/api/templates/built-in")
def list_builtin_templates(user: dict = Depends(auth.resolve_user)):
    """
    Built-in run templates from config.json (F4). Read-only — operators
    edit them by editing config.json. User-saved templates (U10) coexist
    with these in the template picker.
    """
    from core.utils import load_config
    try:
        cfg = load_config()
    except FileNotFoundError:
        return {"templates": []}
    raw = cfg.get("run_templates", {}) or {}
    out = []
    for name, spec in raw.items():
        if name.startswith("_"):
            continue
        out.append({
            "name": name,
            "description": spec.get("description", "") if isinstance(spec, dict) else "",
            "config": spec if isinstance(spec, dict) else {},
            "built_in": True,
        })
    return {"templates": out}


# ---------------------------------------------------------------------------
# Source blacklist (F8) — "don't cite this"
# ---------------------------------------------------------------------------

class BlacklistEntry(BaseModel):
    match_type: str = Field(..., description="doi | url | title_substring")
    match_value: str = Field(..., min_length=1)
    reason: str = ""


@app.get("/api/blacklist")
def list_blacklist(user: dict = Depends(auth.resolve_user)):
    """List the caller's source blacklist entries (F8)."""
    return {"entries": db.list_blacklist(user["user_id"])}


@app.post("/api/blacklist")
def add_blacklist_entry(body: BlacklistEntry,
                        user: dict = Depends(auth.resolve_user)):
    """Add a source to the blacklist (F8)."""
    if body.match_type not in ("doi", "url", "title_substring"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "match_type must be doi, url, or title_substring")
    db.add_to_blacklist(user["user_id"], body.match_type,
                        body.match_value, body.reason)
    return {"ok": True}


@app.delete("/api/blacklist")
def remove_blacklist_entry(body: BlacklistEntry,
                           user: dict = Depends(auth.resolve_user)):
    """Remove a source from the blacklist (F8)."""
    db.remove_from_blacklist(user["user_id"], body.match_type, body.match_value)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Config editor (F12) — admin-gated structured editing of config.json
# ---------------------------------------------------------------------------

# Sections an operator can edit. Each is validated before writing.
_EDITABLE_SECTIONS = ("themes", "sources", "agent_sources", "run_templates")

# Upper bound on one section's serialised size (review S8).
_MAX_CONFIG_SECTION_BYTES = 256 * 1024


@app.get("/api/config")
def get_config(user: dict = Depends(auth.resolve_user)):
    """
    Full config.json, admin-only (F12).

    Returns the editable sections plus metadata. The LLM routing section
    is included read-only — editing it at runtime is risky and out of
    scope for the web editor.
    """
    auth.require_admin(user)
    from core.utils import load_config
    try:
        cfg = load_config()
    except FileNotFoundError:
        cfg = {}
    # Strip _description / _notes keys from the response — they're operator
    # hints in the file, not data the UI needs to render.
    clean = {}
    for section in _EDITABLE_SECTIONS:
        val = cfg.get(section)
        if isinstance(val, dict):
            clean[section] = {k: v for k, v in val.items()
                              if not k.startswith("_")}
        elif isinstance(val, list):
            clean[section] = val
        else:
            clean[section] = val
    # LLM routing — read-only
    llm = cfg.get("llm", {})
    clean["llm"] = {k: v for k, v in llm.items() if not k.startswith("_")}
    clean["is_admin"] = True
    return clean


@app.put("/api/config/sections/{section}")
def update_config_section(section: str, body: dict,
                          user: dict = Depends(auth.resolve_user)):
    """
    Update one editable section of config.json (F12).

    The body is the new value for that section. The write is atomic
    (temp file + rename) and validated as JSON before reaching disk.
    Sections not in _EDITABLE_SECTIONS are rejected.
    """
    auth.require_admin(user)
    if section not in _EDITABLE_SECTIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Section '{section}' is not editable. "
                            f"Editable: {', '.join(_EDITABLE_SECTIONS)}")

    # config.json is re-read (though now cached) on request paths, so an
    # unbounded section makes every later request more expensive (review S8).
    try:
        incoming_size = len(json.dumps(body))
    except (TypeError, ValueError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Section value is not valid JSON: {e}")
    if incoming_size > _MAX_CONFIG_SECTION_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Section is {incoming_size} bytes; the limit is "
            f"{_MAX_CONFIG_SECTION_BYTES}.")

    from core.utils import load_config, save_config
    try:
        cfg = load_config()
    except FileNotFoundError:
        cfg = {}

    # Preserve _description / _notes keys in dict sections
    old = cfg.get(section, {})
    new_val = body.get("value", body)
    if isinstance(old, dict) and isinstance(new_val, dict):
        # Merge: keep _-prefixed keys from the old, take everything else
        # from the new
        merged = {k: v for k, v in old.items() if k.startswith("_")}
        merged.update(new_val)
        cfg[section] = merged
    else:
        cfg[section] = new_val

    # Validate the new section value is JSON-serializable
    try:
        json.dumps(cfg[section])
    except (TypeError, ValueError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Section value is not valid JSON: {e}")

    try:
        save_config(cfg)
    except Exception as e:
        logger.error(f"[F12] Failed to save config: {e}")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"Could not save config: {e}")
    logger.info(f"[F12] Admin {user['user_id']} updated section '{section}'")
    return {"ok": True, "section": section}


@app.get("/api/config/whoami")
def config_whoami(user: dict = Depends(auth.resolve_user)):
    """Whether the current user is an admin (F12). Used by the UI to
    show or hide the Admin tab."""
    return {"is_admin": users.is_admin(user["user_id"])}


@app.post("/api/templates")
def save_template(body: dict, user: dict = Depends(auth.resolve_user)):
    """Save a config template (model_overrides + source_overrides + limit)."""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name is required")
    users.save_template(user["user_id"], name, body)
    return {"ok": True}


@app.delete("/api/templates/{name}")
def delete_template(name: str, user: dict = Depends(auth.resolve_user)):
    users.delete_template(user["user_id"], name)
    return {"ok": True}


@app.post("/api/runs/{run_id}/stop")
def stop_run(run_id: str, user: dict = Depends(auth.resolve_user)):
    """
    Stop a run mid-flight — typically because it is using the wrong model.

    Returns immediately. If a worker is mid-step it unwinds at its next
    checkpoint, usually the next model call, and the interrupted step is
    discarded so a resume restarts it cleanly.
    """
    auth.require_run_access(user, run_id)
    try:
        result = pipeline.request_cancel(run_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return {**result, "state": pipeline.get_state(run_id)}


@app.post("/api/runs/{run_id}/stop/force")
def force_stop_run(run_id: str, user: dict = Depends(auth.resolve_user)):
    """
    Stop a run now, without waiting for the worker to agree.

    The polled status enforces the deadline automatically; this is the manual
    equivalent for someone who does not want to wait it out.
    """
    auth.require_run_access(user, run_id)
    state = pipeline.get_state(run_id)
    if not state.get("exists"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")

    if state["status"] != "cancelling":
        pipeline.request_cancel(run_id)
    running = [s for s in pipeline.get_steps(run_id) if s["status"] == "running"]
    pipeline.finish_cancel(run_id, running[0]["step_name"] if running else None)

    for job in jobs.jobs_for_run(run_id):
        if job.get("status") in ("queued", "running"):
            jobs.finish(job["job_id"], "done", error="stopped by request (forced)")

    return {"run_id": run_id, "forced": True, "state": pipeline.get_state(run_id)}


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str, body: ResumeRequest,
               user: dict = Depends(auth.resolve_user)):
    """
    Restart a stopped run, optionally with different models.

    The interrupted step runs again from the beginning with the new routing;
    steps completed before the stop are kept.
    """
    auth.require_run_access(user, run_id)

    state = pipeline.get_state(run_id)
    if not state.get("exists"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if state["status"] == "cancelling":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This run is still stopping — wait for it to come to rest, "
            "then resume.")

    if body.models:
        # Provider-level roles, so every agent picks up the change
        users.set_models(user["user_id"], body.provider, body.models)

    pipeline.resume(run_id, body.model_overrides)
    job_id = jobs.enqueue(run_id)
    return {"job_id": job_id, "state": pipeline.get_state(run_id)}


# ---------------------------------------------------------------------------
# Breaks
# ---------------------------------------------------------------------------

@app.get("/api/runs/{run_id}/break/{break_num}")
def get_break(run_id: str, break_num: int,
              user: dict = Depends(auth.resolve_user)):
    """Structured content for a break — what the UI renders as widgets."""
    auth.require_run_access(user, run_id)
    if break_num not in (0, 1, 2):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such break")

    payload = pipeline.break_payload(run_id, break_num, load_config())
    state = pipeline.get_state(run_id)
    payload["is_current"] = state["awaiting_break"] == break_num
    payload.pop("document", None)   # a server path is meaningless to a browser
    return payload


@app.post("/api/runs/{run_id}/break/0/preview")
def preview_break0_theme(run_id: str, theme: str,
                         user: dict = Depends(auth.resolve_user)):
    """
    Break 0 theme preview (F1).

    Fires a single OpenAlex + Semantic Scholar query (3 results each) for the
    given theme so the researcher can confirm coverage before the full Social /
    Grounder run commits to it. Cheap: 2 API calls, no DB writes.

    Query param: ?theme=<theme_id>
    """
    auth.require_run_access(user, run_id)
    if not theme:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Missing required query param: theme")

    config = load_config()
    all_themes = config.get("themes", [])
    theme_obj = next((t for t in all_themes
                      if t.get("theme_id") == theme), None)
    if not theme_obj:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown theme_id: {theme}",
        )

    from agents import social
    # Use the run_id so the rate limiter coordinates with any in-flight work,
    # but the preview never writes to the sources table.
    run = db.get_run(run_id) or {}
    config = pipeline.apply_source_overrides(run_id, load_config())
    return social.preview_theme(theme_obj, run_id=run_id,
                                problem=run.get("problem", ""), config=config)


@app.post("/api/runs/{run_id}/break/{break_num}")
def submit_break(run_id: str, break_num: int, body: BreakSubmission,
                 user: dict = Depends(auth.resolve_user)):
    """
    Answer a break and release the run.

    Structured directives from the UI widgets and free text are combined into
    the same directive language the CLI uses, so both paths are identical
    downstream.
    """
    auth.require_run_access(user, run_id)
    if break_num not in (0, 1, 2):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such break")

    state = pipeline.get_state(run_id)
    if state["awaiting_break"] != break_num:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This run is not waiting on break {break_num} "
            f"(currently: {state['current_step']}). Re-run the break step to "
            f"answer it again.",
        )

    combined = "\n".join([d for d in body.directives if d.strip()]
                         + ([body.instructions] if body.instructions.strip() else []))

    # A break is the safe point to change models for the agents still to come
    if body.model_overrides:
        _store_model_overrides(run_id, body.model_overrides)
    if body.source_overrides:
        pipeline.set_source_overrides(run_id, body.source_overrides)

    result = pipeline.submit_break(run_id, break_num, combined, source="web")
    job_id = jobs.enqueue(run_id)
    return {**result, "job_id": job_id, "state": pipeline.get_state(run_id)}


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

@app.get("/api/steps")
def list_steps():
    """
    The pipeline's shape, and which external services each step uses.

    Lets the UI say what a step will talk to before it runs, and show live
    activity against that list while it does.
    """
    from core import progress
    config = load_config()
    agent_sources = config.get("agent_sources", {})

    out = []
    for step in pipeline.STEP_DEFS:
        services = [s for s in agent_sources.get(step.name, [])
                    if isinstance(s, str) and not s.startswith("_")]
        if step.kind == "agent" and not services:
            services = ["llm"]          # reasoning-only agents still call a model
        elif step.name == "concept_mapper":
            services = ["conceptnet", "llm"]
        out.append({
            "name": step.name, "kind": step.kind, "label": step.label,
            "break_num": step.break_num,
            "services": [{"id": s, "label": progress.label_for(s)} for s in services],
        })
    return {"steps": out}


@app.get("/api/runs/{run_id}/steps/{step_name}/impact")
def rerun_impact(run_id: str, step_name: str,
                 user: dict = Depends(auth.resolve_user)):
    """
    What a re-run would discard.

    The UI shows this before asking for confirmation — re-running Grounder
    throws away the entire run below it.
    """
    auth.require_run_access(user, run_id)
    if step_name not in pipeline.STEP_BY_NAME:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such step")

    state = pipeline.get_state(run_id)
    completed = {s["step_name"] for s in state["steps"]
                 if s["status"] in ("done", "skipped")}
    affected = [step_name] + pipeline.downstream_steps(step_name)
    return {
        "step": step_name,
        "cascade": [
            {"name": n, "label": pipeline.STEP_BY_NAME[n].label,
             "will_discard": n in completed}
            for n in affected
        ],
        "discard_count": sum(1 for n in affected if n in completed),
    }


@app.post("/api/runs/{run_id}/steps/{step_name}/rerun")
def rerun_step(run_id: str, step_name: str, body: RerunRequest,
               user: dict = Depends(auth.resolve_user)):
    """Re-run a step, discarding it and (by default) everything downstream."""
    auth.require_run_access(user, run_id)
    if step_name not in pipeline.STEP_BY_NAME:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such step")

    reset = pipeline.reset_step(run_id, step_name, cascade=body.cascade)
    job_id = jobs.enqueue(run_id)
    return {"reset": reset, "job_id": job_id,
            "state": pipeline.get_state(run_id)}


# ---------------------------------------------------------------------------
# Branch a run (F11) — clone state up to a chosen step into a new run
# ---------------------------------------------------------------------------

class BranchRequest(BaseModel):
    branch_after_step: str = Field(..., description="The last step to clone")
    new_problem: str = Field("", description="Optional new problem statement")
    previous_run_id: str = Field("", description="Optional previous run for F5 dedup")


@app.post("/api/runs/{run_id}/branch")
def branch_run(run_id: str, body: BranchRequest,
               user: dict = Depends(auth.resolve_user)):
    """
    Branch a run — clone its state up to a chosen step into a new run (F11).

    The new run preserves the original's sources, tree, gaps, and break
    instructions for the cloned prefix, then starts fresh from the next
    step. The original run is untouched.
    """
    auth.require_run_access(user, run_id)
    if body.branch_after_step not in pipeline.STEP_BY_NAME:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such step")

    try:
        new_run_id = pipeline.branch_run(
            source_run_id=run_id,
            branch_after_step=body.branch_after_step,
            new_problem=body.new_problem or None,
            previous_run_id=body.previous_run_id or None,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    # Claim the new run for the same user
    users.claim_run(new_run_id, user["user_id"])
    return {
        "new_run_id": new_run_id,
        "state": pipeline.get_state(new_run_id),
    }


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

@app.get("/api/runs/{run_id}/artifacts")
def list_artifacts(run_id: str, user: dict = Depends(auth.resolve_user)):
    auth.require_run_access(user, run_id)
    return {"artifacts": [
        {"artifact_id": a.get("artifact_id"), "output_type": a.get("output_type"),
         "title": a.get("title"), "audience": a.get("audience"),
         "word_count": a.get("word_count"), "format": a.get("format"),
         "date_produced": a.get("date_produced")}
        for a in db.get_artifacts(run_id)
    ]}


@app.get("/api/runs/{run_id}/artifacts/{artifact_id}")
def get_artifact(run_id: str, artifact_id: str,
                 user: dict = Depends(auth.resolve_user)):
    auth.require_run_access(user, run_id)

    artifact = next((a for a in db.get_artifacts(run_id)
                     if a.get("artifact_id") == artifact_id), None)
    if not artifact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")

    content = ""
    path = artifact.get("file_path") or ""
    if path:
        # Confine reads to the artifacts directory — the stored path must not
        # be able to address anything else on disk.
        artifacts_root = (Path(__file__).parent.parent / "artifacts").resolve()
        try:
            resolved = Path(path).resolve()
            resolved.relative_to(artifacts_root)
            content = resolved.read_text()
        except (ValueError, OSError) as e:
            logger.warning(f"Refusing to read artifact path {path}: {e}")
            content = ""

    return {**artifact, "content": content}


# Per-step artifact files on disk (F6). Every agent writes a markdown doc
# to artifacts/{run_id}_{step}.md; this endpoint lists them so the UI can
# show them alongside Scribe's curated artifacts.
_STEP_LABELS = {
    "grounder_foundations":   "Grounder — Foundations",
    "historian_map":          "Historian — Chronological map",
    "theorist_proposals":     "Theorist — Proposals",
    "thinker_directions":     "Thinker — New directions",
    "synthesizer_narrative":  "Synthesizer — Narrative",
    "rude_evaluations":       "Rude — Evaluations",
    "vision_implications":    "Vision — Implications",
    "gaper_gaps":             "Gaper — Gap analysis",
    "understanding_map":      "Scribe — Understanding Map",
    "blog_post":              "Scribe — Blog post",
    "break0_review":          "Break 0 — Review",
    "break1_review":          "Break 1 — Review",
    "break2_review":          "Break 2 — Review",
    "verbose_log":            "Verbose log",
}


@app.get("/api/runs/{run_id}/step-artifacts")
def list_step_artifacts(run_id: str, user: dict = Depends(auth.resolve_user)):
    """
    All markdown files in artifacts/ matching {run_id}_*.md (F6).

    Returns:
      {
        "run_id": str,
        "files": [
          { "filename": str, "step_key": str, "label": str,
            "size": int, "modified": str }
        ]
      }
    """
    auth.require_run_access(user, run_id)
    artifacts_root = (Path(__file__).parent.parent / "artifacts").resolve()
    files = []
    if artifacts_root.exists():
        for path in sorted(artifacts_root.glob(f"{run_id}_*.md")):
            try:
                resolved = path.resolve()
                resolved.relative_to(artifacts_root)  # safety
            except ValueError:
                continue
            # Extract step key: {run_id}_{step_key}.md
            stem = path.stem  # filename without .md
            step_key = stem[len(run_id) + 1:] if stem.startswith(run_id + "_") else stem
            files.append({
                "filename":  path.name,
                "step_key":  step_key,
                "label":     _STEP_LABELS.get(step_key, step_key.replace("_", " ").title()),
                "size":      path.stat().st_size,
                "modified":  datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
            })
    return {"run_id": run_id, "files": files}


@app.get("/api/runs/{run_id}/step-artifacts/{filename}")
def get_step_artifact(run_id: str, filename: str,
                      user: dict = Depends(auth.resolve_user)):
    """Return the contents of one per-step artifact file (F6)."""
    auth.require_run_access(user, run_id)
    # Confine reads to artifacts/{run_id}_*.md — never anything else.
    if not filename.endswith(".md") or not filename.startswith(f"{run_id}_"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid filename")
    artifacts_root = (Path(__file__).parent.parent / "artifacts").resolve()
    target = (artifacts_root / filename).resolve()
    try:
        target.relative_to(artifacts_root)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(f"Could not read {target}: {e}")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Read failed")
    return {
        "run_id":   run_id,
        "filename": filename,
        "step_key": filename[len(run_id) + 1:-len(".md")],
        "content":  content,
        "size":     target.stat().st_size,
    }


# ---------------------------------------------------------------------------
# Argument tree (F9)
# ---------------------------------------------------------------------------

@app.get("/api/runs/{run_id}/tree")
def get_argument_tree(run_id: str, user: dict = Depends(auth.resolve_user)):
    """
    The run's argument tree as a nested JSON structure.

    Returns:
      {
        "run_id": str,
        "tree":   { ...root node with nested children... } | null,
        "stats":  { total_nodes, by_type, claim_statuses, unique_sources },
        "sources": { source_id: {title, authors, year, source_name} },
      }

    Each node has: node_id, node_type, content, status, confidence,
    source_ids (list), agent_origin, metadata (dict), children (list).
    """
    auth.require_run_access(user, run_id)

    from core.argument_tree import TreeBuilder
    tree = TreeBuilder(run_id)
    nested = tree.get_tree()
    stats = tree.get_stats()

    # Attach source metadata so the UI can show what each evidence node
    # points at without a second round-trip.
    source_ids = tree.get_all_source_ids()
    sources_map: dict[str, dict] = {}
    if source_ids:
        all_sources = db.get_sources_by_type("current", run_id=run_id) + \
                      db.get_sources_by_type("seminal", run_id=run_id) + \
                      db.get_sources_by_type("historical", run_id=run_id)
        for s in all_sources:
            sid = s.get("source_id") if isinstance(s, dict) else s["source_id"]
            if sid in source_ids and sid not in sources_map:
                get = (lambda k, _s=s: _s[k] if k in _s.keys() else None) \
                      if not isinstance(s, dict) else (lambda k, _s=s: _s.get(k))
                sources_map[sid] = {
                    "title":       get("title") or "",
                    "authors":     get("authors") or "",
                    "year":        get("year"),
                    "source_name": get("source_name") or "",
                    "doi":         get("doi") or "",
                    "active_link": get("active_link") or "",
                }

    return {
        "run_id":  run_id,
        "tree":    nested,
        "stats":   stats,
        "sources": sources_map,
    }


# ---------------------------------------------------------------------------
# LLM usage tracking (F10)
# ---------------------------------------------------------------------------

@app.get("/api/runs/{run_id}/usage")
def get_llm_usage(run_id: str, user: dict = Depends(auth.resolve_user)):
    """
    Per-agent and per-model token usage for a run.

    Returns:
      {
        "total_tokens": int,
        "total_prompt": int,
        "total_completion": int,
        "total_calls": int,
        "by_agent": { "<agent>": {calls, prompt, completion, total} },
        "by_model":  { "<provider:model>": {calls, prompt, completion, total} },
      }
    """
    auth.require_run_access(user, run_id)
    return db.get_llm_usage_summary(run_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store_model_overrides(run_id: str, overrides: dict) -> dict:
    """
    Record per-agent model choices for a run.

    Persisted, not just held in memory: the worker that runs the agents is a
    different process from this one.
    """
    return pipeline.set_model_overrides(run_id, overrides)


# ---------------------------------------------------------------------------
# Frontend (M5) — served from the same origin, so no CORS in the common case
# ---------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        page = STATIC_DIR / "index.html"
        if page.exists():
            return FileResponse(str(page))
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Frontend not built")

    @app.get("/guide")
    def guide():
        page = STATIC_DIR / "index.html"
        if page.exists():
            return FileResponse(str(page))
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Frontend not built")
