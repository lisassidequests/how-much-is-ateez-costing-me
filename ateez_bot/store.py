"""SQLite storage. One file, no server. All money is stored as plain numbers (2dp)."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY,
  tag TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  price_note TEXT,
  image_file_ids TEXT NOT NULL DEFAULT '[]',
  media_group_id TEXT,
  raw_text TEXT,
  posted_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  post_id INTEGER REFERENCES posts(id),
  title TEXT NOT NULL,
  total_due REAL NOT NULL,
  mailing_fee REAL,
  note TEXT,
  ordered_at TEXT NOT NULL,
  raw_text TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  post_id INTEGER REFERENCES posts(id),
  name TEXT NOT NULL,
  qty INTEGER NOT NULL DEFAULT 1,
  line_total REAL NOT NULL DEFAULT 0,
  arrived_at TEXT,
  last_alert_at TEXT
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY,
  order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
  amount REAL NOT NULL,
  paid_at TEXT NOT NULL,
  reference TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (
  id INTEGER PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  message_id INTEGER,
  text TEXT,
  photo_file_id TEXT,
  media_group_id TEXT,
  forwarded_from_channel INTEGER NOT NULL DEFAULT 0,
  msg_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',   -- queued | done | failed
  error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY,
  inbox_id INTEGER REFERENCES inbox(id),
  kind TEXT NOT NULL,
  data TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed | cancelled
  result TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # -- helpers -----------------------------------------------------------
    def one(self, sql: str, args=()):
        return self.db.execute(sql, args).fetchone()

    def all(self, sql: str, args=()):
        return self.db.execute(sql, args).fetchall()

    def run(self, sql: str, args=()) -> int:
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur.lastrowid

    # -- settings ----------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.run(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- posts -------------------------------------------------------------
    def get_post_by_tag(self, tag: str):
        return self.one("SELECT * FROM posts WHERE tag=?", (tag.lower(),))

    def find_post_by_tag(self, tag: str):
        """Exact match, else a match ignoring - and _ (Telegram cuts hashtags at hyphens), if unique."""
        row = self.get_post_by_tag(tag)
        if row:
            return row
        norm = re.sub(r"[^a-z0-9]", "", tag.lower())
        if not norm:
            return None
        hits = [p for p in self.all("SELECT * FROM posts") if re.sub(r"[^a-z0-9]", "", p["tag"]) == norm]
        return hits[0] if len(hits) == 1 else None

    def get_post(self, post_id: int):
        return self.one("SELECT * FROM posts WHERE id=?", (post_id,))

    def recent_posts(self, limit: int = 60):
        return self.all("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,))

    def upsert_post(self, tag, title, price_note, image_file_ids, media_group_id, raw_text, posted_at):
        """Returns (post_id, created)."""
        existing = self.get_post_by_tag(tag)
        if existing:
            merged = list(dict.fromkeys(json.loads(existing["image_file_ids"]) + list(image_file_ids)))
            self.run(
                "UPDATE posts SET title=?, price_note=COALESCE(?,price_note), image_file_ids=?, "
                "media_group_id=COALESCE(?,media_group_id), raw_text=COALESCE(?,raw_text) WHERE id=?",
                (title, price_note, json.dumps(merged), media_group_id, raw_text, existing["id"]),
            )
            return existing["id"], False
        pid = self.run(
            "INSERT INTO posts(tag,title,price_note,image_file_ids,media_group_id,raw_text,posted_at,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (tag.lower(), title, price_note, json.dumps(list(image_file_ids)), media_group_id,
             raw_text, posted_at, now_iso()),
        )
        return pid, True

    def append_post_image(self, media_group_id: str, file_id: str) -> bool:
        """Attach an extra album photo to the post that shares its media_group_id."""
        row = self.one("SELECT * FROM posts WHERE media_group_id=?", (media_group_id,))
        if not row:
            return False
        ids = json.loads(row["image_file_ids"])
        if file_id not in ids:
            ids.append(file_id)
            self.run("UPDATE posts SET image_file_ids=? WHERE id=?", (json.dumps(ids), row["id"]))
        return True

    # -- inbox -------------------------------------------------------------
    def inbox_add(self, chat_id, message_id, text, photo_file_id, media_group_id,
                  forwarded_from_channel, msg_date) -> int:
        return self.run(
            "INSERT INTO inbox(chat_id,message_id,text,photo_file_id,media_group_id,"
            "forwarded_from_channel,msg_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (chat_id, message_id, text, photo_file_id, media_group_id,
             int(forwarded_from_channel), msg_date, now_iso()),
        )

    def inbox_get(self, inbox_id: int):
        return self.one("SELECT * FROM inbox WHERE id=?", (inbox_id,))

    def inbox_set(self, inbox_id: int, status: str, error: str | None = None) -> None:
        self.run("UPDATE inbox SET status=?, error=? WHERE id=?", (status, error, inbox_id))

    def inbox_queued(self):
        return self.all("SELECT * FROM inbox WHERE status='queued' ORDER BY id")

    # -- drafts ------------------------------------------------------------
    def draft_add(self, kind: str, data: dict, inbox_id: int | None = None) -> int:
        return self.run(
            "INSERT INTO drafts(inbox_id,kind,data,created_at) VALUES(?,?,?,?)",
            (inbox_id, kind, json.dumps(data), now_iso()),
        )

    def draft_get(self, draft_id: int):
        row = self.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if not row:
            return None
        d = dict(row)
        d["data"] = json.loads(d["data"])
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    def draft_update(self, draft_id: int, *, data=None, status=None, result=None) -> None:
        if data is not None:
            self.run("UPDATE drafts SET data=? WHERE id=?", (json.dumps(data), draft_id))
        if status is not None:
            self.run("UPDATE drafts SET status=? WHERE id=?", (status, draft_id))
        if result is not None:
            self.run("UPDATE drafts SET result=? WHERE id=?", (json.dumps(result), draft_id))

    def last_confirmed_draft(self):
        row = self.one(
            "SELECT id FROM drafts WHERE status='confirmed' AND result IS NOT NULL ORDER BY id DESC LIMIT 1"
        )
        return self.draft_get(row["id"]) if row else None

    # -- orders / items / payments ----------------------------------------
    def add_order(self, post_id, title, total_due, mailing_fee, note, ordered_at, raw_text, items) -> tuple[int, list[int]]:
        oid = self.run(
            "INSERT INTO orders(post_id,title,total_due,mailing_fee,note,ordered_at,raw_text,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (post_id, title, total_due, mailing_fee, note, ordered_at, raw_text, now_iso()),
        )
        item_ids = []
        for it in items:
            item_ids.append(self.run(
                "INSERT INTO items(order_id,post_id,name,qty,line_total) VALUES(?,?,?,?,?)",
                (oid, post_id, it["name"], int(it.get("qty") or 1), float(it.get("line_total") or 0)),
            ))
        return oid, item_ids

    def add_payment(self, order_id, amount, paid_at, reference) -> int:
        return self.run(
            "INSERT INTO payments(order_id,amount,paid_at,reference,created_at) VALUES(?,?,?,?,?)",
            (order_id, amount, paid_at, reference, now_iso()),
        )

    def orders_with_balance(self):
        """Every order with how much has been paid so far."""
        return self.all(
            "SELECT o.*, COALESCE((SELECT SUM(amount) FROM payments WHERE order_id=o.id),0) AS paid, "
            "(SELECT MIN(paid_at) FROM payments WHERE order_id=o.id) AS first_paid "
            "FROM orders o ORDER BY o.id DESC"
        )

    def items_pending(self):
        return self.all(
            "SELECT i.*, o.title AS order_title, o.ordered_at, p.tag, p.title AS post_title, "
            "p.image_file_ids, (SELECT MIN(paid_at) FROM payments WHERE order_id=o.id) AS first_paid "
            "FROM items i JOIN orders o ON o.id=i.order_id LEFT JOIN posts p ON p.id=i.post_id "
            "WHERE i.arrived_at IS NULL ORDER BY o.ordered_at"
        )

    def all_payments(self):
        return self.all(
            "SELECT pay.*, o.title AS order_title, p.tag, p.title AS post_title, "
            "(SELECT GROUP_CONCAT(name, ' ') FROM items WHERE order_id=o.id) AS item_names "
            "FROM payments pay LEFT JOIN orders o ON o.id=pay.order_id "
            "LEFT JOIN posts p ON p.id=o.post_id ORDER BY pay.paid_at"
        )

    def mark_arrived(self, item_ids, when: str) -> None:
        for iid in item_ids:
            self.run("UPDATE items SET arrived_at=? WHERE id=? AND arrived_at IS NULL", (when, iid))
