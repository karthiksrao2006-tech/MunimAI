"""
The Ledger agent.

A bill becomes a ledger entry in two steps:
  1. OCR turns the photo into raw text (pluggable backend; EasyOCR on the
     supercomputer for the demo). The agent does not care which OCR is used.
  2. The model turns that raw text into one structured entry: amount in paise,
     direction, category, GST rate, counterparty, date.

Honesty rules baked in:
- Money is parsed to integer paise, never float rupees (matches the schema).
- If the model is unsure of a field it must say so, and the entry is flagged
  needs_review rather than guessed — a wrong number in someone's books is worse
  than an admitted gap.
- The raw OCR text and the model's full extraction are stored, so any entry can
  be traced back to what was actually on the bill.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional, Protocol

import psycopg

from ..llm import LLM
from ..trace import Trace


class OCRBackend(Protocol):
    def read(self, image_path: str) -> str: ...


@dataclass
class LedgerEntry:
    entry_id: Optional[int]
    entry_date: str
    direction: str          # 'in' | 'out'
    amount_paise: int
    category: str
    counterparty: Optional[str]
    gst_rate_bp: Optional[int]
    needs_review: bool
    review_reason: Optional[str]


# The model only COPIES fields off the bill. It is not asked to reason about
# direction — a 7B model is unreliable at that, so we compute it in code from
# the seller/buyer names it copies. Extraction it is good at; inference it is not.
_SYSTEM = (
    "You read Indian business bills and copy fields into JSON. Do NOT infer or "
    "reason — only copy what is printed. "
    "seller_name is the issuer/supplier; buyer_name is who the bill is addressed "
    "to ('Bill to' / 'Buyer'). "
    "amount is the TOTAL payable in rupees (a number, not a string). "
    "gst_rate_percent is the TOTAL GST rate: when CGST and SGST are shown "
    "separately, ADD them (CGST 9% + SGST 9% = 18); IGST or a single 'GST @x%' line "
    "is already the full rate; if no GST is shown, null. "
    "If a field is not printed clearly, use null. Never guess an amount."
)

_SCHEMA_HINT = (
    '{"date": "YYYY-MM-DD or null", "seller_name": "string or null", '
    '"buyer_name": "string or null", "amount_rupees": number|null, '
    '"category": "short label", "gst_rate_percent": number|null}'
)


def _name_matches(ours: str, other: Optional[str]) -> bool:
    """Loose match: do the significant words of our name appear in the field?"""
    if not other:
        return False
    import re
    ours_words = {w for w in re.findall(r"[a-z]+", ours.lower()) if len(w) > 2}
    other_l = other.lower()
    if not ours_words:
        return False
    hits = sum(1 for w in ours_words if w in other_l)
    return hits >= max(1, len(ours_words) // 2)


class LedgerAgent:
    def __init__(self, conn: psycopg.Connection, llm: LLM,
                 ocr: Optional[OCRBackend] = None) -> None:
        self.conn = conn
        self.llm = llm
        self.ocr = ocr

    def from_image(self, business_id: int, image_path: str,
                   *, trace: Optional[Trace] = None) -> LedgerEntry:
        if self.ocr is None:
            raise RuntimeError("No OCR backend configured for from_image().")
        if trace:
            trace.emit("ledger", "tool_call", {"image": image_path}, tool_name="ocr")
        text = self.ocr.read(image_path)
        if trace:
            trace.emit("ledger", "tool_result",
                       {"ocr_chars": len(text), "preview": text[:200]},
                       tool_name="ocr")
        return self.from_text(business_id, text, source="bill_photo",
                              source_media_id=image_path, trace=trace)

    def from_text(self, business_id: int, raw_text: str, *,
                  source: str = "text", source_media_id: Optional[str] = None,
                  trace: Optional[Trace] = None) -> LedgerEntry:
        if trace:
            trace.emit("ledger", "agent_start", {"source": source,
                                                 "chars": len(raw_text)})
        biz = self.conn.execute(
            "SELECT display_name, gstin FROM businesses WHERE id=%s", (business_id,)
        ).fetchone()
        our_name = biz[0] if biz else ""
        parsed = self.llm.json_call(
            _SYSTEM,
            f"Bill text:\n{raw_text}\n\nCopy the fields.",
            schema_hint=_SCHEMA_HINT, trace=trace, agent="ledger", max_tokens=350,
        )

        needs_review = False
        review_reason = None

        # convert rupees -> integer paise defensively; never trust a float blindly
        amount_rupees = parsed.get("amount_rupees")
        amount_paise = 0
        if amount_rupees is None:
            needs_review = True
            review_reason = "amount could not be read"
        else:
            try:
                amount_paise = abs(int(round(float(amount_rupees) * 100)))
            except (TypeError, ValueError):
                needs_review = True
                review_reason = f"unparseable amount {amount_rupees!r}"

        # direction computed in code from who we are vs seller/buyer (not by the LLM)
        seller = parsed.get("seller_name")
        buyer = parsed.get("buyer_name")
        we_are_seller = _name_matches(our_name, seller)
        we_are_buyer = _name_matches(our_name, buyer)
        if we_are_seller and not we_are_buyer:
            direction, counterparty = "in", buyer          # we sold -> money in
        elif we_are_buyer and not we_are_seller:
            direction, counterparty = "out", seller         # we bought -> money out
        else:
            # can't tell which side we are -> don't guess the sign of someone's books
            direction, counterparty = "out", seller or buyer
            needs_review = True
            review_reason = ((review_reason + "; ") if review_reason else "") + (
                f"could not match our business to seller ({seller!r}) or "
                f"buyer ({buyer!r}); direction needs confirmation"
            )

        gst_pct = parsed.get("gst_rate_percent")
        gst_bp = int(round(float(gst_pct) * 100)) if isinstance(gst_pct, (int, float)) else None
        if gst_bp is not None and not (0 <= gst_bp <= 10000):
            gst_bp = None

        entry_date = date.today().isoformat()
        raw_date = str(parsed.get("date") or "").strip()[:10]
        try:  # the model sometimes returns the text "null" or a bad date
            entry_date = date.fromisoformat(raw_date).isoformat()
        except ValueError:
            pass

        row = self.conn.execute(
            """INSERT INTO ledger_entries
               (business_id, entry_date, direction, amount_paise, category,
                counterparty, gst_rate_bp, source, source_media_id,
                needs_review, raw_extraction)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (business_id, entry_date, direction, amount_paise,
             parsed.get("category") or "uncategorised",
             counterparty, gst_bp, source, source_media_id,
             needs_review, psycopg.types.json.Json(
                 {"raw_text": raw_text[:4000], "extraction": parsed})),
        ).fetchone()
        self.conn.commit()

        entry = LedgerEntry(
            entry_id=row[0], entry_date=entry_date, direction=direction,
            amount_paise=amount_paise, category=parsed.get("category") or "uncategorised",
            counterparty=parsed.get("counterparty"), gst_rate_bp=gst_bp,
            needs_review=needs_review,
            review_reason=(review_reason or None),
        )
        if trace:
            trace.emit("ledger", "agent_end",
                       {"amount_paise": amount_paise, "direction": direction,
                        "needs_review": needs_review})
        return entry
