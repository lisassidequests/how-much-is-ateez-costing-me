"""Business logic: turn parsed messages into drafts, apply confirmed drafts, answer questions.

Nothing here talks to Telegram, so it can be tested without a bot token.
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .ai import AIUnavailable, fallback_tag
from .config import Config
from .store import Store

TOL = 0.011  # money comparison tolerance (1 cent)
MAX_GROUPS = 10  # max photo messages sent in one reply


# ---------------------------------------------------------------- helpers
def money(x: float, cur: str) -> str:
    return f"{cur}{x:,.2f}"


def _tokens(s: str | None) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split()


def sim(a: str | None, b: str | None) -> float:
    return difflib.SequenceMatcher(None, " ".join(_tokens(a)), " ".join(_tokens(b))).ratio()


def to_dt(iso: str | None, tz: ZoneInfo, default: datetime | None = None) -> datetime:
    if iso:
        try:
            dt = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=tz)
        except ValueError:
            pass
    return default or datetime.now(tz)


def fmt_date(iso: str | None, tz: ZoneInfo) -> str:
    return to_dt(iso, tz).astimezone(tz).strftime("%d %b %Y") if iso else "?"


def name_match(names: list[str], item_name: str) -> bool:
    item = " ".join(_tokens(item_name))
    for n in names:
        n = " ".join(_tokens(n))
        if len(n) >= 3 and (n in item or item in n):
            return True
    return False


def keyword_ok(keyword: str | None, *fields) -> bool:
    if not keyword:
        return True
    hay = " ".join(str(f) for f in fields if f).lower()
    return all(tok in hay for tok in _tokens(keyword))


def clock_start(row, tz: ZoneInfo) -> datetime:
    """Lateness clock starts at first payment, else the order date."""
    return to_dt(row["first_paid"] or row["ordered_at"], tz)


def post_candidates(store: Store, title: str, n: int = 5) -> list[dict]:
    scored = sorted(((sim(title, p["title"]), p) for p in store.recent_posts(200)),
                    key=lambda t: t[0], reverse=True)
    return [{"id": p["id"], "label": f"#{p['tag']} {p['title']}"} for s, p in scored[:n] if s > 0.25]


# ---------------------------------------------------------------- drafts
def build_draft(store: Store, parsed: dict, ctx: dict, tz: ZoneInfo) -> dict:
    """ctx: image_file_id, media_group_id, raw_text, msg_dt (aware datetime)."""
    kind = parsed.get("kind")
    msg_dt: datetime = ctx["msg_dt"]

    if kind == "channel_post":
        tag = (parsed.get("tag") or fallback_tag(ctx.get("raw_text")) or "").lstrip("#").lower()
        if not tag:
            return {"kind": "unreadable", "reason": "This looks like a channel post but I couldn't find its #serial."}
        title = parsed.get("title") or (ctx.get("raw_text") or "").split("\n")[0][:120] or tag
        fid = ctx.get("image_file_id")
        return {"kind": "channel_post", "tag": tag, "title": title, "price_note": parsed.get("price_note"),
                "image_file_ids": [fid] if fid else [], "media_group_id": ctx.get("media_group_id"),
                "raw_text": ctx.get("raw_text"), "posted_at": msg_dt.isoformat(),
                "exists": store.get_post_by_tag(tag) is not None}

    if kind == "order_confirmation":
        items = [{"name": i["name"], "qty": int(i.get("qty") or 1), "line_total": float(i.get("line_total") or 0)}
                 for i in parsed.get("items") or []]
        item_sum = round(sum(i["line_total"] for i in items), 2)
        total = parsed.get("total_due")
        total = float(total) if total is not None else (item_sum or None)
        if total is None or not items:
            return {"kind": "unreadable", "reason": "I couldn't read the items or total from this order message."}
        title = parsed.get("title") or "(untitled order)"
        warnings = []
        if abs(item_sum - total) > TOL:
            warnings.append(f"Items add up to {item_sum:.2f} but the total says {total:.2f}. Check before confirming.")
        post_id = post_tag = None
        mt = (parsed.get("matched_tag") or "").lstrip("#").lower()
        post = store.get_post_by_tag(mt) if mt and parsed.get("match_confidence") != "low" else None
        cands = post_candidates(store, title)
        if post is None and cands:  # cheap fallback if the model found nothing
            best = store.get_post(cands[0]["id"])
            if sim(title, best["title"]) >= 0.85:
                post = best
        if post is not None:
            post_id, post_tag = post["id"], post["tag"]
        return {"kind": "order", "title": title, "items": items, "total_due": total,
                "mailing_fee": parsed.get("mailing_fee"), "note": parsed.get("note"),
                "post_id": post_id, "post_tag": post_tag, "candidates": cands,
                "ordered_at": msg_dt.isoformat(), "raw_text": ctx.get("raw_text"), "warnings": warnings}

    if kind == "payment_proof":
        amount = parsed.get("amount")
        if amount is None:
            return {"kind": "unreadable", "reason": "I couldn't read the payment amount from this screenshot."}
        amount = float(amount)
        outstanding = [o for o in store.orders_with_balance() if o["total_due"] - o["paid"] > TOL]
        exact = [o for o in outstanding if abs((o["total_due"] - o["paid"]) - amount) <= TOL]
        order_id = exact[0]["id"] if len(exact) == 1 else None
        cands = [{"id": o["id"], "label": f"Order {o['id']}: {o['title'][:40]} (owing {o['total_due'] - o['paid']:.2f})"}
                 for o in outstanding[:6]]
        warnings = []
        if order_id is None:
            warnings.append("No single unpaid order matches this amount exactly. Pick the right one, or confirm to save it unlinked."
                            if cands else "No unpaid order found; this will be saved unlinked.")
        return {"kind": "payment", "amount": amount,
                "paid_at": to_dt(parsed.get("paid_at"), tz, msg_dt).isoformat(),
                "reference": parsed.get("reference"), "order_id": order_id,
                "candidates": cands, "warnings": warnings}

    if kind == "arrival_update":
        tags = [t.lstrip("#").lower() for t in parsed.get("tags") or [] if t]
        if not tags and (t := fallback_tag(ctx.get("raw_text"))):
            tags = [t]
        names = [n for n in parsed.get("names") or [] if n]
        pending = store.items_pending()
        chosen, warnings = [], []
        if tags:
            for tag in tags:
                group = [r for r in pending if r["tag"] == tag]
                if store.get_post_by_tag(tag) is None:
                    warnings.append(f"#{tag} isn't one of your saved posts.")
                if names and group:
                    named = [r for r in group if name_match(names, r["name"])]
                    if named:
                        group = named
                    else:
                        warnings.append(f"Couldn't match {', '.join(names)} under #{tag}; including all its pending items.")
                chosen += group
        elif names:
            chosen = [r for r in pending if name_match(names, r["name"])]
            warnings.append("No #serial in the message, so I matched by item names only.")
        if not chosen:
            return {"kind": "unreadable", "reason": "Arrival notice read, but no pending items matched it."
                    + ("".join(f"\n⚠️ {w}" for w in warnings))}
        return {"kind": "arrival", "tags": tags, "names": names, "note": parsed.get("note"),
                "item_ids": [r["id"] for r in chosen],
                "items": [f"{r['name']} ×{r['qty']}" + (f" (#{r['tag']})" if r["tag"] else "") for r in chosen],
                "arrived_at": msg_dt.isoformat(), "warnings": warnings}

    return {"kind": "unreadable", "reason": parsed.get("summary") or "I didn't recognise this message."}


def render_draft(d: dict, cur: str, tz: ZoneInfo) -> str:
    k = d["kind"]
    warn = "".join(f"\n⚠️ {w}" for w in d.get("warnings", []))
    if k == "channel_post":
        extra = f"\n{d['price_note']}" if d.get("price_note") else ""
        status = "\n(already saved; this will update it)" if d.get("exists") else ""
        return f"📌 Channel post #{d['tag']}\n{d['title']}{extra}{status}"
    if k == "order":
        lines = "\n".join(f"• {i['name']} ×{i['qty']} — {money(i['line_total'], cur)}" for i in d["items"])
        link = (f"🔗 Linked to #{d['post_tag']}" if d.get("post_id")
                else "🔗 Not linked to a channel post (tap Change link to pick one)")
        fee = f"\nMailing fee: {money(d['mailing_fee'], cur)}" if d.get("mailing_fee") else ""
        note = f"\nNote: {d['note']}" if d.get("note") else ""
        return (f"🛒 Order: {d['title']}\n{lines}{fee}\nTotal: {money(d['total_due'], cur)}{note}\n"
                f"{link}\nOrdered: {fmt_date(d['ordered_at'], tz)}{warn}")
    if k == "payment":
        link = f"🔗 Order {d['order_id']}" if d.get("order_id") else "🔗 Not linked to an order"
        ref = f"\nRef: {d['reference']}" if d.get("reference") else ""
        return f"💸 Payment {money(d['amount'], cur)} on {fmt_date(d['paid_at'], tz)}{ref}\n{link}{warn}"
    if k == "arrival":
        lines = "\n".join(f"• {i}" for i in d["items"])
        note = f"\nNote: {d['note']}" if d.get("note") else ""
        return f"📦 Arrived ({len(d['items'])} item(s)):\n{lines}{note}{warn}"
    return f"❓ {d.get('reason', 'Not understood.')}"


def draft_buttons(draft_id: int, d: dict) -> list[list[tuple[str, str]]]:
    rows = [[("✅ Confirm", f"c:{draft_id}"), ("✖️ Cancel", f"x:{draft_id}")]]
    if d["kind"] in ("order", "payment") and d.get("candidates"):
        rows.append([("🔗 Change link" if d["kind"] == "order" else "🔗 Pick order", f"m:{draft_id}")])
    return rows


def candidate_buttons(draft_id: int, d: dict) -> list[list[tuple[str, str]]]:
    rows = [[(c["label"][:55], f"s:{draft_id}:{i}")] for i, c in enumerate(d.get("candidates", []))]
    rows.append([("None of these", f"s:{draft_id}:-1")])
    return rows


def select_candidate(store: Store, draft_id: int, idx: int) -> dict:
    row = store.draft_get(draft_id)
    d = row["data"]
    cand = d["candidates"][idx] if idx >= 0 else None
    if d["kind"] == "order":
        post = store.get_post(cand["id"]) if cand else None
        d["post_id"], d["post_tag"] = (post["id"], post["tag"]) if post else (None, None)
    elif d["kind"] == "payment":
        d["order_id"] = cand["id"] if cand else None
        d["warnings"] = []
    store.draft_update(draft_id, data=d)
    return d


def apply_draft(store: Store, draft_id: int, cur: str, tz: ZoneInfo) -> str:
    row = store.draft_get(draft_id)
    d, k = row["data"], row["data"]["kind"]
    if k == "channel_post":
        pid, created = store.upsert_post(d["tag"], d["title"], d.get("price_note"), d["image_file_ids"],
                                         d.get("media_group_id"), d.get("raw_text"), d.get("posted_at"))
        result = {"type": "post", "post_id": pid, "created": created}
        text = f"✅ Saved #{d['tag']}: {d['title']}"
    elif k == "order":
        oid, item_ids = store.add_order(d.get("post_id"), d["title"], d["total_due"], d.get("mailing_fee"),
                                        d.get("note"), d["ordered_at"], d.get("raw_text"), d["items"])
        result = {"type": "order", "order_id": oid, "item_ids": item_ids}
        text = (f"✅ Order {oid} saved: {len(item_ids)} item(s), {money(d['total_due'], cur)}.\n"
                "Send your payment screenshot when you've paid.")
    elif k == "payment":
        pid = store.add_payment(d.get("order_id"), d["amount"], d["paid_at"], d.get("reference"))
        result = {"type": "payment", "payment_id": pid}
        text = f"✅ Logged {money(d['amount'], cur)}."
        if d.get("order_id"):
            o = next(o for o in store.orders_with_balance() if o["id"] == d["order_id"])
            left = o["total_due"] - o["paid"]
            text += f" Order {o['id']} balance: {money(max(left, 0), cur)}."
    elif k == "arrival":
        fresh = [r["id"] for r in store.all(
            f"SELECT id FROM items WHERE arrived_at IS NULL AND id IN ({','.join('?' * len(d['item_ids']))})",
            d["item_ids"])]
        store.mark_arrived(fresh, d["arrived_at"])
        result = {"type": "arrival", "item_ids": fresh}
        left = len(store.items_pending())
        text = f"✅ Marked {len(fresh)} item(s) as arrived. {left} item(s) still pending."
    else:
        raise ValueError(f"Cannot apply draft kind {k}")
    store.draft_update(draft_id, status="confirmed", result=result)
    return text


def undo_last(store: Store) -> str:
    row = store.last_confirmed_draft()
    if not row:
        return "Nothing to undo."
    r = row["result"]
    if r["type"] == "post":
        if not r["created"]:
            store.draft_update(row["id"], status="undone")
            return "That post was already saved before, so only the update is forgotten; the post remains."
        store.run("UPDATE orders SET post_id=NULL WHERE post_id=?", (r["post_id"],))
        store.run("UPDATE items SET post_id=NULL WHERE post_id=?", (r["post_id"],))
        store.run("DELETE FROM posts WHERE id=?", (r["post_id"],))
        msg = "Removed the last saved post."
    elif r["type"] == "order":
        store.run("DELETE FROM orders WHERE id=?", (r["order_id"],))
        msg = f"Removed order {r['order_id']} and its items (any payments linked to it are kept, unlinked)."
    elif r["type"] == "payment":
        store.run("DELETE FROM payments WHERE id=?", (r["payment_id"],))
        msg = "Removed the last payment."
    else:
        if r["item_ids"]:
            store.run(f"UPDATE items SET arrived_at=NULL WHERE id IN ({','.join('?' * len(r['item_ids']))})",
                      r["item_ids"])
        msg = f"Marked {len(r['item_ids'])} item(s) as not arrived again."
    store.draft_update(row["id"], status="undone")
    return "↩️ " + msg


# ---------------------------------------------------------------- reports
def pending_messages(store: Store, cfg: Config, now: datetime, rows=None) -> list[dict]:
    rows = store.items_pending() if rows is None else rows
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["tag"] or f"order-{r['order_id']}", []).append(r)
    msgs = []
    for grp in groups.values():
        first = grp[0]
        head = (f"#{first['tag']} {first['post_title']}" if first["tag"]
                else f"{first['order_title']} (no channel post linked)")
        lines = [head]
        for r in grp:
            age = (now - clock_start(r, cfg.tz)).days
            since = "payment" if r["first_paid"] else "order"
            late = " ⚠️ LATE" if age >= cfg.late_days else ""
            lines.append(f"• {r['name']} ×{r['qty']}: {age}d since {since}{late}")
        photos = json.loads(first["image_file_ids"]) if first["image_file_ids"] else []
        msgs.append({"text": "\n".join(lines)[:1000], "photo": photos[0] if photos else None,
                     "item_ids": [r["id"] for r in grp]})
    return msgs


def late_rows(store: Store, cfg: Config, now: datetime, keyword: str | None = None):
    return [r for r in store.items_pending()
            if (now - clock_start(r, cfg.tz)).days >= cfg.late_days
            and keyword_ok(keyword, r["name"], r["order_title"], r["post_title"], r["tag"])]


def weekly_digest(store: Store, cfg: Config, now: datetime, keyword: str | None = None) -> list[str]:
    """Text-only roundup of everything still incoming, oldest first, late batches flagged.
    Used for the Sunday digest and /pending. Returns [] when nothing is pending.
    Split into chunks to stay under Telegram's message limit."""
    batches: dict[str, dict] = {}
    pending = [r for r in store.items_pending()
               if keyword_ok(keyword, r["name"], r["order_title"], r["post_title"], r["tag"])]
    for r in pending:
        key = r["tag"] or f"order-{r['order_id']}"
        b = batches.setdefault(key, {
            "head": (f"#{r['tag']} {r['post_title']}" if r["tag"] else f"{r['order_title']} (no post linked)"),
            "names": [], "age": 0})
        b["names"].append(r["name"] if r["qty"] == 1 else f"{r['name']} ×{r['qty']}")
        b["age"] = max(b["age"], (now - clock_start(r, cfg.tz)).days)
    if not batches:
        return []
    ordered = sorted(batches.values(), key=lambda b: -b["age"])
    n_items = len(pending)
    n_late = sum(1 for b in ordered if b["age"] >= cfg.late_days)
    header = f"📦 Still incoming: {n_items} item(s) in {len(ordered)} batch(es)"
    if n_late:
        header += f"\n⚠️ {n_late} batch(es) past {cfg.late_days // 7} weeks. Maybe ask the seller?"
    lines = [f"{'⚠️ ' if b['age'] >= cfg.late_days else '• '}{b['head']}: {', '.join(b['names'])} ({b['age']}d)"
             for b in ordered]
    chunks, cur = [], header
    for line in lines:
        if len(cur) + len(line) + 2 > 3800:
            chunks.append(cur)
            cur = "(continued)"
        cur += "\n" + line
    chunks.append(cur)
    return chunks


