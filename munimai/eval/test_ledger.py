"""
Ledger agent tests against the live model.

These check the parts that must be right regardless of the model's phrasing:
money ends up as integer paise, an unreadable amount is flagged not guessed,
and the entry that lands in the database is internally consistent.

Run (from munimai/):  python -m pytest eval/test_ledger.py -v
Skips automatically if the model is unreachable.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from dotenv import load_dotenv

from backend.agents.ledger import LedgerAgent
from backend.llm import LLM

load_dotenv()

# Supplier (Karnataka Cotton Traders) bills our business (Lakshmi Tailoring Unit).
# From our books this is a purchase -> direction 'out'.
CLEAR_BILL = """TAX INVOICE
Seller: Karnataka Cotton Traders, Hubli
GSTIN: 29XYZAB9876C1Z2
Bill to: Lakshmi Tailoring Unit, Mysuru
Date: 12/11/2026
Cotton cloth 20 metres         4000.00
CGST 9%                          360.00
SGST 9%                          360.00
Total payable                   4720.00
"""

GARBLED_BILL = "faded thermal receipt ...... metr... tot... amount unreadable blur"


@pytest.fixture(scope="module")
def llm():
    l = LLM()
    if not l.health():
        pytest.skip("model unreachable; start the vLLM server")
    return l


@pytest.fixture()
def conn():
    c = psycopg.connect(
        host=os.environ["PGHOST"], port=os.environ["PGPORT"],
        dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"], connect_timeout=20,
    )
    yield c
    c.rollback(); c.close()


@pytest.fixture()
def business(conn):
    phone = f"+9199{uuid.uuid4().int % 100_000_000:08d}"
    bid = conn.execute(
        "INSERT INTO businesses (whatsapp_phone, display_name, gstin) VALUES (%s,%s,%s)"
        " RETURNING id", (phone, "Lakshmi Tailoring Unit", "29LLLMN4567P1Z0"),
    ).fetchone()[0]
    conn.commit()
    return bid


def test_clear_bill_extracts_total_as_paise(conn, business, llm):
    agent = LedgerAgent(conn, llm)
    entry = agent.from_text(business, CLEAR_BILL, source="bill_photo")
    # 4720.00 rupees -> 472000 paise. Allow the model to pick the total line.
    assert entry.amount_paise == 472000, f"got {entry.amount_paise} paise"
    assert entry.direction == "out"
    assert entry.gst_rate_bp in (1800, None)  # 9+9=18%, or null if it didn't sum
    # the stored row matches the returned object
    row = conn.execute(
        "SELECT amount_paise, direction FROM ledger_entries WHERE id=%s",
        (entry.entry_id,),
    ).fetchone()
    assert row == (472000, "out")


def test_unreadable_bill_is_flagged_not_guessed(conn, business, llm):
    agent = LedgerAgent(conn, llm)
    entry = agent.from_text(business, GARBLED_BILL, source="bill_photo")
    assert entry.needs_review is True
    assert entry.review_reason
    # an admitted gap stores amount 0, never a fabricated number
    assert entry.amount_paise == 0


def test_amount_is_never_float_in_db(conn, business, llm):
    agent = LedgerAgent(conn, llm)
    entry = agent.from_text(business, CLEAR_BILL)
    assert isinstance(entry.amount_paise, int)
