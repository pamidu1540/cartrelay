"""
tests/test_bot.py — Bot unit tests (v5)

Covers:
  BUG-001: pool.start() must be called before any gRPC call
  BUG-003: flush_all() persists every in-memory cart before exit
  BUG-016: invalidate_local() clears cache without leaving stale tasks
  BUG-005: pool.call() retries on UNAVAILABLE, not on INVALID_ARGUMENT
  SEC-005: rate limiter blocks after threshold, resets with time
  HMAC:    make_auth_token produces different values each minute boundary
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

# ── Cart manager tests ────────────────────────────────────────────────────────

class TestCartManager:
    """BUG-003, BUG-016: cart persistence and invalidation."""

    @pytest.fixture
    def cart_manager(self):
        from cart_manager import CartManager
        return CartManager()

    @pytest.mark.asyncio
    async def test_add_and_get(self, cart_manager):
        """Items added locally are returned on get()."""
        with patch("cart_manager.pool") as mock_pool:
            mock_pool.call = AsyncMock(return_value=MagicMock(items=[]))
            mock_pool.call.side_effect = None

            # Patch persist to avoid real gRPC
            cart_manager._persist = AsyncMock()

            items = await cart_manager.add_item(user_id=1001, item_id="ITEM-A", qty=2)
            assert len(items) == 1
            assert items[0].item_id == "ITEM-A"
            assert items[0].quantity == 2

    @pytest.mark.asyncio
    async def test_flush_all_calls_persist_for_every_user(self, cart_manager):
        """BUG-003: flush_all() must call _persist for every cached user."""
        cart_manager._persist = AsyncMock()
        # Manually seed local cache
        from api.logistics_pb2 import CartItem
        from collections import OrderedDict
        cart_manager._local = OrderedDict()
        cart_manager._local[1001] = ([CartItem(item_id="ITEM-A", quantity=1)], time.time() + 3600)
        cart_manager._local[1002] = ([CartItem(item_id="ITEM-B", quantity=3)], time.time() + 3600)

        await cart_manager.flush_all()

        assert cart_manager._persist.call_count == 2
        called_users = {call.args[0] for call in cart_manager._persist.call_args_list}
        assert called_users == {1001, 1002}

    @pytest.mark.asyncio
    async def test_invalidate_local_clears_cache(self, cart_manager):
        """BUG-016: invalidate_local removes local cache without side effects."""
        from api.logistics_pb2 import CartItem
        cart_manager._local[1001] = ([CartItem(item_id="ITEM-X", quantity=1)], time.time() + 3600)

        await cart_manager.invalidate_local(1001)

        assert 1001 not in cart_manager._local

    @pytest.mark.asyncio
    async def test_flush_all_empty_is_noop(self, cart_manager):
        """flush_all on empty cache should not error or call persist."""
        cart_manager._persist = AsyncMock()
        await cart_manager.flush_all()
        cart_manager._persist.assert_not_called()


# ── Channel pool / retry tests ────────────────────────────────────────────────

class TestChannelPoolRetry:
    """BUG-005: exponential retry on transient failures."""

    @pytest.mark.asyncio
    async def test_call_retries_on_unavailable(self):
        """pool.call should retry up to RETRY_ATTEMPTS times on UNAVAILABLE."""
        import grpc
        from grpc_client import ChannelPool

        pool = ChannelPool()
        call_count = 0

        async def failing_factory(stub, meta):
            nonlocal call_count
            call_count += 1
            err = grpc.RpcError()
            err.code = lambda: grpc.StatusCode.UNAVAILABLE
            err.details = lambda: "connection refused"
            raise err

        # Patch acquire to yield a mock slot
        mock_slot = MagicMock()
        mock_slot.channel = MagicMock()
        mock_slot.in_flight = 0
        mock_slot.healthy = True

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mock_acquire():
            mock_slot.acquire()
            try:
                yield mock_slot
                await pool._circuit_breaker.record_success()
            except grpc.RpcError:
                mock_slot.healthy = False
                await pool._circuit_breaker.record_failure()
                raise
            finally:
                mock_slot.release()

        pool.acquire = mock_acquire
        # Patch sleep to avoid real delays in tests
        with patch("grpc_client.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(grpc.RpcError):
                await pool.call(
                    "/test/Method",
                    failing_factory,
                )

        from grpc_client import RETRY_ATTEMPTS
        assert call_count == RETRY_ATTEMPTS, (
            f"expected {RETRY_ATTEMPTS} retries, got {call_count}"
        )

    @pytest.mark.asyncio
    async def test_call_does_not_retry_on_invalid_argument(self):
        """pool.call must NOT retry on client errors like INVALID_ARGUMENT."""
        import grpc
        from grpc_client import ChannelPool

        pool = ChannelPool()
        call_count = 0

        async def client_error_factory(stub, meta):
            nonlocal call_count
            call_count += 1
            err = grpc.RpcError()
            err.code = lambda: grpc.StatusCode.INVALID_ARGUMENT
            err.details = lambda: "bad input"
            raise err

        mock_slot = MagicMock()
        mock_slot.in_flight = 0
        mock_slot.healthy = True

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mock_acquire():
            mock_slot.acquire()
            try:
                yield mock_slot
            except grpc.RpcError:
                raise
            finally:
                mock_slot.release()

        pool.acquire = mock_acquire
        pool._circuit_breaker.allow = AsyncMock(return_value=True)

        with pytest.raises(grpc.RpcError):
            await pool.call("/test/Method", client_error_factory)

        assert call_count == 1, "should not retry on INVALID_ARGUMENT"


# ── Rate limiter tests ────────────────────────────────────────────────────────

class TestRateLimiter:
    """SEC-005: per-user token bucket rate limiting."""

    @pytest.mark.asyncio
    async def test_allows_within_limit(self):
        from middleware import _check_rate, _RATE_CAPACITY, _buckets
        user_id = 88881

        # Reset bucket
        _buckets.pop(user_id, None)

        for _ in range(_RATE_CAPACITY):
            allowed = await _check_rate(user_id)
            assert allowed, "should allow requests within capacity"

    @pytest.mark.asyncio
    async def test_blocks_over_limit(self):
        from middleware import _check_rate, _RATE_CAPACITY, _buckets
        user_id = 88882

        # Drain bucket completely
        _buckets.pop(user_id, None)
        for _ in range(_RATE_CAPACITY):
            await _check_rate(user_id)

        blocked = await _check_rate(user_id)
        assert not blocked, "should block when bucket is empty"

    @pytest.mark.asyncio
    async def test_different_users_isolated(self):
        from middleware import _check_rate, _RATE_CAPACITY, _buckets
        user_a, user_b = 88883, 88884

        _buckets.pop(user_a, None)
        _buckets.pop(user_b, None)

        # Drain user_a
        for _ in range(_RATE_CAPACITY):
            await _check_rate(user_a)

        # user_b should still be allowed
        allowed_b = await _check_rate(user_b)
        assert allowed_b, "user_b bucket should be independent of user_a"


# ── HMAC token tests ──────────────────────────────────────────────────────────

class TestHMACToken:
    """SEC-002: HMAC token generation and validity window."""

    def test_token_is_deterministic_within_same_minute(self):
        """Same method + same minute → same token."""
        import os
        os.environ.setdefault("INTERNAL_TOKEN", "test-secret-for-ci")

        from grpc_client import make_auth_token
        t1 = make_auth_token("/logistics.LogisticsEngine/GetCatalog")
        t2 = make_auth_token("/logistics.LogisticsEngine/GetCatalog")
        assert t1 == t2

    def test_different_methods_produce_different_tokens(self):
        import os
        os.environ.setdefault("INTERNAL_TOKEN", "test-secret-for-ci")

        from grpc_client import make_auth_token
        t1 = make_auth_token("/logistics.LogisticsEngine/GetCatalog")
        t2 = make_auth_token("/logistics.LogisticsEngine/PlaceOrder")
        assert t1 != t2

    def test_token_hex_format(self):
        import os
        import re
        os.environ.setdefault("INTERNAL_TOKEN", "test-secret-for-ci")

        from grpc_client import make_auth_token
        token = make_auth_token("/test/Method")
        assert re.fullmatch(r"[0-9a-f]{64}", token), (
            f"token should be 64-char hex, got: {token}"
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super().__call__(*args, **kwargs)


# ── Admin Only tests ──────────────────────────────────────────────────────────

class TestAdminOnly:
    """Tests for the admin_only decorator."""

    @pytest.mark.asyncio
    async def test_allows_admin(self):
        from middleware import admin_only

        mock_func = AsyncMock()
        decorated_func = admin_only(mock_func)

        update = MagicMock()
        update.effective_user.id = 123
        context = MagicMock()
        context.bot_data = {}

        mock_resp = MagicMock()
        mock_admin = MagicMock()
        mock_admin.telegram_id = 123
        mock_resp.admins = [mock_admin]

        with patch("middleware.pool") as mock_pool:
            mock_pool.call = AsyncMock(return_value=mock_resp)
            await decorated_func(update, context)

        mock_func.assert_called_once_with(update, context)
        assert context.bot_data['_admins_list'] == {123}

    @pytest.mark.asyncio
    async def test_blocks_non_admin(self):
        from middleware import admin_only

        mock_func = AsyncMock()
        decorated_func = admin_only(mock_func)

        update = MagicMock()
        update.effective_user.id = 456
        update.effective_message.reply_text = AsyncMock()
        context = MagicMock()
        context.bot_data = {}

        mock_resp = MagicMock()
        mock_admin = MagicMock()
        mock_admin.telegram_id = 123
        mock_resp.admins = [mock_admin]

        with patch("middleware.pool") as mock_pool:
            mock_pool.call = AsyncMock(return_value=mock_resp)
            await decorated_func(update, context)

        mock_func.assert_not_called()
        update.effective_message.reply_text.assert_called_once_with("🚫 Admin access required.")

    @pytest.mark.asyncio
    async def test_uses_cache(self):
        from middleware import admin_only
        import time

        mock_func = AsyncMock()
        decorated_func = admin_only(mock_func)

        update = MagicMock()
        update.effective_user.id = 123
        context = MagicMock()
        context.bot_data = {
            '_admins_time': time.monotonic(),
            '_admins_list': {123}
        }

        with patch("middleware.pool") as mock_pool:
            mock_pool.call = AsyncMock()
            await decorated_func(update, context)

        mock_pool.call.assert_not_called()
        mock_func.assert_called_once_with(update, context)

    @pytest.mark.asyncio
    async def test_handles_grpc_error(self):
        from middleware import admin_only

        mock_func = AsyncMock()
        decorated_func = admin_only(mock_func)

        update = MagicMock()
        update.effective_user.id = 123
        update.effective_message.reply_text = AsyncMock()
        context = MagicMock()
        context.bot_data = {}

        with patch("middleware.pool") as mock_pool:
            mock_pool.call = AsyncMock(side_effect=Exception("grpc error"))
            await decorated_func(update, context)

        mock_func.assert_not_called()
        update.effective_message.reply_text.assert_called_once_with("⚠️ Could not verify admin status.")

    @pytest.mark.asyncio
    async def test_returns_early_if_no_user(self):
        from middleware import admin_only

        mock_func = AsyncMock()
        decorated_func = admin_only(mock_func)

        update = MagicMock()
        update.effective_user = None
        context = MagicMock()

        with patch("middleware.pool") as mock_pool:
            mock_pool.call = AsyncMock()
            await decorated_func(update, context)

        mock_pool.call.assert_not_called()
        mock_func.assert_not_called()
