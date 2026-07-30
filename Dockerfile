# SEEKER — one image, two roles (web and worker), selected by the command.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:$PATH"

# Keep uv pinned so the lockfile is interpreted consistently across builds.
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/

WORKDIR /app

# curl is used by the container healthcheck; nothing else needs building.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Dependencies first, so code edits do not invalidate the layer. SEEKER runs
# directly from /app, so only third-party packages belong in the environment.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project \
      --extra web --extra mysql --extra consensus

RUN useradd --create-home --uid 1000 seeker

COPY --chown=seeker:seeker . .

# Runtime directories the app writes to. db/ is bind-mounted at run time —
# conceptnet.db is 184 MB uncompressed and does not belong in the image.
RUN mkdir -p artifacts logs db exports \
 && chown -R seeker:seeker artifacts logs db exports
USER seeker

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000"]
