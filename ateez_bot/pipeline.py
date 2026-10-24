"""Business logic: turn parsed messages into drafts, apply confirmed drafts, answer questions.

Nothing here talks to Telegram, so it can be tested without a bot token.
"""
from __future__ import annotations

import difflib
import itertools
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .ai import AIUnavailable, fallback_tag, fallback_tags
from .config import Config
from .store import Store, now_iso

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


def clean_name(name: str) -> str:
    """Drop the seller's leading count marker ("X1 OT8" -> "OT8"); quantity is stored separately."""
    return re.sub(r"^\s*[xX×]\s*\d+\s+", "", name).strip() or name.strip()


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


def find_duplicate_order(store: Store, title: str, total: float, items: list[dict]):
    """A saved order with the same product, items and total (could be a re-forward or a genuine repeat)."""
    names = sorted(" ".join(_tokens(i["name"])) for i in items)
    for o in store.orders_with_balance():
        if abs(o["total_due"] - total) > TOL or sim(title, o["title"]) < 0.9:
            continue
        saved = sorted(" ".join(_tokens(r["name"])) for r in store.all("SELECT name FROM items WHERE order_id=?", (o["id"],)))
        if saved == names:
            return o
    return None


def find_duplicate_payment(store: Store, amount: float, paid_at: str, tz: ZoneInfo):
    day = to_dt(paid_at, tz).astimezone(tz).date()
    for p in store.all("SELECT * FROM payments WHERE assumed=0"):
        if abs(p["amount"] - amount) <= TOL and to_dt(p["paid_at"], tz).astimezone(tz).date() == day:
            return p
    return None


def dup_message(dup, tz: ZoneInfo) -> str:
    how = {"confirmed": "and saved it", "cancelled": "but cancelled it",
           "pending": "and it's still waiting for your confirmation", "undone": "saved it, then undid it"
           }.get(dup["draft_status"], "")
    return (f"Possible duplicate: you already forwarded this exact message on {fmt_date(dup['created_at'], tz)} "
            f"{how}. Confirm to process it again, or Cancel.")


def relink_orders(store: Store) -> int:
    """Link orders saved before their channel post was forwarded, when exactly one post clearly matches.
    Needs a close title match (>= 0.85) that beats the runner-up, so similar versions are never mixed up."""
    posts = store.recent_posts(500)
    n = 0
    for o in store.all("SELECT id, title FROM orders WHERE post_id IS NULL"):
        scored = sorted(((sim(o["title"], p["title"]), p) for p in posts), key=lambda t: -t[0])
        if not scored or scored[0][0] < 0.85:
            continue
        if len(scored) > 1 and scored[1][0] > scored[0][0] - 0.02:
            continue  # ambiguous between two similar posts: leave it for the user
        store.run("UPDATE orders SET post_id=? WHERE id=?", (scored[0][1]["id"], o["id"]))
        store.run("UPDATE items SET post_id=? WHERE order_id=?", (scored[0][1]["id"], o["id"]))
        n += 1
    return n


MAX_PAY_CANDIDATES = 12


def plan_allocation(store: Store, amount: float, order_ids: list[int]):
    """Spread a payment over the chosen orders, oldest first, each up to what it still owes.
    Returns ([(order_id, amount_applied, owes)], unallocated_remainder)."""
    bal = {o["id"]: o for o in store.orders_with_balance()}
    remaining, plan = round(amount, 2), []
    for oid in sorted(order_ids):
        if oid not in bal:
            continue
        owes = round(bal[oid]["total_due"] - bal[oid]["real_paid"], 2)   # automatic "assumed" payments don't count
        take = round(min(owes, remaining), 2)
        if owes <= TOL or take <= TOL:
            continue
        plan.append((oid, take, owes))
        remaining = round(remaining - take, 2)
    return plan, remaining


def refresh_payment_plan(store: Store, d: dict) -> dict:
    labels = {c["id"]: c["label"] for c in d["candidates"]}
    plan, remainder = plan_allocation(store, d["amount"], d["selected"])
    d["plan"] = [{"order_id": oid, "label": labels.get(oid, f"Order {oid}"), "amount": amt, "owes": owes}
                 for oid, amt, owes in plan]
    d["remainder"] = remainder
    return d


def toggle_payment_order(store: Store, draft_id: int, order_id: int) -> dict:
    d = store.draft_get(draft_id)["data"]
    if order_id in {c["id"] for c in d["candidates"]}:
        d["selected"] = sorted(set(d["selected"]) ^ {order_id})
    d["warnings"] = [w for w in d["warnings"] if w.startswith("Possible duplicate")]
    refresh_payment_plan(store, d)
    store.draft_update(draft_id, data=d)
    return d


