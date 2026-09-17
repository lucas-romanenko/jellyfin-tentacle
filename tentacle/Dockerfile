# ── Build stage: compile wheels with gcc, which is not shipped in the final image ──
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


# ── Final stage: runtime only (no gcc) ──
FROM python:3.11-slim

WORKDIR /app

# Runtime deps only — ffmpeg + fonts. gcc is intentionally absent here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    fonts-liberation \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno powers yt-dlp's JavaScript challenges for YouTube. Optional: without it
# the YouTube source still resolves most videos (the visionos player client
# needs no JS runtime), but some fall back or fail, so it is worth the ~40MB.
ARG DENO_VERSION=v2.9.6
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) deno_arch="x86_64-unknown-linux-gnu" ;; \
      arm64) deno_arch="aarch64-unknown-linux-gnu" ;; \
      *) echo "No deno build for $arch — YouTube JS challenges unavailable"; deno_arch="" ;; \
    esac; \
    if [ -n "$deno_arch" ]; then \
      curl -fsSL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/download/${DENO_VERSION}/deno-${deno_arch}.zip"; \
      unzip -q /tmp/deno.zip -d /usr/local/bin; \
      chmod +x /usr/local/bin/deno; \
      rm /tmp/deno.zip; \
      deno --version; \
    fi

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY . .

# Ensure the data dir exists. Runs as root so writes to bind-mounted /data and
# /media/* volumes work without the operator having to chown host directories —
# important for an easy-to-deploy, freely-distributed app.
RUN mkdir -p /data/smartlists

EXPOSE 8888

# Liveness/readiness check against the lightweight health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8888/api/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8888", "--workers", "1"]
