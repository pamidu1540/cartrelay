"""
server.py — FastAPI Gateway for the Agent Test Proxy & Simulation Harness.
Exposes REST endpoints on port 8090 so AI Agents can interact with Telegram Bot and Logistics Engine simultaneously.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from tools.agent_harness.runner import harness

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("agent_harness.server")


# ── Request / Response Models ────────────────────────────────────────────────

class SendTextRequest(BaseModel):
    user_id: int = Field(..., description="Simulated Telegram User/Chat ID (e.g. 1001)")
    text: str = Field(..., description="Command (e.g. /start, /catalog, /cart) or user text response")
    username: str = Field("agent_tester", description="Simulated Telegram username")
    first_name: str = Field("Agent", description="Simulated Telegram user first name")


class ClickButtonRequest(BaseModel):
    user_id: int = Field(..., description="Simulated Telegram User/Chat ID (e.g. 1001)")
    callback_data: str = Field(..., description="Inline button callback_data (e.g. cat_select:all:0, checkout)")
    username: str = Field("agent_tester", description="Simulated Telegram username")


class ShareLocationRequest(BaseModel):
    user_id: int = Field(..., description="Simulated Telegram User/Chat ID (e.g. 1001)")
    latitude: float = Field(..., description="GPS Latitude (e.g. 6.9271)")
    longitude: float = Field(..., description="GPS Longitude (e.g. 79.8612)")
    username: str = Field("agent_tester", description="Simulated Telegram username")


class ResetSessionRequest(BaseModel):
    user_id: int | None = Field(None, description="Optional user ID to reset. If omitted, resets all sessions.")


# ── Application Lifecycle ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Agent Test Harness server...")
    await harness.start()
    yield
    logger.info("Shutting down Agent Test Harness server...")
    await harness.stop()


app = FastAPI(
    title="Logistics Engine — Agent Test Proxy & Harness",
    description=(
        "Local simulation proxy allowing AI Agents and E2E test suites to interact "
        "with Telegram Bot flows and verify Go Logistics Engine states in real-time."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ── Health Check ─────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok", "service": "agent-test-harness", "version": "1.0.0"}


# ── Simulated Telegram Chat Endpoints ────────────────────────────────────────

@app.post("/api/v1/chat/send", tags=["Simulated Chat"])
async def send_message(req: SendTextRequest) -> dict[str, Any]:
    """
    Simulate user typing a text message or slash command (e.g. `/start`, `/catalog`, `/cart`, `/admin`).
    Returns the updated active screen state including text and inline buttons.
    """
    screen = await harness.send_text(
        user_id=req.user_id,
        text=req.text,
        username=req.username,
        first_name=req.first_name,
    )
    return screen.to_dict()


@app.post("/api/v1/chat/click", tags=["Simulated Chat"])
async def click_button(req: ClickButtonRequest) -> dict[str, Any]:
    """
    Simulate clicking an inline keyboard button with the specified callback_data.
    Returns the updated active screen state after the button handler finishes.
    """
    screen = await harness.click_button(
        user_id=req.user_id,
        callback_data=req.callback_data,
        username=req.username,
    )
    return screen.to_dict()


@app.post("/api/v1/chat/location", tags=["Simulated Chat"])
async def share_location(req: ShareLocationRequest) -> dict[str, Any]:
    """
    Simulate user tapping the Telegram 'Share Location' button with GPS coordinates.
    """
    screen = await harness.share_location(
        user_id=req.user_id,
        latitude=req.latitude,
        longitude=req.longitude,
        username=req.username,
    )
    return screen.to_dict()


@app.get("/api/v1/chat/screen/{user_id}", tags=["Simulated Chat"])
async def get_screen(user_id: int) -> dict[str, Any]:
    """
    Inspect the active message screen, markdown text, and clickable buttons for a user.
    """
    return harness.get_screen(user_id).to_dict()


@app.get("/api/v1/chat/transcript/{user_id}", tags=["Simulated Chat"])
async def get_transcript(user_id: int) -> list[dict[str, Any]]:
    """
    Fetch the full chronological history of incoming and outgoing messages for a user session.
    """
    return harness.get_transcript(user_id)


@app.post("/api/v1/session/reset", tags=["Simulated Chat"])
async def reset_session(req: ResetSessionRequest | None = None) -> dict[str, str]:
    """
    Reset conversation state, active screens, and in-memory cart caches.
    """
    user_id = req.user_id if req else None
    await harness.reset_session(user_id)
    return {"status": "ok", "message": f"Session {'all' if user_id is None else user_id} reset."}


# ── Dual-Assertion & Backend Inspection Endpoints ────────────────────────────

@app.get("/api/v1/inspect/cart/{user_id}", tags=["Backend Inspection"])
async def inspect_cart(user_id: int) -> list[dict[str, Any]]:
    """
    Directly query the bot's CartManager / Go Engine to inspect cart items and quantities.
    """
    return await harness.inspect_cart(user_id)


@app.get("/api/v1/inspect/grpc/order/{order_id}", tags=["Backend Inspection"])
async def inspect_order(order_id: str, actor_id: int = 0) -> dict[str, Any]:
    """
    Directly query the Go Logistics Engine gRPC service to fetch an order's status, items, and total.
    """
    result = await harness.inspect_order(order_id, actor_id=actor_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/api/v1/inspect/grpc/catalog", tags=["Backend Inspection"])
async def inspect_catalog(page: int = 0, page_size: int = 50) -> list[dict[str, Any]]:
    """
    Directly query the Go Logistics Engine gRPC service to fetch live catalog items and stock levels.
    """
    return await harness.inspect_catalog(page=page, page_size=page_size)


# ── Standalone CLI Runner ────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("tools.agent_harness.server:app", host="127.0.0.1", port=8090, reload=False)
