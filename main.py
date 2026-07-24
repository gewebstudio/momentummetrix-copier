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
from datetime import datetime, timezone, timedelta
from pyrogram import Client, filters
from pyrogram.types import Message

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

API_ID           = int(os.environ['TELEGRAM_API_ID'])
API_HASH         = os.environ['TELEGRAM_API_HASH']
WEBHOOK_BASE     = os.environ['WEBHOOK_BASE_URL']
FIREBASE_PROJECT = os.environ.get('FIREBASE_PROJECT_ID', 'mt5-dashboard-bd063')
PORT             = int(os.environ.get('PORT', 8000))
SIGNAL_RETENTION_DAYS = 7

active_clients: dict[str, dict] = {}

SYMBOLS = [
    'XAUUSD', 'XAGUSD',
    'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'USDCHF', 'NZDUSD',
    'EURGBP', 'EURJPY', 'GBPJPY', 'EURCHF', 'AUDCAD', 'AUDCHF', 'AUDJPY',
    'AUDNZD', 'CADCHF', 'CADJPY', 'CHFJPY', 'EURCAD', 'EURAUD', 'EURNZD',
    'GBPAUD', 'GBPCAD', 'GBPCHF', 'GBPNZD', 'NZDCAD', 'NZDCHF', 'NZDJPY',
    'BTCUSD', 'ETHUSD', 'LTCUSD', 'XRPUSD',
    'US30', 'US500', 'NAS100', 'UK100', 'GER40',
    'USOIL', 'UKOIL',
]


def parse_signal_regex(text: str) -> dict | None:
    text_clean = text.strip()
    text_upper = text_clean.upper()

    action = None
    if any(w in text_upper for w in ['CLOSE ALL', 'CLOSE_ALL', 'EXIT ALL']):
        action = 'CLOSE_ALL'
    elif any(w in text_upper for w in ['CLOSE BUY', 'CLOSE_BUY']):
        action = 'CLOSE_BUY'
    elif any(w in text_upper for w in ['CLOSE SELL', 'CLOSE_SELL']):
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

    symbol = None
    for s in SYMBOLS:
        pattern = s[:3] + r'[/\s]?' + s[3:]
        if re.search(pattern, text_upper):
            symbol = s
            break

    if not symbol:
        return None

    def extract_labeled(labels):
        for label in labels:
            match = re.search(label + r'[:\s]*(\d+\.?\d*)', text_upper)
            if match:
                val = float(match.group(1))
                if val > 0.1:
                    return val
        return None

    entry = extract_labeled(['ENTRY', 'ENTER', 'PRICE', 'EP', 'BUY AT', 'SELL AT', '@'])
    sl    = extract_labeled(['SL', 'STOP LOSS', 'STOP', 'S/L', 'STOPLOSS'])
    tp1   = extract_labeled(['TP1', 'TP 1', 'T1', 'TARGET 1'])
    tp2   = extract_labeled(['TP2', 'TP 2', 'T2', 'TARGET 2'])
    tp3   = extract_labeled(['TP3', 'TP 3', 'T3', 'TARGET 3'])

    if not tp1:
        tp1 = extract_labeled(['TP', 'TAKE PROFIT', 'T/P', 'TARGET'])

    if not entry and not sl and not tp1:
        all_prices = [float(p) for p in re.findall(r'\d+\.?\d+', text_clean) if float(p) > 0.1]
        if len(all_prices) >= 1: entry = all_prices[0]
        if len(all_prices) >= 2: sl    = all_prices[1]
        if len(all_prices) >= 3: tp1   = all_prices[2]
        if len(all_prices) >= 4: tp2   = all_prices[3]

    return {
        'symbol': symbol,
        'action': action,
        'entry': entry,
        'sl': sl,
        'tp1': tp1,
        'tp2': tp2,
        'tp3': tp3,
    }


async def save_signal_to_firestore(
    user_id: str,
    channel: str,
    signal: dict,
    lot_size: float,
    execution_mode: str,
    account_id: str,
) -> str | None:
    try:
        payload = {
            "userId":        user_id,
            "channel":       channel,
            "symbol":        signal.get('symbol', ''),
            "action":        signal.get('action', ''),
            "entry":         signal.get('entry') or 0,
            "sl":            signal.get('sl') or 0,
            "tp":            signal.get('tp1') or 0,
            "tp2":           signal.get('tp2') or 0,
            "tp3":           signal.get('tp3') or 0,
            "lotSize":       lot_size,
            "status":        "PENDING",
            "executionMode": execution_mode,
            "accountId":     account_id or '',
        }
        url = f"{WEBHOOK_BASE}/api/telegram-signal"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                log.info(f"Signal saved: {data.get('id')}")
                return data.get('id')
            else:
                log.error(f"Save signal error: {resp.status_code}")
                return None
    except Exception as e:
        log.error(f"Save signal error: {e}")
        return None


