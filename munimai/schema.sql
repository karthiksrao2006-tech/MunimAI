-- MunimAI schema
-- Design note: the verifier gate is only as good as what the schema can prove.
-- So citations, consent and rule-versions are first-class tables with NOT NULL
-- constraints, not optional columns an agent can forget to fill.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------- businesses
CREATE TABLE IF NOT EXISTS businesses (
    id              BIGSERIAL PRIMARY KEY,
    whatsapp_phone  TEXT        NOT NULL UNIQUE,   -- E.164, the identity key
    display_name    TEXT        NOT NULL,
    language        TEXT        NOT NULL DEFAULT 'kn'  CHECK (language IN ('hi','kn','en')),
    udyam_number    TEXT,
    gstin           TEXT,
    is_synthetic    BOOLEAN     NOT NULL DEFAULT TRUE,  -- demo data is flagged, always
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------- ledger
CREATE TABLE IF NOT EXISTS ledger_entries (
    id              BIGSERIAL PRIMARY KEY,
    business_id     BIGINT      NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    entry_date      DATE        NOT NULL,
    direction       TEXT        NOT NULL CHECK (direction IN ('in','out')),
    amount_paise    BIGINT      NOT NULL CHECK (amount_paise >= 0), -- integer paise, never float rupees
    category        TEXT        NOT NULL,
    counterparty    TEXT,
    gst_rate_bp     INTEGER     CHECK (gst_rate_bp BETWEEN 0 AND 10000), -- basis points: 1800 = 18%
    source          TEXT        NOT NULL CHECK (source IN ('bill_photo','voice','text','upi','manual')),
    source_media_id TEXT,
    ocr_confidence  REAL        CHECK (ocr_confidence BETWEEN 0 AND 1),
    needs_review    BOOLEAN     NOT NULL DEFAULT FALSE,  -- low confidence -> owner confirms
    raw_extraction  JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ledger_business_date_idx ON ledger_entries (business_id, entry_date DESC);

-- ------------------------------------------------------------------ consent
-- Purpose-bound and revocable. The verifier checks this before any tool call
-- that touches financial data.
CREATE TABLE IF NOT EXISTS consents (
    id              BIGSERIAL PRIMARY KEY,
    business_id     BIGINT      NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    purpose         TEXT        NOT NULL,   -- e.g. 'loan_eligibility_assessment'
    scope           TEXT[]      NOT NULL,   -- e.g. {bank_statement, gst_returns}
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    artefact_ref    TEXT,                   -- Account Aggregator consent artefact id
    granted_via     TEXT        NOT NULL CHECK (granted_via IN ('whatsapp_reply','aa_flow','simulated'))
);
CREATE INDEX IF NOT EXISTS consent_active_idx ON consents (business_id, purpose)
    WHERE revoked_at IS NULL;

CREATE OR REPLACE FUNCTION has_active_consent(p_business BIGINT, p_purpose TEXT)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM consents
        WHERE business_id = p_business
          AND purpose     = p_purpose
          AND revoked_at IS NULL
          AND expires_at > now()
    );
$$;

-- -------------------------------------------------------------- scheme rules
-- The citable knowledge base. Every row must carry where it came from and when
-- it was fetched, so an answer can state the rule version it relied on.
CREATE TABLE IF NOT EXISTS scheme_clauses (
    id              BIGSERIAL PRIMARY KEY,
    scheme_code     TEXT        NOT NULL,   -- e.g. 'MUDRA_KISHORE'
    clause_ref      TEXT        NOT NULL,   -- e.g. 'eligibility.loan_amount'
    clause_text     TEXT        NOT NULL,
    source_url      TEXT        NOT NULL,
    source_domain   TEXT        NOT NULL,   -- allowlist is enforced in code
    fetched_at      TIMESTAMPTZ NOT NULL,   -- the rule version
    superseded_at   TIMESTAMPTZ,            -- set when a newer fetch replaces it
    embedding       vector(1024),
    UNIQUE (scheme_code, clause_ref, fetched_at)
);
CREATE INDEX IF NOT EXISTS clause_current_idx ON scheme_clauses (scheme_code)
    WHERE superseded_at IS NULL;

-- ---------------------------------------------------------------- citations
-- A claim shown to the owner must point at the clauses that support it.
CREATE TABLE IF NOT EXISTS claims (
    id              BIGSERIAL PRIMARY KEY,
    business_id     BIGINT      NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    trace_id        UUID        NOT NULL,
    agent           TEXT        NOT NULL,
    claim_text      TEXT        NOT NULL,
    verdict         TEXT        NOT NULL CHECK (verdict IN ('passed','blocked')),
    blocked_reason  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (verdict = 'passed' OR blocked_reason IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS claim_citations (
    claim_id        BIGINT      NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    clause_id       BIGINT      NOT NULL REFERENCES scheme_clauses(id),
    PRIMARY KEY (claim_id, clause_id)
);

-- ------------------------------------------------------------- trace events
-- The glass box: every agent step, in order, for the run tree.
CREATE TABLE IF NOT EXISTS trace_events (
    id              BIGSERIAL PRIMARY KEY,
    trace_id        UUID        NOT NULL,
    seq             INTEGER     NOT NULL,
    parent_seq      INTEGER,
    business_id     BIGINT      REFERENCES businesses(id) ON DELETE SET NULL,
    agent           TEXT        NOT NULL,
    event_type      TEXT        NOT NULL CHECK (event_type IN
                        ('message_in','route','agent_start','tool_call','tool_result',
                         'llm_call','verifier','agent_end','message_out','error')),
    tool_name       TEXT,
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    latency_ms      INTEGER,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (trace_id, seq)
);
CREATE INDEX IF NOT EXISTS trace_lookup_idx ON trace_events (trace_id, seq);

-- ----------------------------------------------------------------- messages
CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    business_id     BIGINT      NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    trace_id        UUID,
    direction       TEXT        NOT NULL CHECK (direction IN ('in','out')),
    channel         TEXT        NOT NULL DEFAULT 'whatsapp',
    wa_message_id   TEXT UNIQUE,            -- dedupes Meta webhook retries
    msg_type        TEXT        NOT NULL CHECK (msg_type IN ('text','audio','image','document')),
    body            TEXT,
    media_path      TEXT,
    language        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_business_idx ON messages (business_id, created_at DESC);

-- --------------------------------------------------------- generated agents
-- Karigar output. A spec is only promoted after its eval cases pass.
CREATE TABLE IF NOT EXISTS agent_specs (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT        NOT NULL,
    spec            JSONB       NOT NULL,
    created_by      TEXT        NOT NULL DEFAULT 'karigar',
    depth           INTEGER     NOT NULL DEFAULT 1 CHECK (depth <= 2),
    eval_passed     INTEGER     NOT NULL DEFAULT 0,
    eval_total      INTEGER     NOT NULL DEFAULT 0,
    promoted        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (NOT promoted OR (eval_total > 0 AND eval_passed = eval_total))
);
