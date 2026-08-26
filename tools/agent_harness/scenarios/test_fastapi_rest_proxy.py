"""
test_fastapi_rest_proxy.py — Verification of the FastAPI REST Gateway for External AI Agents.
Exercises all HTTP endpoints:
- POST /api/v1/chat/send
- POST /api/v1/chat/click
- POST /api/v1/chat/location
- GET /api/v1/chat/screen/{user_id}
- GET /api/v1/chat/transcript/{user_id}
- GET /api/v1/inspect/cart/{user_id}
- POST /api/v1/session/reset
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport

from api import logistics_pb2 as pb
from tools.agent_harness.server import app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scenario.fastapi_proxy")


@pytest.mark.asyncio
async def test_fastapi_rest_proxy_endpoints():
    """Simulates an external AI Agent interacting with the REST proxy via HTTP JSON calls."""
    user_id = 77004
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        # 1. Reset session via HTTP
        resp = await client.post("/api/v1/session/reset", json={"user_id": user_id})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # 2. Send /start command via HTTP
        with patch("handlers.user.core.pool.call", new_callable=AsyncMock) as mock_core_pool:
            async def core_side_effect(endpoint, call_fn, *args, **kwargs):
                if "GetProfile" in endpoint:
                    return pb.CustomerProfile(user_id=user_id, display_name="API User", phone="0779991111")
                elif "ListSavedAddresses" in endpoint:
                    return pb.AddressListResponse(
                        addresses=[
                            pb.SavedAddress(id=1, user_id=user_id, label="Office", address="45 Janadhipathi Mawatha", is_default=True)
                        ]
                    )
                return pb.Empty()

            mock_core_pool.side_effect = core_side_effect

            resp = await client.post(
                "/api/v1/chat/send",
                json={
                    "user_id": user_id,
                    "text": "/start",
                    "username": "api_agent",
                    "first_name": "AgentTester",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "Welcome" in data["text"] or "Logistics Engine" in data["text"]
            assert len(data["inline_keyboard"]) > 0

        # 3. Inspect Screen via HTTP GET
        resp = await client.get(f"/api/v1/chat/screen/{user_id}")
        assert resp.status_code == 200
        screen_data = resp.json()
        assert screen_data["user_id"] == user_id
        assert len(screen_data["inline_keyboard"]) > 0

        # 4. Click Inline Button via HTTP
        with patch("handlers.user.catalog.pool.call", new_callable=AsyncMock) as mock_cat_pool:
            mock_cat_pool.return_value = pb.CatalogResponse(
                items=[
                    pb.Item(id="REST-01", name="Organic Coconut Oil", price=750.0, stock=10, is_available=True)
                ],
                total_count=1,
            )

            resp = await client.post(
                "/api/v1/chat/click",
                json={
                    "user_id": user_id,
                    "callback_data": "cat_select:all:0",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "Organic Coconut Oil" in data["text"] or "All Items" in data["text"]

        # 5. Share GPS Location via HTTP
        with patch("handlers.user.profile.pool.call", new_callable=AsyncMock) as mock_loc_pool:
            mock_loc_pool.return_value = pb.StatusResponse(success=True)

            resp = await client.post(
                "/api/v1/chat/location",
                json={
                    "user_id": user_id,
                    "latitude": 6.9271,
                    "longitude": 79.8612,
                },
            )
            assert resp.status_code == 200

        # 6. Retrieve Transcript via HTTP
        resp = await client.get(f"/api/v1/chat/transcript/{user_id}")
        assert resp.status_code == 200
        transcript = resp.json()
        assert len(transcript) >= 3
        assert any(t["text"] == "/start" for t in transcript)

        # 7. Inspect Cart directly via HTTP
        resp = await client.get(f"/api/v1/inspect/cart/{user_id}")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    logger.info("✅ All FastAPI REST Proxy HTTP endpoints verified successfully!")


if __name__ == "__main__":
    asyncio.run(test_fastapi_rest_proxy_endpoints())