def spent_text(store: Store, cfg: Config, month: str | None, keyword: str | None) -> str:
    cur = cfg.currency
    pays = [p for p in store.all_payments()
            if (not month or to_dt(p["paid_at"], cfg.tz).astimezone(cfg.tz).strftime("%Y-%m") == month)
            and keyword_ok(keyword, p["order_title"], p["post_title"], p["tag"], p["item_names"], p["reference"])]
    label = " ".join(x for x in [keyword, month] if x) or "all time"
    if not pays:
        return f"No payments recorded ({label})."
    total = sum(p["amount"] for p in pays)
    lines = [f"💰 Spent ({label}): {money(total, cur)} across {len(pays)} payment(s)"]
    if not month:
        by_month: dict[str, float] = {}
        for p in pays:
            m = to_dt(p["paid_at"], cfg.tz).astimezone(cfg.tz).strftime("%Y-%m")
            by_month[m] = by_month.get(m, 0) + p["amount"]
        lines.append("\nBy month:")
        lines += [f"• {m}: {money(v, cur)}" for m, v in sorted(by_month.items())]
    by_batch: dict[str, float] = {}
    for p in pays:
        key = f"#{p['tag']}" if p["tag"] else (p["order_title"] or "unlinked payment")[:40]
        by_batch[key] = by_batch.get(key, 0) + p["amount"]
    lines.append("\nBy batch:")
    lines += [f"• {k}: {money(v, cur)}" for k, v in sorted(by_batch.items(), key=lambda kv: -kv[1])[:10]]
    return "\n".join(lines)


