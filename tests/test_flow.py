"""End-to-end tests of the pipeline using a fake Claude parser (no network, no tokens needed).

The sample data mirrors the real messages the seller sends.
"""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
import httpx
import pytest

from ateez_bot import pipeline as pl
from ateez_bot.ai import AIUnavailable, classify_error, fallback_tag, fallback_tags
from ateez_bot.bot import BotApp
from ateez_bot.config import Config
from ateez_bot.store import Store

TZ = ZoneInfo("Asia/Singapore")


def make_cfg(**kw):
    base = dict(telegram_token="123:abc", anthropic_api_key="x", allowed_user_id=1, model="m",
                db_path=":memory:", tz=TZ, currency="S$", late_days=28, digest_day=0, digest_hour=10,
                healthcheck_url=None)
    base.update(kw)
    return Config(**base)


class FakeParser:
    def __init__(self, *results):
        self.results = list(results)

    async def classify(self, **kw):
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


async def fake_image(_file_id):
    return b"img"


def ingest(store, parser, cfg, *, text=None, photo=None, channel=False, when=None, group=None):
    when = when or datetime.now(TZ)
    iid = store.inbox_add(1, 1, text, photo, group, channel, when.isoformat())
    return asyncio.run(pl.run_inbox_item(store, parser, cfg, store.inbox_get(iid), fake_image))


def confirm(store, out, cfg):
    assert out["type"] == "draft", out
    return pl.apply_draft(store, out["draft_id"], cfg.currency, cfg.tz)


POST_P5 = {"kind": "channel_post", "summary": "post", "tag": "anghp5_2",
           "title": "ateez golden hour pt.5 makestar lucky draw", "price_note": "$8.50 individual"}
ORDER_P5 = {"kind": "order_confirmation", "summary": "order",
            "title": "ateez golden hour pt.5 makestar lucky draw",
            "items": [{"name": "OT8 INDIVIDUAL", "qty": 8, "line_total": 68.0},
                      {"name": "OT4 UNIT", "qty": 4, "line_total": 46.0}],
            "total_due": 114.0, "note": "first payment, pending second payment",
            "matched_tag": "anghp5_2", "match_confidence": "high"}
ORDER_DEJAVU = {"kind": "order_confirmation", "summary": "order",
                "title": "INSTOCKS - ATEEZ ANITEEZ IN DEJAVU OFFLINE HTB EXTRA 50K BENEFIT PCS",
                "items": [{"name": "MINGI", "qty": 1, "line_total": 7.5},
                          {"name": "HONGJOONG", "qty": 1, "line_total": 8.0}],
                "total_due": 15.5, "matched_tag": None, "match_confidence": None}


def setup_orders(store, cfg, when):
    ingest(store, FakeParser(POST_P5), cfg, text="#anghp5_2 ...", photo="PHOTO1", channel=True)
    out = ingest(store, FakeParser(POST_P5), cfg, text="#anghp5_2 ...", photo="PHOTO1", channel=True)
    confirm(store, out, cfg)
    for parsed in (ORDER_P5, ORDER_DEJAVU):
        confirm(store, ingest(store, FakeParser(parsed), cfg, text="order", when=when), cfg)


@pytest.fixture
def env():
    return make_cfg(), Store(":memory:")