async def update_signal_status(doc_id: str, status: str, error: str = None):
    try:
        url = f"{WEBHOOK_BASE}/api/telegram-signal"
        payload = {"id": doc_id, "status": status, "error": error}
        async with httpx.AsyncClient(timeout=10) as client:
            await client.patch(url, json=payload)
    except Exception as e:
        log.error(f"Update signal error: {e}")


async def forward_signal_to_webhook(
    user_id: str,
    account_id: str,
    signal: dict,
    lot_size: float,
    channel: str,
) -> bool:
    """Forward parsed signal to MomentumMetrix webhook"""
    try:
        payload = {
            "symbol":  signal.get("symbol", ""),
            "action":  signal.get("action", ""),
            "price":   signal.get("entry"),
            "sl":      signal.get("sl"),
            "tp":      signal.get("tp1"),
            "lotSize": lot_size,
            "magic":   88888,
            "comment": f"TG:{channel[:20]}",
        }

        url = f"{WEBHOOK_BASE}/api/webhook/tradingview"
        if account_id:
            url += f"?account={account_id}"

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                log.info(f"✅ Forwarded: {payload['action']} {payload['symbol']}")
                return True
            else:
                log.error(f"❌ Webhook error {resp.status_code}: {resp.text}")
                return False

    except Exception as e:
        log.error(f"Forward signal error: {e}")
        return False


async def cleanup_old_signals():
    """Delete signals older than SIGNAL_RETENTION_DAYS days"""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=SIGNAL_RETENTION_DAYS)).isoformat()
        url = (
            f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
            f"/databases/(default)/documents:runQuery"
        )
        query = {
            "structuredQuery": {
                "from": [{"collectionId": "telegram_signals"}],
                "where": {
                    "fieldFilter": {
                        "field": {"fieldPath": "createdAt"},
                        "op": "LESS_THAN",
                        "value": {"timestampValue": cutoff}
                    }
                },
                "limit": 100
            }
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=query)
            if resp.status_code != 200:
                return

            results = resp.json()
            deleted = 0
            for result in results:
                doc = result.get('document')
                if not doc:
                    continue
                doc_name = doc.get('name', '')
                if doc_name:
                    del_resp = await client.delete(
                        f"https://firestore.googleapis.com/v1/{doc_name}"
                    )
                    if del_resp.status_code == 200:
                        deleted += 1

            if deleted > 0:
                log.info(f"🧹 Cleaned up {deleted} old signals")

    except Exception as e:
        log.error(f"Cleanup error: {e}")


