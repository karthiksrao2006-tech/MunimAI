"""
Score an adapter (or the plain 4-bit base with --adapter none) on the held-out
scenarios, directly on GPU0 beside the live vLLM server, never through it.
Prints one JSON line prefixed RESULT.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import unsloth  # noqa: F401
import torch
from unsloth import FastLanguageModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selfloop.common import (SYSTEM_PROMPT, extract_json, load_jsonl,  # noqa: E402
                             score_prediction, user_prompt)

BASE = "/home/admin/munimai/models/Qwen2.5-7B-Instruct-base"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--vram_fraction", type=float, default=0.24)
    a = ap.parse_args()

    torch.cuda.set_per_process_memory_fraction(a.vram_fraction, 0)
    name = BASE if a.adapter == "none" else a.adapter
    model, tok = FastLanguageModel.from_pretrained(
        model_name=name, max_seq_length=1024, load_in_4bit=True, dtype=None)
    FastLanguageModel.for_inference(model)

    rows = load_jsonl(Path(a.data))
    tot = {"direction": 0, "amount": 0, "gst": 0, "pass": 0}
    for sc in rows:
        msgs = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt(sc["bill_text"])}]
        ids = tok.apply_chat_template(msgs, add_generation_prompt=True,
                                      return_tensors="pt", return_dict=True).to("cuda")
        with torch.no_grad():
            out = model.generate(**ids, max_new_tokens=220, do_sample=False)
        text = tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)
        s = score_prediction(extract_json(text), sc)
        for k in tot:
            tot[k] += int(s[k])
    n = max(len(rows), 1)
    print("RESULT " + json.dumps({"n": len(rows), "score": tot["pass"] / n,
                                  "fields": {k: tot[k] / n for k in ("direction", "amount", "gst")}}))


if __name__ == "__main__":
    main()
