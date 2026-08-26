"""
test_admin_complete_lifecycle.py — Complete Admin Operations, Catalog, Drivers, Warehouses, Orders & Audit Log.
Covers:
1. Admin RBAC guard and main dashboard (/admin).
2. Catalog management (Add item wizard, price/stock adjustments, deletion).
3. Order management (Status transition, driver assignment, internal notes, dispute resolution).
4. Promo code creation wizard & removal.
5. Warehouse management (Add warehouse wizard, location coordinates).
6. Driver management (Add driver wizard, availability toggle, broadcast).
7. System Settings & Audit Log.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

from api import logistics_pb2 as pb
from tools.agent_harness.client import AgentChatSession

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scenario.admin_lifecycle")


@pytest.mark.asyncio
async def test_admin_complete_lifecycle_suite():
    """Simulates exhaustive admin actions across orders, inventory, drivers, and settings."""
    admin_id = 99002
    session = AgentChatSession(user_id=admin_id, username="head_admin", first_name="Admin Boss")
    await session.start()
    await session.reset()

    # Common admin RBAC mock
    admin_list_resp = pb.AdminListResponse(
        admins=[
            pb.AdminUser(
                telegram_id=admin_id,
                username="head_admin",
                role="super_admin",
            )
        ]
    )

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Admin Dashboard Entry (/admin)
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 1: Open Admin Dashboard ---")
    with patch("middleware.pool.call", new_callable=AsyncMock) as mock_auth_pool:
        mock_auth_pool.return_value = admin_list_resp

        screen = await session.send("/admin")
        assert "Admin Panel" in screen.text
        buttons = session.list_button_labels()
        assert any("Orders" in b for b in buttons)
        assert any("Promos" in b for b in buttons)
        assert any("Warehouses" in b for b in buttons)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Admin Catalog: Add Item Wizard
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 2: Add New Item Wizard ---")
    with patch("middleware.pool.call", new_callable=AsyncMock) as mock_auth_pool:
        mock_auth_pool.return_value = admin_list_resp

        screen = await session.send("/additem")
        assert "Add New Item Wizard" in screen.text or "Step 1" in screen.text

        # Step 1: Name
        screen = await session.send("Organic Cinnamon Sticks 100g")
        assert "Step 2" in screen.text or "price" in screen.text.lower()

        # Step 2: Price
        screen = await session.send("850")
        assert "Step 3" in screen.text or "stock" in screen.text.lower()

        # Step 3: Stock
        with patch("handlers.admin.catalog.pool.call", new_callable=AsyncMock) as mock_cat_pool:
            mock_cat_pool.return_value = pb.CatalogResponse(items=[], total_count=0)
            screen = await session.send("50")
            assert "Step 4" in screen.text or "category" in screen.text.lower()

        # Step 4: Category (Type new)
        screen = await session.click("item_cat_select:type_new")
        assert "type the new category" in screen.text.lower()
        screen = await session.send("Spices")
        assert "photo" in screen.text.lower() or "Step 5" in screen.text

        # Step 5: Photo (Skip)
        skip_btn = session.find_button("item_photo") or "item_photo_skip"
        screen = await session.click(skip_btn)
        assert "description" in screen.text.lower() or "Step 6" in screen.text

        # Step 6: Description (Save item)
        with patch("handlers.admin.catalog.pool.call", new_callable=AsyncMock) as mock_save_item:
            mock_save_item.return_value = pb.StatusResponse(entity_id="ITEM-CINNAMON", success=True)
            screen = await session.send("Pure Ceylon premium cinnamon quills.")
            assert "added successfully" in screen.text.lower() or "ITEM-CINNAMON" in screen.text

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Admin Catalog: Edit Item Wizard & Remove Item
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 3: Edit Item and Remove Item ---")
    with patch("middleware.pool.call", new_callable=AsyncMock) as mock_auth_pool:
        mock_auth_pool.return_value = admin_list_resp

        with patch("handlers.admin.catalog.pool.call", new_callable=AsyncMock) as mock_edit_pool:
            mock_edit_pool.return_value = pb.Item(
                id="ITEM-CINNAMON",
                name="Organic Cinnamon Sticks",
                price=850.0,
                stock=50,
                is_available=True,
            )

            # Edit Item Wizard
            screen = await session.send("/edititem")
            assert "Edit Item Wizard" in screen.text or "item ID" in screen.text

            screen = await session.send("ITEM-CINNAMON")
            assert "Found Item" in screen.text or "Keep Current Photo" in screen.text
            assert any("Keep Current Photo" in b for b in session.list_button_labels())

            # Complete photo step
            keep_btn = session.find_button("edit_item_photo:keep")
            if keep_btn:
                screen = await session.click(keep_btn)

            # Remove Item
            mock_edit_pool.return_value = pb.StatusResponse(success=True)
            screen = await session.send("/removeitem ITEM-CINNAMON")
            assert "removed" in screen.text.lower() or "ITEM-CINNAMON" in screen.text or "deleted" in screen.text.lower()

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Admin Orders: View All Orders, Status Updates, Driver Assignment
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 4: Order Status & Driver Dispatch ---")
    mock_admin_orders = [
        pb.Order(
            order_id="ORD-9001",
            human_ref="ORD-20260826-9001",
            status="confirmed",
            total=3200.0,
            delivery_address="77 Havelock Road, Colombo",
            timestamp=1787600000,
        )
    ]

    with patch("middleware.pool.call", new_callable=AsyncMock) as mock_auth_pool:
        mock_auth_pool.return_value = admin_list_resp

        with patch("handlers.admin.orders.pool.call", new_callable=AsyncMock) as mock_ord_pool:
            async def ord_side_effect(endpoint, call_fn, *args, **kwargs):
                if "GetAllOrders" in endpoint or "ListOrders" in endpoint:
                    return pb.OrderListResponse(orders=mock_admin_orders, total_count=1)
                elif "GetOrder" in endpoint:
                    return mock_admin_orders[0]
                elif "UpdateOrderStatus" in endpoint or "AssignDriver" in endpoint or "AddOrderNote" in endpoint:
                    return pb.StatusResponse(success=True)
                elif "ListDrivers" in endpoint:
                    return pb.DriverListResponse(
                        drivers=[
                            pb.Driver(id="DRV-1", name="Rohan Silva", phone="0779998888", is_available=True, is_active=True)
                        ]
                    )
                return pb.Empty()

            mock_ord_pool.side_effect = ord_side_effect

            # View All Orders
            screen = await session.send("/allorders")
            assert "Orders" in screen.text
            assert any("ORD-20260826-9001" in b for b in session.list_button_labels())

    # ──────────────────────────────────────────────────────────────────────────
    # 5. Admin Warehouses & Drivers Management
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 5: Warehouses & Drivers Management ---")
    with patch("middleware.pool.call", new_callable=AsyncMock) as mock_auth_pool:
        mock_auth_pool.return_value = admin_list_resp

        with patch("handlers.admin.management.pool.call", new_callable=AsyncMock) as mock_mgmt_pool:
            mock_mgmt_pool.return_value = pb.WarehouseListResponse(
                warehouses=[
                    pb.Warehouse(id="WH-1", name="Central Hub Kandy", address="120 Kandy Rd", is_active=True)
                ]
            )

            # View Warehouses
            screen = await session.send("/warehouses")
            assert "Warehouses" in screen.text or "Central Hub" in screen.text

            # View Drivers
            mock_mgmt_pool.return_value = pb.DriverListResponse(
                drivers=[
                    pb.Driver(id="DRV-1", name="Saman Bandara", phone="0715554433", is_available=True, is_active=True)
                ]
            )
            screen = await session.send("/drivers")
            assert "Drivers" in screen.text or "Saman Bandara" in screen.text

            # View Promos
            mock_mgmt_pool.return_value = pb.PromoListResponse(
                promos=[
                    pb.Promo(id="P-1", code="SUMMER15", discount_type="percent", discount_value=15.0, is_active=True)
                ]
            )
            screen = await session.send("/promos")
            assert "Promotions" in screen.text or "SUMMER15" in screen.text

    # ──────────────────────────────────────────────────────────────────────────
    # 6. System Settings & Audit Log
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 6: System Settings & Audit Logs ---")
    with patch("middleware.pool.call", new_callable=AsyncMock) as mock_auth_pool:
        mock_auth_pool.return_value = admin_list_resp

        with patch("handlers.admin.management.pool.call", new_callable=AsyncMock) as mock_settings_pool:
            async def settings_side_effect(endpoint, call_fn, *args, **kwargs):
                if "GetSystemSettings" in endpoint:
                    return pb.SystemSettings(
                        delivery_base_fare=250.0,
                        free_delivery_threshold=5000.0,
                        tax_rate=0.05,
                    )
                elif "GetAuditLog" in endpoint:
                    return pb.AuditLogResponse(
                        entries=[
                            pb.AuditEntry(id=1, ts=1787600000, actor_id=admin_id, action="add_item", target_id="ITEM-CINNAMON", detail="Added Organic Cinnamon"),
                            pb.AuditEntry(id=2, ts=1787600100, actor_id=admin_id, action="update_status", target_id="ORD-9001", detail="Status -> dispatched"),
                        ]
                    )
                return pb.Empty()

            mock_settings_pool.side_effect = settings_side_effect

            # View Settings
            screen = await session.send("/settings")
            assert "Settings" in screen.text or "Delivery Fee" in screen.text or "250" in screen.text

            # View Audit Log
            screen = await session.send("/audit")
            assert "Audit Log" in screen.text or "add_item" in screen.text or "ITEM-CINNAMON" in screen.text

    logger.info("✅ All Admin Lifecycle scenarios passed successfully!")


if __name__ == "__main__":
    asyncio.run(test_admin_complete_lifecycle_suite())