async def get_user_configs() -> list[dict]:
    """Fetch active Telegram copier configs from Firestore"""
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

                # Parse channels array (new format with objects)
                channels_raw = fields.get("channels", {}).get("arrayValue", {}).get("values", [])
                channels = []
                for ch in channels_raw:
                    ch_fields = ch.get("mapValue", {}).get("fields", {})
                    if ch_fields:
                        channels.append({
                            "username":      ch_fields.get("username", {}).get("stringValue", ""),
                            "title":         ch_fields.get("title", {}).get("stringValue", ""),
                            "enabled":       ch_fields.get("enabled", {}).get("booleanValue", True),
                            "lotSize":       ch_fields.get("lotSize", {}).get("doubleValue", 0.01),
                            "useTP1":        ch_fields.get("useTP1", {}).get("booleanValue", True),
                            "useTP2":        ch_fields.get("useTP2", {}).get("booleanValue", False),
                            "useTP3":        ch_fields.get("useTP3", {}).get("booleanValue", False),
                            "breakeven":     ch_fields.get("breakeven", {}).get("booleanValue", False),
                            "executionMode": ch_fields.get("executionMode", {}).get("stringValue", "auto"),
                        })

                config = {
                    "userId":        fields.get("userId", {}).get("stringValue", ""),
                    "sessionString": fields.get("sessionString", {}).get("stringValue", ""),
                    "accountId":     fields.get("accountId", {}).get("stringValue", ""),
                    "enabled":       fields.get("enabled", {}).get("booleanValue", True),
                    "executionMode": fields.get("executionMode", {}).get("stringValue", "auto"),
                    "channels":      channels,
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
        tg_client = Client(
            name=f"user_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=config["sessionString"],
            in_memory=True,
        )

        channels       = config.get("channels", [])
        account_id     = config.get("accountId", "")
        global_mode    = config.get("executionMode", "auto")

        # Build lookup: username → channel config
        channel_map = {ch["username"].lower().strip("@"): ch for ch in channels if ch.get("enabled")}

        @tg_client.on_message(filters.channel)
        async def handle_message(client: Client, message: Message):
            try:
                chat = message.chat
                chat_username = chat.title or f"@{chat.username}" if chat.username else str(chat.id)
                chat_key = chat_username.lower().strip("@")

                # Check if monitored
                ch_config = channel_map.get(chat_key)
                if not ch_config:
                    # Try by ID
                    ch_config = channel_map.get(str(chat.id))
                if not ch_config:
                    return

                text = message.text or message.caption or ""
                if not text or len(text) < 10:
                    return

                log.info(f"📨 [{chat_username}] {text[:120]}")

                signal = parse_signal_regex(text)
                if not signal:
                    return

                log.info(f"🎯 {signal['action']} {signal['symbol']}")

                lot_size = ch_config.get("lotSize", 0.01)
                exec_mode = ch_config.get("executionMode", global_mode)

                # Save to Firestore first
                doc_id = await save_signal_to_firestore(
                    user_id=user_id,
                    channel=chat.title or chat_username,  # Use title instead of username
                    signal=signal,
                    lot_size=lot_size,
                    execution_mode=exec_mode,
                    account_id=account_id,
                )

                # Auto-execute if mode is auto
                if exec_mode == "auto":
                    success = await forward_signal_to_webhook(
                        user_id=user_id,
                        account_id=account_id,
                        signal=signal,
                        lot_size=lot_size,
                        channel=chat_username,
                    )
                    if doc_id:
                        await update_signal_status(
                            doc_id,
                            "EXECUTED" if success else "FAILED",
                            None if success else "Webhook forward failed"
                        )
                # Manual mode — signal stays PENDING for user to push/decline

            except Exception as e:
                log.error(f"Message handler error: {e}")

        await tg_client.start()
        me = await tg_client.get_me()

        # Populate peer cache by fetching all dialogs
        try:
            async for _ in tg_client.get_dialogs():
                pass
            log.info("Peer cache populated")
        except Exception as e:
            log.warning(f"Could not populate peer cache: {e}")

        # Pre-cache monitored channels specifically
        for ch in channels:
            try:
                username = ch.get("username", "").strip("@")
                if username:
                    await tg_client.get_chat(username)
                    log.info(f"Cached channel: @{username}")
            except Exception as e:
                log.warning(f"Could not cache {ch.get('username')}: {e}")

        enabled_channels = [ch["username"] for ch in channels if ch.get("enabled")]
        log.info(f"✅ Client: {me.first_name} ({user_id}) — {len(enabled_channels)} channels")

        active_clients[user_id] = {
            "client":  tg_client,
            "channels": channels,
            "account_id": account_id,
        }

    except Exception as e:
        log.error(f"Failed to start client for {user_id}: {e}")


async def stop_user_client(user_id: str):
    if user_id not in active_clients:
        return
    try:
        await active_clients[user_id]["client"].stop()
        del active_clients[user_id]
        log.info(f"Stopped client for {user_id}")
    except Exception as e:
        log.error(f"Error stopping {user_id}: {e}")


async def sync_clients():
    cleanup_counter = 0
    while True:
        try:
            configs = await get_user_configs()
            config_ids = {c["userId"] for c in configs}

            for uid in list(active_clients.keys()):
                if uid not in config_ids:
                    await stop_user_client(uid)

            for config in configs:
                user_id = config["userId"]
                if user_id not in active_clients:
                    await start_user_client(config)
                else:
                    # Check if channels changed — restart client if so
                    current_channels = {
                        ch["username"] for ch in active_clients[user_id].get("channels", [])
                    }
                    new_channels = {
                        ch["username"] for ch in config.get("channels", [])
                        if ch.get("enabled")
                    }
                    if current_channels != new_channels:
                        log.info(f"Channel config changed for {user_id} — restarting client")
                        await stop_user_client(user_id)
                        await start_user_client(config)

            log.info(f"Active clients: {len(active_clients)}")

            # Cleanup old signals every hour (60 * 1min cycles)
            cleanup_counter += 1
            if cleanup_counter >= 60:
                await cleanup_old_signals()
                cleanup_counter = 0

        except Exception as e:
            log.error(f"Sync error: {e}")

        await asyncio.sleep(10)


async def health_server():
    from aiohttp import web

    async def health(request):
        return web.json_response({
            "status": "ok",
            "active_clients": len(active_clients),
            "users": list(active_clients.keys()),
        })

    web_app = web.Application()
    web_app.router.add_get("/health", health)
    web_app.router.add_get("/", health)

    runner = web.AppRunner(web_app)
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