def test_full_flow_screenshot_examples(env):
    cfg, store = env
    now = datetime.now(TZ)
    # channel post is saved with its photo
    out = ingest(store, FakeParser(POST_P5), cfg, text="[po] #anghp5_2 ...", photo="PHOTO1", channel=True)
    assert out["draft"]["tag"] == "anghp5_2"
    confirm(store, out, cfg)
    assert store.get_post_by_tag("anghp5_2")["image_file_ids"] == '["PHOTO1"]'

    # lucky-draw order: auto-linked to the post by title, totals reconcile (8x8.50 + 4x11.50 = 114)
    out = ingest(store, FakeParser(ORDER_P5), cfg, text="hi! Thank you...", when=now)
    d = out["draft"]
    assert d["post_tag"] == "anghp5_2" and d["warnings"] == []
    confirm(store, out, cfg)

    # in-stock order has no matching post: saved unlinked, but still works
    out = ingest(store, FakeParser(ORDER_DEJAVU), cfg, text="hi! Thank you...", when=now)
    assert out["draft"]["post_id"] is None and out["draft"]["warnings"] == []
    confirm(store, out, cfg)

    # payment screenshots link to the order with the exactly matching balance
    pay = {"kind": "payment_proof", "summary": "paynow", "amount": 114.0, "paid_at": None, "reference": "lisa"}
    out = ingest(store, FakeParser(pay), cfg, photo="SHOT1")
    assert out["draft"]["selected"] == [1]
    confirm(store, out, cfg)
    pay2 = dict(pay, amount=15.5)
    out = ingest(store, FakeParser(pay2), cfg, photo="SHOT2")
    assert out["draft"]["selected"] == [2]
    confirm(store, out, cfg)

    assert "S$129.50" in pl.spent_text(store, cfg, None, None)
    assert "S$114.00" in pl.spent_text(store, cfg, None, "golden hour")
    assert "Nothing owing" in pl.owing_text(store, cfg)

    # arrival notice by serial marks just that batch
    arr = {"kind": "arrival_update", "summary": "arrived", "tags": ["anghp5_2"], "names": []}
    out = ingest(store, FakeParser(arr), cfg, text="#anghp5_2 arrived")
    assert len(out["draft"]["item_ids"]) == 2
    confirm(store, out, cfg)
    pending = pl.pending_messages(store, cfg, now)
    assert len(pending) == 1 and "MINGI" in pending[0]["text"]

    # /undo puts the items back to pending
    assert "not arrived" in pl.undo_last(store)
    assert len(store.items_pending()) == 4


def test_arrival_by_member_name_within_serial(env):
    cfg, store = env
    setup_orders(store, cfg, datetime.now(TZ))
    parsed = {"kind": "arrival_update", "summary": "x", "tags": [], "names": ["mingi"]}
    out = ingest(store, FakeParser(parsed), cfg, text="mingi arrived")
    assert [i.split(" ")[0] for i in out["draft"]["items"]] == ["MINGI"]
    assert any("matched by item names" in w for w in out["draft"]["warnings"])


def test_total_mismatch_is_flagged(env):
    cfg, store = env
    bad = dict(ORDER_DEJAVU, total_due=20.0)
    out = ingest(store, FakeParser(bad), cfg, text="order")
    assert out["draft"]["warnings"] and "add up" in out["draft"]["warnings"][0]


def test_late_flag_uses_payment_date_and_weekly_digest(env):
    cfg, store = env
    old = datetime.now(TZ) - timedelta(days=30)
    setup_orders(store, cfg, old)
    now = datetime.now(TZ)
    # unpaid: clock starts at order date, so 30 days is already late
    assert len(pl.late_rows(store, cfg, now)) == 4
    # payment made yesterday restarts the clock for that order
    pay = {"kind": "payment_proof", "summary": "x", "amount": 114.0,
           "paid_at": (now - timedelta(days=1)).isoformat(), "reference": None}
    confirm(store, ingest(store, FakeParser(pay), cfg, photo="S"), cfg)
    assert {r["name"] for r in pl.late_rows(store, cfg, now)} == {"MINGI", "HONGJOONG"}

    # weekly digest: one text-only message listing everything incoming, late batch flagged and listed first
    chunks = pl.weekly_digest(store, cfg, now)
    assert len(chunks) == 1
    text = chunks[0]
    assert "4 item(s) in 2 batch(es)" in text and "1 batch(es) past 4 weeks" in text
    lines = text.split("\n")
    assert lines[-2].startswith("⚠️") and "MINGI" in lines[-2]   # oldest (late, unpaid) first
    assert lines[-1].startswith("• #anghp5_2")                    # recently paid batch not flagged

    # nothing incoming -> no digest at all
    store.mark_arrived([r["id"] for r in store.items_pending()], now.isoformat())
    assert pl.weekly_digest(store, cfg, now) == []


def pay_msg(amount, paid_at=None):
    return {"kind": "payment_proof", "summary": "paynow", "amount": amount, "paid_at": paid_at, "reference": None}


def balances(store):
    return {o["id"]: round(o["total_due"] - o["paid"], 2) for o in store.orders_with_balance()}


