# ── Build stage: compile wheels with gcc, which is not shipped in the final image ──
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


# ── Final stage: runtime only (no gcc), runs as a non-root user ──
FROM python:3.11-slim

WORKDIR /app

# Runtime deps only — ffmpeg + fonts. gcc is intentionally absent here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    fonts-liberation \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY . .

# Create the data dir and a non-root user that owns the app + data.
# NOTE: bind-mounted volumes (/data, /media/*) keep the *host* directory's
# ownership, which overrides the chown below. For writes to succeed the host
# must allow uid 10001 (e.g. `chown -R 10001 /docker/appdata/tentacle/data`)
# or the compose file should set `user: "<uid>:<gid>"` to match the host owner.
RUN mkdir -p /data/smartlists \
    && useradd --create-home --uid 10001 tentacle \
    && chown -R tentacle:tentacle /app /data

USER tentacle

EXPOSE 8888

# Liveness/readiness check against the lightweight health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8888/api/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8888", "--workers", "1"]
