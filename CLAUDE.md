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
- The seller's mass "[ARRIVAL PROOF]" posts list many hashtags with quantities ("#anghp5_2 (quantity: x4 unit pcs)"), most belonging to other buyers. Only tags with the user's pending items are used; others are ignored silently. Serials can contain hyphens (`#ANATZ38-2`; Telegram only highlights up to the hyphen), so lookups tolerate `-`/`_` differences. The quantities are the seller's counts for the WHOLE listing across all buyers (e.g. "x1 OT8" is split between several people, and the user may hold only 4 of those 8 cards), so they are shown for information only and never compared with the user's own item counts. An arrival tag marks all of the user's pending items under that post as arrived. When nothing matches, the reply lists the hashtags it read and why none matched.
- Commands: /status = everything the user is still AWAITING (not at the seller yet + at the seller but not received; late batches flagged), /all = every order with payment state and item stages, /settle [order no.] = confirm-draft that sets an order's total to what was actually paid (`orders.settled_from` keeps the old total; /undo restores it) for orders whose items/total/payments don't tally, /delivered = arrived overview (both with `#tag` inline buttons -> callback `t:<post_id>` sends the post's photos); /healthcheck = bot health (formerly /status). Item stages: pending -> `arrived_at` (seller confirmed) -> `received_at` (user confirms via /received, which lists arrived-but-not-received items as a tick-list draft; callbacks r/ra/rc). /delivered = arrived but not yet received.
- Duplicates are flagged, never auto-dropped: exact re-forwards via `inbox.fingerprint` (text + forward time + photo unique id), plus content checks for posts (tag exists), orders (same title/items/total) and payments (same amount, same day). The user decides with Confirm/Cancel.
- Serials are case-insensitive (always lowercased). Orders saved before their channel post was forwarded are linked later by `relink_orders` (title similarity >= 0.85 and clearly better than the runner-up); it runs when a post is confirmed and when an arrival notice is read.
- Payments are separate rows; one payment can cover several orders and orders can be part-paid. The split lives in `allocations` (payment_id, order_id, amount); an order's paid amount is the sum of its allocations (the old `payments.order_id` is migrated into allocations and no longer used). On a payment screenshot the bot pre-ticks the unique combination of unpaid orders whose balances add up to the amount; otherwise the user ticks. Allocation fills oldest order first, each up to what it owes. Spend = sum of payments. The user pays instantly, so confirming an order also records an ASSUMED payment (`payments.assumed=1`, dated the order date, allocated in full to that order); no screenshot is needed. A real payment screenshot ignores assumed payments when matching (orders owe `total_due - real_paid`) and on confirm deletes the assumed payments of the orders it covers (undo restores them), so spend is never doubled and a differing amount leaves a gap that /settle can close. Orders saved before this change have no assumed payment.
- If Claude is unavailable (no credits, bad key, outage) messages stay `queued` in `inbox` and are retried automatically; the user is told once per 6 hours.
- Only `ALLOWED_USER_ID` may use the bot.

## Not yet verified against the live services
The Claude prompt in `ai.py` was written from screenshots of real messages but has not been run against the real API
or real forwarded Telegram messages. When tuning, add the real message text as a case in `tests/` first.
