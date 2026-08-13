"""
bot/tests/conftest.py — Shared pytest fixtures for Telegram Bot test suite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure src/ is on python path for tests
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "engine/src"))


class AsyncMock(MagicMock):
    """Custom AsyncMock for coroutine returns."""
    async def __call__(self, *args, **kwargs):
        return super().__call__(*args, **kwargs)


@pytest.fixture
def mock_async():
    return AsyncMock


@pytest.fixture
def cart_manager():
    from cart_manager import CartManager
    return CartManager()


@pytest.fixture
def rate_limiter():
    from rate_limiter import RateLimiter
    return RateLimiter()
