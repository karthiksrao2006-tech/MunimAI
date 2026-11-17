"""
Forward real interactions to the self-improvement loop on the supercomputer.

Each input (a bill's OCR text, a question, a transcript) is sent, fire-and-forget,
to the loop's small inbox. The loop's Gemma worker then writes new scenarios in
the style of what real users sent, with numbers computed in code, and the boss is
fine-tuned on those. A seed that cannot be delivered is kept locally so nothing
is lost; the user-facing reply never waits on this.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import requests

from . import config  # noqa: F401  (loads .env)

SEED_URL = os.environ.get("SEED_URL", "http://100.86.188.31:8091/seed")
PENDING = Path(__file__).resolve().parent.parent / "seeds_pending.jsonl"


def _deliver(record: dict) -> None:
    try:
        r = requests.post(SEED_URL, json=record, timeout=8,
                          headers={"X-Seed-Token": os.environ.get("SEED_TOKEN", "")})
        r.raise_for_status()
    except requests.RequestException:
        with PENDING.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def send_seed(kind: str, text_en: str, language: str = "en") -> None:
    """kind: 'bill' (OCR text) or 'question'. text_en is the English form."""
    if not text_en.strip():
        return
    record = {"kind": kind, "text": text_en[:1500], "language": language, "ts": time.time()}
    threading.Thread(target=_deliver, args=(record,), daemon=True).start()
