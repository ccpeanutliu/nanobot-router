"""Nanobot multi-user router gateway.

Receives requests, validates SSO token → user_id, starts/reuses a per-user
nanobot instance, then proxies the request through.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from .auth import resolve_user_id
from .config import settings
from .process_manager import ProcessManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

process_manager = ProcessManager()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await process_manager.start_reaper()
    logger.info(
        "Router started — ports %d-%d, idle timeout %ds",
        settings.port_range_start,
        settings.port_range_end - 1,
        settings.idle_timeout_seconds,
    )
    yield
    logger.info("Router shutting down, stopping all instances...")
    await process_manager.shutdown()


app = FastAPI(title="nanobot-router", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------

# Headers that must not be forwarded upstream or downstream
_HOP_BY_HOP = frozenset([
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "host",  # httpx sets this automatically
])


def _forward_headers(request: Request) -> dict[str, str]:
    """Build headers to forward, stripping hop-by-hop and auth."""
    skip = _HOP_BY_HOP | {settings.auth_header.lower()}
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in skip
    }


async def _proxy(user_id: str, request: Request) -> Response:
    """Get/start the user's nanobot instance and proxy the request."""
    try:
        inst = await process_manager.get_or_start(user_id)
    except RuntimeError as e:
        return Response(content=str(e), status_code=503)
    except TimeoutError as e:
        return Response(content=str(e), status_code=503)

    target_url = f"{inst.base_url}{request.url.path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    headers = _forward_headers(request)
    body = await request.body()

    # Detect SSE streaming request
    is_stream = False
    if request.method == "POST" and body:
        try:
            import json
            is_stream = json.loads(body).get("stream", False)
        except Exception:
            pass

    if is_stream:
        async def _stream_generator():
            try:
                # Force identity encoding so nanobot doesn't gzip the SSE stream.
                # httpx adds its own Accept-Encoding by default, which would cause
                # nanobot's aiohttp to compress and buffer chunks before sending.
                stream_headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
                stream_headers["accept-encoding"] = "identity"
                async with httpx.AsyncClient(timeout=settings.proxy_timeout_seconds) as client:
                    async with client.stream(
                        request.method, target_url, headers=stream_headers, content=body,
                    ) as upstream:
                        async for chunk in upstream.aiter_bytes():
                            yield chunk
            except httpx.ConnectError:
                await process_manager.stop(user_id)
            finally:
                process_manager.touch(user_id)

        return StreamingResponse(
            _stream_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    try:
        async with httpx.AsyncClient(timeout=settings.proxy_timeout_seconds) as client:
            upstream = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
    except httpx.ConnectError:
        await process_manager.stop(user_id)
        return Response(content="nanobot instance unavailable, please retry", status_code=503)
    except httpx.TimeoutException:
        return Response(content="Request timed out", status_code=504)

    process_manager.touch(user_id)

    response_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/admin/stats")
async def admin_stats():
    """Internal endpoint: active instances and port usage."""
    return process_manager.stats()


@app.post("/admin/stop/{user_id}")
async def admin_stop(user_id: str):
    """Manually stop a user's instance (workspace preserved)."""
    await process_manager.stop(user_id)
    return {"stopped": user_id}


# Proxy all nanobot API paths — auth required
@app.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_v1(path: str, request: Request):
    user_id = await resolve_user_id(request)
    return await _proxy(user_id, request)


# SSE streaming proxy (nanobot /v1/chat/completions with stream=true)
# The generic proxy above handles it, but we provide a dedicated streaming
# variant that avoids buffering the entire response body.
@app.post("/v1/chat/completions/stream")
async def proxy_stream(request: Request):
    """Alias that explicitly streams the response for SSE clients."""
    user_id = await resolve_user_id(request)

    try:
        inst = await process_manager.get_or_start(user_id)
    except (RuntimeError, TimeoutError) as e:
        return Response(content=str(e), status_code=503)

    target_url = f"{inst.base_url}/v1/chat/completions"
    headers = _forward_headers(request)
    headers["accept-encoding"] = "identity"
    body = await request.body()

    async def _stream():
        try:
            async with httpx.AsyncClient(timeout=settings.proxy_timeout_seconds) as client:
                async with client.stream(
                    "POST",
                    target_url,
                    headers=headers,
                    content=body,
                ) as upstream:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
        except Exception:
            logger.exception("Stream proxy error for user=%s", user_id)
        finally:
            process_manager.touch(user_id)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
