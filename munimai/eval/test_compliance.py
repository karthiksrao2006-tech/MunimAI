"""
Compliance (GST draft) tests.

The arithmetic must be exact and done in code, so most of these need no model.
They seed known ledger entries and assert the GST figures to the paise.

Run (from munimai/):  python -m pytest eval/test_compliance.py -v
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import psycopg
import pytest
from dotenv import load_dotenv

from backend.agents.compliance import ComplianceAgent, _tax_component_paise

load_dotenv()

P_START, P_END = date(2026, 11, 1), date(2026, 11, 30)


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
        "INSERT INTO businesses (whatsapp_phone, display_name) VALUES (%s,%s)"
        " RETURNING id", (phone, "GST Test Unit"),
    ).fetchone()[0]
    conn.commit()
    return bid


def _add(conn, bid, direction, gross_paise, rate_bp, needs_review=False):
    conn.execute(
        "INSERT INTO ledger_entries (business_id, entry_date, direction,"
        " amount_paise, category, gst_rate_bp, source, needs_review)"
        " VALUES (%s,%s,%s,%s,'test',%s,'manual',%s)",
        (bid, date(2026, 11, 15), direction, gross_paise, rate_bp, needs_review),
    )
    conn.commit()


# ----------------------------------------------------------- the maths itself
def test_tax_backcalc_is_exact():
    # Rs 4720 gross at 18% -> Rs 720 tax, Rs 4000 taxable
    assert _tax_component_paise(472000, 1800) == 72000
    # Rs 1180 gross at 18% -> Rs 180 tax
    assert _tax_component_paise(118000, 1800) == 18000
    # zero rate -> zero tax
    assert _tax_component_paise(500000, 0) == 0


def test_output_tax_from_sales(conn, business):
    # two sales: 4720 and 1180 gross at 18% -> output tax 720 + 180 = 900
    _add(conn, business, "in", 472000, 1800)
    _add(conn, business, "in", 118000, 1800)
    d = ComplianceAgent(conn).prepare_gstr3b(business, P_START, P_END, explain=False)
    assert d.output_tax_paise == 90000      # Rs 900
    assert d.sales_count == 2
    assert d.input_credit_paise == 0


def test_itc_from_purchases_and_net_payable(conn, business):
    # sale 11800 @18% -> output tax 1800
    _add(conn, business, "in", 1180000, 1800)
    # purchase 4720 @18% -> ITC 720
    _add(conn, business, "out", 472000, 1800)
    d = ComplianceAgent(conn).prepare_gstr3b(business, P_START, P_END, explain=False)
    assert d.output_tax_paise == 180000     # Rs 1800
    assert d.input_credit_paise == 72000    # Rs 720
    assert d.net_payable_paise == 108000    # Rs 1080 to pay
    assert d.credit_carry_forward_paise == 0


def test_excess_itc_carries_forward_not_negative(conn, business):
    # tiny sale, big purchase -> ITC exceeds output tax
    _add(conn, business, "in", 118000, 1800)     # output tax 180
    _add(conn, business, "out", 1180000, 1800)   # ITC 1800
    d = ComplianceAgent(conn).prepare_gstr3b(business, P_START, P_END, explain=False)
    assert d.net_payable_paise == 0             # never negative
    assert d.credit_carry_forward_paise == 162000  # Rs 1620 carried


def test_review_and_rateless_entries_become_issues(conn, business):
    _add(conn, business, "in", 472000, 1800, needs_review=True)  # excluded -> issue
    _add(conn, business, "out", 500000, None)                    # no rate -> issue
    d = ComplianceAgent(conn).prepare_gstr3b(business, P_START, P_END, explain=False)
    assert len(d.issues) == 2
    assert d.output_tax_paise == 0 and d.input_credit_paise == 0


def test_draft_is_not_filing(conn, business):
    """Sanity: the draft object makes no claim of being filed; the summary field
    is empty unless a model fills it, and the verifier guards the wording."""
    _add(conn, business, "in", 472000, 1800)
    d = ComplianceAgent(conn).prepare_gstr3b(business, P_START, P_END, explain=False)
    assert d.plain_summary == ""   # nothing asserted without an explicit explain step