def find_topup_targets(store: Store, title: str, items: list[dict]) -> list:
    """Earlier orders a second payment could belong to: same product, not already topped up, oldest first.
    If the message lists items, prefer orders with exactly those items."""
    cands = [o for o in store.orders_with_balance() if not o["topup"] and sim(title, o["title"]) >= 0.85]
    names = sorted(" ".join(_tokens(i["name"])) for i in items)
    if names:
        exact = [o for o in cands if sorted(" ".join(_tokens(r["name"])) for r in
                                            store.all("SELECT name FROM items WHERE order_id=?", (o["id"],))) == names]
        cands = exact or cands
    return sorted(cands, key=lambda o: o["id"])


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
        exists = store.get_post_by_tag(tag) is not None
        return {"kind": "channel_post", "tag": tag, "title": title, "price_note": parsed.get("price_note"),
                "image_file_ids": [fid] if fid else [], "media_group_id": ctx.get("media_group_id"),
                "raw_text": ctx.get("raw_text"), "posted_at": msg_dt.isoformat(),
                "exists": exists,
                "warnings": ([f"Possible duplicate: #{tag} is already saved. Confirm to update it, or Cancel."]
                             if exists else [])}

    if kind == "order_confirmation":
        items = [{"name": clean_name(i["name"]), "qty": int(i.get("qty") or 1),
                  "line_total": float(i.get("line_total") or 0)}
                 for i in parsed.get("items") or []]
        item_sum = round(sum(i["line_total"] for i in items), 2)
        total = parsed.get("total_due")
        total = float(total) if total is not None else (item_sum or None)
        title = parsed.get("title") or "(untitled order)"
        warnings = []
        if parsed.get("is_second_payment") and total:
            # the shipping/consolidation payment: adds to the earlier order's cost, no new items or statuses
            targets = find_topup_targets(store, title, items)
            if targets:
                o = targets[0]
                tag = store.one("SELECT p.tag FROM posts p JOIN orders x ON x.post_id=p.id WHERE x.id=?", (o["id"],))
                if len(targets) > 1:
                    warnings.append(f"{len(targets)} earlier orders match this product; added to the oldest (Order {o['id']}). "
                                    "Cancel if it belongs to another.")
                return {"kind": "topup", "order_id": o["id"], "title": o["title"], "tag": tag["tag"] if tag else None,
                        "amount": total, "old_total": o["total_due"], "paid_at": msg_dt.isoformat(), "warnings": warnings}
            if not items:
                return {"kind": "unreadable", "reason": "This looks like a second payment, but I can't find the first "
                        "order for it. Forward the original order message first."}
            warnings.append("This looks like a second payment but I couldn't find the first order, so it is shown as a new order.")
        if total is None or not items:
            return {"kind": "unreadable", "reason": "I couldn't read the items or total from this order message."}
        if abs(item_sum - total) > TOL:
            warnings.append(f"Items add up to {item_sum:.2f} but the total says {total:.2f}. Check before confirming.")
        dup = find_duplicate_order(store, title, total, items)
        if dup:
            warnings.insert(0, f"Possible duplicate: this looks like order {dup['id']} ({fmt_date(dup['ordered_at'], tz)}), "
                               "same product, items and total. Confirm to save it again, or Cancel.")
        post_id = post_tag = None
        mt = (parsed.get("matched_tag") or "").lstrip("#").lower()
        post = store.find_post_by_tag(mt) if mt and parsed.get("match_confidence") != "low" else None
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
        outstanding = [o for o in store.orders_with_balance() if o["total_due"] - o["real_paid"] > TOL]
        cands = [{"id": o["id"], "owes": round(o["total_due"] - o["real_paid"], 2),
                  "label": f"Order {o['id']}: {o['title'][:32]}"} for o in outstanding[:MAX_PAY_CANDIDATES]]
        # one transfer may cover several orders: look for the unique combination of unpaid orders that adds up
        matches = [[c["id"] for c in combo]
                   for n in range(1, len(cands) + 1) for combo in itertools.combinations(cands, n)
                   if abs(sum(c["owes"] for c in combo) - amount) <= TOL]
        warnings = []
        if len(matches) == 1:
            selected = sorted(matches[0])
        else:
            selected = []
            if len(matches) > 1:
                warnings.append("More than one combination of unpaid orders adds up to this amount. Tick the ones it covers.")
            elif cands:
                warnings.append("This amount doesn't match any unpaid order or combination of them. Tick the orders it "
                                "covers (it counts as a part-payment), or confirm to save it unlinked.")
            else:
                warnings.append("No unpaid order found; this will be saved unlinked.")
        paid_at = to_dt(parsed.get("paid_at"), tz, msg_dt).isoformat()
        if find_duplicate_payment(store, amount, paid_at, tz):
            warnings.insert(0, f"Possible duplicate: you already logged a payment of {amount:.2f} on "
                               f"{fmt_date(paid_at, tz)}. Confirm to log it again, or Cancel.")
        return refresh_payment_plan(store, {
            "kind": "payment", "amount": amount, "paid_at": paid_at, "reference": parsed.get("reference"),
            "candidates": cands, "selected": selected, "warnings": warnings})

    if kind == "arrival_update":
        qty_by_tag: dict[str, str] = {}
        tags: list[str] = []
        for a in parsed.get("arrivals") or []:
            t = (a.get("tag") or "").lstrip("#").lower()
            if t:
                tags.append(t)
                if a.get("quantity"):
                    qty_by_tag[t] = a["quantity"]
        tags += [t.lstrip("#").lower() for t in parsed.get("tags") or [] if t]
        if not tags:
            tags = fallback_tags(ctx.get("raw_text"))
        tags = list(dict.fromkeys(tags))
        names = [n for n in parsed.get("names") or [] if n]
        relinked = relink_orders(store)
        pending = store.items_pending()
        chosen, mine, quantities, warnings = [], [], {}, []
        for tag in tags:
            post = store.find_post_by_tag(tag)
            canon = post["tag"] if post else tag
            group = [r for r in pending if r["tag"] == canon]
            if not group or canon in mine:
                continue  # another buyer's batch, or already marked arrived: ignore silently
            if names:
                named = [r for r in group if name_match(names, r["name"])]
                if named:
                    group = named
                else:
                    warnings.append(f"Couldn't match {', '.join(names)} under #{canon}; including all its pending items.")
            chosen += group
            mine.append(canon)
            if tag in qty_by_tag:
                quantities[canon] = qty_by_tag[tag]
        ignored = len(tags) - len(mine)
        if mine and ignored > 0:
            warnings.append(f"Ignored {ignored} other hashtag(s) that aren't in your pending orders.")
        if not tags and names:
            chosen = [r for r in pending if name_match(names, r["name"])]
            warnings.append("No #serial in the message, so I matched by item names only.")
        if not chosen:
            canon = {(store.find_post_by_tag(t) or {"tag": t})["tag"] for t in tags}
            done = [r for r in store.items_arrived() if r["tag"] in canon]
            if done:
                return {"kind": "unreadable", "reason": "Already recorded: your items under these hashtags were marked "
                        f"arrived on {fmt_date(max(r['arrived_at'] for r in done), tz)}. "
                        "You may have forwarded this before. Nothing to do."}
            known = [store.find_post_by_tag(t)["tag"] for t in tags if store.find_post_by_tag(t)]
            detail = [f"Hashtags I read: {', '.join('#' + t for t in tags) or '(none)'}."]
            detail.append(("Saved posts among them: " + ", ".join(f"#{t}" for t in known)
                           + ", but you have no pending items under them.") if known
                          else "None of them are posts you've saved.")
            unlinked = [r for r in pending if not r["tag"]]
            if unlinked:
                detail.append(f"{len(unlinked)} of your pending item(s) aren't linked to any post "
                              f"(e.g. {unlinked[0]['order_title'][:45]}). If one is yours, forward its channel post first.")
            return {"kind": "unreadable", "reason": "Arrival notice read, but none of its hashtags match your pending items.\n"
                    + "\n".join(detail) + ("".join(f"\n⚠️ {w}" for w in warnings))}
        note = " ".join(x for x in [parsed.get("note"),
                                    f"(Linked {relinked} earlier order(s) to their posts.)" if relinked else None] if x) or None
        return {"kind": "arrival", "tags": mine, "names": names, "note": note,
                "quantities": quantities, "item_ids": [r["id"] for r in chosen],
                "items": [f"{r['name']} ×{r['qty']}" + (f" (#{r['tag']})" if r["tag"] else "") for r in chosen],
                "arrived_at": msg_dt.isoformat(), "warnings": warnings}

    return {"kind": "unreadable", "reason": parsed.get("summary") or "I didn't recognise this message."}


