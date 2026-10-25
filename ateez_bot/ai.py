"""Claude-powered message understanding, plus classification of API failures.

The model only *extracts* structured data. It never does arithmetic that gets saved,
and nothing is stored until the user confirms.
"""
from __future__ import annotations

import base64
import re

import anthropic

SYSTEM_PROMPT = """You read Telegram messages for a K-pop merch buyer and extract structured data.
Call the record_message tool exactly once. Never answer in prose.

The buyer purchases through a seller's Telegram channel ("ange's ins"). Message types:

1. channel_post - a forwarded channel announcement (usually a photo plus caption) for a group order.
   It contains a serial hashtag like #anghp6_4 (return it WITHOUT the '#', lowercase, keeping any hyphens/underscores) and a product
   name, e.g. "[po] #anghp6_4 ateez golden hour pt.6 makestar fansign 1.0 ribbon puppy ver."
   Return tag, title (product name without "[po]" or the tag), price_note (e.g. "$6.50 fp per member").
   Some posts are labelled [INSTOCK]: the seller already has the item and does not give it a serial. For those
   return tag = null and title without the "[INSTOCK]" label; never invent a serial.
   The first-person status lines further down (names with check marks) are claim lists; ignore them.

2. order_confirmation - the seller's DM confirming an order. It has NO serial number. Example:
   "Order: X1 MINGI $7.50 / X1 HONGJOONG $8 / INSTOCKS - ATEEZ ANITEEZ IN DEJAVU OFFLINE HTB EXTRA
   50K BENEFIT PCS / Total to transfer: $15.50 (inclusive of local mailing fee)".
   - title = the product/event description line(s), NOT the item lines (here: the INSTOCKS line).
   - items = one entry per item line: name, qty, line_total.
     If a per-piece price is stated and a line is a multi-piece set, expand to pieces:
     "X1 OT8 INDIVIDUAL" with "$8.50/individual pc" means qty 8, line_total 68.00;
     "X1 OT4 UNIT" with "$11.50/unit pc" means qty 4, line_total 46.00.
     Always make the item line_totals reconcile with the stated total where possible.
     Group sizes: "OT8" = one piece of each of the 8 members (8 pieces); "OT4" = 4 pieces.
     A set like "X1 OT8" is qty 8; keep the item name as "OT8" (no "X1").
   - total_due = the "Total to transfer" amount. mailing_fee only if stated separately.
   - is_second_payment = true ONLY if THIS message asks for the second / final payment, i.e. the shipping or
     reconsolidated shipping cost after the goods shipped (wording like "second payment", "final payment", "shipping
     fee"). false for a first request that merely says a second payment is still pending. The items may be missing
     or repeated in such a message; total_due is the amount to transfer now.
   - note = short remark such as "first payment, pending second payment" when present.
   - matched_tag: compare title against the KNOWN POSTS list given by the user and return the tag
     of the post it clearly refers to (same product/event and version), else null. Do not guess
     between near-identical versions; use match_confidence low in that case.

3. payment_proof - a screenshot of a bank/PayNow/PayLah transfer. Return amount (the total transferred, even if it covers several orders), paid_at (ISO 8601 date or
   datetime as shown on the screenshot, else null) and reference (any recipient/comment text).

4. arrival_update - the seller announcing that items have reached her. Often a mass "[ARRIVAL PROOF]" post,
   forwarded from a channel WITH a photo of the cards, listing many hashtags each followed by a quantity, e.g.
   "#anghp5_2 (quantity: x4 unit pcs)  #anghp5_8 (quantity: x1 OT8)  #ANATZ38-2 (quantity: x4 pcs) ...
   [MAILING NOTICE] 31 AUGUST 2026". That is an arrival_update, NOT a channel_post, even though it is a
   forwarded channel photo post. Hashtags can contain hyphens (#ANATZ38-2): return the full serial
   lowercase ("anatz38-2"). Return arrivals = one entry per hashtag {tag, quantity}, where quantity is the
   bracketed text exactly as written ("x4 unit pcs"); also tags (the same list), names (member/item names
   mentioned, may be empty) and note. Most hashtags in a mass post belong to other buyers: list them all.

5. question - the buyer asking about their own records (typed, not forwarded). Return question
   with intent one of: spent, pending (text list), pending_photos (when they ask to see pictures
   of what's pending), status (everything they're still awaiting, not yet with them), all (every order and its status), awaiting (what the seller still has to send them), delivered (what has arrived at the seller), late, owing; optional month (YYYY-MM) and keyword
   (e.g. a group, member or product name).

6. other - anything else.

Be literal: copy names and numbers exactly as written. Use null for anything not present."""

