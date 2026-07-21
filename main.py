"""
MomentumMetrix Telegram Signal Copier
Monitors Telegram channels for trading signals and forwards to MT5 via webhook
"""
import asyncio
import os
import json
import logging
import httpx
from pyrogram import Client, filters
from pyrogram.types import Message
import anthropic

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

# Config from environment
API_ID       = int(os.environ['TELEGRAM_API_ID'])
API_HASH     = os.environ['TELEGRAM_API_HASH']
WEBHOOK_BASE = os.environ['WEBHOOK_BASE_URL']  # your Next.js app URL
ANTHROPIC_KEY = os.environ['ANTHROPIC_API_KEY']
PORT         = int(os.environ.get('PORT', 8000))

# Firestore REST API for reading user configs
FIREBASE_PROJECT = os.environ.get('FIREBASE_PROJECT_ID', 'mt5-dashboard-bd063')

# Anthropic client
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# Active Pyrogram clients per user
# { userId: { client, channels: [list] } }
active_clients: dict[str, dict] = {}


async def parse_signal_with_claude(message_text: str) -> dict | None:
    """Use Claude to extract trading signal from any message format"""
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"""You are a trading signal parser. Extract trading signal data from this Telegram message.

Message:
{message_text}

If this message contains a trading signal, respond with ONLY a JSON object:
{{
  "symbol": "XAUUSD",
  "action": "BUY",
  "entry": 3045.50,
  "sl": 3020.00,
  "tp1": 3070.00,
  "tp2": 3100.00,
  "tp3": null,
  "lotSize": null,
  "comment": "Telegram Signal"
}}

Rules:
- symbol: normalize to broker format (XAUUSD, EURUSD, GBPUSD etc)
- action: must be BUY, SELL, CLOSE_BUY, CLOSE_SELL, or CLOSE_ALL
- entry/sl/tp: extract price levels as numbers, null if not mentioned
- lotSize: extract if mentioned, null otherwise
- If this is NOT a trading signal (news, commentary, etc), respond with: {{"signal": false}}

Respond with JSON only, no explanation."""
            }]
        )

        text = response.content[0].text.strip()
        data = json.loads(text)

        if data.get("signal") is False:
            return None

        if not data.get("symbol") or not data.get("action"):
            return None

        return data

    except Exception as e:
        log.error(f"Claude parse error: {e}")
        return None


async def forward_signal(user_id: str, account_id: str | None, signal: dict, channel: str):
    """Forward parsed signal to MomentumMetrix webhook"""
    try:
        payload = {
            "symbol": signal.get("symbol", ""),
            "action": signal.get("action", ""),
            "price": signal.get("entry"),
            "sl": signal.get("sl"),
            "tp": signal.get("tp1") or signal.get("tp"),
            "lotSize": signal.get("lotSize") or 0.01,
            "magic": 88888,  # Magic number for Telegram signals
            "comment": f"TG:{channel}",
        }

        # Build webhook URL with account if specified
        url = f"{WEBHOOK_BASE}/api/webhook/tradingview"
        if account_id:
            url += f"?account={account_id}"

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                log.info(f"✅ Signal forwarded: {payload['action']} {payload['symbol']} for user {user_id}")
            else:
                log.error(f"❌ Webhook error {resp.status_code}: {resp.text}")

    except Exception as e:
        log.error(f"Forward signal error: {e}")


async def get_user_configs() -> list[dict]:
    """Fetch active Telegram copier configs from Firestore REST API"""
    try:
        url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents/telegram_copiers"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []

            data = resp.json()
            docs = data.get("documents", [])
            configs = []

            for doc in docs:
                fields = doc.get("fields", {})
                config = {
                    "userId": fields.get("userId", {}).get("stringValue", ""),
                    "sessionString": fields.get("sessionString", {}).get("stringValue", ""),
                    "channels": [c.get("stringValue", "") for c in fields.get("channels", {}).get("arrayValue", {}).get("values", [])],
                    "accountId": fields.get("accountId", {}).get("stringValue", ""),
                    "enabled": fields.get("enabled", {}).get("booleanValue", True),
                    "phoneNumber": fields.get("phoneNumber", {}).get("stringValue", ""),
                }
                if config["sessionString"] and config["enabled"]:
                    configs.append(config)

            return configs

    except Exception as e:
        log.error(f"Firestore fetch error: {e}")
        return []


async def start_user_client(config: dict):
    """Start a Pyrogram client for a user"""
    user_id = config["userId"]

    if user_id in active_clients:
        log.info(f"Client already running for user {user_id}")
        return

    try:
        app = Client(
            name=f"user_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=config["sessionString"],
            in_memory=True,
        )

        channels = config.get("channels", [])
        account_id = config.get("accountId", "")

        @app.on_message(filters.channel)
        async def handle_message(client: Client, message: Message):
            try:
                # Check if message is from a monitored channel
                chat = message.chat
                chat_username = f"@{chat.username}" if chat.username else str(chat.id)

                monitored = any(
                    ch.lower().strip("@") == chat_username.lower().strip("@") or
                    ch == str(chat.id)
                    for ch in channels
                )

                if not monitored:
                    return

                text = message.text or message.caption or ""
                if not text or len(text) < 10:
                    return

                log.info(f"📨 Message from {chat_username} for user {user_id}: {text[:100]}")

                # Parse with Claude
                signal = await parse_signal_with_claude(text)
                if not signal:
                    log.info(f"Not a signal, skipping")
                    return

                log.info(f"🎯 Signal parsed: {signal['action']} {signal['symbol']}")

                # Forward to webhook
                await forward_signal(user_id, account_id, signal, chat_username)

            except Exception as e:
                log.error(f"Message handler error: {e}")

        await app.start()
        me = await app.get_me()
        log.info(f"✅ Started client for {me.first_name} ({user_id}) monitoring {len(channels)} channels")

        active_clients[user_id] = {
            "client": app,
            "channels": channels,
            "account_id": account_id,
        }

    except Exception as e:
        log.error(f"Failed to start client for user {user_id}: {e}")


async def stop_user_client(user_id: str):
    """Stop a user's Pyrogram client"""
    if user_id not in active_clients:
        return
    try:
        await active_clients[user_id]["client"].stop()
        del active_clients[user_id]
        log.info(f"Stopped client for user {user_id}")
    except Exception as e:
        log.error(f"Error stopping client for {user_id}: {e}")


async def sync_clients():
    """Periodically sync active clients with Firestore configs"""
    while True:
        try:
            configs = await get_user_configs()
            config_user_ids = {c["userId"] for c in configs}

            # Stop clients no longer in config
            for user_id in list(active_clients.keys()):
                if user_id not in config_user_ids:
                    await stop_user_client(user_id)

            # Start new clients
            for config in configs:
                if config["userId"] not in active_clients:
                    await start_user_client(config)

            log.info(f"Active clients: {len(active_clients)}")

        except Exception as e:
            log.error(f"Sync error: {e}")

        await asyncio.sleep(60)  # Re-sync every minute


async def health_server():
    """Simple HTTP health check endpoint for Railway"""
    from aiohttp import web

    async def health(request):
        return web.json_response({
            "status": "ok",
            "active_clients": len(active_clients),
            "users": list(active_clients.keys()),
        })

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Health server running on port {PORT}")


async def main():
    log.info("🚀 MomentumMetrix Telegram Copier starting...")

    # Start health server
    await health_server()

    # Initial sync
    configs = await get_user_configs()
    for config in configs:
        await start_user_client(config)

    # Keep syncing
    await sync_clients()


if __name__ == "__main__":
    asyncio.run(main())