def render_draft(d: dict, cur: str, tz: ZoneInfo) -> str:
    k = d["kind"]
    warn = "".join(f"\n⚠️ {w}" for w in d.get("warnings", []))
    if k == "received":
        sel = set(d["selected"])
        groups: dict[str, list] = {}
        for it in d["items"]:
            groups.setdefault(it["head"], []).append(it)
        lines = ["📬 These have reached the seller. Tap the ones that have reached YOU, then Confirm."]
        for head, its in groups.items():
            lines.append(f"\n{head}")
            lines += [f"{'☑️' if it['id'] in sel else '☐'} {it['name']} ×{it['qty']} "
                      f"(arrived {fmt_date(it['arrived_at'], tz)})" for it in its]
        return "\n".join(lines)
    if k == "topup":
        tag = f" (#{d['tag']})" if d.get("tag") else ""
        return (f"➕ Second payment {money(d['amount'], cur)} for Order {d['order_id']}{tag}: {d['title'][:50]}\n"
                f"Added to that item's cost: {money(d['old_total'], cur)} → {money(d['old_total'] + d['amount'], cur)}. "
                f"Counted as paid; no new items or statuses.{warn}")
    if k == "merge":
        return (f"🧩 Merge Order {d['from_id']} into Order {d['into_id']}?\n"
                f"Order {d['from_id']} ({money(d['amount'], cur)}, {d['n_items']} item(s)) becomes a second payment on "
                f"Order {d['into_id']} ({d['into_title'][:45]}). Its items are removed; the amount is added to the cost. "
                "/undo reverses it.")
    if k == "settle":
        diff = d["total_due"] - d["paid"]
        what = (f"you still owe {money(diff, cur)}" if diff > 0 else f"you overpaid by {money(-diff, cur)}")
        return (f"🤝 Settle Order {d['order_id']}: {d['title']}\n"
                f"Listed total {money(d['total_due'], cur)}, you paid {money(d['paid'], cur)} ({what}).\n"
                f"Confirm to treat {money(d['paid'], cur)} as the final total, so the order counts as fully paid. "
                "/undo reverses it.")
    if k == "channel_post":
        extra = f"\n{d['price_note']}" if d.get("price_note") else ""
        return f"📌 Channel post #{d['tag']}\n{d['title']}{extra}{warn}"
    if k == "order":
        lines = "\n".join(f"• {i['name']} ×{i['qty']} — {money(i['line_total'], cur)}" for i in d["items"])
        link = (f"🔗 Linked to #{d['post_tag']}" if d.get("post_id")
                else "🔗 Not linked to a channel post (tap Change link to pick one)")
        fee = f"\nMailing fee: {money(d['mailing_fee'], cur)}" if d.get("mailing_fee") else ""
        note = f"\nNote: {d['note']}" if d.get("note") else ""
        paid = (f"\n💸 Counted as paid in full ({money(d['total_due'], cur)}). No screenshot needed unless "
                "what you actually paid differs." if d.get("total_due", 0) > 0 else "")
        return (f"🛒 Order: {d['title']}\n{lines}{fee}\nTotal: {money(d['total_due'], cur)}{note}\n"
                f"{link}\nOrdered: {fmt_date(d['ordered_at'], tz)}{paid}{warn}")
    if k == "payment":
        ref = f"\nRef: {d['reference']}" if d.get("reference") else ""
        plan = d.get("plan", [])
        if plan:
            link = "Covers:\n" + "\n".join(
                f"• {p['label']}: {money(p['amount'], cur)}"
                + (f" of {money(p['owes'], cur)} (part-payment)" if p["amount"] < p["owes"] - TOL else "")
                for p in plan)
            if d.get("remainder", 0) > TOL:
                link += f"\n⚠️ {money(d['remainder'], cur)} isn't allocated to any order."
        else:
            link = "🔗 Not linked to any order"
        return f"💸 Payment {money(d['amount'], cur)} on {fmt_date(d['paid_at'], tz)}{ref}\n{link}{warn}"
    if k == "arrival":
        lines = "\n".join(f"• {i}" for i in d["items"])
        note = f"\nNote: {d['note']}" if d.get("note") else ""
        qty = d.get("quantities") or {}
        qlines = ("\nSeller's count for the whole listing (all buyers):\n" + "\n".join(f"• #{t}: {q}" for t, q in qty.items())) if qty else ""
        return f"📦 Arrived ({len(d['items'])} item(s)):\n{lines}{qlines}{note}{warn}"
    return f"❓ {d.get('reason', 'Not understood.')}"