TOOL = {
    "name": "record_message",
    "description": "Record the structured contents of the message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": [
                "channel_post", "order_confirmation", "payment_proof",
                "arrival_update", "question", "other"]},
            "summary": {"type": "string", "description": "One short line describing the message."},
            "tag": {"type": ["string", "null"]},
            "title": {"type": ["string", "null"]},
            "price_note": {"type": ["string", "null"]},
            "items": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "integer"},
                    "line_total": {"type": "number"}},
                "required": ["name", "qty", "line_total"]}},
            "total_due": {"type": ["number", "null"]},
            "mailing_fee": {"type": ["number", "null"]},
            "note": {"type": ["string", "null"]},
            "is_second_payment": {"type": ["boolean", "null"]},
            "matched_tag": {"type": ["string", "null"]},
            "match_confidence": {"type": ["string", "null"], "enum": ["high", "medium", "low", None]},
            "amount": {"type": ["number", "null"]},
            "paid_at": {"type": ["string", "null"]},
            "reference": {"type": ["string", "null"]},
            "tags": {"type": "array", "items": {"type": "string"}},
            "arrivals": {"type": "array", "items": {
                "type": "object",
                "properties": {"tag": {"type": "string"}, "quantity": {"type": ["string", "null"]}},
                "required": ["tag"]}},
            "names": {"type": "array", "items": {"type": "string"}},
            "intent": {"type": ["string", "null"],
                       "enum": ["spent", "pending", "pending_photos", "status", "all", "awaiting", "delivered", "late", "owing", None]},
            "month": {"type": ["string", "null"]},
            "keyword": {"type": ["string", "null"]},
        },
        "required": ["kind", "summary"],
    },
}


class AIUnavailable(Exception):
    """The Claude API can't be used right now. kind: credits | auth | temporary."""

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class AIParseError(Exception):
    """Claude answered, but not in a usable shape."""


def classify_error(exc: Exception) -> AIUnavailable | None:
    """Map an anthropic SDK exception to AIUnavailable, or None if it's not an availability problem."""
    msg = str(exc)
    if isinstance(exc, anthropic.BadRequestError) and "credit balance" in msg.lower():
        return AIUnavailable("credits", "Your Anthropic API credit balance is too low.")
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return AIUnavailable("auth", "The Anthropic API key was rejected (invalid, revoked or lacking access).")
    if isinstance(exc, anthropic.RateLimitError):
        return AIUnavailable("temporary", "Anthropic API rate limit hit.")
    if isinstance(exc, anthropic.APIConnectionError):
        return AIUnavailable("temporary", "Couldn't reach the Anthropic API.")
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
        return AIUnavailable("temporary", f"Anthropic API error {exc.status_code}.")
    return None


def fallback_tags(text: str | None) -> list[str]:
    """Regex backup for serial hashtags (may contain _ and -) if the model misses them."""
    if not text:
        return []
    found = [m.group(1).rstrip("-_").lower()
             for m in re.finditer(r"#([A-Za-z][A-Za-z0-9_\-]*\d[A-Za-z0-9_\-]*)", text)]
    return list(dict.fromkeys(found))


def fallback_tag(text: str | None) -> str | None:
    tags = fallback_tags(text)
    return tags[0] if tags else None


class Parser:
    def __init__(self, api_key: str, model: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)
        self.model = model

    async def classify(self, *, text: str | None, image: bytes | None,
                       from_channel: bool, known_posts: list[tuple[str, str]], today: str) -> dict:
        posts = "\n".join(f"#{t} - {title}" for t, title in known_posts) or "(none saved yet)"
        context = (
            f"Today: {today}\n"
            f"Forwarded from a channel: {'yes' if from_channel else 'no'}\n"
            f"KNOWN POSTS:\n{posts}\n\n"
            f"Message text/caption:\n{text or '(none)'}"
        )
        content: list[dict] = []
        if image:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": base64.b64encode(image).decode()}})
        content.append({"type": "text", "text": context})
        try:
            resp = await self.client.messages.create(
                model=self.model, max_tokens=1500, system=SYSTEM_PROMPT,
                tools=[TOOL], tool_choice={"type": "tool", "name": "record_message"},
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIError as exc:
            unavailable = classify_error(exc)
            if unavailable:
                raise unavailable from exc
            raise AIParseError(f"Claude API error: {exc}") from exc
        for block in resp.content:
            if block.type == "tool_use" and block.name == "record_message":
                return dict(block.input)
        raise AIParseError("Claude did not return structured data.")
