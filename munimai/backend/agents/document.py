"""
The Document agent - assembles the lender-ready loan file (PS 3.2 deliverable).

This is the thing MunimAI actually hands a lender: one packet that pulls
together what the other agents produced -
  - business identity (Udyam, GSTIN)
  - a cash-flow summary from the ledger
  - GST compliance status (the latest GSTR-3B draft figures)
  - the matched scheme with its cited clauses
  - the credit affordability view with its reasons and the consent reference

Its second job is gap detection: it lists what a bank would still need before
it could act. An incomplete file is a failed outcome for a lender, so naming the
gap precisely is the point - "half the sorting arrives done" only helps if the
missing half is spelled out.

It asserts nothing as filed or submitted; it prepares a file for the owner to
submit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import psycopg

from ..trace import Trace


@dataclass
class LoanFile:
    business_name: str
    gstin: Optional[str]
    udyam: Optional[str]
    months_of_books: int
    avg_monthly_income_rupees: float
    gst_net_payable_rupees: float
    gst_issues: int
    matched_scheme: Optional[str]
    scheme_citations: list[str]
    credit_band: Optional[str]
    credit_score: Optional[int]
    affordable_emi_rupees: float
    consent_on_file: bool
    completeness_pct: int
    gaps: list[str] = field(default_factory=list)


class DocumentAgent:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def assemble(
        self,
        business_id: int,
        *,
        gst_net_payable_paise: int = 0,
        gst_issues: int = 0,
        matched_scheme: Optional[str] = None,
        scheme_citation_ids: Optional[list[int]] = None,
        credit_band: Optional[str] = None,
        credit_score: Optional[int] = None,
        affordable_emi_paise: int = 0,
        trace: Optional[Trace] = None,
    ) -> LoanFile:
        if trace:
            trace.emit("document", "agent_start", {"business_id": business_id})

        biz = self.conn.execute(
            "SELECT display_name, gstin, udyam_number FROM businesses WHERE id=%s",
            (business_id,),
        ).fetchone()
        name, gstin, udyam = (biz or ("Unknown", None, None))

        stats = self.conn.execute(
            """SELECT count(DISTINCT date_trunc('month', entry_date)),
                      COALESCE(AVG(amount_paise) FILTER (WHERE direction='in'),0)
               FROM ledger_entries
               WHERE business_id=%s AND needs_review=FALSE""",
            (business_id,),
        ).fetchone()
        months = int(stats[0] or 0)
        avg_income_paise = int(stats[1] or 0)

        consent = self.conn.execute(
            "SELECT has_active_consent(%s,'loan_eligibility_assessment')",
            (business_id,),
        ).fetchone()[0]

        citations: list[str] = []
        if scheme_citation_ids:
            rows = self.conn.execute(
                "SELECT clause_ref, source_url FROM scheme_clauses WHERE id=ANY(%s)",
                (scheme_citation_ids,),
            ).fetchall()
            citations = [f"{r[0]} ({r[1]})" for r in rows]

        # --- gap detection: what a bank would still need ---
        gaps: list[str] = []
        if not udyam:
            gaps.append("Udyam registration number missing")
        if not gstin:
            gaps.append("GSTIN missing")
        if months < 3:
            gaps.append(f"only {months} month(s) of books; most lenders want 6+")
        if gst_issues:
            gaps.append(f"{gst_issues} GST entr(ies) need review before filing")
        if not consent:
            gaps.append("owner consent for financial data not yet given")
        if not matched_scheme:
            gaps.append("no scheme matched yet")
        if not credit_band or credit_band in ("thin", "insufficient"):
            gaps.append("credit profile is thin; more transaction history would help")

        # completeness: fraction of the core checklist satisfied
        checklist = [
            bool(udyam), bool(gstin), months >= 6, gst_issues == 0, bool(consent),
            bool(matched_scheme), credit_band in ("fair", "strong"),
        ]
        completeness = round(100 * sum(checklist) / len(checklist))

        loan_file = LoanFile(
            business_name=name, gstin=gstin, udyam=udyam,
            months_of_books=months,
            avg_monthly_income_rupees=avg_income_paise / 100,
            gst_net_payable_rupees=gst_net_payable_paise / 100,
            gst_issues=gst_issues,
            matched_scheme=matched_scheme, scheme_citations=citations,
            credit_band=credit_band, credit_score=credit_score,
            affordable_emi_rupees=affordable_emi_paise / 100,
            consent_on_file=bool(consent),
            completeness_pct=completeness, gaps=gaps,
        )
        if trace:
            trace.emit("document", "agent_end",
                       {"completeness_pct": completeness, "gaps": len(gaps)})
        return loan_file

    @staticmethod
    def render(f: LoanFile) -> str:
        """A plain text rendering of the file, for the demo / a banker to read."""
        lines = [
            "LENDER-READY LOAN FILE  (prepared by MunimAI - not submitted)",
            "=" * 58,
            f"Business      : {f.business_name}",
            f"Udyam / GSTIN : {f.udyam or '-'} / {f.gstin or '-'}",
            f"Books         : {f.months_of_books} months, avg income "
            f"Rs {f.avg_monthly_income_rupees:,.0f}/month",
            f"GST (3B draft): net payable Rs {f.gst_net_payable_rupees:,.0f}"
            + (f", {f.gst_issues} issue(s)" if f.gst_issues else ", clean"),
            f"Scheme        : {f.matched_scheme or '-'}",
        ]
        for c in f.scheme_citations:
            lines.append(f"                cite: {c}")
        lines += [
            f"Credit        : {f.credit_band or '-'} "
            f"({f.credit_score if f.credit_score is not None else '-'}/100), "
            f"affordable EMI up to Rs {f.affordable_emi_rupees:,.0f}/month",
            f"Consent       : {'on file' if f.consent_on_file else 'NOT given'}",
            "-" * 58,
            f"COMPLETENESS  : {f.completeness_pct}%",
        ]
        if f.gaps:
            lines.append("Still needed before a bank can act:")
            lines += [f"  - {g}" for g in f.gaps]
        else:
            lines.append("No gaps - ready for the owner to submit.")
        return "\n".join(lines)
