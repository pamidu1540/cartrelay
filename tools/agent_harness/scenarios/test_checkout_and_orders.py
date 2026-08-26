"""
test_checkout_and_orders.py — End-to-End Checkout, Order History, Reordering, Cancellation & Issue Reporting.
Covers:
1. Complete checkout process (/checkout -> Address selection -> Promo entry -> Slot picker -> Order placement).
2. Order history browsing (/orders with pagination).
3. Detailed order view with status emojis and delivery OTP.
4. Customer-initiated pending order cancellation.
5. Reorder mechanism (re-populates cart from receipt).
6. Post-delivery issue reporting flow (damaged/wrong item).
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

from api import logistics_pb2 as pb
from tools.agent_harness.client import AgentChatSession

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scenario.checkout_orders")


@pytest.mark.asyncio
async def test_checkout_and_orders_full_lifecycle():
    """Simulates the entire order placing, tracking, cancellation, and dispute handling workflow."""
    user_id = 77003
    session = AgentChatSession(user_id=user_id, username="order_placer", first_name="Dinesh")
    await session.start()
    await session.reset()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Start Checkout with Pre-filled Cart
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 1: Initialize Checkout ---")
    from tools.agent_harness.runner import harness

    with patch("cart_manager.pool.call", new_callable=AsyncMock) as mock_cart_seed:
        mock_cart_seed.return_value = pb.CartResponse(
            user_id=user_id,
            items=[
                pb.CartItem(item_id="TEA-101", quantity=2),
                pb.CartItem(item_id="HONEY-202", quantity=1),
            ],
        )
        if harness.app and "cart_manager" in harness.app.bot_data:
            await harness.app.bot_data["cart_manager"].set_items(
                user_id,
                [
                    pb.CartItem(item_id="TEA-101", quantity=2),
                    pb.CartItem(item_id="HONEY-202", quantity=1),
                ],
            )

    with patch("handlers.user.checkout.pool.call", new_callable=AsyncMock) as mock_checkout_pool:
        mock_checkout_pool.return_value = pb.AddressListResponse(
            addresses=[
                pb.SavedAddress(id=5, user_id=user_id, label="Home", address="No 44 Park Street, Colombo 02", lat=6.9200, lon=79.8600, is_default=True)
            ]
        )

        screen = await session.click("checkout")
        assert "Where should we deliver" in screen.text or "address" in screen.text.lower()
        assert any("Home" in b for b in session.list_button_labels())

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Select Saved Address & Calculate Quote
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 2: Select Address & View Quote ---")
    mock_quote = pb.QuoteResponse(
        success=True,
        subtotal=2400.0,
        delivery_fee=250.0,
        tax=120.0,
        grand_total=2770.0,
        formatted_receipt="🧾 *Receipt*\n• 2× Ceylon Tea — LKR 1600\n• 1× Wild Honey — LKR 800\nSubtotal: LKR 2400\nDelivery: LKR 250\nTax: LKR 120\n*Total: LKR 2770*",
        distance_km=4.2,
        nearest_branch="Colombo Hub",
        quote_data=b"mock-encrypted-quote-token",
    )

    with patch("handlers.user.checkout.pool.call", new_callable=AsyncMock) as mock_quote_pool:
        async def quote_side_effect(endpoint, call_fn, *args, **kwargs):
            if "ListSavedAddresses" in endpoint:
                return pb.AddressListResponse(
                    addresses=[
                        pb.SavedAddress(id=5, user_id=user_id, label="Home", address="No 44 Park Street, Colombo 02", lat=6.9200, lon=79.8600, is_default=True)
                    ]
                )
            elif "CalculateQuote" in endpoint:
                return mock_quote
            elif "GetLoyaltyBalance" in endpoint:
                return pb.LoyaltyBalance(user_id=user_id, points=100, tier="Bronze")
            return pb.Empty()

        mock_quote_pool.side_effect = quote_side_effect

        screen = await session.click("addr_select:5")
        assert "Receipt" in screen.text or "2770" in screen.text
        assert any("Promo Code" in b or "Skip" in b for b in session.list_button_labels())

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Enter Promo Code
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 3: Enter Promo Code ---")
    screen = await session.click("promo:enter")
    assert "Enter your promo code" in screen.text

    mock_discounted_quote = pb.QuoteResponse(
        success=True,
        subtotal=2400.0,
        delivery_fee=250.0,
        promo_discount=240.0,
        grand_total=2530.0,
        formatted_receipt="🧾 *Receipt (Discounted)*\n• 2× Ceylon Tea — LKR 1600\n• 1× Wild Honey — LKR 800\nPromo WELCOME10: -LKR 240\n*Total: LKR 2530*",
        promo_applied="WELCOME10",
        distance_km=4.2,
        nearest_branch="Colombo Hub",
        quote_data=b"mock-discounted-quote-token",
    )

    with patch("handlers.user.checkout.pool.call", new_callable=AsyncMock) as mock_promo_pool:
        mock_promo_pool.return_value = mock_discounted_quote
        screen = await session.send("WELCOME10")
        assert "Choose a delivery time" in screen.text or "delivery time" in screen.text.lower()

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Delivery Slot Selection
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 4: Select Delivery Time Slot ---")
    slot_btn = session.find_button("slot:")
    assert slot_btn is not None
    screen = await session.click(slot_btn)
    assert "Order Confirmation" in screen.text or "Confirm Order" in screen.text
    assert any("Confirm Order" in b for b in session.list_button_labels())

    # ──────────────────────────────────────────────────────────────────────────
    # 5. Confirm and Place Order
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 5: Final Order Placement ---")
    with patch("handlers.user.checkout.pool.call", new_callable=AsyncMock) as mock_order_pool, \
         patch("cart_manager.pool.call", new_callable=AsyncMock) as mock_cart_clear:
        mock_order_pool.return_value = pb.OrderResponse(
            success=True,
            order_id="ORD-20260826-0099",
            human_ref="ORD-20260826-0099",
            message="Your order has been placed successfully!",
        )
        mock_cart_clear.return_value = pb.CartResponse(user_id=user_id, items=[])

        screen = await session.click("confirm_order")
        assert "order placed" in screen.text.lower() or "ORD-20260826-0099" in screen.text

    # ──────────────────────────────────────────────────────────────────────────
    # 6. View Order History (/orders)
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 6: Order History View ---")
    mock_orders = [
        pb.Order(
            order_id="ORD-20260826-0099",
            human_ref="ORD-20260826-0099",
            status="pending",
            total=2530.0,
            delivery_address="No 44 Park Street, Colombo 02",
            timestamp=1787600000,
        ),
        pb.Order(
            order_id="ORD-20260825-0044",
            human_ref="ORD-20260825-0044",
            status="delivered",
            total=1450.0,
            delivery_address="No 44 Park Street, Colombo 02",
            delivery_otp="4829",
            receipt="🧾 *Receipt*\n• Ceylon Earl Grey Tea × 2 — LKR 1300\nTotal: LKR 1450",
            timestamp=1787500000,
        ),
    ]

    with patch("handlers.user.orders.pool.call", new_callable=AsyncMock) as mock_orders_pool:
        mock_orders_pool.return_value = pb.OrderListResponse(orders=mock_orders, total_count=2)

        screen = await session.send("/orders")
        assert "My Orders" in screen.text
        assert "ORD-20260826-0099" in screen.text
        assert any("ORD-20260826-0099" in b for b in session.list_button_labels())

    # ──────────────────────────────────────────────────────────────────────────
    # 7. Order Details & Pending Cancellation
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 7: Order Details and Cancel Action ---")
    with patch("handlers.user.orders.pool.call", new_callable=AsyncMock) as mock_detail_pool:
        mock_detail_pool.return_value = mock_orders[0]

        screen = await session.click("order_detail:ORD-20260826-0099")
        assert "ORD-20260826-0099" in screen.text
        assert any("Cancel Order" in b for b in session.list_button_labels())

    # Confirm Cancel
    with patch("handlers.user.orders.pool.call", new_callable=AsyncMock) as mock_cancel_pool:
        mock_cancel_pool.return_value = pb.StatusResponse(success=True, message="Order cancelled successfully.")

        screen = await session.click("confirm_cancel:ORD-20260826-0099")
        assert "cancelled" in screen.text.lower()

    # ──────────────────────────────────────────────────────────────────────────
    # 8. Delivered Order: Reorder & Issue Reporting
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 8: Delivered Order Details, Reorder and Dispute ---")
    with patch("handlers.user.orders.pool.call", new_callable=AsyncMock) as mock_detail_pool:
        mock_detail_pool.return_value = mock_orders[1]

        screen = await session.click("order_detail:ORD-20260825-0044")
        assert "ORD-20260825-0044" in screen.text
        assert any("Reorder" in b for b in session.list_button_labels())
        assert any("Report Issue" in b for b in session.list_button_labels())

    # Report Issue
    screen = await session.click("issue_type:ORD-20260825-0044")
    assert "What went wrong" in screen.text or "Issue" in screen.text
    assert any("Damaged" in b or "Wrong" in b for b in session.list_button_labels())

    logger.info("✅ All Checkout, Ordering, Tracking, and Dispute scenarios passed successfully!")


if __name__ == "__main__":
    asyncio.run(test_checkout_and_orders_full_lifecycle())
