"""
conftest.py — shared pytest fixtures for the bot test suite.
"""

from __future__ import annotations

import os
import sys

import pytest

# Ensure the src directory and generated api stubs are on the path
SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(SRC_DIR))

# Set a dummy INTERNAL_TOKEN so imports don't crash
os.environ.setdefault("INTERNAL_TOKEN", "test-token-64-chars-padded-to-make-tests-work-ok!")
os.environ.setdefault("ENGINE_SOCKET", "/tmp/test-logistics.sock")


@pytest.fixture(autouse=True)
def reset_rate_buckets():
    """Reset rate-limit buckets between tests to avoid state leakage."""
    try:
        from middleware import _buckets
        _buckets.clear()
    except ImportError:
        pass
    yield
    try:
        from middleware import _buckets
        _buckets.clear()
    except ImportError:
        pass