def test_one_payment_covering_several_orders(env):
    cfg, store = env
    setup_orders(store, cfg, datetime.now(TZ))                      # order 1 owes 114.00, order 2 owes 15.50
    out = ingest(store, FakeParser(pay_msg(129.5)), cfg, photo="S")
    d = out["draft"]
    assert d["selected"] == [1, 2] and d["remainder"] == 0           # the only combination that adds up
    text = pl.render_draft(d, "S$", TZ)
    assert "Covers:" in text and "Order 1" in text and "Order 2" in text and "⚠️" not in text
    msg = confirm(store, out, cfg)
    assert "Order 1: S$114.00 paid, balance S$0.00" in msg and "Order 2: S$15.50 paid, balance S$0.00" in msg
    assert balances(store) == {1: 0.0, 2: 0.0}
    assert "Nothing owing" in pl.owing_text(store, cfg)
    spent = pl.spent_text(store, cfg, None, None)
    assert "S$129.50 across 1 payment(s)" in spent and "#anghp5_2: S$114.00" in spent
    assert "S$15.50" in pl.spent_text(store, cfg, None, "mingi")      # the share that went to the Mingi/Hongjoong order
    # undoing the payment reopens both orders
    assert "Removed the last payment" in pl.undo_last(store)
    assert balances(store) == {1: 114.0, 2: 15.5}


def test_payment_picks_unique_combination_among_many_orders(env):
    cfg, store = env
    setup_orders(store, cfg, datetime.now(TZ))
    store.add_order(None, "third order", 21.5, None, None, datetime.now(TZ).isoformat(), "x",
                    [{"name": "SAN", "qty": 1, "line_total": 21.5}])
    d = ingest(store, FakeParser(pay_msg(135.5)), cfg, photo="S")["draft"]   # 114 + 21.5
    assert d["selected"] == [1, 3]
    assert ingest(store, FakeParser(pay_msg(37.0)), cfg, photo="S")["draft"]["selected"] == [2, 3]  # 15.5 + 21.5


def test_ambiguous_or_unmatched_payment_asks_you_to_tick(env):
    cfg, store = env
    setup_orders(store, cfg, datetime.now(TZ))
    store.add_order(None, "twin order", 15.5, None, None, datetime.now(TZ).isoformat(), "x",
                    [{"name": "SAN", "qty": 1, "line_total": 15.5}])         # same balance as order 2
    d = ingest(store, FakeParser(pay_msg(15.5)), cfg, photo="S")["draft"]
    assert d["selected"] == [] and "More than one combination" in d["warnings"][0]
    assert len(d["candidates"]) == 3

    out = ingest(store, FakeParser(pay_msg(50.0)), cfg, photo="S")           # matches nothing: treat as part-payment
    assert out["draft"]["selected"] == [] and "doesn't match" in out["draft"]["warnings"][0]
    d = pl.toggle_payment_order(store, out["draft_id"], 1)
    assert d["selected"] == [1] and d["plan"][0]["amount"] == 50.0 and d["warnings"] == []
    assert "part-payment" in pl.render_draft(d, "S$", TZ)
    assert any("☑️ Order 1" in b[0] for row in pl.draft_buttons(out["draft_id"], d) for b in row)
    assert pl.toggle_payment_order(store, out["draft_id"], 1)["selected"] == []     # tap again to untick
    pl.toggle_payment_order(store, out["draft_id"], 1)
    confirm(store, out, cfg)
    assert balances(store)[1] == 64.0
    # the follow-up payment for the rest matches exactly
    assert ingest(store, FakeParser(pay_msg(64.0)), cfg, photo="S")["draft"]["selected"] == [1]


def test_overpayment_leaves_a_visible_remainder(env):
    cfg, store = env
    setup_orders(store, cfg, datetime.now(TZ))
    out = ingest(store, FakeParser(pay_msg(200.0)), cfg, photo="S")
    d = pl.toggle_payment_order(store, out["draft_id"], 1)
    d = pl.toggle_payment_order(store, out["draft_id"], 2)
    assert d["remainder"] == 70.5 and "S$70.50 isn't allocated" in pl.render_draft(d, "S$", TZ)
    msg = confirm(store, out, cfg)
    assert "S$70.50 wasn't allocated" in msg and balances(store) == {1: 0.0, 2: 0.0}
    assert "S$200.00" in pl.spent_text(store, cfg, None, None)               # you did pay 200


