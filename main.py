"""
MomentumMetrix Telegram Signal Copier
Monitors Telegram channels for trading signals and forwards to MT5 via webhook
"""
import asyncio
import os
import re
import json
import logging
import httpx
from pyrogram import Client, filters
from pyrogram.types import Message

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

# Config from environment
API_ID        = int(os.environ['TELEGRAM_API_ID'])
API_HASH      = os.environ['TELEGRAM_API_HASH']
WEBHOOK_BASE  = os.environ['WEBHOOK_BASE_URL']
FIREBASE_PROJECT = os.environ.get('FIREBASE_PROJECT_ID', 'mt5-dashboard-bd063')
PORT          = int(os.environ.get('PORT', 8000))

# Active Pyrogram clients per user
# { userId: { client, channels } }
active_clients: dict[str, dict] = {}

# Known trading symbols
SYMBOLS = [
    'XAUUSD', 'XAGUSD',
    'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'USDCHF', 'NZDUSD',
    'EURGBP', 'EURJPY', 'GBPJPY', 'EURCHF', 'AUDCAD', 'AUDCHF', 'AUDJPY',
    'AUDNZD', 'CADCHF', 'CADJPY', 'CHFJPY', 'EURCAD', 'EURAUD', 'EURNZD',
    'GBPAUD', 'GBPCAD', 'GBPCHF', 'GBPNZD', 'NZDCAD', 'NZDCHF', 'NZDJPY',
    'USDNOK', 'USDSEK', 'USDSGD', 'USDZAR', 'USDMXN',
    'BTCUSD', 'ETHUSD', 'LTCUSD', 'XRPUSD',
    'US30', 'US500', 'NAS100', 'UK100', 'GER40',
    'USOIL', 'UKOIL',
]


def parse_signal_regex(text: str) -> dict | None:
    """Parse trading signal from message text using regex"""
    text_clean = text.strip()
    text_upper = text_clean.upper()

    # --- Detect action ---
    action = None

    # Close signals first
    if any(w in text_upper for w in ['CLOSE ALL', 'CLOSE_ALL', 'EXIT ALL']):
        action = 'CLOSE_ALL'
    elif any(w in text_upper for w in ['CLOSE BUY', 'CLOSE_BUY', 'EXIT BUY']):
        action = 'CLOSE_BUY'
    elif any(w in text_upper for w in ['CLOSE SELL', 'CLOSE_SELL', 'EXIT SELL']):
        action = 'CLOSE_SELL'
    elif re.search(r'\bBUY\b', text_upper) and not re.search(r'\bSELL\b', text_upper):
        action = 'BUY'
    elif re.search(r'\bSELL\b', text_upper) and not re.search(r'\bBUY\b', text_upper):
        action = 'SELL'
    elif re.search(r'\bLONG\b', text_upper):
        action = 'BUY'
    elif re.search(r'\bSHORT\b', text_upper):
        action = 'SELL'

    if not action:
        return None

    # --- Detect symbol ---
    symbol = None
    for s in SYMBOLS:
        # Match symbol with word boundaries or slashes (e.g. XAU/USD or XAUUSD)
        pattern = s[:3] + r'[/\s]?' + s[3:]
        if re.search(pattern, text_upper):
            symbol = s
            break

    if not symbol:
        return None

    # --- Extract price levels ---
    # Look for labeled prices first
    def extract_labeled(labels: list[str]) -> float | None:
        for label in labels:
            match = re.search(label + r'[:\s]*(\d+\.?\d*)', text_upper)
            if match:
                val = float(match.group(1))
                if val > 0.1:
                    return val
        return None

    entry = extract_labeled(['ENTRY', 'ENTER', 'PRICE', 'EP', 'BUY AT', 'SELL AT', 'BUY@', 'SELL@', '@'])
    sl    = extract_labeled(['SL', 'STOP LOSS', 'STOP', 'S/L', 'STOPLOSS'])
    tp1   = extract_labeled(['TP1', 'TP 1', 'T1', 'TARGET 1', 'TAKE PROFIT 1'])
    tp2   = extract_labeled(['TP2', 'TP 2', 'T2', 'TARGET 2', 'TAKE PROFIT 2'])
    tp3   = extract_labeled(['TP3', 'TP 3', 'T3', 'TARGET 3', 'TAKE PROFIT 3'])

    # If no labeled TP, try generic TP
    if not tp1:
        tp1 = extract_labeled(['TP', 'TAKE PROFIT', 'T/P', 'TARGET'])

    # Fallback: extract all numbers if nothing labeled
    if not entry and not sl and not tp1:
        all_prices = [float(p) for p in re.findall(r'\d+\.?\d+', text_clean) if float(p) > 0.1]
        if len(all_prices) >= 1: entry = all_prices[0]
        if len(all_prices) >= 2: sl    = all_prices[1]
        if len(all_prices) >= 3: tp1   = all_prices[2]
        if len(all_prices) >= 4: tp2   = all_prices[3]

    log.info(f"Parsed: {action} {symbol} entry={entry} sl={sl} tp1={tp1} tp2={tp2}")

    return {
        'symbol': symbol,
        'action': action,
        'entry': entry,
        'sl': sl,
        'tp1': tp1,
        'tp2': tp2,
        'tp3': tp3,
    }