def owing_text(store: Store, cfg: Config, keyword: str | None = None) -> str:
    rows = [o for o in store.orders_with_balance() if o["total_due"] - o["paid"] > TOL
            and keyword_ok(keyword, o["title"])]
    if not rows:
        return "✅ Nothing owing. All saved orders are fully paid."
    lines = ["🧾 Unpaid / part-paid orders:"]
    for o in rows:
        lines.append(f"• Order {o['id']} {o['title'][:45]}: owing {money(o['total_due'] - o['paid'], cfg.currency)} "
                     f"of {money(o['total_due'], cfg.currency)}")
    return "\n".join(lines)


def answer_question(store: Store, cfg: Config, parsed: dict, now: datetime) -> list[dict]:
    intent, kw, month = parsed.get("intent"), parsed.get("keyword"), parsed.get("month")
    if intent == "spent":
        return [{"text": spent_text(store, cfg, month, kw)}]
    if intent == "owing":
        return [{"text": owing_text(store, cfg, kw)}]
    if intent == "pending":  # compact text list of everything not arrived, late or not
        chunks = weekly_digest(store, cfg, now, kw)
        return [{"text": c} for c in chunks] or [{"text": "🎉 Nothing pending."}]
    if intent in ("pending_photos", "late"):
        rows = (late_rows(store, cfg, now, kw) if intent == "late" else
                [r for r in store.items_pending()
                 if keyword_ok(kw, r["name"], r["order_title"], r["post_title"], r["tag"])])
        if not rows:
            return [{"text": "🎉 Nothing late." if intent == "late" else "🎉 Nothing pending."}]
        return cap_messages(pending_messages(store, cfg, now, rows))
    return [{"text": "I can answer questions about spending, pending items, late items and what you owe. "
                     "Try /spent, /pending, /late or /owing."}]