def test_old_single_order_payments_are_migrated(tmp_path):
    path = str(tmp_path / "pay.db")
    store = Store(path)
    store.add_order(None, "t", 15.5, None, None, datetime.now(TZ).isoformat(), "x",
                    [{"name": "A", "qty": 1, "line_total": 15.5}])
    store.run("INSERT INTO payments(order_id,amount,paid_at,created_at) VALUES(1, 15.5, '2026-10-01T00:00:00+08:00', 'x')")
    store.db.close()
    reopened = Store(path)                                    # what happens when the VM gets the new version
    assert balances(reopened) == {1: 0.0}
    assert reopened.one("SELECT order_id FROM payments")["order_id"] is None
    Store(path)                                               # a second restart must not double count
    assert balances(Store(path)) == {1: 0.0}


def test_credits_exhausted_queues_then_recovers(env):
    cfg, store = env
    down = AIUnavailable("credits", "no credits")
    out = ingest(store, FakeParser(down), cfg, text="#anghp5_2 ...", photo="P", channel=True)
    assert out["type"] == "ai_down" and out["kind"] == "credits"
    queued = store.inbox_queued()
    assert len(queued) == 1                                                   # nothing lost
    out = asyncio.run(pl.run_inbox_item(store, FakeParser(POST_P5), cfg, queued[0], fake_image))
    assert out["type"] == "draft" and store.inbox_queued() == []


def test_unexpected_error_marks_failed_not_lost(env):
    cfg, store = env
    out = ingest(store, FakeParser(RuntimeError("boom")), cfg, text="hello")
    assert out["type"] == "failed"
    assert store.one("SELECT status FROM inbox")["status"] == "failed"


def test_album_extra_photos_attach_to_post(env):
    cfg, store = env
    out = ingest(store, FakeParser(POST_P5), cfg, text="#anghp5_2 x", photo="P1", channel=True, group="G1")
    confirm(store, out, cfg)
    out = ingest(store, FakeParser(), cfg, photo="P2", channel=True, group="G1")  # no caption, no AI call
    assert out["type"] == "info"
    assert store.get_post_by_tag("anghp5_2")["image_file_ids"] == '["P1", "P2"]'


def test_question_routing(env):
    cfg, store = env
    setup_orders(store, cfg, datetime.now(TZ))
    q = {"kind": "question", "summary": "q", "intent": "pending", "keyword": "mingi"}
    out = ingest(store, FakeParser(q), cfg, text="what mingi stuff hasn't arrived?")
    assert out["type"] == "answers" and "MINGI" in out["messages"][0]["text"]


def test_pending_text_vs_photos(env):
    cfg, store = env
    setup_orders(store, cfg, datetime.now(TZ))      # nothing late yet: both views still list everything
    now = datetime.now(TZ)
    text = pl.answer_question(store, cfg, {"intent": "pending"}, now)
    assert len(text) == 1 and text[0].get("photo") is None
    assert "4 item(s)" in text[0]["text"] and "MINGI" in text[0]["text"] and "OT8" in text[0]["text"]
    photos = pl.answer_question(store, cfg, {"intent": "pending_photos"}, now)
    assert any(m.get("photo") == "PHOTO1" for m in photos)   # the post's picture comes with its batch
    only = pl.answer_question(store, cfg, {"intent": "pending", "keyword": "mingi"}, now)
    assert "MINGI" in only[0]["text"] and "OT8" not in only[0]["text"]


def test_classify_error_detects_credit_problem():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req)
    credits = anthropic.BadRequestError(
        "Your credit balance is too low to access the Anthropic API.", response=resp, body=None)
    assert classify_error(credits).kind == "credits"
    auth = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=req), body=None)
    assert classify_error(auth).kind == "auth"
    other = anthropic.BadRequestError("invalid schema", response=resp, body=None)
    assert classify_error(other) is None


def test_item_names_drop_seller_count_prefix(env):
    cfg, store = env
    parsed = dict(ORDER_DEJAVU, items=[{"name": "X1 OT8", "qty": 8, "line_total": 15.5}])
    out = ingest(store, FakeParser(parsed), cfg, text="order")
    assert out["draft"]["items"][0]["name"] == "OT8"
    assert pl.clean_name("x2 HONGJOONG") == "HONGJOONG" and pl.clean_name("OT4 UNIT") == "OT4 UNIT"


