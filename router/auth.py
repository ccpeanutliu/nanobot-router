"""SSO token → user_id resolution.

Supports three modes (configured via ROUTER_AUTH_MODE):
  - "jwt"           : validate JWT locally (JWKS or symmetric secret)
  - "introspection" : POST token to an OAuth introspection endpoint
  - "disabled"      : pass token value as-is for user_id (dev / testing)
"""

from __future__ import annotations

import logging
from functools import lru_cache

import httpx
from fastapi import HTTPException, Request

from .config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JWKS key cache
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_jwks_client():
    """Return a PyJWT JWKSClient (cached)."""
    import jwt  # PyJWT

    return jwt.PyJWKClient(settings.jwt_jwks_url, cache_keys=True)


# ---------------------------------------------------------------------------
# JWT validation
# ---------------------------------------------------------------------------

def _validate_jwt(token: str) -> str:
    """Validate JWT and return user_id claim. Raises HTTPException on failure."""
    import jwt  # PyJWT

    options: dict = {} if settings.jwt_audience else {"verify_aud": False}
    audience = settings.jwt_audience or None

    try:
        if settings.jwt_jwks_url:
            client = _get_jwks_client()
            signing_key = client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=settings.jwt_algorithm_list,
                options=options,
                audience=audience,
            )
        elif settings.jwt_secret:
            payload = jwt.decode(
                token,
                settings.jwt_secret,
                algorithms=settings.jwt_algorithm_list,
                options=options,
                audience=audience,
            )
        else:
            raise HTTPException(status_code=500, detail="JWT auth configured but no secret or JWKS URL set")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    user_id = payload.get(settings.jwt_user_id_claim)
    logger.info(f"get user_id: {user_id}. claim: {settings.jwt_user_id_claim}")
    if not user_id:
        raise HTTPException(status_code=401, detail=f"Token missing '{settings.jwt_user_id_claim}' claim")
    return str(user_id)


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

async def _introspect_token(token: str) -> str:
    """Call OAuth introspection endpoint, return user_id. Raises HTTPException on failure."""
    if not settings.introspection_url:
        raise HTTPException(status_code=500, detail="Introspection URL not configured")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                settings.introspection_url,
                data={settings.introspection_token_field: token},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.error("Introspection request failed: %s", e)
        raise HTTPException(status_code=502, detail="SSO introspection failed")

    if not data.get(settings.introspection_active_field):
        raise HTTPException(status_code=401, detail="Token inactive or expired")

    user_id = data.get(settings.introspection_user_id_field)
    if not user_id:
        raise HTTPException(status_code=401, detail="Introspection response missing user_id")
    return str(user_id)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def resolve_user_id(request: Request) -> str:
    """Extract and validate SSO token from request, return sanitized user_id."""
    if settings.auth_mode == "disabled":
        # Dev mode: use X-User-Id header directly
        user_id = request.headers.get("X-User-Id", "dev-user")
        return _sanitize_user_id(user_id)

    raw = request.headers.get(settings.auth_header, "")
    if not raw:
        raise HTTPException(status_code=401, detail="Missing auth header")

    # Strip "Bearer " prefix if present
    token = raw.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")

    if settings.auth_mode == "jwt":
        user_id = _validate_jwt(token)
    elif settings.auth_mode == "introspection":
        user_id = await _introspect_token(token)
    else:
        raise HTTPException(status_code=500, detail=f"Unknown auth_mode: {settings.auth_mode!r}")

    return _sanitize_user_id(user_id)


def _sanitize_user_id(user_id: str) -> str:
    """Strip characters that are unsafe in filesystem paths."""
    import re
    safe = re.sub(r"[^\w\-@.]", "_", user_id)
    if not safe:
        raise HTTPException(status_code=401, detail="user_id resolved to empty string")
    # Limit length to avoid excessively long directory names
    return safe[:128]
