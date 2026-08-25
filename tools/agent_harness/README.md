# AI Agent Test Proxy & Simulation Harness

An isolated, local simulation proxy allowing AI Agents, automated QA runners, and developers to test and verify the Telegram Bot frontend and Go Logistics Engine backend simultaneously without needing a real Telegram account or live Telegram Cloud connection.

---

## Architecture

```
AI Agent (HTTP / SDK) ──▶ FastAPI Simulation Gateway (Port 8090)
                               │
                               ▼
                    Mock Telegram Dispatcher
                    (Tracks real-time screen markdown,
                     inline buttons & reply keyboards)
                               │
                               ▼
                    Bot Handlers & CartManager
                               │ (gRPC)
                               ▼
                    Go Logistics Engine (Port 50051)
```

---

## Features

1. **Telegram UI Emulation**:
   - Send commands (`/start`, `/catalog`, `/cart`, `/admin`, `/profile`) and user text responses.
   - Click inline buttons with `callback_data`.
   - Send GPS location coordinates.
   - Inspect active screen markdown, photo file IDs, and interactive buttons.
2. **Dual-Assertion & Verification**:
   - Inspect CartManager in-memory/persisted cart states directly.
   - Inspect Go Engine orders, catalogs, and warehouses via direct gRPC calls.
3. **Completely Isolated**:
   - Resides strictly in `tools/agent_harness/` without altering production bot code or Docker configurations.

---

## Quickstart

### 1. Launching the HTTP Simulation Server
```powershell
uv run --directory bot python -m uvicorn tools.agent_harness.server:app --port 8090
```
Interactive OpenAPI documentation will be available at: `http://127.0.0.1:8090/docs`

### 2. Running Automated Agent Scenarios
```powershell
uv run --directory bot pytest tools/agent_harness/scenarios/
```

### 3. Programmatic Usage (Python SDK)
```python
from tools.agent_harness.client import AgentChatSession

async def main():
    session = AgentChatSession(user_id=1001, username="test_user")
    await session.start()

    # Send /start
    screen = await session.send("/start")
    print("Bot replied:", screen.text)
    print("Available buttons:", session.list_button_labels())

    # Click Browse Catalog
    screen = await session.click("cat_select:all:0")
    print("Catalog screen:", screen.text)

    # Inspect Cart in Backend Engine
    cart = await session.inspect_cart()
    print("Backend cart state:", cart)
```
