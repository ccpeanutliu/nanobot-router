"""Router configuration via environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROUTER_", env_file=".env", extra="ignore")

    # --- Nanobot process ---
    nanobot_config_path: str = "/config/nanobot.json"
    nanobot_bin: str = "nanobot"          # path or command name
    workspace_base: str = "/data/workspaces"
    nanobot_bind_host: str = "127.0.0.1"
    port_range_start: int = 9000
    port_range_end: int = 9200           # exclusive → 200 slots

    # --- Lifecycle ---
    idle_timeout_seconds: int = 300       # kill process after 5 min idle
    reap_interval_seconds: int = 60       # how often to check for idle processes
    startup_timeout_seconds: int = 30     # max wait for nanobot /health

    # --- Auth (SSO) ---
    # Mode: "jwt" | "introspection" | "disabled"
    auth_mode: str = "jwt"

    # JWT mode: validate token locally with JWKS or symmetric secret
    jwt_jwks_url: str = ""               # if set, fetch public keys from here
    jwt_secret: str = ""                 # HS256 symmetric secret (dev/fallback)
    jwt_algorithms: str = "RS256,HS256"  # comma-separated list
    jwt_audience: str = ""               # optional aud check
    jwt_user_id_claim: str = "sub"       # claim that contains user ID

    # Introspection mode: POST token to this endpoint, read user_id from response
    introspection_url: str = ""
    introspection_token_field: str = "token"
    introspection_user_id_field: str = "sub"
    introspection_active_field: str = "active"  # must be truthy

    # Header to read the raw SSO token from
    auth_header: str = "Authorization"   # Bearer <token>

    # --- Proxy ---
    proxy_timeout_seconds: float = 120.0  # forwarded to nanobot as well

    @property
    def jwt_algorithm_list(self) -> list[str]:
        return [a.strip() for a in self.jwt_algorithms.split(",") if a.strip()]


settings = Settings()
