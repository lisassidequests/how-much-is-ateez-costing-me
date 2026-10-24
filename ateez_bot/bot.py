"""Telegram layer: thin handlers around pipeline.py, plus scheduled jobs."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, time, timedelta

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from . import pipeline as pl
from .ai import Parser
from .config import Config
from .store import Store

log = logging.getLogger("ateez_bot")

HELP = (
    "Forward me:\n"
    "• her channel posts (with the photo) so I know what each #serial is\n"
    "• her order confirmation DMs\n"
    "• your payment screenshots\n"
    "• her arrival updates\n"
    "I'll show what I understood and wait for your ✅ before saving anything.\n\n"
    "/pending [name]: text list of everything not arrived yet, late or not\n"
    "/pending_photos [name]: same, with the photos\n"
    "/late: items past the late threshold\n"
    "/spent [YYYY-MM or name]: how much you've spent\n"
    "/owing: unpaid or part-paid orders\n"
    "/undo: reverse the last thing you confirmed\n"
    "/status [name]: everything you're still awaiting (not at the seller yet, or at the seller but not with you), "
    "with a button per # for the photos\n"
    "/all [name]: every order at a glance: paid or owing, and where each item is\n"
    "/settle [order no.]: close an order whose total and payments don't match, treating what you paid as final\n"
    "/delivered [name]: arrived with the seller but not with you yet (# buttons too)\n"
    "/received [name]: tick off what has reached you\n"
    "/healthcheck: is the bot running OK?\n"
    "You can also just ask, e.g. \"how much have I spent on ateez this month?\""
)

AI_DOWN_TEXT = {
    "credits": "⚠️ Your Anthropic API credits have run out. Top up in the Anthropic Console (Billing). "
               "Your messages are saved and I'll process them automatically once credits are back.",
    "auth": "⚠️ The Anthropic API key was rejected. Update ANTHROPIC_API_KEY in the server's .env and run "
            "`sudo systemctl restart ateez-bot`. Your messages are saved in the meantime.",
    "temporary": "⚠️ Claude is temporarily unavailable. Your messages are saved and I'll retry every 15 minutes.",
}


def markup(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


class BotApp:
    def __init__(self, cfg: Config, store: Store, parser: Parser):
        self.cfg, self.store, self.parser = cfg, store, parser
        self.app: Application | None = None

    # ------------------------------------------------------------ plumbing
    def authorized(self, update: Update) -> bool:
        u = update.effective_user
        return bool(u) and self.cfg.allowed_user_id is not None and u.id == self.cfg.allowed_user_id

    async def _download(self, file_id: str) -> bytes:
        f = await self.app.bot.get_file(file_id)
        return bytes(await f.download_as_bytearray())

    async def send(self, chat_id: int, text: str, **kw):
        return await self.app.bot.send_message(chat_id=chat_id, text=text, **kw)

    async def send_messages(self, chat_id: int, msgs: list[dict]) -> None:
        for m in msgs:
            if m.get("photo"):
                try:
                    await self.app.bot.send_photo(chat_id=chat_id, photo=m["photo"], caption=m["text"])
                    continue
                except BadRequest:
                    log.warning("Photo send failed, falling back to text")
            kw = {"reply_markup": markup(m["buttons"])} if m.get("buttons") else {}
            await self.send(chat_id, m["text"], **kw)

    def _rate_limited_alert_due(self, key: str, hours: float) -> bool:
        last = self.store.get_setting(key)
        if last and datetime.fromisoformat(last) > datetime.now().astimezone() - timedelta(hours=hours):
            return False
        self.store.set_setting(key, datetime.now().astimezone().isoformat())
        return True

    # ------------------------------------------------------------ delivering results
    async def deliver(self, chat_id: int, out: dict) -> None:
        t = out["type"]
        if t == "draft":
            d = out["draft"]
            await self.send(chat_id, pl.render_draft(d, self.cfg.currency, self.cfg.tz),
                            reply_markup=markup(pl.draft_buttons(out["draft_id"], d)))
        elif t == "answers":
            await self.send_messages(chat_id, out["messages"])
        elif t == "info":
            await self.send(chat_id, out["text"])
        elif t == "ai_down":
            self.store.set_setting("ai_down", "1")
            if self._rate_limited_alert_due(f"ai_alert_{out['kind']}", 6):
                await self.send(chat_id, AI_DOWN_TEXT[out["kind"]])
            else:
                await self.send(chat_id, "📥 Saved. I'll process it as soon as Claude is available.")
        elif t == "failed":
            await self.send(chat_id, f"⚠️ I couldn't process that message ({out['error']}). It's saved; "
                                     "try forwarding it again or tell Claude Code to look at the logs.")

    async def process(self, inbox_id: int) -> None:
        row = self.store.inbox_get(inbox_id)
        out = await pl.run_inbox_item(self.store, self.parser, self.cfg, row, self._download)
        await self.deliver(row["chat_id"], out)

    # ------------------------------------------------------------ handlers
    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if self.cfg.allowed_user_id is None:
            await update.effective_message.reply_text(
                f"Your Telegram user ID is {uid}.\nSet ALLOWED_USER_ID={uid} in .env and restart the bot. "
                "Until then I ignore everything else so strangers can't use your API credits.")
            return
        if self.authorized(update):
            await update.effective_message.reply_text("Hi! " + HELP)

    async def cmd_help(self, update: Update, ctx):
        if self.authorized(update):
            await update.effective_message.reply_text(HELP)

    async def _reply_report(self, update: Update, parsed: dict):
        msgs = pl.answer_question(self.store, self.cfg, parsed, datetime.now(self.cfg.tz))
        await self.send_messages(update.effective_chat.id, msgs)

    async def cmd_pending(self, update: Update, ctx):
        if self.authorized(update):
            await self._reply_report(update, {"intent": "pending", "keyword": " ".join(ctx.args) or None})

    async def cmd_pending_photos(self, update: Update, ctx):
        if self.authorized(update):
            await self._reply_report(update, {"intent": "pending_photos", "keyword": " ".join(ctx.args) or None})

    async def cmd_late(self, update: Update, ctx):
        if self.authorized(update):
            await self._reply_report(update, {"intent": "late", "keyword": " ".join(ctx.args) or None})

    async def cmd_owing(self, update: Update, ctx):
        if self.authorized(update):
            await self._reply_report(update, {"intent": "owing", "keyword": " ".join(ctx.args) or None})

    async def cmd_spent(self, update: Update, ctx):
        if not self.authorized(update):
            return
        month = next((a for a in ctx.args if re.fullmatch(r"\d{4}-\d{2}", a)), None)
        kw = " ".join(a for a in ctx.args if a != month) or None
        await self._reply_report(update, {"intent": "spent", "month": month, "keyword": kw})

    async def cmd_undo(self, update: Update, ctx):
        if self.authorized(update):
            await update.effective_message.reply_text(pl.undo_last(self.store))

    async def cmd_healthcheck(self, update: Update, ctx):
        if self.authorized(update):
            extra = "\n⚠️ Claude is currently unavailable." if self.store.get_setting("ai_down") == "1" else ""
            failed = self.store.one("SELECT COUNT(*) c FROM inbox WHERE status='failed'")["c"]
            extra += f"\nMessages that failed to process: {failed}" if failed else ""
            await update.effective_message.reply_text(pl.health_text(self.store, self.cfg) + extra)

    async def cmd_status(self, update: Update, ctx):
        if self.authorized(update):
            await self._reply_report(update, {"intent": "status", "keyword": " ".join(ctx.args) or None})

    async def cmd_delivered(self, update: Update, ctx):
        if self.authorized(update):
            await self._reply_report(update, {"intent": "delivered", "keyword": " ".join(ctx.args) or None})

    async def cmd_all(self, update: Update, ctx):
        if self.authorized(update):
            await self._reply_report(update, {"intent": "all", "keyword": " ".join(ctx.args) or None})

    async def cmd_settle(self, update: Update, ctx):
        if not self.authorized(update):
            return
        arg = ctx.args[0].lstrip("#") if ctx.args else None
        order_id = int(arg) if arg and arg.isdigit() else None
        drafts = pl.settle_drafts(self.store, order_id)
        if not drafts:
            await update.effective_message.reply_text(
                "Nothing to settle: every order's payments match its total." if order_id is None
                else f"Order {order_id} doesn't exist or already matches its payments.")
            return
        for d in drafts:
            draft_id = self.store.draft_add("settle", d)
            await self.send(update.effective_chat.id, pl.render_draft(d, self.cfg.currency, self.cfg.tz),
                            reply_markup=markup(pl.draft_buttons(draft_id, d)))

    async def cmd_received(self, update: Update, ctx):
        if not self.authorized(update):
            return
        drafts = pl.received_drafts(self.store, self.cfg, " ".join(ctx.args) or None)
        if not drafts:
            await update.effective_message.reply_text(
                "Nothing is waiting to reach you. Items show up here once the seller confirms they've arrived.")
            return
        for d in drafts:
            draft_id = self.store.draft_add("received", d)
            await self.send(update.effective_chat.id, pl.render_draft(d, self.cfg.currency, self.cfg.tz),
                            reply_markup=markup(pl.draft_buttons(draft_id, d)))

    async def send_batch(self, chat_id: int, post_id: int) -> None:
        """Photos of one post (what you ordered), with item statuses as the caption."""
        post = self.store.get_post(post_id)
        if not post:
            await self.send(chat_id, "I can't find that post any more.")
            return
        caption = pl.batch_caption(self.store, self.cfg, post_id, datetime.now(self.cfg.tz))
        photos = json.loads(post["image_file_ids"])[:10]
        try:
            if len(photos) > 1:
                await self.app.bot.send_media_group(chat_id=chat_id, media=[
                    InputMediaPhoto(f, caption=caption if i == 0 else None) for i, f in enumerate(photos)])
                return
            if photos:
                await self.app.bot.send_photo(chat_id=chat_id, photo=photos[0], caption=caption)
                return
        except BadRequest:
            log.warning("Photo send failed for post %s, falling back to text", post_id)
        await self.send(chat_id, caption + ("" if not photos else "\n(photo unavailable)")
                        + ("\n(no photo saved for this post)" if not photos else ""))

    async def on_message(self, update: Update, ctx):
        if not self.authorized(update):
            return
        msg = update.effective_message
        text = msg.text or msg.caption
        file_id = None
        if msg.photo:
            file_id = msg.photo[-1].file_id
        elif msg.document and (msg.document.mime_type or "").startswith("image/"):
            file_id = msg.document.file_id
        if not text and not file_id:
            return
        origin = msg.forward_origin
        when = origin.date if origin is not None else msg.date
        fingerprint = None
        if origin is not None:  # same original message forwarded twice => same text, origin time and photo
            unique = (msg.photo[-1].file_unique_id if msg.photo
                      else msg.document.file_unique_id if msg.document else "")
            fingerprint = hashlib.sha256(f"{text or ''}|{when.isoformat()}|{unique}".encode()).hexdigest()[:20]
        inbox_id = self.store.inbox_add(
            msg.chat_id, msg.message_id, text, file_id, msg.media_group_id,
            origin is not None and origin.type == "channel", when.isoformat(), fingerprint)
        await self.process(inbox_id)

    async def on_callback(self, update: Update, ctx):
        q = update.callback_query
        await q.answer()
        if not self.authorized(update):
            return
        action, *rest = q.data.split(":")
        if action == "t":  # tapped a #serial button: send that batch's photos
            await self.send_batch(q.message.chat_id, int(rest[0]))
            return
        draft_id = int(rest[0])
        row = self.store.draft_get(draft_id)
        if not row or row["status"] != "pending":
            await q.edit_message_reply_markup(None)
            return
        cur, tz = self.cfg.currency, self.cfg.tz
        d = row["data"]
        if action == "o":  # tick/untick an order on a payment
            d = pl.toggle_payment_order(self.store, draft_id, int(rest[1]))
            await q.edit_message_text(pl.render_draft(d, cur, tz), reply_markup=markup(pl.draft_buttons(draft_id, d)))
            return
        if action in ("r", "ra"):  # toggle one item / select-all on a /received list
            d = pl.toggle_received(self.store, draft_id, int(rest[1]) if action == "r" else None)
            await q.edit_message_text(pl.render_draft(d, cur, tz), reply_markup=markup(pl.draft_buttons(draft_id, d)))
            return
        if action == "rc":
            if d["selected"]:
                result = pl.apply_draft(self.store, draft_id, cur, tz)
                await q.edit_message_text(pl.render_draft(d, cur, tz) + "\n\n" + result)
            return
        if action == "c":
            result = pl.apply_draft(self.store, draft_id, cur, tz)
            await q.edit_message_text(pl.render_draft(d, cur, tz) + "\n\n" + result)
        elif action == "x":
            self.store.draft_update(draft_id, status="cancelled")
            await q.edit_message_text("✖️ Cancelled. Nothing saved.")
        elif action == "m":
            await q.edit_message_reply_markup(markup(pl.candidate_buttons(draft_id, d)))
        elif action == "s":
            d = pl.select_candidate(self.store, draft_id, int(rest[1]))
            await q.edit_message_text(pl.render_draft(d, cur, tz), reply_markup=markup(pl.draft_buttons(draft_id, d)))

    async def on_error(self, update, ctx: ContextTypes.DEFAULT_TYPE):
        log.exception("Unhandled error", exc_info=ctx.error)
        if self.cfg.allowed_user_id and self._rate_limited_alert_due("error_alert", 1):
            try:
                await self.send(self.cfg.allowed_user_id,
                                f"⚠️ Something went wrong inside the bot: {type(ctx.error).__name__}: {ctx.error}")
            except Exception:  # noqa: BLE001
                log.exception("Could not send error alert")

    # ------------------------------------------------------------ jobs
    async def job_retry_queue(self, ctx: ContextTypes.DEFAULT_TYPE):
        rows = self.store.inbox_queued()
        if not rows:
            return
        announced = False
        for row in rows:
            out = await pl.run_inbox_item(self.store, self.parser, self.cfg, row, self._download)
            if out["type"] == "ai_down":
                await self.deliver(row["chat_id"], out)
                return
            if not announced and self.store.get_setting("ai_down") == "1":
                self.store.set_setting("ai_down", "0")
                await self.send(row["chat_id"], f"✅ Claude is back. Processing {len(rows)} saved message(s). "
                                                "Please confirm the drafts below.")
            announced = True
            await self.deliver(row["chat_id"], out)

    async def job_weekly_digest(self, ctx: ContextTypes.DEFAULT_TYPE):
        """Text-only roundup of everything still incoming. Photos stay on demand via /pending_photos."""
        uid = self.cfg.allowed_user_id
        if not uid:
            return
        for chunk in pl.weekly_digest(self.store, self.cfg, datetime.now(self.cfg.tz)):
            await self.send(uid, chunk)

    async def job_health_ping(self, ctx: ContextTypes.DEFAULT_TYPE):
        url = self.cfg.healthcheck_url
        if not url:
            return
        if self.store.get_setting("ai_down") == "1":
            url = url.rstrip("/") + "/fail"  # Healthchecks.io: signals failure, so you also get its email
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.get(url)
        except Exception:  # noqa: BLE001
            log.warning("Health ping failed")

    # ------------------------------------------------------------ wiring
    def build(self) -> Application:
        async def post_init(application: Application) -> None:
            # Fills Telegram's "/" command menu so you don't have to remember the names.
            await application.bot.set_my_commands([
                ("pending", "What hasn't arrived yet (text list)"),
                ("pending_photos", "What hasn't arrived yet, with photos"),
                ("status", "Everything I'm still awaiting (tap # for photos)"),
                ("all", "Every order and its status at a glance"),
                ("settle", "Close an order whose payments don't match its total"),
                ("delivered", "Arrived with the seller, not yet with me"),
                ("received", "Mark what has reached me"),
                ("late", "Items past the late threshold"),
                ("spent", "How much I've spent"),
                ("owing", "Unpaid or part-paid orders"),
                ("undo", "Reverse the last confirmed item"),
                ("healthcheck", "Is the bot running OK?"),
                ("help", "How to use this bot"),
            ])

        app = Application.builder().token(self.cfg.telegram_token).post_init(post_init).build()
        self.app = app
        for name, fn in [("start", self.cmd_start), ("help", self.cmd_help), ("pending", self.cmd_pending),
                         ("pending_photos", self.cmd_pending_photos),
                         ("late", self.cmd_late), ("spent", self.cmd_spent), ("owing", self.cmd_owing),
                         ("undo", self.cmd_undo), ("healthcheck", self.cmd_healthcheck),
                         ("status", self.cmd_status), ("delivered", self.cmd_delivered),
                         ("received", self.cmd_received), ("all", self.cmd_all), ("settle", self.cmd_settle)]:
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(
            (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.IMAGE, self.on_message))
        app.add_error_handler(self.on_error)
        jq = app.job_queue
        jq.run_daily(self.job_weekly_digest, days=(self.cfg.digest_day,),
                     time=time(self.cfg.digest_hour, 0, tzinfo=self.cfg.tz))
        jq.run_repeating(self.job_retry_queue, interval=900, first=60)
        if self.cfg.healthcheck_url:
            jq.run_repeating(self.job_health_ping, interval=300, first=10)
        return app
