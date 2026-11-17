"""
Load the scheme knowledge base into Postgres.

The allowlist is enforced HERE, at load time, not just at query time: a clause
whose source domain is not official never enters the database at all. So even a
bug elsewhere cannot cause MunimAI to cite a non-official source: there is
nothing non-official stored to cite.

Every kb/*.json file is loaded (one per scheme). Re-running is idempotent:
(scheme_code, clause_ref, fetched_at) is UNIQUE. When a page is re-fetched, the
older clauses from the same source_url are marked superseded, never deleted, so
a rule change leaves a history and old citations stay resolvable.

Run:  python -m backend.kb.loader
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg

from ..config import CITATION_ALLOWLIST, DBSettings

KB_DIR = Path(__file__).parent


def load(verbose: bool = True) -> dict:
    db = DBSettings.from_env()
    inserted, rejected, skipped, superseded = 0, [], 0, 0
    per_scheme: dict[str, int] = {}

    with psycopg.connect(db.conninfo) as conn:
        for path in sorted(KB_DIR.glob("*.json")):
            clauses = json.loads(path.read_text(encoding="utf-8"))
            newest: dict[tuple[str, str], str] = {}
            for clause in clauses:
                domain = clause["source_domain"].lower()
                if domain not in CITATION_ALLOWLIST:
                    rejected.append((clause["clause_ref"], domain))
                    continue
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
                    per_scheme[clause["scheme_code"]] = per_scheme.get(clause["scheme_code"], 0) + 1
                else:
                    skipped += 1
                key = (clause["scheme_code"], clause["source_url"])
                newest[key] = max(newest.get(key, ""), clause["fetched_at"])
            for (code, url), fetched in newest.items():
                cur = conn.execute(
                    """UPDATE scheme_clauses SET superseded_at = now()
                       WHERE scheme_code=%s AND source_url=%s
                         AND fetched_at < %s AND superseded_at IS NULL""",
                    (code, url, fetched))
                superseded += cur.rowcount
        conn.commit()

    result = {"inserted": inserted, "already_present": skipped,
              "superseded": superseded, "rejected": rejected, "per_scheme": per_scheme}
    if verbose:
        print(f"loaded {inserted} clauses ({skipped} already present, {superseded} superseded)")
        for code, n in sorted(per_scheme.items()):
            print(f"  {code}: +{n}")
        if rejected:
            print(f"REJECTED {len(rejected)} clause(s) from non-allowlisted domains:")
            for ref, dom in rejected[:20]:
                print(f"  {ref}  <- {dom}")
        else:
            print("all clauses came from allowlisted official domains")
    return result


if __name__ == "__main__":
    load()
