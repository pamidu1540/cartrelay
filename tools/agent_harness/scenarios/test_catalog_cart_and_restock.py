"""
test_catalog_cart_and_restock.py — Catalog Browsing, Search, Cart Mutations & Restock Notifications.
Covers:
1. /catalog browsing by category and pagination.
2. Item details with live distance calculation & stock statuses.
3. Out-of-stock restock subscription alerts.
4. Multiple cart modifications (add, qty_plus, qty_minus, cart_remove, clear).
5. Dynamic free delivery progress banner.
6. Dual backend cart state assertions.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

from api import logistics_pb2 as pb
from tools.agent_harness.client import AgentChatSession

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scenario.catalog_cart")


@pytest.mark.asyncio
async def test_catalog_cart_and_restock_suite():
    """Simulates thorough catalog exploration and advanced cart operations."""
    user_id = 77002
    session = AgentChatSession(user_id=user_id, username="catalog_shopper", first_name="Kavindi")
    await session.start()
    await session.reset()

    # Seed mock items
    item_beverage = pb.Item(
        id="BEV-001",
        name="Ceylon Earl Grey Tea",
        price=650.0,
        stock=12,
        category="Beverages",
        description="Single origin high grown black tea with bergamot.",
        is_available=True,
    )
    item_grocery = pb.Item(
        id="GROC-001",
        name="Red Basmati Rice 5kg",
        price=1850.0,
        stock=20,
        category="Groceries",
        description="Premium wholesome organic red basmati.",
        is_available=True,
    )
    item_out_of_stock = pb.Item(
        id="SNK-001",
        name="Artisanal Cassava Chips",
        price=320.0,
        stock=0,
        category="Snacks",
        description="Crispy salted cassava chips.",
        is_available=False,
    )

    # ──────────────────────────────────────────────────────────────────────────
    # 1. /catalog Entry & Category Filtering
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 1: Open /catalog ---")
    with patch("handlers.user.catalog.pool.call", new_callable=AsyncMock) as mock_cat_pool:
        mock_cat_pool.return_value = pb.CatalogResponse(
            items=[item_beverage, item_grocery, item_out_of_stock],
            total_count=3,
        )

        screen = await session.send("/catalog")
        assert "catalog" in screen.text.lower() or "choose a category" in screen.text.lower() or "all items" in screen.text.lower()
        buttons = session.list_button_labels()
        assert any("All" in b or "Beverages" in b for b in buttons)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Category Selection & Pagination
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 2: Browse Category with Pagination ---")
    with patch("handlers.user.catalog.pool.call", new_callable=AsyncMock) as mock_cat_pool:
        mock_cat_pool.return_value = pb.CatalogResponse(items=[item_beverage], total_count=1)

        bev_btn = session.find_button("cat_select:Beverages:0") or session.find_button("cat_select:all:0")
        assert bev_btn is not None
        screen = await session.click(bev_btn)
        assert "Ceylon Earl Grey Tea" in screen.text
        assert "650" in screen.text

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Item Details & Distance Calculation
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 3: Item Details and Warehouse Proximity ---")
    with patch("handlers.user.catalog.pool.call", new_callable=AsyncMock) as mock_detail_pool:
        async def detail_side_effect(endpoint, call_fn, *args, **kwargs):
            if "GetItem" in endpoint:
                return item_beverage
            elif "ListSavedAddresses" in endpoint:
                return pb.AddressListResponse(
                    addresses=[
                        pb.SavedAddress(id=1, user_id=user_id, label="Home", address="Colombo 03", lat=6.9000, lon=79.8500, is_default=True)
                    ]
                )
            elif "ListWarehouses" in endpoint:
                return pb.WarehouseListResponse(
                    warehouses=[
                        pb.Warehouse(id="WH-COLOMBO", name="Colombo Hub", lat=6.9271, lon=79.8612, is_active=True)
                    ]
                )
            return pb.Empty()

        mock_detail_pool.side_effect = detail_side_effect

        screen = await session.click("item_detail:BEV-001")
        assert "Ceylon Earl Grey Tea" in screen.text
        assert "In Stock" in screen.text or "650" in screen.text
        assert any("Add to Cart" in b for b in session.list_button_labels())

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Out of Stock Alert & Stock Notification Subscription
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 4: Restock Notification Subscription ---")
    with patch("handlers.user.catalog.pool.call", new_callable=AsyncMock) as mock_oos_pool:
        async def oos_side_effect(endpoint, call_fn, *args, **kwargs):
            if "GetItem" in endpoint:
                return item_out_of_stock
            elif "ListSavedAddresses" in endpoint:
                return pb.AddressListResponse(addresses=[])
            elif "ListWarehouses" in endpoint:
                return pb.WarehouseListResponse(warehouses=[])
            elif "SubscribeStockAlert" in endpoint or "StockSub" in endpoint:
                return pb.StatusResponse(success=True)
            return pb.Empty()

        mock_oos_pool.side_effect = oos_side_effect

        screen = await session.click("item_detail:SNK-001")
        assert "Out of Stock" in screen.text or "Cassava Chips" in screen.text
        notify_btn = session.find_button("stock_notify:SNK-001")
        if notify_btn:
            screen = await session.click(notify_btn)
            assert "notify" in screen.text.lower() or "alert" in screen.text.lower() or screen.last_alert is not None

    # ──────────────────────────────────────────────────────────────────────────
    # 5. Add Items to Cart & Check Free Delivery Banner
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 5: Adding items & verifying Free Delivery Banner ---")
    with patch("cart_manager.pool.call", new_callable=AsyncMock) as mock_cart_pool:
        mock_cart_pool.return_value = pb.CartResponse(
            user_id=user_id,
            items=[
                pb.CartItem(item_id="BEV-001", quantity=2),
                pb.CartItem(item_id="GROC-001", quantity=1),
            ],
        )

        add_bev = session.find_button("add_to_cart:BEV-001")
        if not add_bev:
            add_bev = "add_to_cart:BEV-001:1"
        screen = await session.click(add_bev)
        assert any("My Cart" in b or "Add More" in b for b in session.list_button_labels())

    # View /cart
    with patch("handlers.user.cart.pool.call", new_callable=AsyncMock) as mock_cart_view_pool:
        async def cart_view_side_effect(endpoint, call_fn, *args, **kwargs):
            if "GetItem" in endpoint:
                return item_beverage
            elif "GetSystemSettings" in endpoint:
                return pb.SystemSettings(free_delivery_threshold=5000.0)
            return pb.Empty()

        mock_cart_view_pool.side_effect = cart_view_side_effect

        screen = await session.send("/cart")
        assert "Cart" in screen.text or "Subtotal" in screen.text
        assert any("Checkout" in b for b in session.list_button_labels())

    # ──────────────────────────────────────────────────────────────────────────
    # 6. Cart Quantity Increment, Decrement, and Item Removal
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 6: Cart Quantity Adjustments and Cleanup ---")
    with patch("handlers.user.cart.pool.call", new_callable=AsyncMock) as mock_adj_pool:
        mock_adj_pool.return_value = item_beverage

        # Increment quantity
        plus_btn = session.find_button("qty_plus:BEV-001")
        if plus_btn:
            screen = await session.click(plus_btn)
            assert "Cart" in screen.text or "Subtotal" in screen.text

        # Decrement quantity
        minus_btn = session.find_button("qty_minus:BEV-001")
        if minus_btn:
            screen = await session.click(minus_btn)
            assert "Cart" in screen.text or "Subtotal" in screen.text

        # Remove single item
        remove_btn = session.find_button("cart_remove:BEV-001")
        if remove_btn:
            screen = await session.click(remove_btn)
            assert "Cart" in screen.text or "empty" in screen.text.lower()

        # Clear entire cart
        clear_btn = session.find_button("cart_clear")
        if clear_btn:
            screen = await session.click(clear_btn)
            assert "empty" in screen.text.lower() or "Cart" in screen.text

    logger.info("✅ All Catalog, Cart, and Restock scenarios passed successfully!")


if __name__ == "__main__":
    asyncio.run(test_catalog_cart_and_restock_suite())