async def forward_signal(user_id: str, account_id: str | None, signal: dict, channel: str):
    """Forward parsed signal to MomentumMetrix webhook"""
    try:
        payload = {
            "symbol": signal.get("symbol", ""),
            "action": signal.get("action", ""),
            "price": signal.get("entry"),
            "sl": signal.get("sl"),
            "tp": signal.get("tp1") or signal.get("tp"),
            "lotSize": 0.01,
            "magic": 88888,
            "comment": f"TG:{channel[:20]}",
        }

        url = f"{WEBHOOK_BASE}/api/webhook/tradingview"
        if account_id:
            url += f"?account={account_id}"

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                log.info(f"✅ Forwarded: {payload['action']} {payload['symbol']} for user {user_id}")
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
                    "channels": [
                        c.get("stringValue", "")
                        for c in fields.get("channels", {}).get("arrayValue", {}).get("values", [])
                    ],
                    "accountId": fields.get("accountId", {}).get("stringValue", ""),
                    "enabled": fields.get("enabled", {}).get("booleanValue", True),
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
        return

    try:
        app = Client(
            name=f"user_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=config["sessionString"],
            in_memory=True,
        )

        channels  = config.get("channels", [])
        account_id = config.get("accountId", "")

        @app.on_message(filters.channel)
        async def handle_message(client: Client, message: Message):
            try:
                chat = message.chat
                chat_username = f"@{chat.username}" if chat.username else str(chat.id)

                # Check if monitored channel
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

                log.info(f"📨 [{chat_username}] {text[:120]}")

                # Parse signal
                signal = parse_signal_regex(text)
                if not signal:
                    log.info("Not a signal, skipping")
                    return

                log.info(f"🎯 Signal: {signal['action']} {signal['symbol']}")
                await forward_signal(user_id, account_id, signal, chat_username)

            except Exception as e:
                log.error(f"Message handler error: {e}")

        await app.start()
        me = await app.get_me()
        log.info(f"✅ Client started: {me.first_name} ({user_id}) — {len(channels)} channels")

        active_clients[user_id] = {
            "client": app,
            "channels": channels,
            "account_id": account_id,
        }

    except Exception as e:
        log.error(f"Failed to start client for {user_id}: {e}")


async def stop_user_client(user_id: str):
    """Stop a user's Pyrogram client"""
    if user_id not in active_clients:
        return
    try:
        await active_clients[user_id]["client"].stop()
        del active_clients[user_id]
        log.info(f"Stopped client for {user_id}")
    except Exception as e:
        log.error(f"Error stopping {user_id}: {e}")


async def sync_clients():
    """Periodically sync active clients with Firestore configs"""
    while True:
        try:
            configs = await get_user_configs()
            config_ids = {c["userId"] for c in configs}

            # Stop removed clients
            for uid in list(active_clients.keys()):
                if uid not in config_ids:
                    await stop_user_client(uid)

            # Start new clients
            for config in configs:
                if config["userId"] not in active_clients:
                    await start_user_client(config)

            log.info(f"Active clients: {len(active_clients)}")

        except Exception as e:
            log.error(f"Sync error: {e}")

        await asyncio.sleep(60)


async def health_server():
    """Simple HTTP health check for Railway"""
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
    log.info(f"Health server on port {PORT}")


async def main():
    log.info("🚀 MomentumMetrix Copier starting...")
    await health_server()

    configs = await get_user_configs()
    for config in configs:
        await start_user_client(config)

    await sync_clients()


if __name__ == "__main__":
    asyncio.run(main())
