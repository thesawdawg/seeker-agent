# SEEKER — one image, two roles (web and worker), selected by the command.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is used by the container healthcheck; nothing else needs building.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Dependencies first, so code edits do not invalidate the layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runtime directories the app writes to. db/ is bind-mounted at run time —
# conceptnet.db is 184 MB uncompressed and does not belong in the image.
RUN mkdir -p artifacts logs db exports \
 && useradd --create-home --uid 1000 seeker \
 && chown -R seeker:seeker /app
USER seeker

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000"]