def draft_buttons(draft_id: int, d: dict) -> list[list[tuple[str, str]]]:
    if d["kind"] == "received":
        sel = set(d["selected"])
        marks = [(f"{'☑️' if it['id'] in sel else '☐'} {it['name']}" + (f" (#{it['tag']})" if it["tag"] else ""),
                  f"r:{draft_id}:{it['id']}") for it in d["items"]]
        all_sel = len(sel) == len(d["items"])
        rows = [marks[i:i + 2] for i in range(0, len(marks), 2)]
        rows.append([("☐ Clear all" if all_sel else "☑️ Select all", f"ra:{draft_id}")])
        rows.append([(f"✅ Confirm ({len(sel)})", f"rc:{draft_id}"), ("✖️ Cancel", f"x:{draft_id}")])
        return rows
    if d["kind"] == "payment":  # tick the orders this payment covers
        sel = set(d["selected"])
        marks = [(f"{'☑️' if c['id'] in sel else '☐'} Order {c['id']} · {c['owes']:.2f}", f"o:{draft_id}:{c['id']}")
                 for c in d["candidates"]]
        rows = [marks[i:i + 2] for i in range(0, len(marks), 2)]
        rows.append([("✅ Confirm", f"c:{draft_id}"), ("✖️ Cancel", f"x:{draft_id}")])
        return rows
    rows = [[("✅ Confirm", f"c:{draft_id}"), ("✖️ Cancel", f"x:{draft_id}")]]
    if d["kind"] == "order" and d.get("candidates"):
        rows.append([("🔗 Change link", f"m:{draft_id}")])
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
        linked = relink_orders(store)
        if linked:
            text += f"\nLinked {linked} earlier order(s) to it."
    elif k == "order":
        oid, item_ids = store.add_order(d.get("post_id"), d["title"], d["total_due"], d.get("mailing_fee"),
                                        d.get("note"), d["ordered_at"], d.get("raw_text"), d["items"])
        result = {"type": "order", "order_id": oid, "item_ids": item_ids}
        text = f"✅ Order {oid} saved: {len(item_ids)} item(s), {money(d['total_due'], cur)}."
        if d["total_due"] > 0:  # you pay instantly, so a payment request counts as paid
            pid = store.add_payment(d["total_due"], d["ordered_at"], "assumed from order", assumed=True)
            store.add_allocation(pid, oid, d["total_due"])
            result["payment_id"] = pid
            text += "\n💸 Counted as paid in full. If you paid a different amount, send the payment screenshot and I'll correct it."
    elif k == "payment":
        plan, remainder = plan_allocation(store, d["amount"], d["selected"])
        replaced = [{"amount": r["amount"], "paid_at": r["paid_at"], "reference": r["reference"], "order_id": r["order_id"]}
                    for r in store.assumed_payments_for([oid for oid, _a, _o in plan])]
        for r in store.assumed_payments_for([oid for oid, _a, _o in plan]):   # the real payment replaces the assumed one
            store.run("DELETE FROM payments WHERE id=?", (r["id"],))
        pid = store.add_payment(d["amount"], d["paid_at"], d.get("reference"))
        for oid, amt, _owes in plan:
            store.add_allocation(pid, oid, amt)
        result = {"type": "payment", "payment_id": pid, "replaced": replaced}
        text = f"✅ Logged {money(d['amount'], cur)}."
        if plan:
            bal = {o["id"]: o for o in store.orders_with_balance()}
            text += "".join(
                f"\n• Order {oid}: {money(amt, cur)} paid, balance {money(max(bal[oid]['total_due'] - bal[oid]['paid'], 0), cur)}"
                for oid, amt, _owes in plan)
            if remainder > TOL:
                text += f"\n⚠️ {money(remainder, cur)} wasn't allocated to any order."
        else:
            text += " (not linked to an order)"
    elif k == "topup":
        store.run("UPDATE orders SET total_due=total_due+?, topup=topup+? WHERE id=?",
                  (d["amount"], d["amount"], d["order_id"]))
        pid = store.add_payment(d["amount"], d["paid_at"], "second payment", assumed=True)
        store.add_allocation(pid, d["order_id"], d["amount"])
        result = {"type": "topup", "order_id": d["order_id"], "amount": d["amount"], "payment_id": pid}
        text = f"✅ Added {money(d['amount'], cur)} to Order {d['order_id']}'s cost, counted as paid."
    elif k == "merge":
        src = store.one("SELECT * FROM orders WHERE id=?", (d["from_id"],))
        dst = store.one("SELECT id FROM orders WHERE id=?", (d["into_id"],))
        if not src or not dst:
            raise ValueError("One of those orders no longer exists.")
        snap = {"order": dict(src), "items": [dict(r) for r in store.all("SELECT * FROM items WHERE order_id=?", (src["id"],))],
                "alloc_ids": [r["id"] for r in store.all("SELECT id FROM allocations WHERE order_id=?", (src["id"],))]}
        store.run("UPDATE allocations SET order_id=? WHERE order_id=?", (dst["id"], src["id"]))
        store.run("UPDATE orders SET total_due=total_due+?, topup=topup+? WHERE id=?",
                  (src["total_due"], src["total_due"], dst["id"]))
        store.run("DELETE FROM orders WHERE id=?", (src["id"],))
        result = {"type": "merge", "into_id": dst["id"], "snapshot": snap}
        text = f"✅ Merged Order {src['id']} into Order {dst['id']} ({money(src['total_due'], cur)} added to its cost)."
    elif k == "settle":
        o = store.one("SELECT total_due FROM orders WHERE id=?", (d["order_id"],))
        if not o:
            raise ValueError("That order no longer exists.")
        paid = store.one("SELECT COALESCE(SUM(amount),0) s FROM allocations WHERE order_id=?", (d["order_id"],))["s"]
        store.run("UPDATE orders SET settled_from=?, total_due=? WHERE id=?", (o["total_due"], paid, d["order_id"]))
        result = {"type": "settle", "order_id": d["order_id"], "old_total": o["total_due"]}
        text = f"✅ Order {d['order_id']} settled at {money(paid, cur)}."
    elif k == "arrival":
        fresh = [r["id"] for r in store.all(
            f"SELECT id FROM items WHERE arrived_at IS NULL AND id IN ({','.join('?' * len(d['item_ids']))})",
            d["item_ids"])]
        store.mark_arrived(fresh, d["arrived_at"])
        result = {"type": "arrival", "item_ids": fresh}
        left = len(store.items_pending())
        text = (f"✅ Marked {len(fresh)} item(s) as arrived. {left} item(s) still pending. "
                "When they reach you, use /received.")
    elif k == "received":
        ids = d["selected"]
        fresh = [r["id"] for r in store.all(
            f"SELECT id FROM items WHERE received_at IS NULL AND id IN ({','.join('?' * len(ids))})", ids)] if ids else []
        store.mark_received(fresh, now_iso())
        result = {"type": "received", "item_ids": fresh}
        left = store.one("SELECT COUNT(*) c FROM items WHERE arrived_at IS NOT NULL AND received_at IS NULL")["c"]
        text = f"✅ Marked {len(fresh)} item(s) as received. {left} item(s) still waiting to reach you."
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
        if r.get("payment_id"):
            store.run("DELETE FROM payments WHERE id=?", (r["payment_id"],))
        store.run("DELETE FROM orders WHERE id=?", (r["order_id"],))
        msg = f"Removed order {r['order_id']} and its items (real payments linked to it are kept, unlinked)."
    elif r["type"] == "payment":
        store.run("DELETE FROM payments WHERE id=?", (r["payment_id"],))
        for a in r.get("replaced", []):  # bring back the assumed payments this one had replaced
            if store.one("SELECT 1 FROM orders WHERE id=?", (a["order_id"],)):
                pid = store.add_payment(a["amount"], a["paid_at"], a["reference"], assumed=True)
                store.add_allocation(pid, a["order_id"], a["amount"])
        msg = "Removed the last payment."
    elif r["type"] == "topup":
        store.run("DELETE FROM payments WHERE id=?", (r["payment_id"],))
        store.run("UPDATE orders SET total_due=total_due-?, topup=topup-? WHERE id=?",
                  (r["amount"], r["amount"], r["order_id"]))
        msg = f"Removed the second payment from order {r['order_id']}."
    elif r["type"] == "merge":
        snap = r["snapshot"]
        o = snap["order"]
        cols = ",".join(o)
        store.run(f"INSERT INTO orders({cols}) VALUES({','.join('?' * len(o))})", list(o.values()))
        for it in snap["items"]:
            store.run(f"INSERT INTO items({','.join(it)}) VALUES({','.join('?' * len(it))})", list(it.values()))
        for aid in snap["alloc_ids"]:
            store.run("UPDATE allocations SET order_id=? WHERE id=?", (o["id"], aid))
        store.run("UPDATE orders SET total_due=total_due-?, topup=topup-? WHERE id=?",
                  (o["total_due"], o["total_due"], r["into_id"]))
        msg = f"Un-merged: order {o['id']} is back."
    elif r["type"] == "settle":
        store.run("UPDATE orders SET total_due=?, settled_from=NULL WHERE id=?", (r["old_total"], r["order_id"]))
        msg = f"Reopened order {r['order_id']} (total back to {r['old_total']:.2f})."
    elif r["type"] == "received":
        if r["item_ids"]:
            store.run(f"UPDATE items SET received_at=NULL WHERE id IN ({','.join('?' * len(r['item_ids']))})",
                      r["item_ids"])
        msg = f"Marked {len(r['item_ids'])} item(s) as not received again."
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


