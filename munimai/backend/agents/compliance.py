"""
The Compliance agent — prepares a GST summary (problem statement 3.1).

It produces a simplified GSTR-3B-style draft from the ledger:
  - output tax   : GST collected on sales (direction 'in')
  - input credit : GST paid on purchases (direction 'out'), i.e. ITC
  - net payable  : output tax minus input credit (carried forward if negative)

Design rules that keep it trustworthy:
- ALL arithmetic is done in integer paise, in Python, never by the model. The
  model scored 0.60 on code/maths in our tests; a GST figure must be exact.
- amount_paise on a ledger entry is the GROSS (tax-inclusive) total, so tax is
  extracted as gross - gross/(1+rate), the standard back-calculation.
- Entries flagged needs_review, or taxable entries with no GST rate, are listed
  as issues the owner must resolve BEFORE filing — not silently included.
- The output is explicitly a DRAFT. The agent never claims anything was filed;
  the verifier blocks any such wording.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import psycopg

from ..llm import LLM
from ..trace import Trace


def _tax_component_paise(gross_paise: int, rate_bp: int) -> int:
    """GST inside a tax-inclusive gross amount, in paise. Integer maths only.
    gross = taxable * (1 + rate); tax = gross - taxable = gross*rate/(10000+rate)."""
    if rate_bp <= 0:
        return 0
    return round(gross_paise * rate_bp / (10000 + rate_bp))


@dataclass
class GSTIssue:
    entry_id: int
    reason: str


@dataclass
class GSTDraft:
    period: str
    outward_taxable_paise: int       # taxable value of sales
    output_tax_paise: int            # GST on sales
    inward_taxable_paise: int        # taxable value of purchases
    input_credit_paise: int          # ITC on purchases
    net_payable_paise: int           # output_tax - ITC, floored at 0
    credit_carry_forward_paise: int  # if ITC exceeded output tax
    sales_count: int
    purchase_count: int
    issues: list[GSTIssue] = field(default_factory=list)
    plain_summary: str = ""          # model-written, for the owner


class ComplianceAgent:
    def __init__(self, conn: psycopg.Connection, llm: Optional[LLM] = None) -> None:
        self.conn = conn
        self.llm = llm

    def prepare_gstr3b(
        self, business_id: int, period_start: date, period_end: date,
        *, trace: Optional[Trace] = None, explain: bool = True,
    ) -> GSTDraft:
        period = f"{period_start.isoformat()}..{period_end.isoformat()}"
        if trace:
            trace.emit("compliance", "agent_start", {"period": period})

        rows = self.conn.execute(
            """SELECT id, direction, amount_paise, gst_rate_bp, category, needs_review
               FROM ledger_entries
               WHERE business_id = %s AND entry_date BETWEEN %s AND %s
               ORDER BY entry_date""",
            (business_id, period_start, period_end),
        ).fetchall()

        out_taxable = out_tax = in_taxable = in_tax = 0
        sales = purchases = 0
        issues: list[GSTIssue] = []

        for eid, direction, gross, rate_bp, category, needs_review in rows:
            if needs_review:
                issues.append(GSTIssue(eid, "entry flagged for review; excluded"))
                continue
            if rate_bp is None:
                # a taxable line with no rate can't be placed correctly
                issues.append(GSTIssue(eid, f"no GST rate on '{category}'; excluded"))
                continue
            tax = _tax_component_paise(gross, rate_bp)
            taxable = gross - tax
            if direction == "in":        # a sale -> output tax
                out_taxable += taxable; out_tax += tax; sales += 1
            else:                        # a purchase -> input credit (ITC)
                in_taxable += taxable; in_tax += tax; purchases += 1

        net = out_tax - in_tax
        net_payable = max(net, 0)
        carry = max(-net, 0)

        if trace:
            trace.emit("compliance", "tool_result",
                       {"output_tax_paise": out_tax, "itc_paise": in_tax,
                        "net_payable_paise": net_payable, "issues": len(issues)},
                       tool_name="gstr3b.compute")

        draft = GSTDraft(
            period=period, outward_taxable_paise=out_taxable, output_tax_paise=out_tax,
            inward_taxable_paise=in_taxable, input_credit_paise=in_tax,
            net_payable_paise=net_payable, credit_carry_forward_paise=carry,
            sales_count=sales, purchase_count=purchases, issues=issues,
        )

        if explain and self.llm is not None:
            draft.plain_summary = self._explain(draft, trace)
        if trace:
            trace.emit("compliance", "agent_end",
                       {"net_payable_rupees": net_payable / 100})
        return draft

    def _explain(self, d: GSTDraft, trace: Optional[Trace]) -> str:
        """The model turns the computed figures into a plain-language note. It is
        given the numbers; it must not change them, only explain."""
        facts = (
            f"Period: {d.period}. Sales: {d.sales_count} "
            f"(taxable Rs {d.outward_taxable_paise/100:.0f}, GST collected "
            f"Rs {d.output_tax_paise/100:.0f}). Purchases: {d.purchase_count} "
            f"(input credit Rs {d.input_credit_paise/100:.0f}). "
            f"Net GST to pay: Rs {d.net_payable_paise/100:.0f}. "
            f"Credit carried forward: Rs {d.credit_carry_forward_paise/100:.0f}. "
            f"Unresolved issues: {len(d.issues)}."
        )
        system = (
            "You explain a GST summary to a small business owner in simple terms. "
            "Use ONLY the figures given; do not invent or recompute any number. "
            "This is a DRAFT for review, not a filing — say so. Do not claim "
            "anything has been filed or submitted. 3-4 short sentences."
        )
        result = self.llm.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": facts}],
            max_tokens=220, trace=trace, agent="compliance",
        )
        return result.text.strip()
