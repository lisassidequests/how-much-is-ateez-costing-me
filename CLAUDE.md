# Project notes for Claude Code

Telegram bot that tracks one person's K-pop group-order purchases: what was ordered, paid, arrived, and late.
Single user. Python 3.10+, SQLite, python-telegram-bot (long polling), Anthropic API for parsing.

## Layout
- `ateez_bot/ai.py` – Claude call (tool-use forced to `record_message`), prompt, API-error classification (`AIUnavailable` kinds: credits / auth / temporary)
- `ateez_bot/pipeline.py` – all logic with no Telegram imports: parsed message -> draft -> confirm -> DB; reports; late alerts; inbox queue processing
- `ateez_bot/store.py` – SQLite schema + queries
- `ateez_bot/bot.py` – Telegram handlers and scheduled jobs (retry queue every 15 min, daily late check, health ping)
- `tests/test_flow.py` – end-to-end tests with a fake parser; run `pytest`

## Domain rules (keep these)
- Seller's channel posts carry a serial hashtag (e.g. `#anghp6_4`); her order-confirmation DMs have NO serial and are matched to posts by product title; arrival updates cite serials.
- Nothing is saved until the user taps Confirm. Claude only extracts; sums and totals are computed in code.
- Amounts are SGD. User pays instantly; late fees are ignored.
- Item is "late" 28 days (LATE_DAYS) after first payment, or after order date if unpaid.
- Proactive reminder is a single text-only weekly digest (Sunday 10:00 SGT) of everything still incoming, late batches flagged. No daily alerts, no photos (photos only via /pending_photos; /pending is the same text list on demand). `items.last_alert_at` is now unused.
- "OT8" = one piece of each of the 8 members (8 pieces total); "OT4" = 4 pieces. Item qty is in pieces, and the seller's "X1" count prefix is stripped from item names.
- Payments are separate rows (partial/second payments exist). Spend = sum of payments.
- If Claude is unavailable (no credits, bad key, outage) messages stay `queued` in `inbox` and are retried automatically; the user is told once per 6 hours.
- Only `ALLOWED_USER_ID` may use the bot.

## Not yet verified against the live services
The Claude prompt in `ai.py` was written from screenshots of real messages but has not been run against the real API
or real forwarded Telegram messages. When tuning, add the real message text as a case in `tests/` first.