MASS_PROOF = {"kind": "arrival_update", "summary": "mass proof", "tags": [], "names": [], "arrivals": [
    {"tag": "anghp5_2", "quantity": "x4 unit pcs"}, {"tag": "anghp5_4", "quantity": "x2 unit pcs"},
    {"tag": "anghp5_8", "quantity": "x1 OT8"}, {"tag": "anatz38-2", "quantity": "x4 pcs"},
    {"tag": "anatz56", "quantity": "x1 OT8"}]}


def test_mass_arrival_proof_ignores_other_buyers_tags(env):
    cfg, store = env
    setup_orders(store, cfg, datetime.now(TZ))
    out = ingest(store, FakeParser(MASS_PROOF), cfg, text="[ARRIVAL PROOF] ...", photo="PROOF", channel=True)
    d = out["draft"]
    assert d["kind"] == "arrival" and len(d["item_ids"]) == 2           # only your own batch
    assert d["quantities"] == {"anghp5_2": "x4 unit pcs"}
    assert any("Ignored 4 other hashtag" in w for w in d["warnings"])
    assert not any("isn't one of your saved posts" in w for w in d["warnings"])  # no noise about others' tags
    assert "x4 unit pcs" in pl.render_draft(d, "S$", TZ)


def test_mass_arrival_proof_with_nothing_of_yours(env):
    cfg, store = env
    out = ingest(store, FakeParser(MASS_PROOF), cfg, text="[ARRIVAL PROOF] ...", photo="PROOF", channel=True)
    assert out["type"] == "info" and "none of its hashtags" in out["text"]
    assert "#anghp5_2" in out["text"] and "None of them are posts you've saved" in out["text"]


def test_hyphenated_serials_match_either_way(env):
    cfg, store = env
    store.upsert_post("anatz38-2", "some post", None, [], None, None, None)
    for variant in ("anatz38-2", "ANATZ38_2", "anatz382"):
        assert store.find_post_by_tag(variant)["tag"] == "anatz38-2"
    assert store.find_post_by_tag("anatz38") is None   # no sloppy prefix matching
    assert fallback_tags("#ANATZ38-2 (quantity: x4 pcs) #anghp5_2") == ["anatz38-2", "anghp5_2"]


def test_status_and_delivered_overviews_have_photo_buttons(env):
    cfg, store = env
    now = datetime.now(TZ)
    setup_orders(store, cfg, now)
    msgs = pl.answer_question(store, cfg, {"intent": "status"}, now)
    assert len(msgs) == 1 and "Awaiting 4 item(s)" in msgs[0]["text"] and "4 not at the seller yet" in msgs[0]["text"]
    labels = [b[0] for row in msgs[0]["buttons"] for b in row]
    assert labels == ["#anghp5_2"]                       # the unlinked batch has no # to tap
    post_id = store.get_post_by_tag("anghp5_2")["id"]
    assert msgs[0]["buttons"][0][0][1] == f"t:{post_id}"
    assert pl.answer_question(store, cfg, {"intent": "delivered"}, now)[0]["text"] == "Nothing is waiting to reach you."

    arr = {"kind": "arrival_update", "summary": "x", "tags": ["anghp5_2"], "names": []}
    confirm(store, ingest(store, FakeParser(arr), cfg, text="#anghp5_2 arrived"), cfg)
    delivered = pl.answer_question(store, cfg, {"intent": "delivered"}, now)[0]
    assert "2 item(s) in 1 batch(es)" in delivered["text"]
    assert delivered["buttons"][0][0][0] == "#anghp5_2"
    status = pl.answer_question(store, cfg, {"intent": "status"}, now)[0]
    # still awaiting: 2 not at the seller + 2 at the seller but not with me
    assert "Awaiting 4 item(s)" in status["text"] and "2 at the seller" in status["text"]
    assert "📬" in status["text"] and "at seller since" in status["text"]
    cap = pl.batch_caption(store, cfg, post_id, now)
    assert "✅ OT8 INDIVIDUAL ×8" in cap and cap.startswith("#anghp5_2")


