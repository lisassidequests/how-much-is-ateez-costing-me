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
from ateez_bot.ai import AIUnavailable, classify_error, fallback_tag
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


def test_fallback_tag():
    assert fallback_tag("[po] #anghp6_4 ateez golden hour") == "anghp6_4"
    assert fallback_tag("no tag here, just #hello") is None


def test_bot_wires_up_offline(env):
    cfg, store = env
    app = BotApp(cfg, store, FakeParser()).build()
    assert app.job_queue is not None
