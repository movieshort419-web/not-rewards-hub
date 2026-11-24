# NOT Rewards Hub — v4

Private Telegram bot starter for a NOT rewards/mining concept. It is not affiliated with Notcoin.

## Included
- Telegram bot using aiogram
- Supabase Postgres via server-side Data API
- 0.09 NOT/hour, 5-hour mining sessions
- Hourly server-tracked mining credits
- 0.20 NOT reward only for verified ad events
- 10 NOT qualified referral reward, once per referral
- 1st withdrawal: 10 verified ads; later withdrawals: 30
- 2% withdrawal fee
- Admin approval/rejection
- Generic provider postback endpoint with duplicate-event protection
- Railway Docker deployment files
- Payout is intentionally OFF/dry-run in this v4 starter

## Important
A SmartLink click is NOT automatically treated as a completed ad. Only use a provider verification/postback method that your ad provider actually supports and permits. The included `/verifyad` command is an admin test mechanism.

Never put a Telegram bot token, Supabase secret key, seed phrase, or private key in source code or chat.
