"""
The glass box.

Every step an agent takes is appended to a JSONL file on the laptop AND to the
trace_events table. Two sinks on purpose: the file survives a database or
network outage (so a demo run is never lost), the table makes runs queryable.

A trace is one inbound message and everything that followed from it.

Read a run back with:  python -m backend.trace <trace_id>
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import TRACE_DIR

EventType = str  # constrained by the DB CHECK; kept loose here deliberately

_SECRET_KEYS = {
    "password", "token", "api_key", "apikey", "authorization",
    "ulcaapikey", "inference_key", "udyat_key", "pgpassword",
}


def _redact(obj: Any) -> Any:
    """Never let a credential reach a trace file. Traces get shared in demos."""
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if k.lower().replace("-", "_") in _SECRET_KEYS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, str) and len(obj) > 4000:
        return obj[:4000] + f"... <truncated {len(obj) - 4000} chars>"
    return obj


@dataclass
class Trace:
    """One run. Not thread-safe across traces; each message gets its own."""

    trace_id: uuid.UUID = field(default_factory=uuid.uuid4)
    business_id: Optional[int] = None
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _conn: Any = field(default=None, repr=False)
    _path: Optional[Path] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        self._path = TRACE_DIR / f"{self.trace_id}.jsonl"

    def attach_db(self, conn: Any) -> None:
        """Optional. Without it the trace still writes to file."""
        self._conn = conn

    # ------------------------------------------------------------------ emit
    def emit(
        self,
        agent: str,
        event_type: EventType,
        payload: Optional[dict] = None,
        *,
        tool_name: Optional[str] = None,
        parent_seq: Optional[int] = None,
        latency_ms: Optional[int] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
    ) -> int:
        with self._lock:
            self._seq += 1
            seq = self._seq

        record = {
            "trace_id": str(self.trace_id),
            "seq": seq,
            "parent_seq": parent_seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "business_id": self.business_id,
            "agent": agent,
            "event_type": event_type,
            "tool_name": tool_name,
            "payload": _redact(payload or {}),
            "latency_ms": latency_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }

        # File sink first: it must work even if the DB is unreachable.
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # pragma: no cover - disk problems
            print(f"[trace] could not write {self._path}: {exc}", file=sys.stderr)

        if self._conn is not None:
            try:
                self._conn.execute(
                    """INSERT INTO trace_events
                       (trace_id, seq, parent_seq, business_id, agent, event_type,
                        tool_name, payload, latency_ms, tokens_in, tokens_out)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (trace_id, seq) DO NOTHING""",
                    (
                        str(self.trace_id), seq, parent_seq, self.business_id,
                        agent, event_type, tool_name,
                        json.dumps(record["payload"], ensure_ascii=False),
                        latency_ms, tokens_in, tokens_out,
                    ),
                )
                self._conn.commit()
            except Exception as exc:  # the trace must never break the request
                print(f"[trace] db write failed (file sink still ok): {exc}",
                      file=sys.stderr)

        return seq

    # --------------------------------------------------------------- helpers
    @contextmanager
    def step(
        self,
        agent: str,
        event_type: EventType,
        payload: Optional[dict] = None,
        *,
        tool_name: Optional[str] = None,
    ) -> Iterator[dict]:
        """
        Times a step and records its result. Yields a dict the caller can put
        the outcome into:

            with trace.step("ledger", "tool_call", {"file": p}, tool_name="ocr") as out:
                out["result"] = run_ocr(p)
        """
        start = time.perf_counter()
        out: dict = {}
        start_seq = self.emit(agent, event_type, payload, tool_name=tool_name)
        try:
            yield out
        except Exception as exc:
            self.emit(
                agent, "error",
                {"error": repr(exc), "during": event_type},
                parent_seq=start_seq,
                latency_ms=int((time.perf_counter() - start) * 1000),
            )
            raise
        else:
            if out:
                self.emit(
                    agent,
                    "tool_result" if event_type == "tool_call" else f"{event_type}_done",
                    out,
                    tool_name=tool_name,
                    parent_seq=start_seq,
                    latency_ms=int((time.perf_counter() - start) * 1000),
                )

    @property
    def path(self) -> Path:
        assert self._path is not None
        return self._path


# ----------------------------------------------------------------- run tree
_INDENT_FOR = {
    "message_in": 0, "route": 1, "agent_start": 1, "llm_call": 2,
    "tool_call": 2, "tool_result": 3, "verifier": 1, "agent_end": 1,
    "message_out": 0, "error": 2,
}

_MARK = {
    "message_in": "IN ", "message_out": "OUT", "route": "-->",
    "agent_start": "[+]", "agent_end": "[-]", "llm_call": " ~ ",
    "tool_call": " * ", "tool_result": "  =", "verifier": "[?]",
    "error": "!!!",
}


def print_run_tree(trace_id: str, path: Optional[Path] = None) -> None:
    """Render a trace file as an indented run tree, for reading what happened."""
    p = path or (TRACE_DIR / f"{trace_id}.jsonl")
    if not p.exists():
        print(f"No trace file at {p}")
        return

    print(f"\nTrace {trace_id}\n" + "=" * 72)
    total_ms = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        et = e["event_type"]
        indent = "  " * _INDENT_FOR.get(et, 2)
        mark = _MARK.get(et, "   ")
        lat = f"  {e['latency_ms']}ms" if e.get("latency_ms") else ""
        total_ms += e.get("latency_ms") or 0
        label = e.get("tool_name") or e["agent"]
        detail = ""
        payload = e.get("payload") or {}
        if payload:
            first = next(iter(payload.items()))
            text = str(first[1])
            detail = f"  {first[0]}={text[:90]}" + ("..." if len(text) > 90 else "")
        print(f"{indent}{mark} {label:<14}{detail}{lat}")
    print("=" * 72)
    print(f"total measured latency: {total_ms} ms\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m backend.trace <trace_id>")
        raise SystemExit(2)
    print_run_tree(sys.argv[1])
