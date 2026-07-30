# MCPO + Primo MCP Server
#
# Builds an image that runs mcpo (MCP-to-OpenAPI proxy) wrapping the
# Primo MCP server, exposing Primo library catalog search as REST endpoints.
#
# The resulting container serves OpenAPI-compatible endpoints at :8000
# that the seeker-agent pipeline can call to search a library catalog.

FROM python:3.14-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/

# Install the locked bridge dependencies separately from the server source.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Copy the Primo MCP server
COPY primo_server.py /app/primo_server.py

# mcpo listens on 8000 by default and proxies to the MCP server via stdio
# The Primo server gets its config from env vars (PRIMO_API_KEY, PRIMO_VID, etc.)
EXPOSE 8000

# mcpo wraps the primo server; the primo server runs in stdio mode
# API key for mcpo itself defaults to "seeker-mcpo" — override with MCPO_API_KEY
CMD ["mcpo", "--port", "8000", "--api-key", "seeker-mcpo", \
     "--", "python", "/app/primo_server.py", "--transport", "stdio"]
