"""
Idempotent schema additions for the features added after the first schema:
the eligibility profile (businesses.attrs), sealed bank files (seals) and a
web-click consent source. Safe to run on every start.
"""

from __future__ import annotations

import psycopg


def ensure(conn: psycopg.Connection) -> None:
    conn.execute("ALTER TABLE businesses ADD COLUMN IF NOT EXISTS attrs JSONB NOT NULL DEFAULT '{}'::jsonb")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seals (
               id             BIGSERIAL PRIMARY KEY,
               token          TEXT        NOT NULL UNIQUE,
               business_id    BIGINT      NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
               kind           TEXT        NOT NULL,
               sealed_at      TIMESTAMPTZ NOT NULL,
               entries        INTEGER     NOT NULL,
               first_date     DATE,
               last_date      DATE,
               total_in_paise BIGINT      NOT NULL,
               total_out_paise BIGINT     NOT NULL,
               ledger_head    TEXT        NOT NULL,
               docs_digest    TEXT        NOT NULL,
               root           TEXT        NOT NULL,
               snapshot       JSONB       NOT NULL,
               pdf_sha256     TEXT
           )""")
    # consent can now be granted by a click in the web app
    conn.execute("ALTER TABLE consents DROP CONSTRAINT IF EXISTS consents_granted_via_check")
    conn.execute("ALTER TABLE consents ADD CONSTRAINT consents_granted_via_check "
                 "CHECK (granted_via IN ('whatsapp_reply','aa_flow','simulated','web_click'))")
    conn.commit()