def test_received_workflow(env):
    cfg, store = env
    now = datetime.now(TZ)
    setup_orders(store, cfg, now)
    assert pl.received_drafts(store, cfg) == []                       # nothing has reached the seller yet
    arr = {"kind": "arrival_update", "summary": "x", "tags": ["anghp5_2"], "names": []}
    assert "/received" in confirm(store, ingest(store, FakeParser(arr), cfg, text="#anghp5_2 arrived"), cfg)

    drafts = pl.received_drafts(store, cfg)
    assert len(drafts) == 1 and len(drafts[0]["items"]) == 2
    did = store.draft_add("received", drafts[0])
    assert "☐ OT8 INDIVIDUAL" in pl.render_draft(drafts[0], "S$", TZ)   # nothing pre-selected
    ids = [i["id"] for i in drafts[0]["items"]]
    d = pl.toggle_received(store, did, ids[0])
    assert d["selected"] == [ids[0]]
    assert any("Confirm (1)" in b[0] for row in pl.draft_buttons(did, d) for b in row)
    assert len(pl.toggle_received(store, did)["selected"]) == 2        # select all
    assert pl.toggle_received(store, did)["selected"] == []            # ...and clear
    pl.toggle_received(store, did, ids[0])
    msg = pl.apply_draft(store, did, "S$", TZ)
    assert "Marked 1 item(s) as received. 1 item(s) still waiting" in msg

    # /delivered now lists only the one still to reach you, and counts the received one
    text = pl.answer_question(store, cfg, {"intent": "delivered"}, now)[0]["text"]
    assert "1 item(s) in 1 batch(es)" in text and "1 item(s) already received" in text
    assert "📬" in pl.batch_caption(store, cfg, store.get_post_by_tag("anghp5_2")["id"], now)
    assert len(pl.received_drafts(store, cfg)[0]["items"]) == 1

    assert "not received" in pl.undo_last(store)
    assert len(pl.received_drafts(store, cfg)[0]["items"]) == 2


def test_old_database_gets_received_column(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, post_id INTEGER, "
                "name TEXT NOT NULL, qty INTEGER NOT NULL DEFAULT 1, line_total REAL NOT NULL DEFAULT 0, "
                "arrived_at TEXT, last_alert_at TEXT)")
    con.commit()
    con.close()
    store = Store(path)
    assert "received_at" in [r["name"] for r in store.all("PRAGMA table_info(items)")]


def send_with_fingerprint(store, cfg, parsed, text, when, fp):
    iid = store.inbox_add(1, 1, text, None, None, False, when.isoformat(), fp)
    return asyncio.run(pl.run_inbox_item(store, FakeParser(parsed), cfg, store.inbox_get(iid), fake_image))


def test_exact_reforward_is_flagged_once(env):
    cfg, store = env
    when = datetime.now(TZ)
    first = send_with_fingerprint(store, cfg, ORDER_DEJAVU, "order text", when, "fp1")
    assert first["draft"]["warnings"] == []
    confirm(store, first, cfg)
    second = send_with_fingerprint(store, cfg, ORDER_DEJAVU, "order text", when, "fp1")
    w = second["draft"]["warnings"]
    assert len(w) == 1 and "already forwarded this exact message" in w[0] and "saved it" in w[0]
    # still just a draft: the user decides
    assert second["type"] == "draft" and len(store.all("SELECT id FROM orders")) == 1


def test_same_order_sent_as_new_message_is_flagged(env):
    cfg, store = env
    now = datetime.now(TZ)
    confirm(store, ingest(store, FakeParser(ORDER_DEJAVU), cfg, text="order", when=now), cfg)
    again = ingest(store, FakeParser(ORDER_DEJAVU), cfg, text="order again", when=now + timedelta(hours=1))
    assert "this looks like order 1" in again["draft"]["warnings"][0]
    other = ingest(store, FakeParser(ORDER_P5), cfg, text="a different order", when=now)
    assert other["draft"]["warnings"] == []


def test_duplicate_payment_and_post_are_flagged(env):
    cfg, store = env
    pay = {"kind": "payment_proof", "summary": "x", "amount": 15.5, "paid_at": "2026-10-01T10:00:00+08:00"}
    confirm(store, ingest(store, FakeParser(pay), cfg, photo="S1"), cfg)
    again = ingest(store, FakeParser(dict(pay, paid_at="2026-10-01T18:30:00+08:00")), cfg, photo="S2")
    assert "already logged a payment of 15.50" in again["draft"]["warnings"][0]
    other_day = ingest(store, FakeParser(dict(pay, paid_at="2026-10-02T10:00:00+08:00")), cfg, photo="S3")
    assert other_day["draft"]["warnings"] == [] or "already logged" not in other_day["draft"]["warnings"][0]

    confirm(store, ingest(store, FakeParser(POST_P5), cfg, text="#anghp5_2 x", photo="P", channel=True), cfg)
    post_again = ingest(store, FakeParser(POST_P5), cfg, text="#anghp5_2 x", photo="P", channel=True)
    assert "#anghp5_2 is already saved" in post_again["draft"]["warnings"][0]
    assert "Possible duplicate" in pl.render_draft(post_again["draft"], "S$", TZ)


