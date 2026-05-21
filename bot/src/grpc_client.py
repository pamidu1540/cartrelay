"""
grpc_client.py — gRPC channel pool for the logistics bot (v5)

Fixes addressed:
  BUG-001: channels created lazily inside the asyncio loop (not at module level)
  BUG-005: exponential-backoff retry on every stub call
  BUG-010: unhealthy channels removed and replaced asynchronously
  PERF-008: least-in-flight channel selection replaces round-robin
  SEC-001: TLS credentials used when TLS_CERT_FILE env is set
  SEC-002: HMAC-signed x-auth-token header on every call
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import grpc
import grpc.aio

from api import logistics_pb2_grpc

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

SOCKET_PATH: str = os.getenv("ENGINE_SOCKET", "/app/sockets/logistics.sock")
GRPC_HOST: str = os.getenv("ENGINE_HOST", "")  # non-empty = TCP mode
GRPC_PORT: int = int(os.getenv("ENGINE_PORT", "50051"))
POOL_SIZE: int = int(os.getenv("GRPC_POOL_SIZE", "4"))
INTERNAL_TOKEN: str = os.getenv("INTERNAL_TOKEN", "")
TLS_CERT_FILE: str = os.getenv("TLS_CERT_FILE", "")

# Retry settings
RETRY_ATTEMPTS: int = 3
RETRY_BASE_DELAY: float = 0.5   # seconds; doubles each attempt
RETRY_MAX_DELAY: float = 4.0

# Circuit breaker
CB_FAILURE_THRESHOLD: int = 5   # consecutive failures to trip
CB_RECOVERY_TIMEOUT: float = 10.0  # seconds before half-open probe


# ── HMAC token generation ─────────────────────────────────────────────────────

def make_auth_token(method: str) -> str:
    """Generate HMAC-SHA256 token matching interceptors.go validateHMACToken."""
    minute = int(time.time()) // 60
    msg = f"{method}:{minute}".encode()
    token = hmac.HMAC(INTERNAL_TOKEN.encode(), msg, hashlib.sha256).hexdigest()
    return token


# ── Channel wrapper ────────────────────────────────────────────────────────────

@dataclass
class ChannelSlot:
    channel: grpc.aio.Channel
    in_flight: int = 0
    healthy: bool = True
    last_health_check: float = field(default_factory=time.monotonic)

    def acquire(self) -> None:
        self.in_flight += 1

    def release(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)


# ── Circuit breaker ────────────────────────────────────────────────────────────

class CircuitBreaker:
    """Half-open circuit breaker wrapping the channel pool."""

    def __init__(self, threshold: int = CB_FAILURE_THRESHOLD,
                 timeout: float = CB_RECOVERY_TIMEOUT) -> None:
        self._threshold = threshold
        self._timeout = timeout
        self._failures = 0
        self._opened_at: float | None = None
        self._state = "closed"  # closed | open | half-open
        self._lock = asyncio.Lock()

    async def allow(self) -> bool:
        async with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if time.monotonic() - self._opened_at >= self._timeout:
                    self._state = "half-open"
                    logger.info("circuit breaker → half-open, probing engine")
                    return True
                return False
            return True  # half-open: allow one probe

    async def record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            if self._state != "closed":
                logger.info("circuit breaker → closed")
            self._state = "closed"

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state == "half-open":
                self._state = "open"
                self._opened_at = time.monotonic()
                logger.warning("circuit breaker → open (probe failed)")
            elif self._failures >= self._threshold:
                self._state = "open"
                self._opened_at = time.monotonic()
                logger.warning(
                    "circuit breaker tripped after %d failures", self._failures
                )

    @property
    def state(self) -> str:
        return self._state


# ── Channel pool ──────────────────────────────────────────────────────────────

class ChannelPool:
    """
    Manages POOL_SIZE gRPC channels.

    BUG-001 fix: __init__ does NOT open any channels.
    Call await pool.start() inside the bot's post_init hook.
    """

    def __init__(self) -> None:
        self._slots: list[ChannelSlot] = []
        self._lock = asyncio.Lock()
        self._circuit_breaker = CircuitBreaker()
        self._health_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Create all channels. Must be called from within the asyncio loop."""
        async with self._lock:
            for _ in range(POOL_SIZE):
                ch = await self._make_channel()
                self._slots.append(ChannelSlot(channel=ch))
        self._health_task = asyncio.create_task(self._health_loop())
        logger.info(
            "gRPC channel pool started: %d channels → %s",
            POOL_SIZE,
            self._target(),
        )

    async def stop(self) -> None:
        if self._health_task:
            self._health_task.cancel()
        async with self._lock:
            for slot in self._slots:
                await slot.channel.close()
            self._slots.clear()
        logger.info("gRPC channel pool stopped")

    # ── Channel selection ──────────────────────────────────────────────────────

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[ChannelSlot]:
        """
        PERF-008: least-in-flight selection.
        Raises RuntimeError if the circuit breaker is open.
        """
        if not await self._circuit_breaker.allow():
            raise RuntimeError(
                f"Engine is temporarily unavailable "
                f"(circuit breaker {self._circuit_breaker.state})"
            )

        async with self._lock:
            healthy = [s for s in self._slots if s.healthy]
            if not healthy:
                # All channels unhealthy — allow one attempt anyway
                healthy = self._slots

            slot = min(healthy, key=lambda s: s.in_flight)
            slot.acquire()

        try:
            yield slot
            await self._circuit_breaker.record_success()
        except grpc.RpcError as exc:
            code = exc.code()
            if code in (
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.UNKNOWN,
            ):
                slot.healthy = False
                await self._circuit_breaker.record_failure()
            raise
        finally:
            slot.release()

    # ── Retry helper ──────────────────────────────────────────────────────────

    async def call(self, method_name: str, coro_factory, *args, **kwargs) -> Any:
        """
        BUG-005 fix: wraps a stub call in exponential-backoff retry.

        Usage:
            resp = await pool.call(
                "/logistics.LogisticsEngine/GetCatalog",
                lambda stub: stub.GetCatalog(request, metadata=meta),
            )
        """
        delay = RETRY_BASE_DELAY
        last_exc: Exception | None = None

        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                meta = _build_metadata(method_name, kwargs.get("user_id"))
                async with self.acquire() as slot:
                    stub = logistics_pb2_grpc.LogisticsEngineStub(slot.channel)
                    return await coro_factory(stub, meta)
            except grpc.RpcError as exc:
                last_exc = exc
                code = exc.code()
                # Do not retry on client errors
                if code in (
                    grpc.StatusCode.INVALID_ARGUMENT,
                    grpc.StatusCode.NOT_FOUND,
                    grpc.StatusCode.ALREADY_EXISTS,
                    grpc.StatusCode.PERMISSION_DENIED,
                    grpc.StatusCode.UNAUTHENTICATED,
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                ):
                    raise
                if attempt < RETRY_ATTEMPTS:
                    logger.warning(
                        "gRPC %s attempt %d/%d failed (%s) — retrying in %.1fs",
                        method_name, attempt, RETRY_ATTEMPTS,
                        code.name, delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, RETRY_MAX_DELAY)
            except RuntimeError:
                # Circuit breaker open
                raise

        raise last_exc  # type: ignore[misc]

    # ── Health loop ───────────────────────────────────────────────────────────

    async def _health_loop(self) -> None:
        """
        BUG-010 fix: periodically check channel health.
        Unhealthy channels are replaced with fresh ones.
        """
        while True:
            await asyncio.sleep(30)
            async with self._lock:
                for i, slot in enumerate(self._slots):
                    state = slot.channel.get_state(try_to_connect=False)
                    is_healthy = state not in (
                        grpc.ChannelConnectivity.TRANSIENT_FAILURE,
                        grpc.ChannelConnectivity.SHUTDOWN,
                    )
                    if not is_healthy and slot.in_flight == 0:
                        logger.warning(
                            "replacing unhealthy channel %d (state=%s)", i, state
                        )
                        await slot.channel.close()
                        self._slots[i] = ChannelSlot(
                            channel=await self._make_channel()
                        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _target(self) -> str:
        if GRPC_HOST:
            return f"{GRPC_HOST}:{GRPC_PORT}"
        return f"unix://{SOCKET_PATH}"

    async def _make_channel(self) -> grpc.aio.Channel:
        target = self._target()
        opts = [
            ("grpc.max_receive_message_length", 10 * 1024 * 1024),
            ("grpc.max_send_message_length", 10 * 1024 * 1024),
        ]

        # SEC-001: use TLS if cert file is configured
        if TLS_CERT_FILE and os.path.exists(TLS_CERT_FILE):
            with open(TLS_CERT_FILE, "rb") as f:
                creds = grpc.ssl_channel_credentials(f.read())
            return grpc.aio.secure_channel(target, creds, options=opts)

        return grpc.aio.insecure_channel(target, options=opts)


# ── Metadata builder ──────────────────────────────────────────────────────────

def _build_metadata(
    method: str,
    user_id: int | None = None,
    role: str = "",
) -> list[tuple[str, str]]:
    """Build gRPC metadata for a request including the HMAC token."""
    meta = [("x-auth-token", make_auth_token(method))]
    if user_id:
        meta.append(("x-actor-id", str(user_id)))
    if role:
        meta.append(("x-actor-role", role))
    return meta


# ── Module-level singleton ─────────────────────────────────────────────────────
# BUG-001 fix: pool is created here but NOT started.
# start() is called from the bot's post_init hook.

pool = ChannelPool()
