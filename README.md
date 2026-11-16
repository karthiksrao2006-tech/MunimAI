# MunimAI

An agentic assistant on WhatsApp that turns a micro-enterprise's bills and UPI
activity into clean compliance records, matched government schemes, and a
lender-ready credit file.

Built for **Tech4Bharat 2026** (JanAI Mass Entrepreneurship Challenge), problem
statement **3.2** — access to formal credit and schemes — with problem statement
**3.1** (GST compliance paperwork) built in as a feature.

> **Thesis:** compliance is not a cost centre. It is the data engine that makes
> a micro business bankable. Every bill filed for GST also becomes a verified
> line in a credit file.

---

## Status

Early. Infrastructure and the trust layer work; the agents are being built.

| Component | State |
|---|---|
| Bhashini ASR/TTS (Hindi, Kannada) | Working, verified round-trip |
| Postgres 16 + pgvector schema | Applied, 11/11 guarantee tests passing |
| Trace writer ("glass box") | Working |
| Qwen2.5-7B-AWQ serving | Smoke-tested: 0.90 on JSON tool-calls, 14.4 GB VRAM |
| Orchestrator + agents | Not built yet |
| WhatsApp gateway | Not built yet |
| Scheme knowledge base | Not built yet |

Nothing here files a GST return or submits a loan application. Those actions are
simulated, by design — see *Boundaries*.

---

## How it works

```
WhatsApp (voice / photo / text)
        │
   Bhashini ASR            Indic speech → text
        │
   Tiered Cognition Router  ~85% handled by a local 7B; hard cases escalate
        │
   Orchestrator (LangGraph) plans across days, holds state in Postgres
        │
   ┌────┴─────────────────────────────────────────┐
   Ledger   Compliance   Scheme   Credit   Document   Follow-up
   └────┬─────────────────────────────────────────┘
        │
   Verifier gate            blocks anything uncited, unconsented or stale
        │
   WhatsApp reply (+ optional Bhashini TTS voice note)
```

A separate **batch tier** runs overnight on a GPU box for fine-tuning, bulk OCR
and credit re-scoring. It never serves live traffic.

### The verifier gate

Four rules, enforced before anything reaches the user:

1. **Citation required** — a scheme eligibility claim with no retrieved clause is rejected.
2. **Rule version current** — every answer carries the notification date it relied on.
3. **Consent active** — any tool call touching financial data checks for a live, purpose-bound consent.
4. **Prepare, never file** — filing and submission are simulated; the owner acts.

These are enforced in the database as well as in code, so the guarantee survives
a bug in an agent. See `munimai/eval/test_schema_guarantees.py` — each test
asserts the database *refuses* something the product must never do.

---

## Stack

- **Reasoning** — Qwen2.5-7B-Instruct-AWQ (Apache 2.0) served with vLLM, capped at ~15 GB VRAM
- **Indic speech** — [Bhashini](https://bhashini.gov.in) (MeitY), ASR + TTS for Hindi and Kannada
- **Data** — PostgreSQL 16 + pgvector
- **Orchestration** — LangGraph
- **Channel** — WhatsApp Cloud API

Indic language work is deliberately handled by Bhashini rather than the LLM:
measured Kannada generation quality from a 7B model was poor (0.70 script
ratio, one degenerate output), while Bhashini round-tripped both languages at
100% script accuracy.

---

## Setup

Requires Python 3.11+, and a reachable PostgreSQL 16 with the `vector` extension.

```bash
cp .env.example .env     # then fill in your own credentials
pip install -r requirements.txt
psql "$PG_CONNINFO" -f munimai/schema.sql
python -m pytest munimai/eval/test_schema_guarantees.py -v
```

Credentials are read from the environment only and are never hardcoded. `.env`
is gitignored and must stay that way — it holds live API keys.

---

## Boundaries

- **Filing stays with the owner or their professional.** MunimAI prepares; it does not file.
- **Financial data is used only with consent**, purpose-bound and revocable.
- **Every eligibility claim cites its source**, restricted to an allowlist of official government domains.
- **Demo data is synthetic** and flagged as such in the schema (`businesses.is_synthetic`).

---

## Licence

Not yet chosen.
