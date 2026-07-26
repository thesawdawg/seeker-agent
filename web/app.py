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

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.keys import _load_env
_load_env()

from core import breaks, database as db, jobs, llm, pipeline, users
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

class LoginRequest(BaseModel):
    base_url: str = Field(..., description="Provider base URL, e.g. http://localhost:3000/api")
    api_key: str
    provider: str = "open-webui"
    display_name: str = ""
    models: dict = Field(default_factory=dict)


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
# Auth
# ---------------------------------------------------------------------------

@app.post("/api/auth/login")
def login(body: LoginRequest, response: Response):
    """Validate a provider key, identify the user, and start a session."""
    result = auth.login(body.base_url, body.api_key, body.provider,
                        body.display_name, body.models)
    response.set_cookie(
        "seeker_session", result["session"],
        max_age=result["expires_in"], httponly=True, samesite="lax",
    )
    return result


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
        "credentials":  users.list_credentials(user["user_id"]),
    }


# ---------------------------------------------------------------------------
# Provider credentials and models
# ---------------------------------------------------------------------------

@app.get("/api/credentials")
def list_credentials(user: dict = Depends(auth.resolve_user)):
    return {"credentials": users.list_credentials(user["user_id"])}


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
def list_agents():
    """Agent names and default routing, so the UI can offer per-agent models."""
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
def list_runs(user: dict = Depends(auth.resolve_user)):
    """The caller's runs, newest first.

    Uses get_run_summary (review O6) instead of get_state to avoid building
    full step-state objects for every run when only progress and
    awaiting_break are needed.
    """
    out = []
    for run_id in users.runs_for_user(user["user_id"]):
        run = db.get_run(run_id)
        if not run:
            continue
        summary = pipeline.get_run_summary(run_id)
        out.append({
            "run_id":         run_id,
            "problem":        run.get("problem", ""),
            "status":         run.get("status", ""),
            "created_at":     run.get("created_at"),
            "progress":       summary["progress"],
            "awaiting_break": summary["awaiting_break"],
        })
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"runs": out}


@app.post("/api/runs", status_code=status.HTTP_201_CREATED)
def create_run(body: CreateRunRequest, user: dict = Depends(auth.resolve_user)):
    """Start a run. Returns immediately; a worker picks it up."""
    cfg = users.provider_config(user["user_id"], body.provider)
    if not cfg or not cfg.configured:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"No credentials stored for provider '{body.provider}'. "
            f"PUT /api/credentials first.",
        )

    run_id = pipeline.create_run(body.problem)
    users.claim_run(run_id, user["user_id"], body.provider)
    if body.model_overrides:
        _store_model_overrides(run_id, body.model_overrides)
    if body.source_overrides:
        pipeline.set_source_overrides(run_id, body.source_overrides)

    job_id = jobs.enqueue(run_id)
    logger.info(f"Run {run_id} created by {user['user_id']} (job {job_id})")
    return {"run_id": run_id, "job_id": job_id,
            "state": pipeline.get_state(run_id)}


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
def run_status(run_id: str, user: dict = Depends(auth.resolve_user)):
    """
    The polling endpoint. Deliberately small — the frontend hits this every
    couple of seconds, so it returns step state and nothing heavy.
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
    }


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
    return {
        "run_id": run_id,
        "health": health,
        "inserted": inserted,
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
        if source_id in keyless:
            has_key = True
            note = "keyless"
        elif env_var:
            env_set = bool(os.environ.get(env_var, "").strip())
            stored = bool(users.get_source_api_key(user["user_id"], source_id))
            has_key = env_set or stored
            note = "env" if env_set else ("stored" if stored else "no key — will skip")
        else:
            note = "no key check defined"
        exhausted_until = db.source_exhausted_until(source_id, user["user_id"])
        out.append({
            "source_id": source_id,
            "enabled": enabled,
            "has_key": has_key,
            "note": note,
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
