"""
Tests that the schema enforces the promises the product makes.

These are not CRUD tests. Each one asserts that the database REFUSES something
MunimAI must never do, so the guarantee holds even if an agent is buggy or a
future contributor forgets the rule.

Run:  python -m pytest munimai/eval/test_schema_guarantees.py -v
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from dotenv import load_dotenv

load_dotenv()


@pytest.fixture()
def conn():
    c = psycopg.connect(
        host=os.environ["PGHOST"], port=os.environ["PGPORT"],
        dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"], connect_timeout=20,
    )
    yield c
    c.rollback()
    c.close()


@pytest.fixture()
def business(conn):
    """A throwaway business, rolled back after each test."""
    phone = f"+9199{uuid.uuid4().int % 100_000_000:08d}"
    row = conn.execute(
        "INSERT INTO businesses (whatsapp_phone, display_name, language)"
        " VALUES (%s,%s,'kn') RETURNING id",
        (phone, "Test Tailoring Unit"),
    ).fetchone()
    return row[0]


def test_blocked_claim_must_say_why(conn, business):
    """A blocked claim without a reason is useless to the owner and to us."""
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO claims (business_id, trace_id, agent, claim_text, verdict)"
            " VALUES (%s,%s,'scheme','You qualify for MUDRA','blocked')",
            (business, str(uuid.uuid4())),
        )


def test_money_is_integer_paise_and_never_negative(conn, business):
    """Float rupees cause rounding drift; negative amounts hide sign errors."""
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO ledger_entries"
            " (business_id, entry_date, direction, amount_paise, category, source)"
            " VALUES (%s, CURRENT_DATE, 'out', -500, 'fabric', 'bill_photo')",
            (business,),
        )


def test_gst_rate_is_basis_points_in_range(conn, business):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO ledger_entries"
            " (business_id, entry_date, direction, amount_paise, category, source, gst_rate_bp)"
            " VALUES (%s, CURRENT_DATE, 'out', 500000, 'fabric', 'bill_photo', 99999)",
            (business,),
        )


def test_expired_consent_is_not_active(conn, business):
    """has_active_consent() is what the verifier calls before touching data."""
    conn.execute(
        "INSERT INTO consents (business_id, purpose, scope, expires_at, granted_via)"
        " VALUES (%s,'loan_eligibility_assessment','{bank_statement}',%s,'simulated')",
        (business, datetime.now(timezone.utc) - timedelta(days=1)),
    )
    active = conn.execute(
        "SELECT has_active_consent(%s,'loan_eligibility_assessment')", (business,)
    ).fetchone()[0]
    assert active is False, "an expired consent must not count as active"


def test_revoked_consent_is_not_active(conn, business):
    conn.execute(
        "INSERT INTO consents (business_id, purpose, scope, expires_at, revoked_at, granted_via)"
        " VALUES (%s,'loan_eligibility_assessment','{bank_statement}',%s,now(),'simulated')",
        (business, datetime.now(timezone.utc) + timedelta(days=30)),
    )
    active = conn.execute(
        "SELECT has_active_consent(%s,'loan_eligibility_assessment')", (business,)
    ).fetchone()[0]
    assert active is False, "a revoked consent must not count as active"


def test_valid_consent_is_active(conn, business):
    conn.execute(
        "INSERT INTO consents (business_id, purpose, scope, expires_at, granted_via)"
        " VALUES (%s,'loan_eligibility_assessment','{bank_statement}',%s,'whatsapp_reply')",
        (business, datetime.now(timezone.utc) + timedelta(days=30)),
    )
    active = conn.execute(
        "SELECT has_active_consent(%s,'loan_eligibility_assessment')", (business,)
    ).fetchone()[0]
    assert active is True


def test_scheme_clause_requires_source_and_fetch_time(conn):
    """A clause with no provenance can never support a citation."""
    with pytest.raises(psycopg.errors.NotNullViolation):
        conn.execute(
            "INSERT INTO scheme_clauses (scheme_code, clause_ref, clause_text,"
            " source_url, source_domain) VALUES"
            " ('MUDRA_KISHORE','eligibility.x','some text','https://mudra.org.in/x','mudra.org.in')"
        )


def test_generated_agent_cannot_be_promoted_without_passing_evals(conn):
    """Karigar may propose; only a passing eval may promote."""
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO agent_specs (name, spec, eval_passed, eval_total, promoted)"
            " VALUES ('eway_karnataka','{}'::jsonb, 1, 3, TRUE)"
        )


def test_generated_agent_recursion_depth_is_capped(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO agent_specs (name, spec, depth)"
            " VALUES ('runaway','{}'::jsonb, 3)"
        )


def test_trace_sequence_is_unique_per_run(conn, business):
    tid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO trace_events (trace_id, seq, business_id, agent, event_type)"
        " VALUES (%s,1,%s,'router','message_in')", (tid, business),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO trace_events (trace_id, seq, business_id, agent, event_type)"
            " VALUES (%s,1,%s,'ledger','agent_start')", (tid, business),
        )


def test_whatsapp_message_id_dedupes_webhook_retries(conn, business):
    """Meta retries webhooks; a retry must not create a second ledger run."""
    wa_id = f"wamid.{uuid.uuid4().hex}"
    for _ in range(1):
        conn.execute(
            "INSERT INTO messages (business_id, direction, msg_type, body, wa_message_id)"
            " VALUES (%s,'in','text','hello',%s)", (business, wa_id),
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO messages (business_id, direction, msg_type, body, wa_message_id)"
            " VALUES (%s,'in','text','hello',%s)", (business, wa_id),
        )
