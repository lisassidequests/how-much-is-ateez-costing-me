# How much is ATEEZ costing me?

A private Telegram bot that tracks K-pop merch group orders: what you ordered, what you've paid, what has arrived,
and what's overdue. You forward messages to it; it reads them with Claude, shows what it understood, and saves only after you tap ✅.

## What you forward
| Forward this | Bot does |
|---|---|
| Seller's channel post (photo + `#serial`) | Saves the serial, title and photo |
| Seller's order confirmation DM | Saves items and total; links to the channel post by product name (asks if unsure) |
| Your payment screenshot | Logs the payment and links it to the order with the matching balance |
| Seller's "arrived" update | Marks the items arrived (by `#serial`, and member names if given) |

## Commands
`/pending [name]` (text list of everything not arrived, late or not) · `/pending_photos [name]` (same, with photos) · `/late` · `/spent [YYYY-MM or name]` · `/owing` · `/status [name]` (everything you're expecting) · `/delivered [name]` (arrived with the seller, not with you yet) · `/received [name]` (tick off what has reached you) · `/undo` · `/healthcheck`
Or ask in plain words: "how much have I spent on ateez this month?"

`/status` and `/delivered` list your batches with a tappable `#serial` button under each. Tap one and the bot sends that post's photos, with each item marked ⏳ waiting or ✅ arrived. Each item goes through three stages: ⏳ ordered → ✅ arrived with the seller (her update) → 📬 received by you. When she confirms arrivals, run `/received`, tap the items that have reached you, and press Confirm.

Every Sunday at 10:00 SGT it sends one text message listing everything still incoming, oldest first, with batches past 4 weeks flagged ⚠️. No photos, so it stays short; use `/pending_photos` when you want the pictures. Change the day/time with `DIGEST_DAY` / `DIGEST_HOUR` in `.env`.

## If something breaks
- **Out of Anthropic credits / bad key / outage:** the bot tells you in Telegram, keeps your forwarded messages, and processes them automatically when it recovers.
- **Bot down:** set `HEALTHCHECK_URL` (free at healthchecks.io) and it emails you when pings stop, or when Claude is unavailable.
- Logs: `journalctl -u ateez-bot -f`

## Setup (about 10 minutes)
1. **Telegram bot:** message @BotFather, `/newbot`, copy the token.
2. **Anthropic key:** console.anthropic.com → API keys. Also set a monthly spend limit there.
3. **Server:** an Ubuntu VM (Oracle Cloud free tier works). Then:
   ```bash
   git clone https://github.com/lisassidequests/how-much-is-ateez-costing-me
   cd how-much-is-ateez-costing-me
   bash deploy/setup.sh        # creates .env, then stops
   nano .env                   # paste token + key
   bash deploy/setup.sh        # starts the service
   ```
4. Message your bot `/start`. It replies with your Telegram user ID. Put it in `.env` as `ALLOWED_USER_ID`, then `sudo systemctl restart ateez-bot`.
   (Until then the bot ignores everyone.)

To run locally instead: `pip install -r requirements.txt && python -m ateez_bot`.

## Development
`pip install -r requirements-dev.txt && pytest`. Project notes for Claude Code are in `CLAUDE.md`.
