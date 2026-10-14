"""
grpc_client.py — gRPC channel pool for the logistics bot

Manages a pool of async gRPC channels to the engine with:
  - Lazy channel creation inside the asyncio event loop
  - Least-in-flight channel selection
  - Exponential-backoff retry on transient failures
  - Circuit breaker to shed load when the engine is down
  - Periodic health checks replacing unhealthy channels
  - HMAC-signed auth token header on every call
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
POOL_SIZE: int = int(os.getenv("GRPC_POOL_SIZE", "4"))
INTERNAL_TOKEN: str = os.getenv("INTERNAL_TOKEN", "")

# Retry settings
RETRY_ATTEMPTS: int = 1 if os.getenv("TESTING") else 3
RETRY_BASE_DELAY: float = 0.05 if os.getenv("TESTING") else 0.5
RETRY_MAX_DELAY: float = 0.1 if os.getenv("TESTING") else 4.0
RPC_TIMEOUT: float = float(os.getenv("RPC_TIMEOUT", "0.5" if os.getenv("TESTING") else "10.0"))

# Circuit breaker
CB_FAILURE_THRESHOLD: int = 5   # consecutive failures to trip
CB_RECOVERY_TIMEOUT: float = 10.0  # seconds before half-open probe


# ── HMAC token generation ─────────────────────────────────────────────────────

def make_auth_token(method: str) -> str:
    """Generate HMAC-SHA256 token matching interceptors.go validateHMACToken."""
    now = int(time.time())
    msg = f"{method}:{now}".encode()
    # SEC: read token from env on each call to avoid holding secret in module-level variable
    token = os.getenv("INTERNAL_TOKEN", "")
    return hmac.HMAC(token.encode(), msg, hashlib.sha256).hexdigest()


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
    Manages POOL_SIZE gRPC channels to the engine.

    Channels are not opened in __init__; call await pool.start() inside the
    bot's post_init hook to ensure channels are created inside the event loop.
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
        Least-in-flight channel selection.
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
        Wraps a stub call in exponential-backoff retry.

        Usage:
            resp = await pool.call(
                "/logistics.LogisticsEngine/GetCatalog",
                lambda stub, meta: stub.GetCatalog(request, metadata=meta),
            )
        """
        delay = RETRY_BASE_DELAY
        last_exc: Exception | None = None

        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                meta = _build_metadata(method_name, kwargs.get("user_id"))
                async with self.acquire() as slot:
                    if not hasattr(slot, "stub"):
                        slot.stub = logistics_pb2_grpc.LogisticsEngineStub(slot.channel)
                    return await asyncio.wait_for(coro_factory(slot.stub, meta), timeout=RPC_TIMEOUT)
            except TimeoutError:
                last_exc = Exception(f"gRPC call {method_name} timed out after {RPC_TIMEOUT}s")
                if attempt < RETRY_ATTEMPTS:
                    logger.warning(
                        "gRPC %s attempt %d/%d timed out — retrying in %.1fs",
                        method_name, attempt, RETRY_ATTEMPTS, delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, RETRY_MAX_DELAY)
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
        """Periodically check channel health, replacing unhealthy channels."""
        while True:
            await asyncio.sleep(30)
            bad_indices = []
            async with self._lock:
                for i, slot in enumerate(self._slots):
                    state = slot.channel.get_state(try_to_connect=False)
                    is_healthy = state not in (
                        grpc.ChannelConnectivity.TRANSIENT_FAILURE,
                        grpc.ChannelConnectivity.SHUTDOWN,
                    )
                    if not is_healthy and slot.in_flight == 0:
                        bad_indices.append(i)

            for i in bad_indices:
                logger.warning("replacing unhealthy channel %d", i)
                new_channel = await self._make_channel()
                async with self._lock:
                    old_slot = self._slots[i]
                    self._slots[i] = ChannelSlot(channel=new_channel)
                await old_slot.channel.close()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _target(self) -> str:
        return f"unix://{SOCKET_PATH}"

    async def _make_channel(self) -> grpc.aio.Channel:
        target = self._target()
        opts = [
            ("grpc.max_receive_message_length", 10 * 1024 * 1024),
            ("grpc.max_send_message_length", 10 * 1024 * 1024),
        ]

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


# Module-level singleton — created here but NOT started.
# start() is called from the bot's post_init hook after the event loop is running.
pool = ChannelPool()
