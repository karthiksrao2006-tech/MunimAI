"""
The Application agent: drafts what the bank asks you to submit.

A covering letter to the Branch Manager, a loan application filled from the
owner's profile, books and documents, a repayment illustration, a document
checklist and a declaration page. Every number is computed in code. Facts we do
not have stay as visible blanks and are listed first under "still needed".
The model only phrases the short purpose paragraph from supplied facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

import psycopg

from .. import docstore
from ..llm import LLM
from ..pdfdoc import html_to_pdf, inr, stamp_footer, t
from ..trace import Trace
from .dossier import monthly_rows

FOOTER = ("DRAFT prepared by MunimAI for the owner's review. Banks use their own forms and make their own "
          "decision. Nothing here has been submitted.")
BLANK = "<span class='blank'>________________</span>"

BANKS = ["State Bank of India", "Bank of Baroda", "Canara Bank", "Union Bank of India",
         "Punjab National Bank", "Karnataka Bank", "Karnataka Gramin Bank", "Other bank"]
SCHEMES = ["MUDRA Shishu", "MUDRA Kishore", "MUDRA Tarun", "PMEGP", "CGTMSE-covered term loan",
           "Stand-Up India", "PM SVANidhi", "Working capital (OD/CC)", "Other"]


@dataclass
class AppRequest:
    bank: str
    scheme: str
    amount_rupees: int
    purpose: str
    tenure_months: int = 36
    phone: str = ""
    address: str = ""


def emi(principal: float, annual_pct: float, months: int) -> float:
    r = annual_pct / 1200
    return principal / months if r == 0 else principal * r * (1 + r) ** months / ((1 + r) ** months - 1)


class ApplicationAgent:
    def __init__(self, conn: psycopg.Connection, llm: LLM,
                 translate: Optional[Callable[[str], str]] = None) -> None:
        self.conn, self.llm, self.translate = conn, llm, translate

    def _purpose_paragraph(self, req: AppRequest, name: str, months: int, avg_in: float) -> str:
        facts = (f"Business: {name}. Loan sought: Rs {req.amount_rupees:,} under {req.scheme} from {req.bank}. "
                 f"Purpose stated by the owner: {req.purpose}. Months of books: {months}. "
                 f"Average monthly receipts from the books: Rs {avg_in:,.0f}.")
        try:
            r = self.llm.chat([
                {"role": "system", "content": "Write 2 or 3 plain, respectful sentences for a bank loan "
                 "application explaining the purpose of the loan. Use ONLY the facts given. Do not add "
                 "numbers, promises, collateral claims or any fact that is not in the input."},
                {"role": "user", "content": facts}], max_tokens=160)
            text = r.text.strip()
            if text:
                return text
        except Exception:
            pass
        return f"The loan is requested for the following purpose: {req.purpose}."

    def build(self, business_id: int, req: AppRequest, *, trace: Optional[Trace] = None) -> bytes:
        c = self.conn
        if trace:
            trace.emit("application", "agent_start", {"bank": req.bank, "scheme": req.scheme,
                                                      "amount": req.amount_rupees})
        name, gstin, udyam = c.execute(
            "SELECT display_name, gstin, udyam_number FROM businesses WHERE id=%s", (business_id,)).fetchone()
        months = monthly_rows(c, business_id)
        turnover = sum(m[1] for m in months) / 100
        avg_in = turnover / len(months) if months else 0.0
        docs = docstore.list_docs(c, business_id)
        have = {d["kind"] for d in docs}
        consent = c.execute("SELECT has_active_consent(%s,'loan_eligibility_assessment')", (business_id,)).fetchone()[0]

        needed = []
        if not gstin:
            needed.append("GSTIN (if registered)")
        if not udyam:
            needed.append("Udyam registration number")
        if not req.phone:
            needed.append("Mobile number")
        if not req.address:
            needed.append("Business address")
        for k, lbl in (("id_proof", "Identity proof (Aadhaar / PAN)"), ("bank_statement", "Bank statement (6 months)"),
                       ("udyam_certificate", "Udyam certificate")):
            if k not in have:
                needed.append(lbl)
        needed += ["Existing loans, if any", "Years in this business", "Collateral details, if the bank asks"]

        purpose = self._purpose_paragraph(req, name, len(months), avg_in)
        today = f"{date.today():%d %B %Y}"
        amt = inr(req.amount_rupees, paise=False)

        letter = (f"<p>{today}</p><p>The Branch Manager<br>{t(req.bank)}<br>Branch: {BLANK}</p>"
                  f"<p><b>Subject: Application for a loan of {amt} under {t(req.scheme)}</b></p>"
                  f"<p>Respected Sir/Madam,</p>"
                  f"<p>I, the proprietor of {t(name)}, request a loan of {amt} under {t(req.scheme)} for a "
                  f"tenure of {req.tenure_months} months.</p><p>{t(purpose)}</p>"
                  f"<p>My business records for the last {len(months)} month(s) and the supporting documents are "
                  f"enclosed. I will gladly provide any further information you need.</p>"
                  f"<p>Yours faithfully,<br><br>{BLANK}<br>{t(name)}</p>")

        form = ("<table>"
                f"<tr><td>Applicant / business</td><td>{t(name)}</td></tr>"
                f"<tr><td>GSTIN</td><td>{t(gstin) if gstin else BLANK}</td></tr>"
                f"<tr><td>Udyam number</td><td>{t(udyam) if udyam else BLANK}</td></tr>"
                f"<tr><td>Mobile</td><td>{t(req.phone) if req.phone else BLANK}</td></tr>"
                f"<tr><td>Business address</td><td>{t(req.address) if req.address else BLANK}</td></tr>"
                f"<tr><td>Loan scheme</td><td>{t(req.scheme)}</td></tr>"
                f"<tr><td>Amount requested</td><td>{amt}</td></tr>"
                f"<tr><td>Tenure</td><td>{req.tenure_months} months</td></tr>"
                f"<tr><td>Purpose</td><td>{t(req.purpose)}</td></tr>"
                f"<tr><td>Turnover in the books ({len(months)} months)</td><td>{inr(turnover, paise=False)}</td></tr>"
                f"<tr><td>Average monthly receipts</td><td>{inr(avg_in, paise=False)}</td></tr>"
                f"<tr><td>Existing loans</td><td>{BLANK}</td></tr>"
                f"<tr><td>Years in business</td><td>{BLANK}</td></tr></table>")

        rows = "".join(f"<tr><td>{r}%</td><td class='r'>{inr(emi(req.amount_rupees, r, req.tenure_months), paise=False)}</td>"
                       f"<td class='r'>{inr(emi(req.amount_rupees, r, req.tenure_months) * req.tenure_months, paise=False)}</td></tr>"
                       for r in (9, 10, 11, 12))
        repay = ("<h2>Repayment illustration</h2><table><tr><th>If the yearly rate were</th>"
                 "<th class='r'>Monthly instalment</th><th class='r'>Total repaid</th></tr>"
                 f"{rows}</table><p class='muted'>Illustration only. The bank sets the actual rate and terms.</p>")
        if not consent:
            repay += "<p class='muted'>An affordability view is not included because no consent for a credit assessment is on file.</p>"

        lst = [("Identity proof (Aadhaar / PAN)", "id_proof"), ("Address proof", None),
               ("Udyam registration certificate", "udyam_certificate"), ("GST certificate, if registered", "gst_certificate"),
               ("Bank statement, last 6 months", "bank_statement"), ("Sales and purchase bills", "invoice"),
               ("Passport-size photographs", None), ("Quotation for machinery or stock, if applicable", None)]
        check = "".join(f"<tr><td>{a}</td><td class='{'ok' if k and k in have else 'no'}'>"
                        f"{'Present' if k and k in have else ('Missing' if k else 'Bring')}</td></tr>" for a, k in lst)
        portal = ("<p class='note'>Some schemes are applied for on an official portal rather than at the branch "
                  "(for example PMEGP, Udyam and Jan Samarth). This draft does not file anything: copy the fields "
                  "above across yourself and confirm the current list with your branch.</p>")

        decl = ("<h2>Declaration</h2><p>I declare that the information in this application is true and complete to the "
                "best of my knowledge, and I authorise the bank to verify it.</p>"
                f"<div class='sig'>Signature: {BLANK} &nbsp;&nbsp; Date: {BLANK} &nbsp;&nbsp; Place: {BLANK}</div>")

        body = ("<h1>Loan application pack</h1>"
                f"<p class='muted'>{t(name)} &middot; {t(req.bank)} &middot; {t(req.scheme)}</p>"
                "<div class='note'><b>Still needed from you:</b> " + "; ".join(t(x) for x in needed) + ".</div>"
                f"<h2>1. Covering letter</h2>{letter}<h2>2. Application details</h2>{form}"
                f"{repay}<h2>3. Documents to bring</h2><table><tr><th>Document</th><th>Status</th></tr>{check}</table>{portal}{decl}")
        if self.translate:
            try:
                body += f"<h2>Translation aid (the English pages above are the submission copy)</h2><p>{t(self.translate(purpose))}</p>"
            except Exception:
                pass
        if trace:
            trace.emit("application", "agent_end", {"blanks": len(needed)})
        return stamp_footer(html_to_pdf(body), FOOTER)
