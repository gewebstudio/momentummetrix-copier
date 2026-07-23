"""
Authentication service for Telegram user login
Handles phone number + OTP flow, session string generation, and channel listing
"""
import os
import logging
import httpx
from pyrogram import Client
from aiohttp import web
from aiohttp.web_middlewares import middleware

log = logging.getLogger(__name__)

API_ID   = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
PORT     = int(os.environ.get('PORT', 8000))
FIREBASE_PROJECT = os.environ.get('FIREBASE_PROJECT_ID', 'mt5-dashboard-bd063')

# Temporary storage for pending auth sessions
pending_auth: dict[str, dict] = {}


@middleware
async def cors_middleware(request, handler):
    if request.method == 'OPTIONS':
        response = web.Response()
    else:
        try:
            response = await handler(request)
        except Exception as e:
            response = web.json_response({"error": str(e)}, status=500)
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response


async def send_otp(request):
    """Step 1: Send OTP to phone number"""
    try:
        data    = await request.json()
        phone   = data.get("phone", "").strip()
        user_id = data.get("userId", "").strip()

        if not phone or not user_id:
            return web.json_response({"error": "phone and userId required"}, status=400)

        tg_client = Client(
            name=f"auth_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True,
        )

        await tg_client.connect()
        sent = await tg_client.send_code(phone)

        pending_auth[phone] = {
            "client": tg_client,
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
        data     = await request.json()
        phone    = data.get("phone", "").strip()
        code     = data.get("code", "").strip()
        password = data.get("password", "").strip()

        if not phone or not code:
            return web.json_response({"error": "phone and code required"}, status=400)

        pending = pending_auth.get(phone)
        if not pending:
            return web.json_response(
                {"error": "No pending auth for this phone. Please request a new code."},
                status=400
            )

        tg_client = pending["client"]

        try:
            await tg_client.sign_in(phone, pending["phone_code_hash"], code)
        except Exception as e:
            error_str = str(e).lower()
            if "two-steps" in error_str or "password" in error_str or "2fa" in error_str:
                if not password:
                    return web.json_response({"error": "2FA_REQUIRED"}, status=400)
                await tg_client.check_password(password)
            else:
                raise

        session_string = await tg_client.export_session_string()
        me = await tg_client.get_me()

        await tg_client.disconnect()
        del pending_auth[phone]

        log.info(f"Auth successful for {me.first_name} ({phone})")

        return web.json_response({
            "success": True,
            "sessionString": session_string,
            "name": me.first_name,
            "username": me.username or "",
            "userId": pending["user_id"],
        })

    except Exception as e:
        log.error(f"Verify OTP error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def get_channels(request):
    """Return list of channels/groups the user is subscribed to"""
    try:
        user_id = request.query.get("userId", "")
        if not user_id:
            return web.json_response({"error": "userId required"}, status=400)

        # Fetch session string from Firestore
        firestore_url = (
            f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
            f"/databases/(default)/documents/telegram_copiers/{user_id}"
        )
        async with httpx.AsyncClient(timeout=10) as http_client:
            resp = await http_client.get(firestore_url)
            if resp.status_code != 200:
                return web.json_response({"error": "User config not found"}, status=404)

            firestore_data = resp.json()
            session_string = (
                firestore_data.get("fields", {})
                .get("sessionString", {})
                .get("stringValue", "")
            )

        if not session_string:
            return web.json_response({"error": "No session found for this user"}, status=404)

        # Connect with user session and fetch dialogs
        tg_client = Client(
            name=f"fetch_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string,
            in_memory=True,
        )

        await tg_client.start()
        channels = []

        async for dialog in tg_client.get_dialogs():
            chat = dialog.chat
            if chat.type.name in ["CHANNEL", "SUPERGROUP", "GROUP"]:
                channels.append({
                    "id": str(chat.id),
                    "title": chat.title or "",
                    "username": f"@{chat.username}" if chat.username else str(chat.id),
                    "type": chat.type.name.lower(),
                    "members": getattr(chat, 'members_count', 0) or 0,
                })

        await tg_client.stop()

        log.info(f"Fetched {len(channels)} channels for user {user_id}")
        return web.json_response({"channels": channels})

    except Exception as e:
        log.error(f"Get channels error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def options_handler(request):
    return web.Response()


async def health(request):
    return web.json_response({
        "status": "ok",
        "service": "MomentumMetrix Telegram Copier"
    })


async def start_auth_server():
    web_app = web.Application(middlewares=[cors_middleware])
    web_app.router.add_post("/auth/send-otp", send_otp)
    web_app.router.add_post("/auth/verify-otp", verify_otp)
    web_app.router.add_get("/channels", get_channels)
    web_app.router.add_get("/health", health)
    web_app.router.add_get("/", health)
    web_app.router.add_options("/auth/send-otp", options_handler)
    web_app.router.add_options("/auth/verify-otp", options_handler)
    web_app.router.add_options("/channels", options_handler)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Auth server running on port {PORT}")
    return runner
