# MCPO + Primo MCP Server
#
# Builds an image that runs mcpo (MCP-to-OpenAPI proxy) wrapping the
# Primo MCP server, exposing Primo library catalog search as REST endpoints.
#
# The resulting container serves OpenAPI-compatible endpoints at :8000
# that the seeker-agent pipeline can call to search a library catalog.

FROM python:3.11-slim

WORKDIR /app

# Install mcpo, fastmcp, and httpx
RUN pip install --no-cache-dir \
    mcpo \
    "fastmcp>=2.0" \
    httpx

# Copy the Primo MCP server
COPY primo_server.py /app/primo_server.py

# mcpo listens on 8000 by default and proxies to the MCP server via stdio
# The Primo server gets its config from env vars (PRIMO_API_KEY, PRIMO_VID, etc.)
EXPOSE 8000

# mcpo wraps the primo server; the primo server runs in stdio mode
# API key for mcpo itself defaults to "seeker-mcpo" — override with MCPO_API_KEY
CMD ["mcpo", "--port", "8000", "--api-key", "seeker-mcpo", \
     "--", "python3", "/app/primo_server.py", "--transport", "stdio"]
