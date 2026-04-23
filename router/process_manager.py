"""Lifecycle management for per-user nanobot subprocess instances.

Each user gets their own `nanobot serve` process bound to a unique port.
Processes are killed after `idle_timeout_seconds` of inactivity, but their
workspace directories are preserved so memory is available on next login.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NanobotInstance:
    user_id: str
    port: int
    process: asyncio.subprocess.Process
    last_activity: float = field(default_factory=time.monotonic)

    @property
    def base_url(self) -> str:
        return f"http://{settings.nanobot_bind_host}:{self.port}"

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def is_alive(self) -> bool:
        return self.process.returncode is None

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_activity


# ---------------------------------------------------------------------------
# Process manager
# ---------------------------------------------------------------------------

class ProcessManager:
    def __init__(self) -> None:
        self._instances: dict[str, NanobotInstance] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._available_ports: set[int] = set(
            range(settings.port_range_start, settings.port_range_end)
        )
        self._global_lock = asyncio.Lock()
        self._reap_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_or_start(self, user_id: str) -> NanobotInstance:
        """Return the running instance for *user_id*, starting one if needed."""
        # Fast path: instance already running
        inst = self._instances.get(user_id)
        if inst and inst.is_alive():
            inst.touch()
            return inst

        # Slow path: acquire per-user lock to avoid double-start
        user_lock = await self._get_user_lock(user_id)
        async with user_lock:
            # Re-check under lock
            inst = self._instances.get(user_id)
            if inst and inst.is_alive():
                inst.touch()
                return inst

            inst = await self._start(user_id)
            self._instances[user_id] = inst
            return inst

    def touch(self, user_id: str) -> None:
        """Update last-activity timestamp for user_id (call after each request)."""
        if inst := self._instances.get(user_id):
            inst.touch()

    async def stop(self, user_id: str) -> None:
        """Gracefully stop a user's instance (workspace is preserved)."""
        inst = self._instances.pop(user_id, None)
        if inst:
            await self._kill(inst)
            self._available_ports.add(inst.port)

    async def start_reaper(self) -> None:
        """Start background task that kills idle processes periodically."""
        self._reap_task = asyncio.create_task(self._reap_loop(), name="reaper")

    async def shutdown(self) -> None:
        """Gracefully stop all instances (called on app shutdown)."""
        if self._reap_task:
            self._reap_task.cancel()

        user_ids = list(self._instances.keys())
        await asyncio.gather(*(self.stop(uid) for uid in user_ids), return_exceptions=True)

    def stats(self) -> dict:
        instances = [
            {
                "user_id": inst.user_id,
                "port": inst.port,
                "pid": inst.process.pid,
                "idle_seconds": round(inst.idle_seconds()),
                "alive": inst.is_alive(),
            }
            for inst in self._instances.values()
        ]
        return {
            "active_instances": len(instances),
            "available_ports": len(self._available_ports),
            "instances": instances,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        async with self._global_lock:
            if user_id not in self._locks:
                self._locks[user_id] = asyncio.Lock()
            return self._locks[user_id]

    async def _allocate_port(self) -> int:
        async with self._global_lock:
            if not self._available_ports:
                raise RuntimeError("No free ports available — max concurrent users reached")
            return self._available_ports.pop()

    async def _start(self, user_id: str) -> NanobotInstance:
        port = await self._allocate_port()
        workspace = Path(settings.workspace_base) / user_id
        workspace.mkdir(parents=True, exist_ok=True)

        cmd = [
            settings.nanobot_bin,
            "serve",
            "--host", settings.nanobot_bind_host,
            "--port", str(port),
            "--workspace", str(workspace),
            "--config", settings.nanobot_config_path,
            "--timeout", str(int(settings.proxy_timeout_seconds)),
        ]

        log_path = workspace / "router.log"
        log_file = open(log_path, "a")  # noqa: WPS515 — kept open for subprocess lifetime

        logger.info("Starting nanobot for user=%s port=%d workspace=%s", user_id, port, workspace)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=log_file,
            stderr=log_file,
            # Give each process its own process group so SIGTERM doesn't
            # propagate from the router to all children.
            start_new_session=True,
        )

        inst = NanobotInstance(user_id=user_id, port=port, process=process)

        try:
            await self._wait_healthy(inst)
        except Exception:
            await self._kill(inst)
            self._available_ports.add(port)
            raise

        logger.info("nanobot ready for user=%s pid=%d port=%d", user_id, process.pid, port)
        return inst

    async def _wait_healthy(self, inst: NanobotInstance) -> None:
        """Poll /health until nanobot is ready or timeout."""
        deadline = time.monotonic() + settings.startup_timeout_seconds
        url = f"{inst.base_url}/health"

        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if not inst.is_alive():
                    raise RuntimeError(f"nanobot process for {inst.user_id} exited during startup")
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        return
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(0.5)

        raise TimeoutError(
            f"nanobot for {inst.user_id} did not become healthy "
            f"within {settings.startup_timeout_seconds}s"
        )

    @staticmethod
    async def _kill(inst: NanobotInstance) -> None:
        """Terminate process gracefully, then force-kill if needed."""
        if inst.process.returncode is not None:
            return  # already exited

        try:
            # Send SIGTERM to the process group
            os.killpg(os.getpgid(inst.process.pid), 15)  # SIGTERM
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(inst.process.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(inst.process.pid), 9)  # SIGKILL
            except ProcessLookupError:
                pass
            await inst.process.wait()

        logger.info("Stopped nanobot for user=%s (port %d)", inst.user_id, inst.port)

    async def _reap_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(settings.reap_interval_seconds)
                await self._reap_idle()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in reaper loop")

    async def _reap_idle(self) -> None:
        threshold = settings.idle_timeout_seconds
        to_kill = [
            user_id
            for user_id, inst in list(self._instances.items())
            if inst.idle_seconds() > threshold or not inst.is_alive()
        ]
        for user_id in to_kill:
            logger.info("Reaping idle/dead instance for user=%s", user_id)
            await self.stop(user_id)
