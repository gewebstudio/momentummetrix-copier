"""
MomentumMetrix Telegram Signal Copier
Monitors Telegram channels and forwards raw messages to Next.js for parsing
"""
import asyncio
import os
import logging
import httpx
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

active_clients: dict[str, dict] = {}


async def forward_raw_message(
    user_id: str,
    channel: str,
    channel_title: str,
    message: str,
) -> bool:
    """Forward raw message to Next.js API for parsing"""
    try:
        payload = {
            "userId":       user_id,
            "channel":      channel,
            "channelTitle": channel_title,
            "message":      message,
        }

        url = f"{WEBHOOK_BASE}/api/telegram-message"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()

            if resp.status_code == 200:
                if data.get('skipped'):
                    log.info(f"Skipped: {data.get('reason', 'unknown')}")
                elif data.get('type') == 'signal':
                    log.info(f"✅ Signal saved: {data.get('parsed', {})}")
                elif data.get('type') == 'update':
                    log.info(f"🔄 Update saved: {data.get('updateType')}")
                return True
            else:
                log.error(f"API error {resp.status_code}: {data}")
                return False

    except Exception as e:
        log.error(f"Forward message error: {e}")
        return False


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

                # Parse channels
                channels_raw = fields.get("channels", {}).get("arrayValue", {}).get("values", [])
                channels = []
                for ch in channels_raw:
                    ch_fields = ch.get("mapValue", {}).get("fields", {})
                    if ch_fields:
                        channels.append({
                            "username": ch_fields.get("username", {}).get("stringValue", ""),
                            "title":    ch_fields.get("title", {}).get("stringValue", ""),
                            "enabled":  ch_fields.get("enabled", {}).get("booleanValue", True),
                        })

                config = {
                    "userId":        fields.get("userId", {}).get("stringValue", ""),
                    "sessionString": fields.get("sessionString", {}).get("stringValue", ""),
                    "enabled":       fields.get("enabled", {}).get("booleanValue", True),
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

        channels = config.get("channels", [])
        enabled_usernames = {
            ch["username"].lower().strip("@")
            for ch in channels
            if ch.get("enabled")
        }

        @tg_client.on_message(filters.channel)
        async def handle_message(client: Client, message: Message):
            try:
                chat = message.chat
                chat_username = f"@{chat.username}" if chat.username else str(chat.id)
                chat_key = chat_username.lower().strip("@")
                chat_title = chat.title or chat_username

                # Check if monitored
                if chat_key not in enabled_usernames and str(chat.id) not in enabled_usernames:
                    return

                text = message.text or message.caption or ""
                if not text or len(text) < 5:
                    return

                log.info(f"📨 [{chat_title}] {text[:100]}")

                # Forward raw message to Next.js for parsing
                await forward_raw_message(
                    user_id=user_id,
                    channel=chat_username,
                    channel_title=chat_title,
                    message=text,
                )

            except Exception as e:
                log.error(f"Message handler error: {e}")

        await tg_client.start()
        me = await tg_client.get_me()

        # Populate peer cache
        try:
            async for _ in tg_client.get_dialogs():
                pass
            log.info("Peer cache populated")
        except Exception as e:
            log.warning(f"Peer cache warning: {e}")

        # Pre-cache monitored channels
        for ch in channels:
            try:
                username = ch.get("username", "").strip("@")
                if username:
                    await tg_client.get_chat(username)
            except Exception as e:
                log.warning(f"Could not cache {ch.get('username')}: {e}")

        log.info(f"✅ Client: {me.first_name} ({user_id}) — {len(enabled_usernames)} channels")

        active_clients[user_id] = {
            "client":   tg_client,
            "channels": [ch["username"].lower().strip("@") for ch in channels if ch.get("enabled")],
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
    """Sync active clients with Firestore configs"""
    while True:
        try:
            configs = await get_user_configs()
            config_ids = {c["userId"] for c in configs}

            # Stop removed clients
            for uid in list(active_clients.keys()):
                if uid not in config_ids:
                    await stop_user_client(uid)

            for config in configs:
                user_id = config["userId"]
                if user_id not in active_clients:
                    await start_user_client(config)
                else:
                    # Restart if channels changed
                    current = set(active_clients[user_id].get("channels", []))
                    new = {
                        ch["username"].lower().strip("@")
                        for ch in config.get("channels", [])
                        if ch.get("enabled")
                    }
                    if current != new:
                        log.info(f"Channels changed for {user_id} — restarting")
                        await stop_user_client(user_id)
                        await start_user_client(config)

            log.info(f"Active clients: {len(active_clients)}")

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