BATCHES_PER_MESSAGE = 12


def overview(store: Store, cfg: Config, now: datetime, mode: str, keyword: str | None = None) -> list[dict]:
    """mode 'expecting' = everything you're still awaiting (not at the seller yet, or at the seller but not with you),
    'delivered' = at the seller, not yet with you. One message per group of batches; each batch has a tappable
    #serial button (callback t:<post_id>) that sends that post's photos."""
    def ok(r):
        return keyword_ok(keyword, r["name"], r["order_title"], r["post_title"], r["tag"])
    pending = [r for r in store.items_pending() if ok(r)] if mode == "expecting" else []
    at_seller = [r for r in store.items_arrived(unreceived_only=True) if ok(r)]
    if not pending and not at_seller:
        return []
    batches: dict[tuple, dict] = {}
    for stage, rows in (("pending", pending), ("arrived", at_seller)):
        for r in rows:
            b = batches.setdefault((stage, r["tag"] or f"order-{r['order_id']}"), {
                "stage": stage, "tag": r["tag"], "post_id": r["post_id"], "names": [], "age": 0, "arrived": "",
                "head": f"#{r['tag']} {r['post_title']}" if r["tag"] else f"{r['order_title']} (no post linked)"})
            b["names"].append(r["name"] if r["qty"] == 1 else f"{r['name']} ×{r['qty']}")
            if stage == "pending":
                b["age"] = max(b["age"], (now - clock_start(r, cfg.tz)).days)
            else:
                b["arrived"] = max(b["arrived"], r["arrived_at"])
    waiting = sorted((b for b in batches.values() if b["stage"] == "pending"), key=lambda b: -b["age"])
    arrived = sorted((b for b in batches.values() if b["stage"] == "arrived"), key=lambda b: b["arrived"], reverse=True)
    ordered = waiting + arrived
    got = store.one("SELECT COUNT(*) c FROM items WHERE received_at IS NOT NULL")["c"]
    if mode == "expecting":
        header = (f"📦 Awaiting {len(pending) + len(at_seller)} item(s) not yet with you:\n"
                  f"⏳ {len(pending)} not at the seller yet · 📬 {len(at_seller)} at the seller, on their way to you"
                  + ("\nUse /received when they reach you." if at_seller else ""))
        late = sum(1 for b in waiting if b["age"] >= cfg.late_days)
        if late:
            header += f"\n⚠️ {late} batch(es) past {cfg.late_days // 7} weeks. Maybe ask the seller?"
    else:
        header = (f"📬 Reached the seller, not yet received by you: {len(at_seller)} item(s) in {len(arrived)} batch(es)\n"
                  "Use /received when they reach you." + (f"\n✔️ {got} item(s) already received." if got else ""))
    msgs = []
    for i in range(0, len(ordered), BATCHES_PER_MESSAGE):
        chunk = ordered[i:i + BATCHES_PER_MESSAGE]
        lines = [header if i == 0 else "(continued)"]
        for b in chunk:
            if b["stage"] == "pending":
                lines.append(f"{'⚠️' if b['age'] >= cfg.late_days else '⏳'} {b['head']}: {', '.join(b['names'])} ({b['age']}d)")
            else:
                lines.append(f"{'📬' if mode == 'expecting' else '✅'} {b['head']}: {', '.join(b['names'])} "
                             f"(at seller since {fmt_date(b['arrived'], cfg.tz)})")
        buttons = [(f"#{b['tag']}", f"t:{b['post_id']}") for b in chunk if b["post_id"]]
        if buttons:
            lines.append("\nTap a # below to see the photos.")
        msgs.append({"text": "\n".join(lines),
                     "buttons": [buttons[j:j + 3] for j in range(0, len(buttons), 3)] or None})
    return msgs


