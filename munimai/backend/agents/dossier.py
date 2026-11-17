"""
The Dossier agent: assembles the bank-ready evidence pack as a PDF.

Every figure is computed in code from the ledger and the stored documents; the
model never writes a number. Eligibility text comes only from the Scheme agent
after the Verifier has passed it, with its clause citations. The credit section
appears only when the owner's consent is on file. Original uploads are appended
only if the owner ticked them, and Aadhaar/PAN stay masked everywhere else.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import psycopg

from .. import docstore
from ..llm import LLM
from ..pdfdoc import append_originals, html_to_pdf, inr, stamp_footer, t
from ..trace import Trace
from .compliance import ComplianceAgent
from .credit import CreditAgent
from .document import DocumentAgent
from .scheme import SchemeAgent
from .verifier import Verifier

FOOTER = ("Prepared by MunimAI from documents supplied by the owner. A draft for the owner's review; "
          "not a bank decision or an approval.")

_REQUIRED = [
    ("udyam_certificate", "Udyam registration certificate"),
    ("gst_certificate", "GST registration certificate"),
    ("id_proof", "Identity proof (Aadhaar / PAN)"),
    ("bank_statement", "Bank statement (last 6 months)"),
    ("invoice", "Sales and purchase bills"),
]
_KIND_LABEL = {k: v for k, v in _REQUIRED} | {"other": "Other document"}


def monthly_rows(conn: psycopg.Connection, business_id: int) -> list[tuple[str, int, int]]:
    rows = conn.execute(
        """SELECT to_char(date_trunc('month', entry_date), 'Mon YYYY'),
                  COALESCE(SUM(amount_paise) FILTER (WHERE direction='in'),0)::bigint,
                  COALESCE(SUM(amount_paise) FILTER (WHERE direction='out'),0)::bigint
           FROM ledger_entries WHERE business_id=%s AND needs_review=FALSE
           GROUP BY date_trunc('month', entry_date)
           ORDER BY date_trunc('month', entry_date) DESC LIMIT 12""", (business_id,)).fetchall()
    return [(r[0], int(r[1]), int(r[2])) for r in reversed(rows)]


class DossierAgent:
    def __init__(self, conn: psycopg.Connection, llm: LLM) -> None:
        self.conn = conn
        self.llm = llm

    def build(self, business_id: int, *, trace: Optional[Trace] = None) -> bytes:
        c = self.conn
        if trace:
            trace.emit("dossier", "agent_start", {"business_id": business_id})
        biz = c.execute("SELECT display_name, gstin, udyam_number FROM businesses WHERE id=%s",
                        (business_id,)).fetchone()
        name, gstin, udyam = biz
        months = monthly_rows(c, business_id)
        docs = docstore.list_docs(c, business_id)

        # GST summary over the books' date range (numbers computed in code)
        rng = c.execute("SELECT min(entry_date), max(entry_date) FROM ledger_entries WHERE business_id=%s",
                        (business_id,)).fetchone()
        gst = None
        if rng[0]:
            gst = ComplianceAgent(c, self.llm).prepare_gstr3b(business_id, rng[0], rng[1],
                                                              trace=trace, explain=False)

        # eligibility: the Scheme agent plus the Verifier, never free text
        verifier = Verifier(c)
        scheme_text, matched, clause_ids = "", None, []
        try:
            ans, claim = SchemeAgent(c, self.llm).answer(
                business_id, "Which government loan scheme could suit a small business like mine, "
                "and what are the eligibility rules?", trace=trace)
            verdict = verifier.check(claim)
            if verdict.passed:
                scheme_text, matched, clause_ids = ans.text, ans.scheme_code, verdict.valid_clause_ids
        except Exception:
            pass
        clauses = c.execute("SELECT clause_ref, source_url FROM scheme_clauses WHERE id=ANY(%s)",
                            (clause_ids,)).fetchall() if clause_ids else []

        # credit view only with consent (the Verifier enforces it)
        view, credit_ok = None, False
        try:
            view, credit_claim = CreditAgent(c).assess(business_id, trace=trace)
            credit_ok = verifier.check(credit_claim).passed
        except Exception:
            pass

        loan = DocumentAgent(c).assemble(
            business_id,
            gst_net_payable_paise=gst.net_payable_paise if gst else 0,
            gst_issues=len(gst.issues) if gst else 0,
            matched_scheme=matched, scheme_citation_ids=clause_ids,
            credit_band=view.band if (view and credit_ok) else None,
            credit_score=view.score if (view and credit_ok) else None,
            affordable_emi_paise=view.suggested_emi_paise if (view and credit_ok) else 0, trace=trace)

        h = [f"<h1>Business loan file</h1><p class='muted'>{t(name)} &middot; prepared {date.today():%d %B %Y}</p>",
             "<div class='note'>This file was prepared by MunimAI from the owner's own books and documents. "
             "It is a draft for the owner to review before sharing. It is not an application, a bank decision "
             "or an approval.</div>",
             f"<h2>Business profile</h2><table><tr><td>Business name</td><td>{t(name)}</td></tr>"
             f"<tr><td>GSTIN</td><td>{t(gstin) if gstin else '<span class=blank>not provided</span>'}</td></tr>"
             f"<tr><td>Udyam number</td><td>{t(udyam) if udyam else '<span class=blank>not provided</span>'}</td></tr>"
             f"<tr><td>Months of books</td><td>{loan.months_of_books}</td></tr>"
             f"<tr><td>File completeness</td><td>{loan.completeness_pct}%</td></tr></table>"]

        if months:
            tin, tout = sum(m[1] for m in months), sum(m[2] for m in months)
            rows = "".join(f"<tr><td>{m[0]}</td><td class='r'>{inr(m[1])}</td><td class='r'>{inr(m[2])}</td>"
                           f"<td class='r'>{inr(m[1]-m[2])}</td></tr>" for m in months)
            h.append("<h2>Income and expenses (from the owner's books)</h2><table><tr><th>Month</th>"
                     "<th class='r'>Money in</th><th class='r'>Money out</th><th class='r'>Net</th></tr>"
                     f"{rows}<tr><th>Total</th><th class='r'>{inr(tin)}</th><th class='r'>{inr(tout)}</th>"
                     f"<th class='r'>{inr(tin-tout)}</th></tr></table>")
        else:
            h.append("<h2>Income and expenses</h2><p class='blank'>No entries in the books yet.</p>")

        if gst:
            h.append("<h2>GST summary (draft, not filed)</h2><table>"
                     f"<tr><td>Tax collected on sales</td><td class='r'>{inr(gst.output_tax_paise)}</td></tr>"
                     f"<tr><td>Input tax credit on purchases</td><td class='r'>{inr(gst.input_credit_paise)}</td></tr>"
                     f"<tr><td>Net payable</td><td class='r'>{inr(gst.net_payable_paise)}</td></tr>"
                     f"<tr><td>Entries needing review</td><td class='r'>{len(gst.issues)}</td></tr></table>")

        if scheme_text:
            cites = "".join(f"<li>{t(r[0])} &mdash; {t(r[1])}</li>" for r in clauses)
            h.append(f"<h2>Scheme fit</h2><p>{t(scheme_text)}</p><p class='muted'>Official clauses relied on:</p><ul>{cites}</ul>")
        else:
            h.append("<h2>Scheme fit</h2><p class='muted'>No scheme answer passed the citation check, so none is stated.</p>")

        if credit_ok and view:
            h.append("<h2>Affordability</h2><table>"
                     f"<tr><td>Average monthly money in</td><td class='r'>{inr(view.avg_monthly_inflow_paise)}</td></tr>"
                     f"<tr><td>Average monthly net</td><td class='r'>{inr(view.avg_monthly_net_paise)}</td></tr>"
                     f"<tr><td>Trend</td><td class='r'>{t(view.trend)}</td></tr>"
                     f"<tr><td>Cautious monthly instalment</td><td class='r'>{inr(view.suggested_emi_paise)}</td></tr></table>"
                     f"<p class='muted'>{t(view.limitation)}</p>")
        else:
            h.append("<h2>Affordability</h2><p class='muted'>Not included: the owner has not given consent "
                     "for a credit assessment.</p>")

        have = {d["kind"] for d in docs}
        check = "".join(f"<tr><td>{lbl}</td><td class='{'ok' if k in have else 'no'}'>"
                        f"{'Present' if k in have else 'Missing'}</td></tr>" for k, lbl in _REQUIRED)
        h.append(f"<h2>Proof checklist</h2><table><tr><th>Document</th><th>Status</th></tr>{check}</table>")
        if docs:
            lines = "".join(
                f"<tr><td>{t(d['filename'])}</td><td>{_KIND_LABEL.get(d['kind'], d['kind'])}</td>"
                f"<td>{t(', '.join(f'{k}: {v}' for k, v in (d['facts'] or {}).items()))}</td>"
                f"<td>{'Attached' if d['include'] else 'Not attached'}</td></tr>" for d in docs)
            h.append("<h2>Documents on file</h2><table><tr><th>File</th><th>Type</th><th>Details found</th><th>In this file</th></tr>"
                     f"{lines}</table><p class='muted'>ID numbers are masked. Details read by OCR may contain errors; check them against the originals.</p>")
        if loan.gaps:
            h.append("<h2>Still needed</h2><ul>" + "".join(f"<li>{t(g)}</li>" for g in loan.gaps) + "</ul>")

        pdf = html_to_pdf("".join(h))
        attach = [(r[0], r[1]) for r in c.execute(
            "SELECT path, filename FROM documents WHERE business_id=%s AND include_in_bank_file ORDER BY id",
            (business_id,)).fetchall()]
        if attach:
            pdf = append_originals(pdf, attach)
        if trace:
            trace.emit("dossier", "agent_end", {"pages_attached": len(attach), "completeness": loan.completeness_pct})
        return stamp_footer(pdf, FOOTER)
