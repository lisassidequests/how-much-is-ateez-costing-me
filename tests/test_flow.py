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
    assert out["draft"]["order_id"] == 1
    confirm(store, out, cfg)
    pay2 = dict(pay, amount=15.5)
    out = ingest(store, FakeParser(pay2), cfg, photo="SHOT2")
    assert out["draft"]["order_id"] == 2
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


def test_ambiguous_payment_asks_instead_of_guessing(env):
    cfg, store = env
    setup_orders(store, cfg, datetime.now(TZ))
    pay = {"kind": "payment_proof", "summary": "x", "amount": 50.0}
    out = ingest(store, FakeParser(pay), cfg, photo="S")
    assert out["draft"]["order_id"] is None and len(out["draft"]["candidates"]) == 2
    d = pl.select_candidate(store, out["draft_id"], 0)
    assert d["order_id"] == out["draft"]["candidates"][0]["id"]


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
    assert len(msgs) == 1 and "Expecting 4 item(s) in 2 batch(es)" in msgs[0]["text"]
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
    assert "2 item(s) in 1 batch(es)" in status["text"] and status["buttons"] is None
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


def test_fallback_tag():
    assert fallback_tag("[po] #anghp6_4 ateez golden hour") == "anghp6_4"
    assert fallback_tag("no tag here, just #hello") is None


def test_bot_wires_up_offline(env):
    cfg, store = env
    app = BotApp(cfg, store, FakeParser()).build()
    assert app.job_queue is not None