ALL_PER_MESSAGE = 12


def all_orders(store: Store, cfg: Config, now: datetime, keyword: str | None = None) -> list[dict]:
    """Every order at a glance: payment state and where each item is (ordered / at seller / with you)."""
    orders = [o for o in store.orders_with_balance() if keyword_ok(keyword, o["title"])]
    if not orders:
        return []
    items: dict[int, list] = {}
    for r in store.all("SELECT i.*, p.tag, (SELECT MIN(pay.paid_at) FROM payments pay JOIN allocations a "
                       "ON a.payment_id=pay.id WHERE a.order_id=i.order_id) AS first_paid, o.ordered_at "
                       "FROM items i JOIN orders o ON o.id=i.order_id LEFT JOIN posts p ON p.id=i.post_id"):
        items.setdefault(r["order_id"], []).append(r)
    orders.sort(key=lambda o: o["ordered_at"])
    blocks = []
    for o in orders:
        its = items.get(o["id"], [])
        pend = [r for r in its if not r["arrived_at"]]
        at = [r for r in its if r["arrived_at"] and not r["received_at"]]
        got = [r for r in its if r["received_at"]]
        diff = o["total_due"] - o["paid"]
        if abs(diff) <= TOL:
            pay = "✅ paid" + (f" (settled, was {money(o['settled_from'], cfg.currency)})" if o["settled_from"] is not None else "")
        elif diff > 0:
            pay = f"💸 owing {money(diff, cfg.currency)}" if o["paid"] > TOL else "💸 unpaid"
        else:
            pay = f"↩️ overpaid {money(-diff, cfg.currency)}"
        late = any((now - clock_start(r, cfg.tz)).days >= cfg.late_days for r in pend)
        parts = []
        if pend:
            parts.append(f"{'⚠️' if late else '⏳'} {len(pend)} ordered")
        if at:
            parts.append(f"📬 {len(at)} at seller")
        if got:
            parts.append(f"✔️ {len(got)} received")
        tag = next((f"#{r['tag']} " for r in its if r["tag"]), "")
        total = money(o["total_due"], cfg.currency) + (
            f" (incl. {money(o['topup'], cfg.currency)} 2nd payment)" if o["topup"] else "")
        blocks.append(f"Order {o['id']} · {tag}{o['title'][:48]}\n"
                      f"   {total} · {pay} · {' · '.join(parts) or 'no items'}")
    done = sum(1 for o in orders if all(r["received_at"] for r in items.get(o["id"], [])) and items.get(o["id"]))
    header = f"🗂 {len(orders)} order(s), {done} fully received. Oldest first."
    return [{"text": "\n\n".join([header if i == 0 else "(continued)"] + blocks[i:i + ALL_PER_MESSAGE])}
            for i in range(0, len(blocks), ALL_PER_MESSAGE)]


