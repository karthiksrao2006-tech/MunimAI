"""
Turn benchmark scenarios into supervised fine-tuning data for the Ledger task.

Prompts are imported from the real LedgerAgent so training matches inference
exactly. Targets are derived from the deterministic ground truth, never from a
model. Scenarios are split by id hash: ~20% are held out and never trained on,
so benchmark scores on the held-out set stay honest.

Run:  python -m scripts.build_sft_data
Writes eval/benchmark/sft_train.jsonl
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from backend.agents.ledger import _SCHEMA_HINT, _SYSTEM

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "eval" / "benchmark" / "scenarios.jsonl"
OUT = ROOT / "eval" / "benchmark" / "sft_train.jsonl"


def is_heldout(scenario_id: str) -> bool:
    return int(hashlib.sha256(scenario_id.encode()).hexdigest(), 16) % 5 == 0


def _system_prompt() -> str:
    return (f"{_SYSTEM}\n\nRespond with ONLY a valid JSON object matching this "
            f"shape: {_SCHEMA_HINT}. No prose, no code fences.")


def _target(sc: dict) -> dict | None:
    text = sc["bill_text"]
    seller = re.search(r"^Seller:\s*(.+)$", text, re.M)
    buyer = re.search(r"^Bill to:\s*(.+)$", text, re.M)
    if not seller or not buyer:
        return None
    gst_bp = sc.get("expected_gst_rate_bp")
    return {
        "date": None,
        "seller_name": seller.group(1).strip(),
        "buyer_name": buyer.group(1).strip(),
        "amount_rupees": sc["expected_amount_paise"] / 100,
        "category": sc.get("tag", "goods"),
        "gst_rate_percent": (gst_bp / 100) if gst_bp else None,
    }


def main() -> None:
    n_train = n_held = 0
    with SCENARIOS.open(encoding="utf-8") as f, OUT.open("w", encoding="utf-8") as out:
        for line in f:
            if not line.strip():
                continue
            sc = json.loads(line)
            if is_heldout(sc["id"]):
                n_held += 1
                continue
            target = _target(sc)
            if target is None:
                continue
            out.write(json.dumps({"messages": [
                {"role": "system", "content": _system_prompt()},
                {"role": "user",
                 "content": f"Bill text:\n{sc['bill_text']}\n\nCopy the fields."},
                {"role": "assistant", "content": json.dumps(target)},
            ]}, ensure_ascii=False) + "\n")
            n_train += 1
    print(f"train={n_train} heldout={n_held} -> {OUT}")


if __name__ == "__main__":
    main()
