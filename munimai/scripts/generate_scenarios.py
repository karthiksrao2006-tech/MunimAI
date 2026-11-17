"""
Scenario generator for the self-improvement loop.

Gemma (the CPU/RAM worker) supplies creative VARIETY ONLY — business type,
names, item, city, which side of the bill we are. It never supplies numbers.
Python deterministically picks the amount and GST split and renders the bill
text, then computes the ground truth (direction, amount_paise, gst_rate_bp)
the same way the real LedgerAgent is supposed to. This mirrors the rest of
the codebase's rule: fragile model reasoning never becomes a label on faith.

Run:  python -m scripts.generate_scenarios --n 16
Appends to eval/benchmark/scenarios.jsonl.
"""

from __future__ import annotations

import argparse
import json
import random
import uuid
from pathlib import Path

from backend.agents.ledger import _name_matches
from backend.config import LLMSettings
from backend.llm import LLM

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = ROOT / "eval" / "benchmark" / "scenarios.jsonl"

_GST_RATES_PCT = [0, 5, 12, 18, 28]

_GEN_SYSTEM = (
    "You invent VARIETY for training data about Indian micro-businesses. "
    "You never invent amounts or tax numbers — only names, business types, "
    "items, and cities. Be realistic and diverse: street vendors, kirana "
    "stores, tailoring units, auto-repair shops, small manufacturers, "
    "mobile-repair shops, tea stalls, from different Indian towns."
)

_GEN_SCHEMA = (
    '{"scenarios": [{"business_type": "string", "our_business_name": "string", '
    '"counterparty_name": "string", "item_description": "string", '
    '"city": "string", "we_are": "seller"|"buyer"}]}'
)


def _render_bill(our_name: str, counterparty: str, we_are: str,
                  item: str, amount_rupees: float, gst_pct: int,
                  regime: str) -> tuple[str, str, int]:
    """Returns (bill_text, direction, expected_gst_bp)."""
    seller = our_name if we_are == "seller" else counterparty
    buyer = counterparty if we_are == "seller" else our_name
    lines = ["TAX INVOICE", f"Seller: {seller}", f"Bill to: {buyer}",
             f"{item}  {amount_rupees:.2f}"]
    expected_gst_bp = None
    if gst_pct > 0 and regime == "cgst_sgst":
        half = gst_pct / 2
        cgst = amount_rupees * half / 100
        sgst = cgst
        lines.append(f"CGST {half:g}%  {cgst:.2f}")
        lines.append(f"SGST {half:g}%  {sgst:.2f}")
        total = amount_rupees + cgst + sgst
        lines.append(f"Total payable  {total:.2f}")
        expected_gst_bp = gst_pct * 100
    elif gst_pct > 0 and regime == "igst":
        igst = amount_rupees * gst_pct / 100
        lines.append(f"IGST {gst_pct}%  {igst:.2f}")
        total = amount_rupees + igst
        lines.append(f"Total payable  {total:.2f}")
        expected_gst_bp = gst_pct * 100
    else:
        total = amount_rupees
        lines.append(f"Total payable  {total:.2f}")
    direction = "in" if we_are == "seller" else "out"
    return "\n".join(lines), direction, expected_gst_bp, round(total * 100)


def generate_batch(n: int, worker: LLM | None = None) -> list[dict]:
    worker = worker or LLM(LLMSettings.from_env_worker())
    parsed = worker.json_call(
        _GEN_SYSTEM,
        f"Invent {n} distinct scenarios as a JSON array under key 'scenarios'.",
        schema_hint=_GEN_SCHEMA, agent="scenario_gen", max_tokens=1800,
    )
    raw = parsed.get("scenarios", [])
    scenarios = []
    for item in raw:
        try:
            our_name = str(item["our_business_name"]).strip()
            counterparty = str(item["counterparty_name"]).strip()
            we_are = item["we_are"] if item.get("we_are") in ("seller", "buyer") else "seller"
            desc = str(item.get("item_description", "goods")).strip()
            tag = str(item.get("business_type", "general")).strip()
        except (KeyError, TypeError):
            continue
        if not our_name or not counterparty:
            continue
        # sanity: our own generator's name-match logic must be able to tell
        # the sides apart, or the scenario is ambiguous by construction
        if _name_matches(our_name, counterparty):
            continue

        amount = round(random.uniform(300, 60_000), 2)
        gst_pct = random.choice(_GST_RATES_PCT)
        regime = random.choice(["cgst_sgst", "igst"]) if gst_pct > 0 else "none"
        bill_text, direction, gst_bp, amount_paise = _render_bill(
            our_name, counterparty, we_are, desc, amount, gst_pct, regime)

        scenarios.append({
            "id": str(uuid.uuid4()),
            "tag": tag,
            "our_business_name": our_name,
            "bill_text": bill_text,
            "expected_direction": direction,
            "expected_amount_paise": amount_paise,
            "expected_gst_rate_bp": gst_bp,
            "source": "gemma-worker-gen",
        })
    return scenarios


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=16)
    args = ap.parse_args()

    batch = generate_batch(args.n)
    SCENARIOS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SCENARIOS_PATH.open("a", encoding="utf-8") as f:
        for s in batch:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"Wrote {len(batch)} scenarios (requested {args.n}) -> {SCENARIOS_PATH}")


if __name__ == "__main__":
    main()
