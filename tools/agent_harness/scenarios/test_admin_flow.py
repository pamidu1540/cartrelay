"""
test_admin_flow.py — Administrator wizard flow simulation test scenario.
Exercises: /admin -> Navigation -> Promos list -> Create Promo Wizard -> Backend verification.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

from api import logistics_pb2 as pb
from tools.agent_harness.client import AgentChatSession

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scenario.admin")


@pytest.mark.asyncio
async def test_admin_panel_and_promo_wizard():
    """Simulates an admin user navigating the dashboard and running the promo wizard."""
    # Using admin user ID
    session = AgentChatSession(user_id=99001, username="admin_tester", first_name="SuperAdmin")
    await session.start()
    await session.reset()

    # Mock admin check in middleware
    with patch("middleware.pool.call", new_callable=AsyncMock) as mock_admin_check:
        mock_admin_check.return_value = pb.AdminListResponse(
            admins=[
                pb.AdminUser(
                    telegram_id=99001,
                    username="admin_tester",
                    role="super_admin",
                )
            ]
        )

        # 1. Open Admin Panel
        screen = await session.send("/admin")
        assert "Admin Panel" in screen.text
        buttons = session.list_button_labels()
        assert any("Orders" in b for b in buttons)
        assert any("Promos" in b for b in buttons)
        assert any("Warehouses" in b for b in buttons)

        # 2. Open Promos section and run wizard
        with patch("handlers.admin.management.pool.call", new_callable=AsyncMock) as mock_mgmt_call:
            async def mgmt_side_effect(endpoint, call_fn, *args, **kwargs):
                if "ListPromos" in endpoint:
                    return pb.PromoListResponse(
                        promos=[
                            pb.Promo(
                                id="PROMO-1",
                                code="WELCOME10",
                                discount_type="percent",
                                discount_value=10.0,
                                is_active=True,
                            ),
                            pb.Promo(
                                id="PROMO-2",
                                code="SUMMER20",
                                discount_type="percent",
                                discount_value=20.0,
                                is_active=True,
                            ),
                        ]
                    )
                elif "AddPromo" in endpoint:
                    return pb.StatusResponse(entity_id="PROMO-SUMMER20", success=True)
                return pb.Empty()

            mock_mgmt_call.side_effect = mgmt_side_effect

            screen = await session.click("admin_menu:promos")
            assert "Promotions" in screen.text
            assert any("Create Promo" in b for b in session.list_button_labels())

            # 3. Start Promo Creation Wizard
            screen = await session.click("promo_add:start")
            assert "Create Promo Code Wizard" in screen.text or "Promo Code" in screen.text

            # 4. Step 1: Send Promo Code Name
            screen = await session.send("SUMMER20")
            assert "discount type" in screen.text.lower()

            # 5. Step 2: Choose Percentage
            screen = await session.click("promo_flow_type:percent")
            assert "percentage value" in screen.text.lower()

            # 6. Step 3: Enter Value
            screen = await session.send("20")
            assert "minimum order" in screen.text.lower()

            # 7. Step 4: Enter Minimum Order & Save
            screen = await session.send("1000")
            assert "created" in screen.text.lower() or "SUMMER20" in screen.text

    logger.info("✅ Admin Panel & Promo Wizard scenario passed successfully!")


if __name__ == "__main__":
    asyncio.run(test_admin_panel_and_promo_wizard())
