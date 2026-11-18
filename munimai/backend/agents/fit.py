"""
The Fit agent: "what would make me eligible?"

For each scheme it checks the owner's facts against the published conditions
and says, per condition, whether the owner meets it, fails it, or it is not
known yet, with the official clause it comes from. For every failed condition
it states what would have to change.

Nothing here is written by a model. Each rule is a small deterministic check
bound to a clause in the knowledge base (resolved by scheme and clause_ref, so
it follows the current version of the rule). The result is then passed through
the Verifier: a scheme verdict that cannot cite a current clause is not shown.
The owner's own figures decide the outcome; the lender or portal decides the
application, and the answer says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import psycopg

from .verifier import Claim, Verifier

CONSENT_PURPOSE = "loan_eligibility_assessment"
LAKH, CRORE = 100_000, 10_000_000

EDU_RANK = {"none": 0, "below8": 1, "8th": 2, "10th": 3, "12th": 4, "graduate": 5}
EDU_LABEL = {"none": "no schooling", "below8": "below VIII standard", "8th": "VIII standard", "10th": "X standard",
             "12th": "XII standard", "graduate": "graduate"}

# What the form asks, in order (key, label, type, options)
FIELDS = [
    ("activity", "What does the business do?", "choice",
     [("manufacturing", "Making things"), ("service", "Service"), ("trading", "Buying and selling"), ("agri", "Farming")]),
    ("loan_amount", "How much do you want to borrow (Rs)?", "number", []),
    ("age", "Your age", "number", []),
    ("gender", "Gender", "choice", [("female", "Woman"), ("male", "Man"), ("other", "Other")]),
    ("category", "Social category", "choice", [("sc", "SC"), ("st", "ST"), ("obc", "OBC"), ("general", "General")]),
    ("education", "Education", "choice", [(k, v) for k, v in EDU_LABEL.items()]),
    ("new_business", "Is this a new business you have not started yet?", "bool", []),
    ("availed_govt_subsidy", "Have you taken a government subsidy or scheme loan before?", "bool", []),
    ("loan_last_5y", "Any government-scheme loan in the last 5 years?", "bool", []),
    ("street_vendor", "Are you a street vendor?", "bool", []),
    ("artisan_trade", "Do you work with hands and tools in a traditional trade (carpenter, potter, tailor, etc.)?", "bool", []),
    ("govt_service", "Are you or a family member in government service?", "bool", []),
    ("entity_type", "How is the business registered?", "choice",
     [("proprietorship", "Proprietorship / not registered"), ("partnership", "Registered partnership firm"),
      ("llp", "LLP"), ("pvt_ltd", "Private limited company"), ("cooperative", "Cooperative society"), ("other", "Other")]),
    ("years_incorporated", "Years since the business was incorporated or registered", "number", []),
    ("innovative", "Does it bring something new (a product, process or service)?", "bool", []),
    ("msme_category", "MSME size", "choice", [("micro", "Micro"), ("small", "Small"), ("medium", "Medium")]),
]


def _num(a: dict, k: str) -> Optional[float]:
    v = a.get(k)
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _bool(a: dict, k: str) -> Optional[bool]:
    v = a.get(k)
    if v in (True, "true", "yes", 1, "1"):
        return True
    if v in (False, "false", "no", 0, "0"):
        return False
    return None


def _rs(x: float) -> str:
    if x >= CRORE:
        return f"Rs {x / CRORE:g} crore"
    if x >= LAKH:
        return f"Rs {x / LAKH:g} lakh"
    return f"Rs {x:,.0f}"


@dataclass
class Rule:
    label: str
    clause: tuple[str, str]                 # (scheme_code, clause_ref)
    check: Callable[[dict, dict], tuple[str, str]]   # (attrs, ctx) -> (status, why)
    need: str                                # what would make it pass
    uses_books: bool = False


def _age_rule(minimum: int = 18):
    def f(a, c):
        v = _num(a, "age")
        if v is None:
            return "unknown", "Your age is not known yet."
        return ("pass", f"You are {v:g}.") if v >= minimum else ("fail", f"You are {v:g}; the minimum is {minimum}.")
    return f


def _new_unit(a, c):
    v = _bool(a, "new_business")
    if v is None:
        return "unknown", "Not known whether this is a new business."
    return ("pass", "A new project.") if v else ("fail", "An existing business is not eligible for new-unit assistance.")


def _no_subsidy(a, c):
    v = _bool(a, "availed_govt_subsidy")
    if v is None:
        return "unknown", "Not known whether you took a government subsidy earlier."
    return ("pass", "No earlier government subsidy.") if not v else ("fail", "You have availed a government subsidy before.")


def _pmegp_edu(a, c):
    amt, act, edu = _num(a, "loan_amount"), a.get("activity"), a.get("education")
    if amt is None or act not in ("manufacturing", "service", "trading"):
        return "unknown", "Needs the project cost and whether it is manufacturing or service/business."
    limit = 10 * LAKH if act == "manufacturing" else 5 * LAKH
    if amt <= limit:
        return "pass", f"At {_rs(amt)} (not above {_rs(limit)}) no education condition applies."
    if edu not in EDU_RANK:
        return "unknown", f"Above {_rs(limit)} you need at least VIII standard; your education is not known."
    return (("pass", f"Above {_rs(limit)} VIII standard is needed; you have {EDU_LABEL[edu]}.")
            if EDU_RANK[edu] >= EDU_RANK["8th"] else
            ("fail", f"Above {_rs(limit)} VIII standard is needed; you have {EDU_LABEL[edu]}."))


def _udyam(a, c):
    return ("pass", "Udyam number on file.") if c.get("udyam") else ("todo", "No Udyam number on file yet.")


def _nonfarm(a, c):
    act = a.get("activity")
    if act is None:
        return "unknown", "Not known what the business does."
    return (("pass", "A non-farm activity.") if act in ("manufacturing", "service", "trading")
            else ("fail", "Farming is outside PMMY; only allied activities are covered."))


def _within(lo, hi, name):
    def f(a, c):
        v = _num(a, "loan_amount")
        if v is None:
            return "unknown", "The loan amount is not known yet."
        if lo <= v <= hi:
            return "pass", f"{_rs(v)} is within {name}."
        return "fail", f"{_rs(v)} is outside {name}."
    return f


def _sc_st_or_woman(a, c):
    g, cat = a.get("gender"), a.get("category")
    if g == "female":
        return "pass", "Woman entrepreneur."
    if cat in ("sc", "st"):
        return "pass", f"{cat.upper()} entrepreneur."
    if g is None and cat is None:
        return "unknown", "Gender and social category are not known."
    if g is not None and cat is not None:
        return "fail", "The scheme is for SC/ST or women entrepreneurs."
    return "unknown", "Needs both gender and social category."


def _flag(key, want, ok_msg, bad_msg, unk_msg):
    def f(a, c):
        v = _bool(a, key)
        if v is None:
            return "unknown", unk_msg
        return ("pass", ok_msg) if v == want else ("fail", bad_msg)
    return f


def _entity(a, c):
    e = a.get("entity_type")
    if e is None:
        return "unknown", "Not known how the business is registered."
    return (("pass", "Registered in an eligible form.") if e in ("pvt_ltd", "partnership", "llp", "cooperative")
            else ("fail", "A proprietorship or unregistered firm cannot be recognised as a startup."))


def _years(a, c):
    v = _num(a, "years_incorporated")
    if v is None:
        return "unknown", "Years since incorporation are not known."
    return ("pass", f"{v:g} years, within 10.") if v <= 10 else ("fail", f"{v:g} years; the limit is 10.")


def _turnover(a, c):
    t = c.get("turnover_rupees")
    if t is None:
        return "unknown", "No turnover available (add bills and allow me to read your books)."
    return (("pass", f"Your recorded turnover is {_rs(t)}, under Rs 200 crore.") if t < 200 * CRORE
            else ("fail", f"Your recorded turnover is {_rs(t)}, above Rs 200 crore."))


def _size(a, c):
    v = a.get("msme_category")
    if v is None:
        return "unknown", "MSME size is not known."
    return ("pass", f"A {v} enterprise.") if v in ("micro", "small") else ("fail", "CGTMSE covers micro and small enterprises.")


SCHEMES: dict[str, dict] = {
    "MUDRA": {"name": "MUDRA loan (PMMY)", "rules": [
        Rule("Non-farm income-generating activity", ("MUDRA_GENERAL", "eligibility.who_can_apply"), _nonfarm,
             "PMMY is for manufacturing, processing, trading or services."),
        Rule("Loan need up to Rs 20 lakh", ("MUDRA_GENERAL", "eligibility.who_can_apply"),
             _within(0, 20 * LAKH, "Rs 20 lakh"), "Keep the loan at Rs 20 lakh or less."),
    ]},
    "PMEGP": {"name": "PMEGP (subsidy-linked loan)", "rules": [
        Rule("Age above 18", ("PMEGP", "faq.eligibility_conditions_of_beneficiar.2"), _age_rule(18), "You must be above 18."),
        Rule("A new project", ("PMEGP", "faq.eligibility_conditions_of_beneficiar.3"), _new_unit,
             "PMEGP assists new units only."),
        Rule("No earlier government subsidy", ("PMEGP", "faq.eligibility_conditions_of_beneficiar.3"), _no_subsidy,
             "Units that already took a government subsidy are not eligible."),
        Rule("Education condition for larger projects", ("PMEGP", "faq.eligibility_conditions_of_beneficiar.2"), _pmegp_edu,
             "Above Rs 10 lakh (manufacturing) or Rs 5 lakh (service/business) you need VIII standard pass; "
             "a project at or below that cost has no education condition."),
        Rule("Udyam registration", ("PMEGP", "faq.trading_activities.7"), _udyam,
             "Register on the Udyam portal before the physical verification."),
    ]},
    "STANDUP_INDIA": {"name": "Stand-Up India", "rules": [
        Rule("SC/ST or woman entrepreneur", ("STANDUP_INDIA", "home.what_is_stand_up_india.2"), _sc_st_or_woman,
             "The scheme is for SC/ST or women entrepreneurs (51% holding for companies)."),
        Rule("Above 18 years", ("STANDUP_INDIA", "home.salient_features.3"), _age_rule(18), "You must be above 18."),
        Rule("A new (greenfield) enterprise", ("STANDUP_INDIA", "home.salient_features.3"), _new_unit,
             "Loans are for setting up a greenfield enterprise."),
        Rule("Loan between Rs 10 lakh and Rs 1 crore", ("STANDUP_INDIA", "home.salient_features.3"),
             _within(10 * LAKH, CRORE, "Rs 10 lakh to Rs 1 crore"), "Ask for a loan between Rs 10 lakh and Rs 1 crore."),
    ]},
    "PM_SVANIDHI": {"name": "PM SVANidhi", "rules": [
        Rule("Street vendor", ("PM_SVANIDHI", "home.a_special_micro_credit_facility_for_.1"),
             _flag("street_vendor", True, "A street vendor.", "Not a street vendor.", "Not known whether you are a street vendor."),
             "PM SVANidhi is for street vendors."),
    ]},
    "PM_VISHWAKARMA": {"name": "PM Vishwakarma", "rules": [
        Rule("Artisan in a traditional trade", ("PM_VISHWAKARMA", "home.eligibility_criteria.7"),
             _flag("artisan_trade", True, "Works in a traditional trade.", "Not in a listed trade.", "Not known whether you work in a traditional trade."),
             "You must work with hands and tools in one of the 18 listed traditional trades."),
        Rule("Age 18 or more", ("PM_VISHWAKARMA", "home.eligibility_criteria.7"), _age_rule(18), "You must be at least 18."),
        Rule("No similar scheme loan in the past 5 years", ("PM_VISHWAKARMA", "home.eligibility_criteria.7"),
             _flag("loan_last_5y", False, "No similar loan in 5 years.", "You took a similar scheme loan in the last 5 years.",
                   "Not known whether you took a similar loan in 5 years."),
             "Not eligible if a PMEGP, SVANidhi or MUDRA type loan was taken in the past 5 years."),
        Rule("Not in government service", ("PM_VISHWAKARMA", "home.eligibility_criteria.8"),
             _flag("govt_service", False, "Not in government service.", "You or a family member is in government service.",
                   "Not known whether anyone in the family is in government service."),
             "A person in government service and their family are not eligible."),
    ]},
    "STARTUP_INDIA": {"name": "Startup India (DPIIT recognition)", "rules": [
        Rule("Registered as a company, partnership, LLP or cooperative",
             ("STARTUP_INDIA", "startup_recognition_.eligibility_criteria_for_startup_rec.2"), _entity,
             "Register as a Private Limited Company, Partnership Firm, LLP or Cooperative Society."),
        Rule("Within 10 years of incorporation", ("STARTUP_INDIA", "startup_recognition_.eligibility_criteria_for_startup_rec.2"),
             _years, "An entity is a startup for up to 10 years from incorporation."),
        Rule("Turnover under Rs 200 crore", ("STARTUP_INDIA", "startup_scheme_html.annual_turnover.4"), _turnover,
             "Turnover must stay under Rs 200 crore.", uses_books=True),
        Rule("Works on innovation or improvement", ("STARTUP_INDIA", "startup_recognition_.eligibility_criteria_for_startup_rec.2"),
             _flag("innovative", True, "Brings something new.", "No innovation or improvement.", "Not known whether it is innovative."),
             "The startup should work towards innovation or improvement of products, services or processes."),
    ]},
    "CGTMSE": {"name": "CGTMSE guarantee cover", "rules": [
        Rule("Micro or small enterprise", ("CGTMSE", "home.introduction.3"), _size,
             "The guarantee fund supports micro and small enterprises."),
        Rule("Credit within the Rs 10 crore cover ceiling", ("CGTMSE", "home.overview.1"),
             _within(0, 10 * CRORE, "the Rs 10 crore ceiling"), "Guarantee cover is capped at Rs 10 crore."),
        Rule("Apply through a bank or lender", ("CGTMSE", "home.step_1_approach_a_member_lending_ins.6"),
             lambda a, c: ("todo", "The guarantee is arranged by your lender, not directly by you."),
             "Approach a Member Lending Institution (a bank) and ask for CGTMSE cover."),
    ]},
}

_ICON = {"pass": "met", "fail": "not met", "unknown": "not known yet", "todo": "to do"}


@dataclass
class CriterionResult:
    label: str
    status: str
    why: str
    need: str
    clause_id: Optional[int]
    clause_ref: str
    source_url: str


@dataclass
class SchemeFit:
    code: str
    name: str
    status: str                                  # eligible | almost | not_eligible | unverified
    criteria: list[CriterionResult] = field(default_factory=list)
    cited_clause_ids: list[int] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    to_fix: list[str] = field(default_factory=list)
    summary: str = ""


class FitAgent:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def _clause(self, scheme: str, ref: str) -> Optional[tuple[int, str]]:
        r = self.conn.execute(
            "SELECT id, source_url FROM scheme_clauses WHERE scheme_code=%s AND clause_ref=%s AND superseded_at IS NULL"
            " ORDER BY fetched_at DESC LIMIT 1", (scheme, ref)).fetchone()
        return (r[0], r[1]) if r else None

    def context(self, business_id: int) -> dict:
        udyam = self.conn.execute("SELECT udyam_number FROM businesses WHERE id=%s", (business_id,)).fetchone()[0]
        consent = self.conn.execute("SELECT has_active_consent(%s,%s)", (business_id, CONSENT_PURPOSE)).fetchone()[0]
        turnover = None
        if consent:
            t = self.conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) FROM ledger_entries WHERE business_id=%s AND direction='in'"
                " AND needs_review=FALSE AND entry_date > CURRENT_DATE - INTERVAL '12 months'", (business_id,)).fetchone()[0]
            turnover = int(t) / 100 if t else None
        return {"udyam": udyam, "turnover_rupees": turnover, "consent": consent}

    def evaluate(self, business_id: int, attrs: dict, *, only: Optional[list[str]] = None) -> list[SchemeFit]:
        ctx = self.context(business_id)
        verifier = Verifier(self.conn)
        out: list[SchemeFit] = []
        for code, spec in SCHEMES.items():
            if only and code not in only:
                continue
            crit: list[CriterionResult] = []
            used_books = False
            for rule in spec["rules"]:
                status, why = rule.check(attrs, ctx)
                cl = self._clause(*rule.clause)
                if rule.uses_books and ctx["turnover_rupees"] is not None:
                    used_books = True
                crit.append(CriterionResult(rule.label, status, why, rule.need,
                                            cl[0] if cl else None, rule.clause[1], cl[1] if cl else ""))
            cited = sorted({c.clause_id for c in crit if c.clause_id and c.status != "unknown"})
            fails = [c for c in crit if c.status == "fail"]
            unknown = [c for c in crit if c.status == "unknown"]
            status = "not_eligible" if fails else ("almost" if unknown else "eligible")
            text = (f"{spec['name']}: " + {"eligible": "meets the published conditions checked",
                                            "almost": "no condition failed, some facts are still needed",
                                            "not_eligible": "does not meet: " + "; ".join(c.label for c in fails)}[status])
            claim = Claim(business_id=business_id, agent="fit", text=text, cited_clause_ids=cited,
                          needs_citation=bool(cited) or status != "almost",
                          used_financial_data=used_books, consent_purpose=CONSENT_PURPOSE if used_books else None)
            verdict = verifier.check(claim) if (cited or status != "almost") else None
            fit = SchemeFit(code, spec["name"], status, crit, verdict.valid_clause_ids if verdict else [])
            if verdict is not None and not verdict.passed:
                fit.status = "unverified"
                fit.summary = "Could not confirm against a current official clause: " + "; ".join(verdict.reasons)
            else:
                fit.blockers = [f"{c.label}: {c.why}" for c in fails]
                fit.to_fix = [c.need for c in fails] + [c.need for c in crit if c.status == "todo"]
                fit.summary = text + "."
            out.append(fit)
        order = {"eligible": 0, "almost": 1, "not_eligible": 2, "unverified": 3}
        return sorted(out, key=lambda f: order[f.status])


def to_dict(f: SchemeFit) -> dict:
    return {"code": f.code, "name": f.name, "status": f.status, "summary": f.summary, "blockers": f.blockers,
            "to_fix": f.to_fix,
            "criteria": [{"label": c.label, "status": c.status, "state": _ICON[c.status], "why": c.why, "need": c.need,
                          "clause_id": c.clause_id, "clause_ref": c.clause_ref, "source_url": c.source_url}
                         for c in f.criteria]}


def reply_text(fits: list[SchemeFit]) -> str:
    """The chat answer, written in code from the computed result."""
    lines, refs = [], []
    ok = [f for f in fits if f.status == "eligible"]
    almost = [f for f in fits if f.status == "almost"]
    no = [f for f in fits if f.status == "not_eligible"]
    if ok:
        lines.append("You meet the published conditions I checked for: " + ", ".join(f.name for f in ok) + ".")
    if almost:
        lines.append("Nothing rules you out, but I need a few more facts for: " + ", ".join(f.name for f in almost) + ".")
    for f in no[:3]:
        why = f.blockers[0] if f.blockers else f.summary
        fix = next((c.need for c in f.criteria if c.status == "fail"), "")
        lines.append(f"Not eligible for {f.name}: {why} To change this: {fix}")
    todo = [f"{f.name}: {c.need}" for f in ok for c in f.criteria if c.status == "todo"][:2]
    if todo:
        lines.append("Next steps: " + " ".join(todo))
    lines.append("This checks the published conditions only. The bank or portal decides the application.")
    for f in fits:
        if f.status in ("eligible", "not_eligible"):
            for c in f.criteria:
                if c.clause_id and c.status in ("pass", "fail", "todo") and c.clause_ref not in refs:
                    refs.append(c.clause_ref)
    return "\n\n".join(lines) + (f"\n\n(Sources: {', '.join(refs[:6])})" if refs else "")
