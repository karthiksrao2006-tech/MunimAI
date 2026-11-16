"""
A minimal end-to-end run: one question in, one verified answer out, with the
full trace printed. This is the vertical slice the demo is built on.

    python -m backend.orchestrator.run_once "Can I get a MUDRA loan of 3 lakh
        for my tailoring unit?"

It uses the live model and the real knowledge base. If the model is
unreachable it says so plainly rather than inventing an answer.
"""

from __future__ import annotations

import sys
import uuid

import psycopg

from ..agents.scheme import SchemeAgent
from ..agents.verifier import Verifier
from ..config import DBSettings
from ..llm import LLM
from ..trace import Trace, print_run_tree


SYNTHETIC_PHONE = "+919900000001"   # Lakshmi, the demo business


def ensure_demo_business(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM businesses WHERE whatsapp_phone=%s", (SYNTHETIC_PHONE,)
    ).fetchone()
    if row:
        return row[0]
    bid = conn.execute(
        "INSERT INTO businesses (whatsapp_phone, display_name, language, is_synthetic)"
        " VALUES (%s,%s,'kn',TRUE) RETURNING id",
        (SYNTHETIC_PHONE, "Lakshmi Tailoring Unit"),
    ).fetchone()[0]
    conn.commit()
    return bid


def main(question: str, scheme: str = "MUDRA_KISHORE") -> None:
    db = DBSettings.from_env()
    llm = LLM()

    with psycopg.connect(db.conninfo) as conn:
        business_id = ensure_demo_business(conn)
        trace = Trace(business_id=business_id)
        trace.attach_db(conn)
        trace.emit("router", "message_in", {"text": question, "channel": "demo"})

        if not llm.health():
            trace.emit("router", "error", {"msg": "model unreachable"})
            print("Model is unreachable. Start the vLLM server on the "
                  "supercomputer, or run in REPLAY mode.")
            return

        trace.emit("router", "route", {"to": "scheme", "scheme": scheme})
        agent = SchemeAgent(conn, llm)
        answer, claim = agent.answer(business_id, question, scheme, trace=trace)

        verifier = Verifier(conn)
        verdict = verifier.check(claim)
        trace.emit("verifier", "verifier",
                   {"passed": verdict.passed, "reasons": verdict.reasons,
                    "valid_clauses": verdict.valid_clause_ids})

        # what the owner would actually receive
        if verdict.passed:
            cited = conn.execute(
                "SELECT clause_ref, source_url FROM scheme_clauses WHERE id=ANY(%s)",
                (verdict.valid_clause_ids,),
            ).fetchall()
            reply = answer.text + "\n\nSources:\n" + "\n".join(
                f"  - {ref}: {url}" for ref, url in cited
            )
        else:
            reply = ("I can't confirm that yet — " + "; ".join(verdict.reasons)
                     + ". Let me get the right clause before I answer.")
        trace.emit("router", "message_out", {"reply": reply})

        print("\n" + "=" * 72)
        print("OWNER RECEIVES:")
        print("=" * 72)
        print(reply)
        print_run_tree(str(trace.trace_id))


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else (
        "Can I get a MUDRA loan of 3 lakh for my tailoring unit?"
    )
    main(q)
