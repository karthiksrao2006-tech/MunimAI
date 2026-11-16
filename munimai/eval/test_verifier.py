"""
The verifier is the trust spine. These tests assert it blocks what it must.

Run:  python -m pytest eval/test_verifier.py -v
(run from the munimai/ directory)
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from dotenv import load_dotenv

from backend.agents.verifier import Claim, Verifier

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
    phone = f"+9199{uuid.uuid4().int % 100_000_000:08d}"
    bid = conn.execute(
        "INSERT INTO businesses (whatsapp_phone, display_name) VALUES (%s,%s)"
        " RETURNING id", (phone, "Test Unit"),
    ).fetchone()[0]
    conn.commit()
    return bid


@pytest.fixture()
def real_clause(conn):
    """A current, official clause id to cite."""
    row = conn.execute(
        "SELECT id FROM scheme_clauses WHERE superseded_at IS NULL LIMIT 1"
    ).fetchone()
    assert row, "load the knowledge base first: python -m backend.kb.loader"
    return row[0]


# --------------------------------------------------------------- the core two
def test_uncited_eligibility_claim_is_blocked(conn, business):
    v = Verifier(conn)
    verdict = v.check(Claim(
        business_id=business, agent="scheme",
        text="You qualify for a MUDRA Kishore loan up to 5 lakh.",
        cited_clause_ids=[], needs_citation=True,
    ))
    assert verdict.passed is False
    assert any("no citation" in r for r in verdict.reasons)


def test_cited_eligibility_claim_passes(conn, business, real_clause):
    v = Verifier(conn)
    verdict = v.check(Claim(
        business_id=business, agent="scheme",
        text="Based on the clause, Kishore covers loans above 50,000 up to 5 lakh.",
        cited_clause_ids=[real_clause], needs_citation=True,
    ))
    assert verdict.passed is True, verdict.reasons
    assert real_clause in verdict.valid_clause_ids


# --------------------------------------------------------------- the others
def test_fabricated_clause_id_is_blocked(conn, business):
    v = Verifier(conn)
    verdict = v.check(Claim(
        business_id=business, agent="scheme",
        text="You qualify.", cited_clause_ids=[999_999_999], needs_citation=True,
    ))
    assert verdict.passed is False
    assert any("does not exist" in r for r in verdict.reasons)


def test_superseded_clause_is_blocked(conn, business):
    """A stale rule version must not support a live claim."""
    cid = conn.execute(
        """INSERT INTO scheme_clauses
           (scheme_code, clause_ref, clause_text, source_url, source_domain,
            fetched_at, superseded_at)
           VALUES ('MUDRA_TARUN','loan_amount.old','Tarun up to 10 lakh',
                   'https://mudra.org.in/offerings','mudra.org.in',
                   now() - interval '400 days', now() - interval '1 day')
           RETURNING id"""
    ).fetchone()[0]
    conn.commit()
    v = Verifier(conn)
    verdict = v.check(Claim(
        business_id=business, agent="scheme",
        text="Tarun is capped at 10 lakh.", cited_clause_ids=[cid],
        needs_citation=True,
    ))
    assert verdict.passed is False
    assert any("superseded" in r for r in verdict.reasons)


def test_financial_claim_without_consent_is_blocked(conn, business, real_clause):
    v = Verifier(conn)
    verdict = v.check(Claim(
        business_id=business, agent="credit",
        text="Your cash flow supports a 3 lakh loan.",
        cited_clause_ids=[real_clause], needs_citation=True,
        used_financial_data=True, consent_purpose="loan_eligibility_assessment",
    ))
    assert verdict.passed is False
    assert any("no active consent" in r for r in verdict.reasons)


def test_financial_claim_with_consent_passes(conn, business, real_clause):
    conn.execute(
        "INSERT INTO consents (business_id, purpose, scope, expires_at, granted_via)"
        " VALUES (%s,'loan_eligibility_assessment','{bank_statement}',%s,'whatsapp_reply')",
        (business, datetime.now(timezone.utc) + timedelta(days=30)),
    )
    conn.commit()
    v = Verifier(conn)
    verdict = v.check(Claim(
        business_id=business, agent="credit",
        text="Your cash flow supports the requested amount.",
        cited_clause_ids=[real_clause], needs_citation=True,
        used_financial_data=True, consent_purpose="loan_eligibility_assessment",
    ))
    assert verdict.passed is True, verdict.reasons


def test_claim_asserting_a_filing_is_blocked(conn, business, real_clause):
    """The prepare/file line: we may say 'you can apply', never 'I filed it'."""
    v = Verifier(conn)
    verdict = v.check(Claim(
        business_id=business, agent="compliance",
        text="Done — I have filed your GST return with the portal.",
        cited_clause_ids=[real_clause], needs_citation=True,
    ))
    assert verdict.passed is False
    assert any("filing" in r or "prepares" in r for r in verdict.reasons)


def test_blocked_claim_is_recorded_with_reason(conn, business):
    v = Verifier(conn)
    v.check(Claim(business_id=business, agent="scheme",
                  text="You qualify.", cited_clause_ids=[], needs_citation=True))
    row = conn.execute(
        "SELECT verdict, blocked_reason FROM claims WHERE business_id=%s"
        " ORDER BY id DESC LIMIT 1", (business,),
    ).fetchone()
    assert row[0] == "blocked"
    assert row[1] and "citation" in row[1]
