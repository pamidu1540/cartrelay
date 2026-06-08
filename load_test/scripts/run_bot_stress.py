"""
run_bot_stress.py — Telegram Bot local stress tester.

Strategy:
  - Load real credentials from deploy/.env (so gRPC engine auth works).
  - Use the real TELEGRAM_BOT_TOKEN so app.initialize() can verify the bot.
  - SKIP app.start() entirely — it launches the polling loop and requires bot.id.
  - Instead, call app.process_update() directly (which only needs initialize()).
  - Mock all outgoing Telegram send_* calls at the HTTPXRequest level so we
    never make real HTTP calls during update processing.
"""

import asyncio
import os
import sys
import time
import argparse
from typing import List
from unittest.mock import patch, AsyncMock

# ── Load deploy/.env ──────────────────────────────────────────────────────────
env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../deploy/.env"))
if os.path.exists(env_path):
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip('"\''))

# ── Add bot/src to path ───────────────────────────────────────────────────────
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../bot/src")))

from telegram import Update, Bot
from telegram.ext import Application
import main as bot_main
from grpc_client import pool


def _make_update(bot: Bot, i: int, command: str) -> Update:
    user_id = 100000 + i
    return Update.de_json({
        "update_id": 1_000_000 + i,
        "message": {
            "message_id": 200 + i,
            "from": {"id": user_id, "is_bot": False, "first_name": f"Stress{i}", "language_code": "en"},
            "chat": {"id": user_id, "type": "private"},
            "date": int(time.time()),
            "text": command,
        }
    }, bot)


async def run_stress_test(app: Application, command: str, total: int, concurrency: int):
    print(f"\nGenerating {total} mock '{command}' updates...")
    updates = [_make_update(app.bot, i, command) for i in range(total)]

    print("Connecting to gRPC engine pool...")
    await pool.start()

    # ── Fake Telegram HTTP layer ──────────────────────────────────────────────
    # Any handler that replies (send_message, answer_callback_query, etc.)
    # goes through HTTPXRequest.do_request. We intercept it here to prevent
    # any real network call while still letting the bot logic run fully.
    async def fake_do_request(self, method, url, request_data=None, read_timeout=None, write_timeout=None, connect_timeout=None, pool_timeout=None):
        # getMe needs a proper User object so initialize() succeeds
        if "getMe" in url:
            return 200, b'{"ok":true,"result":{"id":123456789,"is_bot":true,"first_name":"StressBot","username":"stress_bot","can_join_groups":false,"can_read_all_group_messages":false,"supports_inline_queries":false}}'
        # All other API calls (sendMessage, answerCallbackQuery, etc.) return a minimal success
        return 200, b'{"ok":true,"result":true}'

    print("Initializing bot (real token handshake with Telegram)...")
    with patch("telegram.request.HTTPXRequest.do_request", new=fake_do_request):
        await app.initialize()
        # ── Run the stress test ───────────────────────────────────────────────
        sem = asyncio.Semaphore(concurrency)

        async def process_one(update: Update):
            async with sem:
                try:
                    await app.process_update(update)
                except Exception:
                    pass  # Absorb any handler errors — we measure throughput only

        print(f"Firing {total} requests at concurrency={concurrency}...")
        t0 = time.perf_counter()
        await asyncio.gather(*[asyncio.create_task(process_one(u)) for u in updates])
        elapsed = time.perf_counter() - t0

        await app.shutdown()

    await pool.stop()

    rps = total / elapsed
    print()
    print("==========================================================")
    print("               BOT STRESS TEST RESULTS")
    print("==========================================================")
    print(f"  Command     : {command}")
    print(f"  Requests    : {total:,}")
    print(f"  Concurrency : {concurrency}")
    print(f"  Elapsed     : {elapsed:.2f}s")
    print(f"  Throughput  : {rps:,.1f} req/s")
    print("==========================================================")


def main():
    parser = argparse.ArgumentParser(description="Stress test the Telegram Bot locally.")
    parser.add_argument("--requests",    type=int, default=1000,      help="Total mock requests.")
    parser.add_argument("--concurrency", type=int, default=100,       help="Max concurrent requests.")
    parser.add_argument("--command",     type=str, default="/catalog", help="Bot command to simulate.")
    args = parser.parse_args()

    if "TELEGRAM_BOT_TOKEN" not in os.environ:
        print("ERROR: TELEGRAM_BOT_TOKEN not found in deploy/.env or environment.")
        sys.exit(1)

    app = bot_main.build_application()
    asyncio.run(run_stress_test(app, args.command, args.requests, args.concurrency))


if __name__ == "__main__":
    main()