def test_arrival_already_recorded_says_so(env):
    cfg, store = env
    setup_orders(store, cfg, datetime.now(TZ))
    arr = {"kind": "arrival_update", "summary": "x", "tags": ["anghp5_2"], "names": []}
    confirm(store, ingest(store, FakeParser(arr), cfg, text="#anghp5_2 arrived"), cfg)
    again = ingest(store, FakeParser(arr), cfg, text="#anghp5_2 arrived")
    assert again["type"] == "info" and "Already recorded" in again["text"]


def test_old_inbox_gets_fingerprint_column(tmp_path):
    import sqlite3
    path = str(tmp_path / "old2.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE inbox (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, message_id INTEGER, "
                "text TEXT, photo_file_id TEXT, media_group_id TEXT, forwarded_from_channel INTEGER NOT NULL "
                "DEFAULT 0, msg_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', error TEXT, "
                "created_at TEXT NOT NULL)")
    con.commit()
    con.close()
    assert "fingerprint" in [r["name"] for r in Store(path).all("PRAGMA table_info(inbox)")]


def test_orders_saved_before_their_post_get_linked_and_case_is_ignored(env):
    cfg, store = env
    now = datetime.now(TZ)
    title = "ATEEZ GOLDEN HOUR PT.5 HELLOLIVE DIGIPACK FANSIGN 1.0 CHICK TEEZ VER."
    confirm(store, ingest(store, FakeParser(dict(ORDER_DEJAVU, title=title)), cfg, text="order", when=now), cfg)
    assert store.one("SELECT post_id FROM orders")["post_id"] is None          # post not forwarded yet
    post = {"kind": "channel_post", "summary": "p", "tag": "ANATZ56", "title": title}
    msg = confirm(store, ingest(store, FakeParser(post), cfg, text="#ANATZ56 ...", photo="P", channel=True), cfg)
    assert "Linked 1 earlier order" in msg and store.get_post_by_tag("anatz56") is not None
    arr = {"kind": "arrival_update", "summary": "x", "tags": ["ANATZ56"], "names": [],
           "arrivals": [{"tag": "ANATZ56", "quantity": "x1 OT8"}]}
    out = ingest(store, FakeParser(arr), cfg, text="#ANATZ56 (quantity: x1 OT8)")
    assert out["draft"]["kind"] == "arrival" and len(out["draft"]["item_ids"]) == 2   # uppercase serial matched
    assert store.find_post_by_tag("AnAtZ56")["tag"] == "anatz56"


def test_arrival_links_orders_even_if_post_was_saved_first(env):
    cfg, store = env
    title = "ATEEZ GOLDEN HOUR PT.5 HELLOLIVE DIGIPACK FANSIGN 1.0 CHICK TEEZ VER."
    store.upsert_post("anatz56", title, None, [], None, None, None)
    # an order that went in unlinked (e.g. the match was missed at the time)
    store.add_order(None, title, 15.5, None, None, datetime.now(TZ).isoformat(), "x",
                    [{"name": "MINGI", "qty": 1, "line_total": 15.5}])
    arr = {"kind": "arrival_update", "summary": "x", "tags": ["ANATZ56"], "names": []}
    out = ingest(store, FakeParser(arr), cfg, text="#ANATZ56 arrived")
    assert out["draft"]["kind"] == "arrival" and "Linked 1 earlier order" in out["draft"]["note"]


