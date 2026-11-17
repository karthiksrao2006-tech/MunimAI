"""
Unattended self-improvement loop for the boss model (Qwen2.5-7B).

Each cycle: Gemma generates fresh scenarios -> held-out split -> score the
current best -> if below TARGET, QLoRA-train a candidate on all train data ->
score it on the same held-out set -> promote only if strictly better.
Gemma keeps generating every cycle until the target is met. State is on disk,
so the loop survives restarts. It never touches the live vLLM server: a
promotion here means "best adapter on disk"; swapping the served checkpoint
(merge + AWQ + vLLM restart) is a separate, human-approved step.

Run (on the supercomputer, via the unsloth env's python):
    python -m selfloop.loop --target 0.97 --max-hours 24
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

from selfloop import gen
from selfloop.common import SYSTEM_PROMPT, is_heldout, load_jsonl, user_prompt

ROOT = Path(__file__).resolve().parent.parent          # ~/munimai
WORK = ROOT / "selfloop_data"
SCEN = WORK / "scenarios.jsonl"
STATE = WORK / "state.json"
LOG = WORK / "loop.log"
PY = sys.executable


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"best_adapter": "none", "best_score": None, "cycle": 0, "history": []}


def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=2))


def append_scenarios(rows: list[dict]) -> None:
    with SCEN.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def sft_row(sc: dict) -> dict:
    target = {"date": None, "seller_name": sc["seller_name"], "buyer_name": sc["buyer_name"],
              "amount_rupees": sc["expected_amount_paise"] / 100, "category": sc.get("tag", "goods"),
              "gst_rate_percent": (sc["expected_gst_rate_bp"] / 100) if sc["expected_gst_rate_bp"] else None}
    return {"messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user_prompt(sc["bill_text"])},
                         {"role": "assistant", "content": json.dumps(target)}]}


def evaluate(adapter: str, heldout_path: Path) -> dict:
    r = subprocess.run([PY, str(ROOT / "selfloop" / "eval_adapter.py"), "--adapter", adapter,
                        "--data", str(heldout_path)], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    raise RuntimeError(f"eval failed: {r.stderr[-600:]}")


def train(train_path: Path, out: Path, epochs: float) -> bool:
    r = subprocess.run([PY, str(ROOT / "selfloop" / "train_qlora.py"), "--data", str(train_path),
                        "--out", str(out), "--epochs", str(epochs)], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"TRAIN FAILED: {r.stderr[-600:]}")
        return False
    peak = [l for l in r.stdout.splitlines() if l.startswith("PEAK_VRAM_GB")]
    log(f"trained -> {out.name} {peak[-1] if peak else ''}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, default=0.97)
    ap.add_argument("--gen-per-cycle", type=int, default=40)
    ap.add_argument("--min-heldout", type=int, default=30)
    ap.add_argument("--epochs", type=float, default=3)
    ap.add_argument("--max-hours", type=float, default=24)
    ap.add_argument("--idle-sleep", type=int, default=900)
    a = ap.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "ckpt").mkdir(exist_ok=True)
    deadline = time.time() + a.max_hours * 3600
    state = load_state()
    log(f"loop start target={a.target} best={state['best_adapter']} score={state['best_score']}")

    while time.time() < deadline:
        state["cycle"] += 1
        c = state["cycle"]

        got = 0
        while got < a.gen_per_cycle and time.time() < deadline:
            try:
                batch = gen.generate(8)
            except Exception as exc:  # worker hiccup must not kill an unattended loop
                log(f"gen error: {exc}"); time.sleep(20); continue
            append_scenarios(batch); got += len(batch)
        rows = load_jsonl(SCEN)
        train_rows = [r for r in rows if not is_heldout(r["id"])]
        held = [r for r in rows if is_heldout(r["id"])]
        log(f"cycle {c}: +{got} scenarios, total={len(rows)} train={len(train_rows)} heldout={len(held)}")
        if len(held) < a.min_heldout:
            save_state(state); continue

        held_path = WORK / "heldout.jsonl"
        held_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in held), encoding="utf-8")
        train_path = WORK / "train.jsonl"
        train_path.write_text("\n".join(json.dumps(sft_row(r), ensure_ascii=False) for r in train_rows), encoding="utf-8")

        try:
            cur = evaluate(state["best_adapter"], held_path)
        except Exception as exc:
            log(f"eval error: {exc}"); time.sleep(60); continue
        state["best_score"] = cur["score"]
        log(f"cycle {c}: best='{Path(state['best_adapter']).name}' heldout_score={cur['score']:.3f} fields={cur['fields']}")

        if cur["score"] >= a.target:
            log(f"TARGET MET ({cur['score']:.3f} >= {a.target}); generating at low cadence")
            state["history"].append({"cycle": c, "score": cur["score"], "action": "target_met"})
            save_state(state); time.sleep(a.idle_sleep); continue

        cand = WORK / "ckpt" / f"cycle_{c}"
        if train(train_path, cand, a.epochs):
            try:
                new = evaluate(str(cand), held_path)
            except Exception as exc:
                log(f"candidate eval error: {exc}"); shutil.rmtree(cand, ignore_errors=True); continue
            if new["score"] > cur["score"]:
                log(f"PROMOTED cycle_{c}: {cur['score']:.3f} -> {new['score']:.3f}")
                state.update(best_adapter=str(cand), best_score=new["score"])
                state["history"].append({"cycle": c, "score": new["score"], "action": "promoted"})
            else:
                log(f"rejected cycle_{c}: {new['score']:.3f} <= {cur['score']:.3f}")
                shutil.rmtree(cand, ignore_errors=True)
                state["history"].append({"cycle": c, "score": new["score"], "action": "rejected"})
        save_state(state)
    log("loop finished (deadline)")


if __name__ == "__main__":
    random.seed()
    main()