def cap_messages(msgs: list[dict]) -> list[dict]:
    if len(msgs) <= MAX_GROUPS:
        return msgs
    return msgs[:MAX_GROUPS] + [{"text": f"…and {len(msgs) - MAX_GROUPS} more batches. Narrow it down, e.g. /pending_photos ateez."}]


def status_text(store: Store, cfg: Config) -> str:
    q = len(store.inbox_queued())
    counts = {t: store.one(f"SELECT COUNT(*) c FROM {t}")["c"] for t in ("posts", "orders", "items", "payments")}
    return (f"🟢 Bot running. Saved: {counts['posts']} posts, {counts['orders']} orders, "
            f"{counts['items']} items, {counts['payments']} payments.\n"
            f"Queued messages waiting for Claude: {q}\n"
            f"Late threshold: {cfg.late_days} days. Model: {cfg.model}")


# ---------------------------------------------------------------- inbox processing
async def run_inbox_item(store: Store, parser, cfg: Config, row, get_image) -> dict:
    """Process one stored message. Returns a dict describing what to tell the user:
    draft | answers | info | ai_down | failed. Messages Claude can't read right now stay 'queued'."""
    try:
        if row["photo_file_id"] and row["media_group_id"] and not row["text"]:
            if store.append_post_image(row["media_group_id"], row["photo_file_id"]):
                store.inbox_set(row["id"], "done")
                return {"type": "info", "text": "📎 Added that photo to the saved post."}
        image = await get_image(row["photo_file_id"]) if row["photo_file_id"] else None
        msg_dt = to_dt(row["msg_date"], cfg.tz)
        now = datetime.now(cfg.tz)
        known = [(p["tag"], p["title"]) for p in store.recent_posts(60)]
        parsed = await parser.classify(text=row["text"], image=image,
                                       from_channel=bool(row["forwarded_from_channel"]),
                                       known_posts=known, today=now.strftime("%Y-%m-%d"))
        if parsed.get("kind") == "question":
            store.inbox_set(row["id"], "done")
            return {"type": "answers", "messages": answer_question(store, cfg, parsed, now)}
        ctx = {"image_file_id": row["photo_file_id"], "media_group_id": row["media_group_id"],
               "raw_text": row["text"], "msg_dt": msg_dt}
        draft = build_draft(store, parsed, ctx, cfg.tz)
        store.inbox_set(row["id"], "done")
        if draft["kind"] == "unreadable":
            return {"type": "info", "text": "❓ " + draft["reason"]}
        draft_id = store.draft_add(draft["kind"], draft, row["id"])
        return {"type": "draft", "draft_id": draft_id, "draft": draft}
    except AIUnavailable as exc:
        return {"type": "ai_down", "kind": exc.kind, "detail": exc.detail}  # stays queued
    except Exception as exc:  # noqa: BLE001 - never lose a message
        store.inbox_set(row["id"], "failed", str(exc)[:300])
        return {"type": "failed", "error": str(exc)[:300]}
