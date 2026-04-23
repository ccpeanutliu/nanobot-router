FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

COPY pyproject.toml uv.lock .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---- Runtime image ----
FROM python:3.12-slim-bookworm

WORKDIR /app

# Copy the virtualenv from builder (contains nanobot + router deps)
COPY --from=builder /app/.venv /app/.venv

COPY router/ ./router/

ENV PATH="/app/.venv/bin:$PATH"

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
