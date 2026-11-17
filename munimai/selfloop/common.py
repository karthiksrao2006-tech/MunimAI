"""
Shared helpers for the self-improvement loop. Stdlib only, so the same files
run on the supercomputer (unsloth env has no psycopg/dotenv).

The Ledger prompts are NOT written here: deploy.py exports them from the real
LedgerAgent into prompts.json so training always matches inference.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
_PROMPTS = json.loads((HERE / "prompts.json").read_text(encoding="utf-8"))
SYSTEM_PROMPT: str = _PROMPTS["system"]


def user_prompt(bill_text: str) -> str:
    return f"Bill text:\n{bill_text}\n\nCopy the fields."


def name_matches(ours: str, other: Optional[str]) -> bool:
    """Same loose matcher the LedgerAgent uses to decide direction."""
    if not other:
        return False
    words = {w for w in re.findall(r"[a-z]+", ours.lower()) if len(w) > 2}
    if not words:
        return False
    other_l = other.lower()
    return sum(1 for w in words if w in other_l) >= max(1, len(words) // 2)


def is_heldout(scenario_id: str) -> bool:
    return int(hashlib.sha256(scenario_id.encode()).hexdigest(), 16) % 5 == 0


def extract_json(text: str) -> Optional[dict]:
    t = text.strip()
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e < s:
        return None
    try:
        obj = json.loads(t[s:e + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def score_prediction(pred: Optional[dict], sc: dict) -> dict:
    """Strict per-field check against deterministic ground truth.
    Direction is derived exactly as the LedgerAgent does: from the copied
    seller/buyer names vs our business name, never from the model."""
    out = {"direction": False, "amount": False, "gst": False}
    if not pred:
        return {**out, "pass": False}
    seller, buyer = pred.get("seller_name"), pred.get("buyer_name")
    ours = sc["our_business_name"]
    s_ok, b_ok = name_matches(ours, seller), name_matches(ours, buyer)
    if s_ok and not b_ok:
        direction = "in"
    elif b_ok and not s_ok:
        direction = "out"
    else:
        direction = None
    out["direction"] = direction == sc["expected_direction"]
    try:
        out["amount"] = abs(int(round(float(pred.get("amount_rupees")) * 100))) == sc["expected_amount_paise"]
    except (TypeError, ValueError):
        out["amount"] = False
    g = pred.get("gst_rate_percent")
    gst_bp = int(round(float(g) * 100)) if isinstance(g, (int, float)) else None
    out["gst"] = gst_bp == sc["expected_gst_rate_bp"]
    return {**out, "pass": all(out.values())}


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]
