"""
The full journey: one business, all the way to a lender-ready file.

This is the orchestrator that chains the agents the way the real product would
across a month:
    ledger (already captured) -> compliance (GST draft) -> scheme (eligibility,
    cited) -> credit (affordability, consent-gated) -> document (the packet)

Every step is verified before it contributes, and the whole run is one trace.

    python -m backend.orchestrator.journey            # runs for the demo business
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import psycopg

from ..agents.compliance import ComplianceAgent
from ..agents.credit import CreditAgent
from ..agents.document import DocumentAgent
from ..agents.scheme import SchemeAgent
from ..agents.verifier import Verifier
from ..config import DBSettings
from ..llm import LLM
from ..trace import Trace, print_run_tree

DEMO_PHONE = "+919900000001"


def run_journey(
    conn: psycopg.Connection, business_id: int, llm: LLM,
    *, question: str = "What loan can I get and am I ready?",
    scheme: str = "MUDRA_KISHORE", trace: Optional[Trace] = None,
) -> str:
    trace = trace or Trace(business_id=business_id)
    trace.attach_db(conn)
    trace.emit("router", "message_in", {"text": question})

    verifier = Verifier(conn)

    # 1. compliance: GST draft for the current month window
    trace.emit("router", "route", {"to": "compliance"})
    draft = ComplianceAgent(conn, llm).prepare_gstr3b(
        business_id, date(2026, 1, 1), date(2026, 12, 31),
        trace=trace, explain=False)

    # 2. scheme: cited eligibility
    trace.emit("router", "route", {"to": "scheme"})
    answer, scheme_claim = SchemeAgent(conn, llm).answer(
        business_id, question, scheme, trace=trace)
    scheme_verdict = verifier.check(scheme_claim)
    matched = scheme if scheme_verdict.passed else None

    # 3. credit: affordability, consent-gated
    trace.emit("router", "route", {"to": "credit"})
    view, credit_claim = CreditAgent(conn).assess(business_id, trace=trace)
    credit_verdict = verifier.check(credit_claim)
    credit_ok = credit_verdict.passed

    # 4. document: assemble the file
    trace.emit("router", "route", {"to": "document"})
    loan_file = DocumentAgent(conn).assemble(
        business_id,
        gst_net_payable_paise=draft.net_payable_paise,
        gst_issues=len(draft.issues),
        matched_scheme=matched,
        scheme_citation_ids=scheme_verdict.valid_clause_ids,
        credit_band=view.band if credit_ok else None,
        credit_score=view.score if credit_ok else None,
        affordable_emi_paise=view.suggested_emi_paise if credit_ok else 0,
        trace=trace)

    rendered = DocumentAgent.render(loan_file)
    trace.emit("router", "message_out", {"completeness_pct": loan_file.completeness_pct})
    return rendered


def main() -> None:
    db = DBSettings.from_env()
    llm = LLM()
    if not llm.health():
        print("Model unreachable; start the vLLM server.")
        return
    with psycopg.connect(db.conninfo) as conn:
        bid = conn.execute(
            "SELECT id FROM businesses WHERE whatsapp_phone=%s", (DEMO_PHONE,)
        ).fetchone()
        if not bid:
            print("Demo business not found; run a ledger/credit seed first.")
            return
        trace = Trace(business_id=bid[0])
        rendered = run_journey(conn, bid[0], llm, trace=trace)
        print("\n" + rendered + "\n")
        print_run_tree(str(trace.trace_id))


if __name__ == "__main__":
    main()
