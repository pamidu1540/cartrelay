"""
client.py — Agent Test Client SDK for interacting with the simulation proxy.
Supports both Direct In-Process testing and Remote HTTP REST testing.
"""

from __future__ import annotations

from typing import Any

from tools.agent_harness.mock_bot import ScreenState
from tools.agent_harness.runner import harness


class AgentChatSession:
    """Convenient client SDK for an AI Agent to conduct automated conversational sessions."""

    def __init__(
        self,
        user_id: int = 1001,
        username: str = "agent_tester",
        first_name: str = "Agent",
    ) -> None:
        self.user_id = user_id
        self.username = username
        self.first_name = first_name

    async def start(self) -> None:
        """Initialise test harness application."""
        await harness.start()

    async def send(self, text: str) -> ScreenState:
        """Send a message or command (e.g. '/start', '/catalog', 'SAVE10')."""
        return await harness.send_text(
            user_id=self.user_id,
            text=text,
            username=self.username,
            first_name=self.first_name,
        )

    async def click(self, callback_data: str) -> ScreenState:
        """Click an inline keyboard button by its callback_data."""
        return await harness.click_button(
            user_id=self.user_id,
            callback_data=callback_data,
            username=self.username,
        )

    async def share_location(self, latitude: float, longitude: float) -> ScreenState:
        """Share GPS location coordinates."""
        return await harness.share_location(
            user_id=self.user_id,
            latitude=latitude,
            longitude=longitude,
            username=self.username,
        )

    def get_screen(self) -> ScreenState:
        """Retrieve current screen state."""
        return harness.get_screen(self.user_id)

    def get_transcript(self) -> list[dict[str, Any]]:
        """Retrieve full transcript."""
        return harness.get_transcript(self.user_id)

    async def reset(self) -> None:
        """Reset conversation session."""
        await harness.reset_session(self.user_id)

    def find_button(self, prefix_or_match: str) -> str | None:
        """Helper to find a button callback_data matching a given prefix or text."""
        screen = self.get_screen()
        for row in screen.inline_keyboard:
            for btn in row:
                if btn.callback_data and (btn.callback_data.startswith(prefix_or_match) or prefix_or_match in btn.text):
                    return btn.callback_data
        return None

    def list_button_labels(self) -> list[str]:
        """List text labels of all currently visible inline buttons."""
        screen = self.get_screen()
        labels: list[str] = []
        for row in screen.inline_keyboard:
            for btn in row:
                labels.append(btn.text)
        return labels

    # ── Backend Verifications ────────────────────────────────────────────────

    async def inspect_cart(self) -> list[dict[str, Any]]:
        return await harness.inspect_cart(self.user_id)

    async def inspect_order(self, order_id: str) -> dict[str, Any]:
        return await harness.inspect_order(order_id, actor_id=self.user_id)

    async def inspect_catalog(self) -> list[dict[str, Any]]:
        return await harness.inspect_catalog()
