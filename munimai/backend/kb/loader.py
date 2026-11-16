"""
Load the scheme knowledge base into Postgres.

The allowlist is enforced HERE, at load time, not just at query time: a clause
whose source domain is not official never enters the database at all. So even a
bug elsewhere cannot cause MunimAI to cite a non-official source — there is
nothing non-official stored to cite.

Run:  python -m backend.kb.loader
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg

from ..config import CITATION_ALLOWLIST, DBSettings

KB_FILE = Path(__file__).parent / "mudra.json"


def load(verbose: bool = True) -> dict:
    data = json.loads(KB_FILE.read_text(encoding="utf-8"))
    db = DBSettings.from_env()

    inserted, rejected, skipped = 0, [], 0
    with psycopg.connect(db.conninfo) as conn:
        for clause in data:
            domain = clause["source_domain"].lower()
            if domain not in CITATION_ALLOWLIST:
                rejected.append((clause["clause_ref"], domain))
                continue
            # idempotent: (scheme_code, clause_ref, fetched_at) is UNIQUE
            row = conn.execute(
                """INSERT INTO scheme_clauses
                   (scheme_code, clause_ref, clause_text, source_url,
                    source_domain, fetched_at)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (scheme_code, clause_ref, fetched_at) DO NOTHING
                   RETURNING id""",
                (clause["scheme_code"], clause["clause_ref"], clause["clause_text"],
                 clause["source_url"], domain, clause["fetched_at"]),
            ).fetchone()
            if row:
                inserted += 1
            else:
                skipped += 1
        conn.commit()

    result = {"inserted": inserted, "already_present": skipped, "rejected": rejected}
    if verbose:
        print(f"loaded {inserted} clauses ({skipped} already present)")
        if rejected:
            print(f"REJECTED {len(rejected)} clause(s) from non-allowlisted domains:")
            for ref, dom in rejected:
                print(f"  {ref}  <- {dom}")
        else:
            print("all clauses came from allowlisted official domains")
    return result


if __name__ == "__main__":
    load()
