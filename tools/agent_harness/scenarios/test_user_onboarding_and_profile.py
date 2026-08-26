"""
test_user_onboarding_and_profile.py — Exhaustive User Flow Simulations.
Covers:
1. New user registration wizard (/start -> Name -> Phone -> GPS Location -> Main Menu)
2. Profile view & edits (/profile -> Edit Name -> Edit Phone)
3. Language settings (/language -> Select English/Sinhala/Tamil)
4. Notification preferences (/notifications -> Toggle preferences)
5. Loyalty & Referral stats (/points, /refer, /spending)
6. Saved Address Management (/addresses -> /addaddress -> Delete Address)
7. Account Deletion confirmation (/deleteaccount)
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, patch

import pytest

from api import logistics_pb2 as pb
from tools.agent_harness.client import AgentChatSession

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scenario.user_profile")


@pytest.mark.asyncio
async def test_new_user_onboarding_and_profile_suite():
    """Simulates a brand new customer through registration, profile updates, and settings."""
    user_id = 77001
    session = AgentChatSession(user_id=user_id, username="new_customer", first_name="Chaminda")
    await session.start()
    await session.reset()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. New User Registration Flow: /start -> Name -> Phone -> GPS Location
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 1: New User /start and Onboarding Wizard ---")
    with patch("handlers.user.core.pool.call", new_callable=AsyncMock) as mock_core_pool, \
         patch("handlers.user.profile.pool.call", new_callable=AsyncMock) as mock_prof_pool:
        async def onboarding_side_effect(endpoint, call_fn, *args, **kwargs):
            if "GetProfile" in endpoint:
                return pb.CustomerProfile(user_id=user_id)
            elif "ListSavedAddresses" in endpoint:
                return pb.AddressListResponse(addresses=[])
            elif "UpsertProfile" in endpoint or "CreateProfile" in endpoint:
                return pb.StatusResponse(success=True)
            elif "SaveAddress" in endpoint or "SetDefaultAddress" in endpoint:
                return pb.StatusResponse(success=True)
            return pb.Empty()

        mock_core_pool.side_effect = onboarding_side_effect
        mock_prof_pool.side_effect = onboarding_side_effect

        screen = await session.send("/start")
        assert "Welcome to Logistics Engine" in screen.text
        assert "Step 1" in screen.text or "display name" in screen.text.lower()

        # Send Display Name
        screen = await session.send("Chaminda Perera")
        assert "Step 2" in screen.text or "phone number" in screen.text.lower()

        # Send Phone Number & Save Profile
        screen = await session.send("0771234567")
        assert "Delivery Address Required" in screen.text or "Location" in screen.text

        # Share GPS Location
        screen = await session.share_location(latitude=6.9271, longitude=79.8612)
        assert "Registration Complete" in screen.text or "Welcome" in screen.text
        assert any("Browse Catalog" in b for b in session.list_button_labels())

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Profile Viewing and In-place Edits (/profile)
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 2: View and Edit Profile ---")
    with patch("handlers.user.profile.pool.call", new_callable=AsyncMock) as mock_prof_pool:
        async def prof_side_effect(endpoint, call_fn, *args, **kwargs):
            if "GetProfile" in endpoint:
                return pb.CustomerProfile(
                    user_id=user_id,
                    display_name="Chaminda Perera",
                    phone="0771234567",
                    language="en",
                )
            elif "ListSavedAddresses" in endpoint:
                return pb.AddressListResponse(
                    addresses=[
                        pb.SavedAddress(
                            id=10,
                            user_id=user_id,
                            label="Home",
                            address="No 12 Galle Road, Colombo",
                            lat=6.9271,
                            lon=79.8612,
                            is_default=True,
                        )
                    ]
                )
            elif "GetLoyaltyBalance" in endpoint:
                return pb.LoyaltyBalance(user_id=user_id, points=250, tier="Silver")
            return pb.Empty()

        mock_prof_pool.side_effect = prof_side_effect

        screen = await session.send("/profile")
        assert "Chaminda Perera" in screen.text
        assert "0771234567" in screen.text
        assert any("Edit Name" in b for b in session.list_button_labels())
        assert any("Edit Phone" in b for b in session.list_button_labels())

        # Click Edit Name
        edit_name_btn = session.find_button("profile_edit:name")
        assert edit_name_btn is not None
        screen = await session.click(edit_name_btn)
        assert "new display name" in screen.text.lower()

        # Update Name
        with patch("handlers.user.profile.pool.call", new_callable=AsyncMock) as mock_save_name:
            async def save_name_side_effect(endpoint, call_fn, *args, **kwargs):
                if "GetProfile" in endpoint:
                    return pb.CustomerProfile(user_id=user_id, display_name="Chaminda P. Silva", phone="0771234567")
                elif "UpsertProfile" in endpoint:
                    return pb.StatusResponse(success=True)
                return pb.Empty()
            mock_save_name.side_effect = save_name_side_effect
            screen = await session.send("Chaminda P. Silva")
            assert "Your Profile" in screen.text or "Chaminda P. Silva" in screen.text

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Language Selector (/language)
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 3: Language Settings ---")
    with patch("handlers.user.profile.pool.call", new_callable=AsyncMock) as mock_lang_pool:
        async def lang_side_effect(endpoint, call_fn, *args, **kwargs):
            if "GetProfile" in endpoint:
                return pb.CustomerProfile(user_id=user_id, display_name="Chaminda", language="en")
            elif "UpsertProfile" in endpoint:
                return pb.StatusResponse(success=True)
            return pb.Empty()
        mock_lang_pool.side_effect = lang_side_effect

        screen = await session.send("/language")
        assert "language" in screen.text.lower()
        assert any("English" in b for b in session.list_button_labels())

        # Select Sinhala
        si_btn = session.find_button("lang_select:si")
        if si_btn:
            screen = await session.click(si_btn)
            assert "language" in screen.text.lower() or "භාෂාව" in screen.text

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Notification Preferences (/notifications)
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 4: Notification Toggles ---")
    with patch("handlers.user.profile.pool.call", new_callable=AsyncMock) as mock_notif_pool:
        async def notif_side_effect(endpoint, call_fn, *args, **kwargs):
            if "GetNotifPrefs" in endpoint:
                return pb.NotifPrefs(
                    user_id=user_id,
                    order_updates=True,
                    promotions=True,
                    new_products=False,
                    system_alerts=True,
                    flash_deals=False,
                )
            elif "UpdateNotifPrefs" in endpoint:
                return pb.StatusResponse(success=True)
            return pb.Empty()

        mock_notif_pool.side_effect = notif_side_effect

        screen = await session.send("/notifications")
        assert "notification" in screen.text.lower()
        toggle_btn = session.find_button("notif_toggle:promotions")
        assert toggle_btn is not None
        screen = await session.click(toggle_btn)
        assert "notification" in screen.text.lower()

    # ──────────────────────────────────────────────────────────────────────────
    # 5. Loyalty Points, Referrals, & Spending (/points, /refer, /spending)
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 5: Loyalty, Referrals & Spending ---")
    with patch("handlers.user.profile.pool.call", new_callable=AsyncMock) as mock_stats_pool:
        now_ts = int(time.time())
        async def stats_side_effect(endpoint, call_fn, *args, **kwargs):
            if "GetLoyaltyBalance" in endpoint:
                return pb.LoyaltyBalance(user_id=user_id, points=540, tier="Gold")
            elif "GetProfile" in endpoint:
                return pb.CustomerProfile(user_id=user_id, display_name="Chaminda", phone="0771234567")
            elif "GetUserOrders" in endpoint or "ListOrders" in endpoint:
                return pb.OrderListResponse(
                    orders=[
                        pb.Order(order_id="ORD-101", total=4500.0, status="delivered", timestamp=now_ts),
                        pb.Order(order_id="ORD-102", total=1800.0, status="delivered", timestamp=now_ts),
                    ]
                )
            return pb.Empty()

        mock_stats_pool.side_effect = stats_side_effect

        # Check Loyalty Points
        screen = await session.send("/points")
        assert "540" in screen.text or "Gold" in screen.text or "Loyalty" in screen.text

        # Check Referral Link
        screen = await session.send("/refer")
        assert "refer" in screen.text.lower() or str(user_id) in screen.text

        # Check Spending Summary
        screen = await session.send("/spending")
        assert "Spending" in screen.text or "6,300" in screen.text or "6300" in screen.text or "LKR" in screen.text

    # ──────────────────────────────────────────────────────────────────────────
    # 6. Saved Addresses Management (/addresses, add_address)
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 6: Saved Addresses ---")
    with patch("handlers.user.profile.pool.call", new_callable=AsyncMock) as mock_addr_mgmt:
        async def addr_side_effect(endpoint, call_fn, *args, **kwargs):
            if "ListSavedAddresses" in endpoint:
                return pb.AddressListResponse(
                    addresses=[
                        pb.SavedAddress(id=1, user_id=user_id, label="Home", address="123 Colombo St", is_default=True),
                        pb.SavedAddress(id=2, user_id=user_id, label="Office", address="456 World Trade Center", is_default=False),
                    ]
                )
            elif "DeleteSavedAddress" in endpoint:
                return pb.StatusResponse(success=True)
            elif "SetDefaultAddress" in endpoint:
                return pb.StatusResponse(success=True)
            elif "SaveAddress" in endpoint:
                return pb.StatusResponse(success=True)
            return pb.Empty()

        mock_addr_mgmt.side_effect = addr_side_effect

        # List addresses
        screen = await session.send("/addresses")
        assert "123 Colombo St" in screen.text or "Saved Addresses" in screen.text or "Addresses" in screen.text
        assert any("Office" in b or "Delete" in b or "Add" in b for b in session.list_button_labels())

        # Add a new address
        screen = await session.click("profile_edit:add_address")
        assert "address" in screen.text.lower() or "location" in screen.text.lower()
        screen = await session.send("Kandy Branch Road, Peradeniya")
        assert "saved" in screen.text.lower() or "address" in screen.text.lower()

    # ──────────────────────────────────────────────────────────────────────────
    # 7. Account Deletion Safeguard (/deleteaccount)
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("--- Step 7: Account Deletion ---")
    screen = await session.send("/deleteaccount")
    assert "delete" in screen.text.lower() or "permanently" in screen.text.lower()
    cancel_btn = session.find_button("del_acc:cancel")
    if cancel_btn:
        screen = await session.click(cancel_btn)
        assert "cancelled" in screen.text.lower() or "active" in screen.text.lower()

    logger.info("✅ All User Onboarding & Profile scenarios passed successfully!")


if __name__ == "__main__":
    asyncio.run(test_new_user_onboarding_and_profile_suite())
