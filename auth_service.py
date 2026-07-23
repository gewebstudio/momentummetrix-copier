"""
Authentication service for Telegram user login
Handles phone number + OTP flow and session string generation
"""
import asyncio
import os
import logging
from pyrogram import Client
from aiohttp import web

log = logging.getLogger(__name__)

_raw_id = os.environ['TELEGRAM_API_ID'].split('=')[-1].strip().split('\\n')[0].strip()
API_ID  = int(_raw_id)
API_HASH = os.environ['TELEGRAM_API_HASH']
PORT     = int(os.environ.get('PORT', 8000))

# Temporary storage for pending auth sessions
# { phone: { client, phone_code_hash } }
pending_auth: dict[str, dict] = {}


async def send_otp(request):
    """Step 1: Send OTP to phone number"""
    try:
        data = await request.json()
        phone = data.get("phone", "").strip()
        user_id = data.get("userId", "").strip()

        if not phone or not user_id:
            return web.json_response({"error": "phone and userId required"}, status=400)

        # Create temporary client for auth
        app = Client(
            name=f"auth_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True,
        )

        await app.connect()
        sent = await app.send_code(phone)

        pending_auth[phone] = {
            "client": app,
            "phone_code_hash": sent.phone_code_hash,
            "user_id": user_id,
        }

        log.info(f"OTP sent to {phone} for user {user_id}")
        return web.json_response({"success": True, "message": "OTP sent to your Telegram"})

    except Exception as e:
        log.error(f"Send OTP error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def verify_otp(request):
    """Step 2: Verify OTP and return session string"""
    try:
        data = await request.json()
        phone = data.get("phone", "").strip()
        code  = data.get("code", "").strip()
        password = data.get("password", "").strip()  # 2FA if enabled

        if not phone or not code:
            return web.json_response({"error": "phone and code required"}, status=400)

        pending = pending_auth.get(phone)
        if not pending:
            return web.json_response({"error": "No pending auth for this phone"}, status=400)

        app = pending["client"]

        try:
            await app.sign_in(phone, pending["phone_code_hash"], code)
        except Exception as e:
            error_str = str(e)
            if "two-steps" in error_str.lower() or "password" in error_str.lower():
                if not password:
                    return web.json_response({"error": "2FA_REQUIRED"}, status=400)
                await app.check_password(password)
            else:
                raise

        # Export session string
        session_string = await app.export_session_string()
        me = await app.get_me()

        await app.disconnect()
        del pending_auth[phone]

        log.info(f"✅ Auth successful for {me.first_name} ({phone})")

        return web.json_response({
            "success": True,
            "sessionString": session_string,
            "name": me.first_name,
            "username": me.username,
            "userId": pending["user_id"],
        })

    except Exception as e:
        log.error(f"Verify OTP error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def health(request):
    return web.json_response({"status": "ok", "service": "auth"})


async def start_auth_server():
    app = web.Application()
    app.router.add_post("/auth/send-otp", send_otp)
    app.router.add_post("/auth/verify-otp", verify_otp)
    app.router.add_get("/health", health)
    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Auth server running on port {PORT}")
    return runner
