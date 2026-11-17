"""
Benchmark runner: scores the currently-served boss model (Qwen, via vLLM)
against every accumulated scenario in eval/benchmark/scenarios.jsonl.

This is the composite score the self-improvement loop (train_loop.py) trains
against. A scenario passes only if ALL of direction, amount, and GST rate
match the deterministic ground truth computed at generation time — partial
credit is tracked per-field for diagnosis, but the headline score is strict.

Run:  python -m scripts.run_benchmark
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from backend.agents.ledger import LedgerAgent
from backend.config import DBSettings
from backend.llm import LLM

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = ROOT / "eval" / "benchmark" / "scenarios.jsonl"
REPORTS_DIR = ROOT / "eval" / "benchmark" / "reports"


def load_scenarios() -> list[dict]:
    if not SCENARIOS_PATH.exists():
        return []
    with SCENARIOS_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def evaluate(llm: LLM, scenarios: list[dict] | None = None) -> dict:
    scenarios = scenarios if scenarios is not None else load_scenarios()
    db = DBSettings.from_env()
    conn = psycopg.connect(db.conninfo)

    field_hits = {"direction": 0, "amount": 0, "gst": 0}
    full_pass = 0
    per_tag: dict[str, list[int]] = {}
    failures = []

    for sc in scenarios:
        phone = f"+9199{uuid.uuid4().int % 100_000_000:08d}"
        bid = conn.execute(
            "INSERT INTO businesses (whatsapp_phone, display_name) VALUES (%s,%s) "
            "RETURNING id", (phone, sc["our_business_name"]),
        ).fetchone()[0]
        conn.commit()

        try:
            entry = LedgerAgent(conn, llm).from_text(bid, sc["bill_text"])
        except Exception as exc:  # model/network failure counts as a fail, not a crash
            failures.append({"id": sc["id"], "error": str(exc)})
            per_tag.setdefault(sc.get("tag", "general"), []).append(0)
            continue

        ok_dir = entry.direction == sc["expected_direction"]
        ok_amt = entry.amount_paise == sc["expected_amount_paise"]
        ok_gst = entry.gst_rate_bp == sc["expected_gst_rate_bp"]
        field_hits["direction"] += int(ok_dir)
        field_hits["amount"] += int(ok_amt)
        field_hits["gst"] += int(ok_gst)
        passed = ok_dir and ok_amt and ok_gst
        full_pass += int(passed)
        per_tag.setdefault(sc.get("tag", "general"), []).append(int(passed))
        if not passed:
            failures.append({
                "id": sc["id"], "tag": sc.get("tag"),
                "expected": {"direction": sc["expected_direction"],
                             "amount_paise": sc["expected_amount_paise"],
                             "gst_rate_bp": sc["expected_gst_rate_bp"]},
                "got": {"direction": entry.direction,
                        "amount_paise": entry.amount_paise,
                        "gst_rate_bp": entry.gst_rate_bp},
            })

    conn.close()

    n = len(scenarios) or 1
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_scenarios": len(scenarios),
        "weighted_score": full_pass / n,
        "field_accuracy": {k: v / n for k, v in field_hits.items()},
        "per_tag_pass_rate": {
            t: sum(v) / len(v) for t, v in per_tag.items()
        },
        "failures": failures[:25],  # cap the report size
    }
    return report


def main() -> None:
    llm = LLM()
    if not llm.health():
        raise SystemExit("Boss model (vLLM) unreachable — cannot benchmark.")
    report = evaluate(llm)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"{report['timestamp'].replace(':', '-')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "failures"}, indent=2))
    print(f"Full report -> {out}")


if __name__ == "__main__":
    main()
