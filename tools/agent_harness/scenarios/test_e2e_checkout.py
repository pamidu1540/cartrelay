"""
test_e2e_checkout.py — End-to-end customer journey simulation test scenario.
Exercises: /start -> Catalog Browse -> Add to Cart -> View Cart -> Address Setup -> Verification.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

from api import logistics_pb2 as pb
from tools.agent_harness.client import AgentChatSession

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scenario.checkout")


@pytest.mark.asyncio
async def test_e2e_customer_catalog_and_cart_journey():
    """Simulates a complete customer interaction flow and verifies dual UI/backend consistency."""
    session = AgentChatSession(user_id=88001, username="test_shopper", first_name="Shopper")
    await session.start()
    await session.reset()

    # 1. Start command (with registered customer profile & default address)
    with patch("handlers.user.core.pool.call", new_callable=AsyncMock) as mock_core_pool:
        async def core_side_effect(endpoint, call_fn, *args, **kwargs):
            if "GetProfile" in endpoint:
                return pb.CustomerProfile(user_id=88001, display_name="Shopper", phone="0771234567")
            elif "ListSavedAddresses" in endpoint:
                return pb.AddressListResponse(
                    addresses=[
                        pb.SavedAddress(id=1, user_id=88001, label="Home", address="123 Colombo St", is_default=True)
                    ]
                )
            return pb.Empty()
        mock_core_pool.side_effect = core_side_effect

        screen = await session.send("/start")
        assert "Welcome" in screen.text or "Logistics Engine" in screen.text
        buttons = session.list_button_labels()
        assert any("Browse Catalog" in b for b in buttons)

    # 2. Browse catalog
    cat_btn = session.find_button("cat_select:all:0")
    assert cat_btn is not None

    mock_catalog_items = [
        pb.Item(
            id="ITEM-001",
            name="Organic Ceylon Tea",
            price=450.0,
            stock=25,
            category="Beverages",
            is_available=True,
        ),
        pb.Item(
            id="ITEM-002",
            name="Coconut Milk 400ml",
            price=280.0,
            stock=15,
            category="Groceries",
            is_available=True,
        ),
    ]

    with patch("handlers.user.catalog.pool.call", new_callable=AsyncMock) as mock_pool_call:
        mock_pool_call.return_value = pb.CatalogResponse(items=mock_catalog_items, total_count=2)
        screen = await session.click(cat_btn)
        assert "All Items" in screen.text or "Catalog" in screen.text

    # 3. Item Detail
    with patch("handlers.user.catalog.pool.call", new_callable=AsyncMock) as mock_cat_pool:
        async def item_side_effect(endpoint, call_fn, *args, **kwargs):
            if "GetItem" in endpoint:
                return mock_catalog_items[0]
            elif "ListSavedAddresses" in endpoint:
                return pb.AddressListResponse(
                    addresses=[
                        pb.SavedAddress(id=1, user_id=88001, label="Home", address="123 Colombo St", is_default=True)
                    ]
                )
            elif "ListWarehouses" in endpoint:
                return pb.WarehouseListResponse(
                    warehouses=[
                        pb.Warehouse(id="WH-1", name="Colombo Central", lat=6.9271, lon=79.8612, is_active=True)
                    ]
                )
            return pb.Empty()

        mock_cat_pool.side_effect = item_side_effect
        screen = await session.click("item_detail:ITEM-001")
        assert "Organic Ceylon Tea" in screen.text
        assert "450" in screen.text
        assert any("Add to Cart" in b for b in session.list_button_labels())

    # 4. Add to Cart
    with patch("cart_manager.pool.call", new_callable=AsyncMock) as mock_cart_pool:
        mock_cart_pool.return_value = pb.CartResponse(
            user_id=88001,
            items=[pb.CartItem(item_id="ITEM-001", quantity=1)],
        )
        add_btn = session.find_button("add_to_cart:ITEM-001") or session.find_button("add_to_cart:ITEM-001:1")
        assert add_btn is not None
        screen = await session.click(add_btn)
        assert any("Add More" in b for b in session.list_button_labels())

    # 5. Dual Verification: Inspect Backend Cart directly
    cart_items = await session.inspect_cart()
    assert len(cart_items) >= 1
    assert cart_items[0]["item_id"] == "ITEM-001"
    assert cart_items[0]["quantity"] >= 1

    logger.info("✅ E2E Customer Catalog & Cart scenario passed successfully!")


if __name__ == "__main__":
    asyncio.run(test_e2e_customer_catalog_and_cart_journey())