def merge_draft(store: Store, from_id: int, into_id: int) -> dict | str:
    """Fold an order that was really a second (shipping) payment into the original order."""
    if from_id == into_id:
        return "Pick two different orders."
    src = store.one("SELECT * FROM orders WHERE id=?", (from_id,))
    dst = store.one("SELECT * FROM orders WHERE id=?", (into_id,))
    if not src or not dst:
        return f"I can't find order {from_id if not src else into_id}."
    n = store.one("SELECT COUNT(*) c FROM items WHERE order_id=?", (from_id,))["c"]
    return {"kind": "merge", "from_id": from_id, "into_id": into_id, "amount": src["total_due"],
            "n_items": n, "into_title": dst["title"]}


def settle_drafts(store: Store, order_id: int | None = None) -> list[dict]:
    """One draft per order whose paid amount differs from its total (or just the one asked for)."""
    rows = [o for o in store.orders_with_balance() if abs(o["total_due"] - o["paid"]) > TOL
            and (order_id is None or o["id"] == order_id)]
    return [{"kind": "settle", "order_id": o["id"], "title": o["title"][:60],
             "total_due": o["total_due"], "paid": o["paid"]} for o in reversed(rows)]


RECEIVED_PER_MESSAGE = 30


def received_drafts(store: Store, cfg: Config, keyword: str | None = None) -> list[dict]:
    """Items that have reached the seller but that you haven't marked received, as selectable drafts."""
    rows = [r for r in store.items_arrived(unreceived_only=True)
            if keyword_ok(keyword, r["name"], r["order_title"], r["post_title"], r["tag"])]
    rows.sort(key=lambda r: (r["tag"] or f"~{r['order_id']}", r["id"]))
    drafts = []
    for i in range(0, len(rows), RECEIVED_PER_MESSAGE):
        drafts.append({"kind": "received", "selected": [], "items": [
            {"id": r["id"], "name": r["name"], "qty": r["qty"], "tag": r["tag"], "arrived_at": r["arrived_at"],
             "head": f"#{r['tag']} {r['post_title']}" if r["tag"] else f"{r['order_title']} (no post linked)"}
            for r in rows[i:i + RECEIVED_PER_MESSAGE]]})
    return drafts


