"""
The Readiness agent: one ledger, two jobs.

The same bills that make the GST draft (problem 3.1) decide how ready a business
looks to a lender (problem 3.2). This agent scores "books and GST discipline"
from the ledger, with every point tied to a stated reason and, for every point
lost, the action that would win it back and how many points it is worth. It also
shows how many entries are holding up the GST draft, so fixing one fixes both.

What it measures: how complete and tidy the records are (GST rates on bills,
entries needing review, months without gaps, input credit captured, GSTIN and
Udyam on file). What it does NOT know: whether GST returns were filed on time,
which only the GST portal can say. It never claims that.

It reads financial data, so the Verifier refuses its claim without the owner's
active consent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import psycopg

from ..trace import Trace
from .compliance import ComplianceAgent
from .verifier import Claim

CONSENT_PURPOSE = "loan_eligibility_assessment"
_GSTIN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_UDYAM = re.compile(r"^UDYAM-[A-Z]{2}-\d{2}-\d{7}$", re.I)


@dataclass
class Factor:
    key: str
    label: str
    max_points: int
    points: int
    reason: str
    action: str

    @property
    def gain(self) -> int:
        return self.max_points - self.points


@dataclass
class Readiness:
    score: int
    band: str                       # ready | getting_there | early
    entries: int
    gst_issues: int
    factors: list[Factor] = field(default_factory=list)
    limitation: str = ("Measures how complete and tidy the books are. It cannot see whether GST returns were "
                       "filed on time; only the GST portal can show that.")

    def top_actions(self, n: int = 3) -> list[Factor]:
        return [f for f in sorted(self.factors, key=lambda f: -f.gain) if f.gain > 0][:n]


def _months(first, last) -> int:
    return (last.year - first.year) * 12 + last.month - first.month + 1


class ReadinessAgent:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def assess(self, business_id: int, *, trace: Optional[Trace] = None) -> tuple[Readiness, Claim]:
        if trace:
            trace.emit("readiness", "agent_start", {"business_id": business_id})
        c = self.conn
        gstin, udyam = c.execute("SELECT gstin, udyam_number FROM businesses WHERE id=%s", (business_id,)).fetchone()
        rows = c.execute(
            "SELECT entry_date, direction, gst_rate_bp, needs_review FROM ledger_entries WHERE business_id=%s",
            (business_id,)).fetchall()
        n = len(rows)
        factors: list[Factor] = []

        def share(num: int, den: int) -> float:
            return num / den if den else 0.0

        counted = [r for r in rows if not r[3]]
        with_rate = sum(1 for r in counted if r[2] is not None)
        s = share(with_rate, len(counted))
        factors.append(Factor("gst_on_bills", "GST rate on every bill", 30, round(30 * s),
                              f"{with_rate} of {len(counted)} entries carry a GST rate" if counted else "no entries yet",
                              "Ask suppliers for GST invoices and make sure each bill shows its GST rate."))
        clean = n - len(counted)
        s = share(len(counted), n)
        factors.append(Factor("clean_entries", "Entries confirmed (none left in review)", 20, round(20 * s),
                              f"{clean} of {n} entries still need your review" if n else "no entries yet",
                              "Open the entries marked for review and confirm or correct them."))
        months_with = {(r[0].year, r[0].month) for r in counted}
        if months_with:
            first, last = min(r[0] for r in counted), max(r[0] for r in counted)
            span = _months(first, last)
            s = share(len(months_with), span)
            reason = f"records in {len(months_with)} of {span} months"
        else:
            s, reason = 0.0, "no dated entries yet"
        factors.append(Factor("continuity", "Books kept month after month", 15, round(15 * s), reason,
                              "Add bills for the months with no records so there are no gaps."))
        purchases = [r for r in counted if r[1] == "out"]
        itc = sum(1 for r in purchases if (r[2] or 0) > 0)
        s = share(itc, len(purchases))
        factors.append(Factor("itc_captured", "Input tax credit captured on purchases", 15, round(15 * s),
                              f"{itc} of {len(purchases)} purchases carry GST you can claim" if purchases else "no purchases yet",
                              "Buy from GST-registered suppliers and keep their invoices."))
        g_ok = bool(gstin and _GSTIN.match(gstin.strip().upper()))
        u_ok = bool(udyam and _UDYAM.match(udyam.strip()))
        factors.append(Factor("identity", "GSTIN and Udyam on file", 20, (10 if g_ok else 0) + (10 if u_ok else 0),
                              ("GSTIN on file" if g_ok else "no GSTIN") + ", " + ("Udyam on file" if u_ok else "no Udyam"),
                              "Add your GSTIN in the profile and upload your Udyam certificate."))

        gst = ComplianceAgent(c, None).prepare_gstr3b(business_id, min(r[0] for r in rows), max(r[0] for r in rows),
                                                      trace=trace, explain=False) if rows else None
        score = min(100, sum(f.points for f in factors))
        band = "ready" if score >= 75 else "getting_there" if score >= 50 else "early"
        view = Readiness(score, band, n, len(gst.issues) if gst else 0, factors)
        claim = Claim(business_id=business_id, agent="readiness",
                      text=f"Books and GST discipline: {band} ({score}/100) from {n} entries.",
                      needs_citation=False, used_financial_data=True, consent_purpose=CONSENT_PURPOSE)
        if trace:
            trace.emit("readiness", "tool_result", {"score": score, "band": band, "gst_issues": view.gst_issues,
                                                    "factors": [(f.key, f.points) for f in factors]}, tool_name="discipline")
            trace.emit("readiness", "agent_end", {"band": band, "score": score})
        return view, claim


def to_dict(v: Readiness) -> dict:
    return {"score": v.score, "band": v.band, "entries": v.entries, "gst_issues": v.gst_issues,
            "limitation": v.limitation,
            "factors": [{"key": f.key, "label": f.label, "max": f.max_points, "points": f.points, "reason": f.reason,
                         "action": f.action, "gain": f.gain} for f in v.factors]}


_BAND = {"ready": "ready to approach a lender", "getting_there": "getting there", "early": "still early"}


def reply_text(v: Readiness, affordability: Optional[dict] = None) -> str:
    parts = [f"Your books and GST discipline score is {v.score} out of 100 ({_BAND[v.band]}), from {v.entries} entries."]
    if affordability:
        parts.append(f"Affordability from your cash flow: {affordability['band']} (score {affordability['score']}/100), "
                     f"a cautious instalment up to Rs {affordability['emi']:,.0f} a month.")
    for f in v.top_actions(3):
        parts.append(f"+{f.gain} points: {f.action} ({f.reason}).")
    if v.gst_issues:
        parts.append(f"{v.gst_issues} {'entry is' if v.gst_issues == 1 else 'entries are'} also holding up your GST draft, so fixing {'it' if v.gst_issues == 1 else 'them'} helps both.")
    parts.append(v.limitation)
    return "\n\n".join(parts)
