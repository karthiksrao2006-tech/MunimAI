"""
The Credit agent — turns the ledger into an explainable affordability view.

What this IS: a transparent scorecard. It reads real cash-flow signals from the
ledger (monthly inflow, net surplus, how many months the business has been
active, and trend) and builds a score where every point is attached to a stated
reason. A banker and the owner see the same reasons.

What this is NOT: a trained, black-box risk model. We have no repayment history
to train or calibrate one, and inventing a "SHAP score" would be dishonest. A
real deployment would calibrate these factors against actual loan performance
and test them for fairness across gender and region — stated plainly as a
limitation, not hidden.

Consent: this agent touches financial data, so it sets used_financial_data on
its claim. The verifier refuses the claim unless the business has active,
purpose-bound consent — the agent cannot bypass that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import psycopg

from ..trace import Trace
from .verifier import Claim

CONSENT_PURPOSE = "loan_eligibility_assessment"


@dataclass
class Factor:
    name: str
    points: int
    reason: str      # plain language, shown to owner and banker


@dataclass
class CreditView:
    months_active: int
    avg_monthly_inflow_paise: int
    avg_monthly_net_paise: int
    trend: str                       # 'rising' | 'steady' | 'falling'
    score: int                       # 0..100, sum of factor points
    band: str                        # 'strong' | 'fair' | 'thin' | 'insufficient'
    suggested_emi_paise: int         # affordable monthly instalment, conservative
    factors: list[Factor] = field(default_factory=list)
    limitation: str = ""


class CreditAgent:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def assess(
        self, business_id: int, as_of: Optional[date] = None,
        *, trace: Optional[Trace] = None,
    ) -> tuple[CreditView, Claim]:
        if trace:
            trace.emit("credit", "agent_start", {"business_id": business_id})

        # monthly inflow / outflow from the ledger (integer paise throughout)
        rows = self.conn.execute(
            """SELECT date_trunc('month', entry_date) AS m,
                      SUM(amount_paise) FILTER (WHERE direction='in')  AS inflow,
                      SUM(amount_paise) FILTER (WHERE direction='out') AS outflow
               FROM ledger_entries
               WHERE business_id = %s AND needs_review = FALSE
               GROUP BY 1 ORDER BY 1""",
            (business_id,),
        ).fetchall()

        # cast DB SUM() (Decimal) to int immediately — all maths stays integer paise
        months = [(r[0], int(r[1] or 0), int(r[2] or 0)) for r in rows]
        n = len(months)
        factors: list[Factor] = []

        if n == 0:
            view = CreditView(0, 0, 0, "steady", 0, "insufficient", 0,
                              [Factor("history", 0, "no ledger activity yet")],
                              "No cash-flow data to assess.")
            claim = self._claim(business_id, view)
            if trace:
                trace.emit("credit", "agent_end", {"band": "insufficient"})
            return view, claim

        inflows = [m[1] for m in months]
        nets = [m[1] - m[2] for m in months]
        avg_inflow = sum(inflows) // n
        avg_net = sum(nets) // n

        # trend: compare first half vs second half of the net series
        if n >= 2:
            half = n // 2
            first = sum(nets[:half]) / max(half, 1)
            second = sum(nets[half:]) / max(n - half, 1)
            trend = ("rising" if second > first * 1.1
                     else "falling" if second < first * 0.9 else "steady")
        else:
            trend = "steady"

        # --- factors, each with a reason ---
        # 1. track record (how many months of books exist)
        if n >= 6:
            factors.append(Factor("track_record", 30, f"{n} months of recorded activity"))
        elif n >= 3:
            factors.append(Factor("track_record", 20, f"{n} months of recorded activity"))
        else:
            factors.append(Factor("track_record", 8,
                                   f"only {n} month(s) of activity; thin history"))

        # 2. monthly income level
        inflow_rupees = avg_inflow / 100
        if inflow_rupees >= 50000:
            factors.append(Factor("income", 25,
                                   f"avg monthly income Rs {inflow_rupees:,.0f}"))
        elif inflow_rupees >= 20000:
            factors.append(Factor("income", 18,
                                   f"avg monthly income Rs {inflow_rupees:,.0f}"))
        elif inflow_rupees > 0:
            factors.append(Factor("income", 10,
                                   f"modest avg monthly income Rs {inflow_rupees:,.0f}"))
        else:
            factors.append(Factor("income", 0, "no recorded income"))

        # 3. surplus (does money left over after costs)
        if avg_net > 0:
            margin = avg_net / avg_inflow if avg_inflow else 0
            pts = 25 if margin >= 0.3 else 18 if margin >= 0.15 else 10
            factors.append(Factor("surplus", pts,
                                   f"positive monthly surplus Rs {avg_net/100:,.0f} "
                                   f"({margin*100:.0f}% of income)"))
        else:
            factors.append(Factor("surplus", 0,
                                   "no monthly surplus; costs equal or exceed income"))

        # 4. trend
        factors.append(Factor("trend",
                              15 if trend == "rising" else 10 if trend == "steady" else 3,
                              f"income trend is {trend}"))

        score = min(sum(f.points for f in factors), 100)
        band = ("strong" if score >= 70 else "fair" if score >= 50
                else "thin" if score >= 30 else "insufficient")

        # conservative affordable EMI: a fraction of average surplus, never of income
        suggested_emi = max(int(avg_net * 0.4), 0) if avg_net > 0 else 0

        view = CreditView(
            months_active=n, avg_monthly_inflow_paise=avg_inflow,
            avg_monthly_net_paise=avg_net, trend=trend, score=score, band=band,
            suggested_emi_paise=suggested_emi, factors=factors,
            limitation=("Transparent affordability scorecard from cash flow, not a "
                        "trained risk model. A lender would calibrate these factors "
                        "against real repayment data and test them for fairness "
                        "before relying on them."),
        )
        if trace:
            trace.emit("credit", "tool_result",
                       {"score": score, "band": band,
                        "suggested_emi_rupees": suggested_emi / 100,
                        "factors": [(f.name, f.points) for f in view.factors]},
                       tool_name="scorecard")
            trace.emit("credit", "agent_end", {"band": band, "score": score})
        return view, self._claim(business_id, view)

    def _claim(self, business_id: int, view: CreditView) -> Claim:
        top = max(view.factors, key=lambda f: f.points) if view.factors else None
        text = (f"Affordability band: {view.band} (score {view.score}/100). "
                f"Suggested affordable EMI up to Rs {view.suggested_emi_paise/100:,.0f} "
                f"per month." + (f" Main factor: {top.reason}." if top else ""))
        # credit uses financial data -> consent is required; needs_citation False
        # because this is an internal assessment, not a cited scheme rule.
        return Claim(
            business_id=business_id, agent="credit", text=text,
            needs_citation=False, used_financial_data=True,
            consent_purpose=CONSENT_PURPOSE,
        )
