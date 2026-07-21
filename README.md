# MomentumMetrix Telegram Copier

Python service that monitors Telegram channels for trading signals and forwards them to MT5 via webhook.

## Architecture

```
Telegram Channel → Pyrogram Client → Claude AI Parser → MomentumMetrix Webhook → MT5 EA
```

## Environment Variables

Set these in Railway:

| Variable | Description |
|----------|-------------|
| `TELEGRAM_API_ID` | From my.telegram.org |
| `TELEGRAM_API_HASH` | From my.telegram.org |
| `WEBHOOK_BASE_URL` | Your Next.js app URL (e.g. https://studio--xxx.hosted.app) |
| `ANTHROPIC_API_KEY` | Claude API key for signal parsing |
| `FIREBASE_PROJECT_ID` | mt5-dashboard-bd063 |
| `PORT` | Set automatically by Railway |

## Endpoints

- `GET /health` — Service health check
- `POST /auth/send-otp` — Send OTP to user's phone
- `POST /auth/verify-otp` — Verify OTP and get session string

## Firestore Structure

Users' Telegram configs are stored in `telegram_copiers` collection:

```
telegram_copiers/{userId}
  - userId: string
  - phoneNumber: string
  - sessionString: string (encrypted session)
  - channels: string[] (e.g. ["@goldSignals", "@forexAlerts"])
  - accountId: string (MT5 account to route signals to)
  - enabled: boolean
  - createdAt: timestamp
```