def test_shared_ot8_listing_marks_all_of_your_cards_arrived(env):
    """You hold 4 single cards from a listing whose x1 OT8 is split across buyers: her count is for the
    whole listing, so it must not be compared with yours."""
    cfg, store = env
    title = 'ATEEZ 7TH MEMBERSHIP KIT "GHOSTEEZ" PCS'
    order = {"kind": "order_confirmation", "summary": "o", "title": title, "total_due": 18.0,
             "items": [{"name": n, "qty": 1, "line_total": 4.5} for n in ("SEONGHWA", "MINGI", "YEOSANG", "WOOYOUNG")]}
    old = datetime.now(TZ) - timedelta(days=72)
    confirm(store, ingest(store, FakeParser(order), cfg, text="order", when=old), cfg)
    post = {"kind": "channel_post", "summary": "p", "tag": "anatz59",
            "title": "ATEEZ 7TH MEMBERSHIP KIT \u201cGHOSTEEZ\u201d PCS"}                 # curly quotes this time
    assert "Linked 1 earlier order" in confirm(
        store, ingest(store, FakeParser(post), cfg, text="[PO CLOSED] #ANATZ59 ...", photo="P", channel=True), cfg)
    proof = {"kind": "arrival_update", "summary": "mass", "tags": [], "names": [], "arrivals": [
        {"tag": "ANATZ57", "quantity": "x8 + x1 admin claim"}, {"tag": "ANATZ58", "quantity": "x8 + x2 admin claim, x2 OT8"},
        {"tag": "ANATZ59", "quantity": "x1 OT8"}, {"tag": "ANATZ60", "quantity": "x2 OT8"}]}
    d = ingest(store, FakeParser(proof), cfg, text="[ARRIVAL PROOF] ...", photo="X", channel=True)["draft"]
    assert len(d["item_ids"]) == 4 and d["quantities"] == {"anatz59": "x1 OT8"}
    assert "whole listing (all buyers)" in pl.render_draft(d, "S$", TZ)


def test_unmatched_arrival_explains_why(env):
    cfg, store = env
    store.add_order(None, "SOME UNLINKED ORDER TITLE", 5.0, None, None, datetime.now(TZ).isoformat(), "x",
                    [{"name": "SAN", "qty": 1, "line_total": 5.0}])
    store.upsert_post("anatz59", "A different product entirely", None, [], None, None, None)
    proof = {"kind": "arrival_update", "summary": "m", "tags": ["anatz59", "anatz60"], "names": []}
    text = ingest(store, FakeParser(proof), cfg, text="x")["text"]
    assert "Hashtags I read: #anatz59, #anatz60" in text
    assert "Saved posts among them: #anatz59, but you have no pending items under them" in text
    assert "aren't linked to any post" in text and "SOME UNLINKED ORDER" in text


def test_fallback_tag():
    assert fallback_tag("[po] #anghp6_4 ateez golden hour") == "anghp6_4"
    assert fallback_tag("no tag here, just #hello") is None


def test_bot_wires_up_offline(env):
    cfg, store = env
    app = BotApp(cfg, store, FakeParser()).build()
    assert app.job_queue is not None


def test_all_overview_and_settle(env):
    cfg, store = env
    now = datetime.now(TZ)
    setup_orders(store, cfg, now)
    out = pl.answer_question(store, cfg, {"intent": "all"}, now)
    assert len(out) == 1 and "order(s)" in out[0]["text"] and out[0]["text"].count("Order ") >= 2
    assert "ordered" in out[0]["text"]
    assert pl.settle_drafts(store) != [] or all(abs(o["total_due"] - o["paid"]) <= pl.TOL for o in store.orders_with_balance())

    # a part-paid order: settle treats what was paid as the final total, and /undo restores it
    oid = store.add_order(None, "MISMATCH ORDER", 54.5, None, None, now.isoformat(), "x",
                          [{"name": "SAN", "qty": 1, "line_total": 54.5}])[0]
    pid = store.add_payment(43.5, now.isoformat(), None)
    store.add_allocation(pid, oid, 43.5)
    ds = pl.settle_drafts(store, oid)
    assert len(ds) == 1 and ds[0]["paid"] == 43.5 and ds[0]["total_due"] == 54.5
    assert "you still owe" in pl.render_draft(ds[0], "S$", TZ)
    did = store.draft_add("settle", ds[0])
    assert "settled at" in pl.apply_draft(store, did, "S$", TZ)
    assert pl.settle_drafts(store, oid) == []
    assert "settled, was" in pl.answer_question(store, cfg, {"intent": "all"}, now)[0]["text"]
    assert "Nothing owing" in pl.owing_text(store, cfg, "MISMATCH")
    assert "Reopened order" in pl.undo_last(store)
    assert len(pl.settle_drafts(store, oid)) == 1
