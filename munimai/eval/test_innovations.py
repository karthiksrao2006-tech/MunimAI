"""
Tests for the three additions: sealed bank files, scheme fit (eligibility gaps)
and books/GST readiness. Deterministic, no model.

Run (from munimai/):  python -m pytest eval/test_innovations.py -v
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import psycopg
import pytest
from dotenv import load_dotenv

from backend import proof, schema_ext
from backend.agents import fit as fitmod
from backend.agents.fit import FitAgent
from backend.agents.readiness import CONSENT_PURPOSE, ReadinessAgent
from backend.agents.verifier import Verifier
from backend.pdfdoc import html_to_pdf

load_dotenv()


@pytest.fixture()
def conn():
    c = psycopg.connect(host=os.environ["PGHOST"], port=os.environ["PGPORT"], dbname=os.environ["PGDATABASE"],
                        user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"], connect_timeout=20)
    schema_ext.ensure(c)
    yield c
    c.rollback()
    c.close()


@pytest.fixture()
def bid(conn):
    phone = f"+9199{uuid.uuid4().int % 100_000_000:08d}"
    b = conn.execute("INSERT INTO businesses (whatsapp_phone, display_name) VALUES (%s,%s) RETURNING id",
                     (phone, "Innovation Test Unit")).fetchone()[0]
    conn.commit()
    return b


def _entry(conn, bid, d, direction, rupees, gst=1800, review=False, cat="sales"):
    return conn.execute(
        "INSERT INTO ledger_entries (business_id, entry_date, direction, amount_paise, category, gst_rate_bp, needs_review, source)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,'manual') RETURNING id", (bid, d, direction, rupees * 100, cat, gst, review)).fetchone()[0]


def _consent(conn, bid, on=True):
    if on:
        conn.execute("INSERT INTO consents (business_id, purpose, scope, expires_at, granted_via)"
                     " VALUES (%s,%s,%s, now() + interval '30 days','web_click')", (bid, CONSENT_PURPOSE, ["ledger"]))
    conn.commit()


# ------------------------------------------------------------------ sealed files
def test_seal_verifies_and_shows_totals(conn, bid):
    _entry(conn, bid, date(2026, 8, 3), "in", 10000)
    _entry(conn, bid, date(2026, 8, 9), "out", 4000)
    conn.commit()
    seal = proof.create_seal(conn, bid, "bank_file")
    v = proof.verify(conn, seal.token)
    assert v["intact"] and v["entries"] == 2
    assert v["total_in_paise"] == 1_000_000 and v["total_out_paise"] == 400_000
    assert v["live"] == {"added": 0, "removed": 0, "changed": 0}


def test_editing_the_books_after_sealing_is_shown(conn, bid):
    e = _entry(conn, bid, date(2026, 8, 3), "in", 10000)
    conn.commit()
    seal = proof.create_seal(conn, bid, "bank_file")
    conn.execute("UPDATE ledger_entries SET amount_paise=%s WHERE id=%s", (9_999_900, e))
    _entry(conn, bid, date(2026, 8, 20), "in", 500)
    conn.commit()
    v = proof.verify(conn, seal.token)
    assert v["intact"], "the sealed record itself is unchanged"
    assert v["live"]["changed"] == 1 and v["live"]["added"] == 1
    assert "Owner's live books differ" in proof.render(v, seal.token) or "live books differ" in proof.render(v, seal.token)


def test_tampering_with_the_sealed_record_breaks_the_seal(conn, bid):
    _entry(conn, bid, date(2026, 8, 3), "in", 10000)
    conn.commit()
    seal = proof.create_seal(conn, bid, "bank_file")
    conn.execute("UPDATE seals SET snapshot = jsonb_set(snapshot, '{entries,0,3}', '99999999') WHERE token=%s", (seal.token,))
    conn.commit()
    assert proof.verify(conn, seal.token)["intact"] is False


def test_pdf_must_be_the_issued_file(conn, bid):
    _entry(conn, bid, date(2026, 8, 3), "in", 10000)
    conn.commit()
    pdf = html_to_pdf("<h1>Business loan file</h1><p>test</p>")
    sealed = proof.seal_pdf(conn, bid, pdf, "bank_file")
    token = conn.execute("SELECT token FROM seals WHERE business_id=%s ORDER BY id DESC LIMIT 1", (bid,)).fetchone()[0]
    assert proof.pdf_matches(conn, token, sealed)
    assert not proof.pdf_matches(conn, token, sealed + b"\n% edited")
    assert not proof.pdf_matches(conn, token, pdf)


def test_unknown_token_is_not_found(conn):
    assert proof.verify(conn, "no-such-token") is None
    assert "Not found" in proof.render(None, "x")


# ------------------------------------------------------------------ scheme fit
WOMAN_NEW = {"activity": "manufacturing", "loan_amount": 1_500_000, "age": 34, "gender": "female", "category": "general",
             "education": "10th", "new_business": True, "availed_govt_subsidy": False, "entity_type": "proprietorship"}


def _by(fits):
    return {f.code: f for f in fits}


def test_fit_matches_published_conditions(conn, bid):
    f = _by(FitAgent(conn).evaluate(bid, WOMAN_NEW))
    assert f["PMEGP"].status in ("eligible", "almost")
    assert not any(c.status == "fail" for c in f["PMEGP"].criteria)
    assert f["STANDUP_INDIA"].status == "eligible"          # woman, new, Rs 15 lakh is inside 10 lakh to 1 crore
    assert f["STARTUP_INDIA"].status == "not_eligible"      # a proprietorship cannot be recognised
    assert any("Private Limited" in n for n in f["STARTUP_INDIA"].to_fix)


def test_fit_education_threshold_and_existing_business(conn, bid):
    a = dict(WOMAN_NEW, gender="male", education="below8", loan_amount=1_200_000)
    p = _by(FitAgent(conn).evaluate(bid, a))["PMEGP"]
    assert p.status == "not_eligible" and any("VIII" in b for b in p.blockers)
    small = _by(FitAgent(conn).evaluate(bid, dict(a, loan_amount=800_000)))["PMEGP"]
    assert not any(c.status == "fail" for c in small.criteria)
    old = _by(FitAgent(conn).evaluate(bid, dict(WOMAN_NEW, new_business=False)))["PMEGP"]
    assert old.status == "not_eligible"


def test_fit_missing_facts_are_unknown_not_guessed(conn, bid):
    f = _by(FitAgent(conn).evaluate(bid, {"activity": "service"}))
    assert f["PMEGP"].status == "almost"
    assert all(c.status in ("pass", "unknown", "todo") for c in f["PMEGP"].criteria)


def test_every_verdict_cites_current_clauses(conn, bid):
    fits = FitAgent(conn).evaluate(bid, WOMAN_NEW)
    for f in fits:
        if f.status in ("eligible", "not_eligible"):
            assert f.cited_clause_ids, f.code
    ids = [i for f in fits for i in f.cited_clause_ids]
    rows = conn.execute("SELECT count(*) FROM scheme_clauses WHERE id=ANY(%s) AND superseded_at IS NULL", (ids,)).fetchone()[0]
    assert rows == len(set(ids))


def test_fit_without_a_clause_is_withheld_not_asserted(conn, bid, monkeypatch):
    monkeypatch.setattr(FitAgent, "_clause", lambda self, s, r: None)
    f = _by(FitAgent(conn).evaluate(bid, WOMAN_NEW))
    assert f["STANDUP_INDIA"].status == "unverified"


def test_turnover_from_books_needs_consent(conn, bid):
    _entry(conn, bid, date.today(), "in", 50000)
    conn.commit()
    a = dict(WOMAN_NEW, entity_type="pvt_ltd", years_incorporated=2, innovative=True)
    t = [c for c in _by(FitAgent(conn).evaluate(bid, a))["STARTUP_INDIA"].criteria if "Turnover" in c.label][0]
    assert t.status == "unknown"
    _consent(conn, bid)
    t = [c for c in _by(FitAgent(conn).evaluate(bid, a))["STARTUP_INDIA"].criteria if "Turnover" in c.label][0]
    assert t.status == "pass"


def test_fit_reply_never_claims_filing(conn, bid):
    text = fitmod.reply_text(FitAgent(conn).evaluate(bid, WOMAN_NEW))
    assert "decides the application" in text
    assert Verifier(conn).check(__import__("backend.agents.verifier", fromlist=["Claim"]).Claim(
        business_id=bid, agent="fit", text=text, needs_citation=False)).passed


# ------------------------------------------------------------------ readiness
def test_readiness_blocked_without_consent(conn, bid):
    _entry(conn, bid, date(2026, 8, 3), "in", 10000)
    conn.commit()
    _view, claim = ReadinessAgent(conn).assess(bid)
    v = Verifier(conn).check(claim)
    assert not v.passed and any("consent" in r for r in v.reasons)


def test_readiness_rewards_tidy_books_and_names_the_fix(conn, bid):
    for m in (6, 7, 8, 9):
        _entry(conn, bid, date(2026, m, 5), "in", 20000)
        _entry(conn, bid, date(2026, m, 12), "out", 8000, cat="cloth")
    conn.execute("UPDATE businesses SET gstin=%s, udyam_number=%s WHERE id=%s",
                 ("29ABCDE1234F1Z5", "UDYAM-KA-03-0012345", bid))
    conn.commit()
    _consent(conn, bid)
    tidy, claim = ReadinessAgent(conn).assess(bid)
    assert Verifier(conn).check(claim).passed
    assert tidy.score == 100 and tidy.band == "ready" and not tidy.top_actions()

    _entry(conn, bid, date(2026, 11, 5), "in", 9000)                           # October is now a gap month
    _entry(conn, bid, date(2026, 11, 6), "out", 3000, gst=None, review=True)   # and one bill is unreviewed
    conn.commit()
    messy, _ = ReadinessAgent(conn).assess(bid)
    assert messy.score < tidy.score
    assert {f.key for f in messy.top_actions(5)} >= {"clean_entries", "continuity"}
    assert sum(f.points for f in messy.factors) == messy.score
    assert messy.gst_issues >= 1


def test_readiness_states_what_it_cannot_see(conn, bid):
    _entry(conn, bid, date(2026, 8, 3), "in", 10000)
    conn.commit()
    view, _ = ReadinessAgent(conn).assess(bid)
    assert "filed on time" in view.limitation


# ------------------------------------------------------------------ router (translated phrasings)
@pytest.mark.parametrize("q,intent", [
    ("Is my balance sheet ready for a loan?", "readiness"),
    ("Are my books of accounts ready for a loan?", "readiness"),
    ("How ready are my accounts for a bank loan?", "readiness"),
    ("What do I need to do to qualify?", "fit"),
    ("What do I have to do to be eligible for the Startup Plan?", "fit"),
    ("How do I become eligible for the Startup Plan?", "fit"),
    ("Can I get a MUDRA loan?", "eligibility"),
    ("Am I eligible for PMEGP?", "eligibility"),
])
def test_router_handles_translated_phrasings(q, intent):
    from backend.router import classify
    assert classify(q) == intent
