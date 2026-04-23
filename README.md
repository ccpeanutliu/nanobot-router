# nanobot-router

A multi-user routing gateway for [nanobot](https://github.com/HKUDS/nanobot).

Each user gets their own isolated nanobot process and workspace. Processes are started on demand, killed after idle timeout, and restarted automatically on the next request — with workspace (memory, session history) fully preserved across restarts.

## How it works

```
Client (SSO token)
    │
    ▼
┌──────────────────────────────────┐
│  nanobot-router  (FastAPI :8080) │
│                                  │
│  1. Validate SSO token → user_id │
│  2. Start nanobot if not running │
│  3. Proxy request to user's port │
└──────────────────────────────────┘
         │          │          │
     port 9000  port 9001  port 9002  ...
         │
    /data/workspaces/user-a/   ← persisted, never deleted
    /data/workspaces/user-b/
```

- Supports up to **200 concurrent users** (one port per user, configurable)
- Idle processes are killed after `ROUTER_IDLE_TIMEOUT_SECONDS` (default 5 min)
- Workspaces survive process restarts — memory and history are always available

## Requirements

- Python 3.11+
- [nanobot](https://github.com/ccpeanutliu/nanobot) (included as git submodule)

## Quick start

```bash
git clone --recurse-submodules git@github.com:ccpeanutliu/nanobot-router.git
cd nanobot-router

cp .env.example .env
# Edit .env — set ROUTER_NANOBOT_CONFIG_PATH to your nanobot config

uv sync
uvicorn router.main:app --host 0.0.0.0 --port 8080
```

Send a request (dev mode uses `X-User-Id` header):

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "X-User-Id: alice" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}]}'
```

## Configuration

All settings are via environment variables (prefix `ROUTER_`). Copy `.env.example` to `.env` to get started.

| Variable | Default | Description |
|---|---|---|
| `ROUTER_AUTH_MODE` | `jwt` | `jwt` / `introspection` / `disabled` |
| `ROUTER_JWT_JWKS_URL` | — | JWKS endpoint for JWT validation |
| `ROUTER_JWT_AUDIENCE` | — | Expected `aud` claim |
| `ROUTER_JWT_USER_ID_CLAIM` | `sub` | JWT claim used as user ID |
| `ROUTER_INTROSPECTION_URL` | — | OAuth token introspection endpoint |
| `ROUTER_NANOBOT_BIN` | `nanobot` | Path to nanobot binary |
| `ROUTER_NANOBOT_CONFIG_PATH` | `/config/nanobot.json` | Shared nanobot config (providers, model, etc.) |
| `ROUTER_WORKSPACE_BASE` | `/data/workspaces` | Root directory for per-user workspaces |
| `ROUTER_PORT_RANGE_START` | `9000` | Start of port pool |
| `ROUTER_PORT_RANGE_END` | `9200` | End of port pool (exclusive) |
| `ROUTER_IDLE_TIMEOUT_SECONDS` | `300` | Kill process after N seconds idle |
| `ROUTER_STARTUP_TIMEOUT_SECONDS` | `30` | Max wait for nanobot `/health` |
| `ROUTER_PROXY_TIMEOUT_SECONDS` | `120.0` | Per-request proxy timeout |

### Auth modes

**`jwt`** — Validate token locally. Set `ROUTER_JWT_JWKS_URL` for RS256 (production) or `ROUTER_JWT_SECRET` for HS256 (testing).

**`introspection`** — POST token to `ROUTER_INTROSPECTION_URL`, read `sub` from response.

**`disabled`** — Use `X-User-Id` request header directly. Dev/testing only.

## Docker

```bash
docker build -t nanobot-router .

docker run \
  -e ROUTER_AUTH_MODE=disabled \
  -v ~/.nanobot/config.json:/config/nanobot.json:ro \
  -v $(pwd)/workspaces:/data/workspaces \
  -p 8080:8080 \
  nanobot-router
```

## Kubernetes

See [`k8s/`](k8s/) for Deployment, Service, Ingress, and PVC manifests.

```bash
# Create namespace and apply manifests
kubectl create namespace nanobot
kubectl apply -f k8s/pvc.yaml
kubectl apply -f k8s/deployment.yaml
```

Key points:
- **`replicas: 1`** — process state is in-memory, not shared across pods
- Workspace PVC requires `ReadWriteMany` (NFS, CephFS, AWS EFS, etc.)
- Set `ROUTER_JWT_JWKS_URL` in the Deployment env to your SSO provider

Clone with submodules in CI/CD:
```bash
git clone --recurse-submodules git@github.com:ccpeanutliu/nanobot-router.git
```

## API

The router exposes the same OpenAI-compatible API as nanobot:

| Endpoint | Description |
|---|---|
| `POST /v1/chat/completions` | Chat (JSON or multipart, streaming supported) |
| `GET /health` | Health check |
| `GET /admin/stats` | Active instances and port usage |
| `POST /admin/stop/{user_id}` | Manually stop a user's instance |
