"""
MomentumMetrix Telegram Copier - Entry Point
Runs both the auth service and copier service together
"""
import asyncio
import logging
import os
from auth_service import start_auth_server
from main import health_server, sync_clients, get_user_configs, start_user_client, active_clients

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)


async def run():
    log.info("🚀 MomentumMetrix Copier Service starting...")

    # Start HTTP server (health + auth endpoints)
    await start_auth_server()

    # Load initial user configs and start clients
    configs = await get_user_configs()
    log.info(f"Found {len(configs)} active user configs")

    for config in configs:
        await start_user_client(config)

    log.info(f"Started {len(active_clients)} Telegram clients")

    # Keep syncing configs every minute
    await sync_clients()


if __name__ == "__main__":
    asyncio.run(run())
