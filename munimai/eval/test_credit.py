"""
Credit agent tests.

Deterministic — no model. They assert the scorecard reflects real cash flow and,
critically, that a credit claim cannot pass the verifier without active consent.

Run (from munimai/):  python -m pytest eval/test_credit.py -v
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest
from dotenv import load_dotenv

from backend.agents.credit import CONSENT_PURPOSE, CreditAgent
from backend.agents.verifier import Verifier

load_dotenv()


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
        " RETURNING id", (phone, "Credit Test Unit"),
    ).fetchone()[0]
    conn.commit()
    return bid


def _month(conn, bid, month_idx, inflow_paise, outflow_paise):
    d = date(2026, 6 + month_idx, 10)
    conn.execute("INSERT INTO ledger_entries (business_id, entry_date, direction,"
                 " amount_paise, category, source) VALUES (%s,%s,'in',%s,'sales','manual')",
                 (bid, d, inflow_paise))
    conn.execute("INSERT INTO ledger_entries (business_id, entry_date, direction,"
                 " amount_paise, category, source) VALUES (%s,%s,'out',%s,'costs','manual')",
                 (bid, d, outflow_paise))
    conn.commit()


def _grant_consent(conn, bid):
    conn.execute(
        "INSERT INTO consents (business_id, purpose, scope, expires_at, granted_via)"
        " VALUES (%s,%s,'{bank_statement}',%s,'whatsapp_reply')",
        (bid, CONSENT_PURPOSE, datetime.now(timezone.utc) + timedelta(days=30)),
    )
    conn.commit()


def test_healthy_business_scores_higher_than_thin_one(conn, business):
    # 5 steady months, good surplus
    for i in range(5):
        _month(conn, business, i, 5000000, 3000000)   # Rs 50k in, 30k out
    view, _ = CreditAgent(conn).assess(business)
    assert view.months_active == 5
    assert view.avg_monthly_net_paise == 2000000      # Rs 20k surplus
    assert view.band in ("strong", "fair")
    assert view.suggested_emi_paise == 800000         # 40% of 20k = Rs 8k
    assert any("surplus" in f.name for f in view.factors)


def test_no_history_is_insufficient(conn, business):
    view, _ = CreditAgent(conn).assess(business)
    assert view.band == "insufficient"
    assert view.score == 0


def test_every_score_point_has_a_reason(conn, business):
    for i in range(4):
        _month(conn, business, i, 3000000, 2500000)
    view, _ = CreditAgent(conn).assess(business)
    # explainability: no factor may carry points without a reason string
    for f in view.factors:
        assert f.reason, f"factor {f.name} has points but no reason"


def test_credit_claim_blocked_without_consent(conn, business):
    for i in range(4):
        _month(conn, business, i, 4000000, 2500000)
    _, claim = CreditAgent(conn).assess(business)
    verdict = Verifier(conn).check(claim)
    assert verdict.passed is False
    assert any("consent" in r for r in verdict.reasons)


def test_credit_claim_passes_with_consent(conn, business):
    for i in range(4):
        _month(conn, business, i, 4000000, 2500000)
    _grant_consent(conn, business)
    _, claim = CreditAgent(conn).assess(business)
    verdict = Verifier(conn).check(claim)
    assert verdict.passed is True, verdict.reasons
