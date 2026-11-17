"""
The Scheme agent.

Given a business profile and a question, it decides whether the business looks
eligible for a MUDRA category, and it may ONLY argue from clauses retrieved out
of the database. The model phrases the answer; it does not supply the facts.

The flow that keeps it honest:
  1. retrieve candidate clauses for the scheme (from Postgres, official only)
  2. hand ONLY those clauses to the model, with their ids
  3. the model returns a verdict plus the ids of the clauses it relied on
  4. we keep only ids that were actually in the retrieved set (no hallucinated ids)
  5. the Verifier then confirms those ids are real and current before the owner
     sees anything

So a clause the model invents cannot become a citation: step 4 drops unknown
ids, and step 5 blocks the claim if nothing real is left.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import psycopg

from ..llm import LLM
from ..trace import Trace
from .verifier import Claim


@dataclass
class SchemeAnswer:
    text: str
    scheme_code: str
    cited_clause_ids: list[int]
    retrieved_clause_ids: list[int]


_SYSTEM = (
    "You are a cautious loan-scheme adviser for Indian micro-enterprises. "
    "You may ONLY use the numbered clauses provided. Never state an eligibility "
    "rule that is not in them. If the clauses do not settle the question, say "
    "what is still needed. Do not claim any application was filed or submitted. "
    "If the question is NOT about loan eligibility, documents, or scheme rules, "
    "do NOT force an answer from the clauses — set cited_ids to [] and say in "
    "answer that this question is outside your scheme knowledge."
)


class SchemeAgent:
    def __init__(self, conn: psycopg.Connection, llm: LLM) -> None:
        self.conn = conn
        self.llm = llm

    def retrieve(self, scheme_code: str, limit: int = 12) -> list[tuple[int, str, str]]:
        """Current clauses for a scheme (and the general ones), newest kept.
        Text retrieval for now; pgvector embeddings can replace this later
        without changing the agent's contract."""
        rows = self.conn.execute(
            """SELECT id, clause_ref, clause_text
               FROM scheme_clauses
               WHERE superseded_at IS NULL
                 AND (scheme_code = %s OR scheme_code = 'MUDRA_GENERAL')
               ORDER BY scheme_code, clause_ref
               LIMIT %s""",
            (scheme_code, limit),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def answer(
        self,
        business_id: int,
        question: str,
        scheme_code: str = "MUDRA_KISHORE",
        *,
        trace: Optional[Trace] = None,
    ) -> tuple[SchemeAnswer, Claim]:
        if trace:
            trace.emit("scheme", "agent_start", {"question": question,
                                                 "scheme": scheme_code})
        clauses = self.retrieve(scheme_code)
        retrieved_ids = [c[0] for c in clauses]
        if trace:
            trace.emit("scheme", "tool_call",
                       {"retrieved": len(clauses), "clause_ids": retrieved_ids},
                       tool_name="kb.retrieve")

        clause_block = "\n".join(f"[{cid}] ({ref}) {text}" for cid, ref, text in clauses)
        user = (
            f"Business question: {question}\n\n"
            f"Clauses you may use:\n{clause_block}\n\n"
            "Decide if the business appears eligible, citing clause numbers. "
            'Return JSON: {"answer": "<plain-language answer naming the clause '
            'numbers you used>", "cited_ids": [<clause numbers>]}'
        )
        parsed = self.llm.json_call(
            _SYSTEM, user,
            schema_hint='{"answer": string, "cited_ids": [int]}',
            trace=trace, agent="scheme", max_tokens=400,
        )

        answer_text = str(parsed.get("answer", "")).strip()
        raw_ids = parsed.get("cited_ids", []) or []
        # keep only ids that were actually retrieved (drop any hallucinated id)
        retrieved_set = set(retrieved_ids)
        cited = [int(i) for i in raw_ids if isinstance(i, (int, str))
                 and str(i).isdigit() and int(i) in retrieved_set]
        dropped = [i for i in raw_ids if i not in cited]
        if trace and dropped:
            trace.emit("scheme", "verifier",
                       {"dropped_unknown_ids": dropped,
                        "note": "model cited ids that were not retrieved"})

        claim = Claim(
            business_id=business_id,
            agent="scheme",
            text=answer_text,
            cited_clause_ids=cited,
            needs_citation=True,
        )
        if trace:
            trace.emit("scheme", "agent_end",
                       {"answer": answer_text[:300], "cited": cited})
        return (
            SchemeAnswer(answer_text, scheme_code, cited, retrieved_ids),
            claim,
        )