def toggle_received(store: Store, draft_id: int, item_id: int | None = None) -> dict:
    """Toggle one item, or (item_id=None) select everything / clear everything."""
    d = store.draft_get(draft_id)["data"]
    ids = [i["id"] for i in d["items"]]
    sel = set(d["selected"])
    if item_id is None:
        sel = set() if sel == set(ids) else set(ids)
    elif item_id in ids:
        sel ^= {item_id}
    d["selected"] = sorted(sel)
    store.draft_update(draft_id, data=d)
    return d


def batch_caption(store: Store, cfg: Config, post_id: int, now: datetime) -> str:
    """Caption for the photos of one post: every item you ordered under it, with status."""
    post = store.get_post(post_id)
    lines = [f"#{post['tag']} {post['title']}"]
    items = store.items_for_post(post_id)
    if not items:
        lines.append("(no orders saved against this post yet)")
    for r in items:
        if r["received_at"]:
            lines.append(f"📬 {r['name']} ×{r['qty']}: received {fmt_date(r['received_at'], cfg.tz)}")
        elif r["arrived_at"]:
            lines.append(f"✅ {r['name']} ×{r['qty']}: arrived with seller {fmt_date(r['arrived_at'], cfg.tz)}")
        else:
            lines.append(f"⏳ {r['name']} ×{r['qty']}: {(now - clock_start(r, cfg.tz)).days}d waiting")
    return "\n".join(lines)[:1000]


def spent_text(store: Store, cfg: Config, month: str | None, keyword: str | None) -> str:
    """Spend = what you actually paid. A payment covering several orders is split across them by allocation."""
    cur = cfg.currency
    spend = []  # (payment_id, local datetime, amount, batch label)
    for p in store.payments_with_allocations():
        local = to_dt(p["paid_at"], cfg.tz).astimezone(cfg.tz)
        if month and local.strftime("%Y-%m") != month:
            continue
        allocated = 0.0
        for a in p["allocs"]:
            allocated += a["amount"]
            if keyword_ok(keyword, a["order_title"], a["post_title"], a["tag"], a["item_names"], p["reference"]):
                spend.append((p["id"], local, a["amount"],
                              f"#{a['tag']}" if a["tag"] else (a["order_title"] or "unlinked")[:40]))
        rest = round(p["amount"] - allocated, 2)
        if rest > TOL and keyword_ok(keyword, p["reference"]):
            spend.append((p["id"], local, rest, "not allocated to an order"))
    label = " ".join(x for x in [keyword, month] if x) or "all time"
    if not spend:
        return f"No payments recorded ({label})."
    total = sum(x[2] for x in spend)
    lines = [f"💰 Spent ({label}): {money(total, cur)} across {len({x[0] for x in spend})} payment(s)"]
    if not month:
        by_month: dict[str, float] = {}
        for _pid, local, amt, _k in spend:
            m = local.strftime("%Y-%m")
            by_month[m] = by_month.get(m, 0) + amt
        lines.append("\nBy month:")
        lines += [f"• {m}: {money(v, cur)}" for m, v in sorted(by_month.items())]
    by_batch: dict[str, float] = {}
    for _pid, _local, amt, key in spend:
        by_batch[key] = by_batch.get(key, 0) + amt
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
    if intent == "all":
        return all_orders(store, cfg, now, kw) or [{"text": "No orders saved yet."}]
    if intent in ("status", "delivered"):
        mode = "expecting" if intent == "status" else "delivered"
        return overview(store, cfg, now, mode, kw) or [{"text": "🎉 Nothing pending." if intent == "status"
                                                         else "Nothing is waiting to reach you."}]
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
    return [{"text": "To mark items as arrived, forward me the seller's arrival update and tap Confirm. "
                     "When they reach you, use /received.\n\n"
                     "Other commands: /status, /all, /delivered, /settle, /merge, /pending, /late, /spent, /owing, /undo, /healthcheck. "
                     "Or ask about spending, pending or late items in plain words."}]


def cap_messages(msgs: list[dict]) -> list[dict]:
    if len(msgs) <= MAX_GROUPS:
        return msgs
    return msgs[:MAX_GROUPS] + [{"text": f"…and {len(msgs) - MAX_GROUPS} more batches. Narrow it down, e.g. /pending_photos ateez."}]


def health_text(store: Store, cfg: Config) -> str:
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
        dup = store.find_duplicate_forward(row["fingerprint"], row["id"]) if row["fingerprint"] else None
        if dup:  # an exact re-forward: say so once, instead of also repeating the content-based warning
            draft["warnings"] = [w for w in draft.get("warnings", []) if not w.startswith("Possible duplicate")]
            draft["warnings"].insert(0, dup_message(dup, cfg.tz))
        draft_id = store.draft_add(draft["kind"], draft, row["id"])
        return {"type": "draft", "draft_id": draft_id, "draft": draft}
    except AIUnavailable as exc:
        return {"type": "ai_down", "kind": exc.kind, "detail": exc.detail}  # stays queued
    except Exception as exc:  # noqa: BLE001 - never lose a message
        store.inbox_set(row["id"], "failed", str(exc)[:300])
        return {"type": "failed", "error": str(exc)[:300]}
