FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Install nanobot + router deps together so uv resolves a single venv
COPY pyproject.toml .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system nanobot-ai[api] && \
    uv pip install --system .

# ---- Runtime image ----
FROM python:3.12-slim-bookworm

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=builder /usr/local/bin/nanobot    /usr/local/bin/nanobot
COPY --from=builder /usr/local/bin/uvicorn    /usr/local/bin/uvicorn

COPY router/ ./router/

# Workspace volume — mount a PVC here in k8s (ReadWriteMany)
VOLUME ["/data/workspaces"]

# Shared nanobot config (providers, model, etc.) — mount as ConfigMap/Secret
VOLUME ["/config"]

EXPOSE 8080

ENV ROUTER_WORKSPACE_BASE=/data/workspaces \
    ROUTER_NANOBOT_CONFIG_PATH=/config/nanobot.json \
    ROUTER_NANOBOT_BIN=nanobot \
    ROUTER_PORT_RANGE_START=9000 \
    ROUTER_PORT_RANGE_END=9200 \
    ROUTER_IDLE_TIMEOUT_SECONDS=300

CMD ["uvicorn", "router.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
