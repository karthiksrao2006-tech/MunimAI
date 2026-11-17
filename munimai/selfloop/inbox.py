"""
Tiny token-protected inbox so the gateway can hand real user inputs to the
loop. Stdlib only. Seeds are appended to seeds.jsonl and wake the loop.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 8192


def start(seeds_path: Path, token: str, wake: threading.Event, port: int = 8091) -> None:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the loop log clean
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            if self.path != "/seed" or n > MAX_BODY or self.headers.get("X-Seed-Token") != token:
                self.send_response(403); self.end_headers(); return
            try:
                rec = json.loads(self.rfile.read(n))
                if rec.get("kind") not in ("bill", "question") or not str(rec.get("text", "")).strip():
                    raise ValueError
            except ValueError:
                self.send_response(400); self.end_headers(); return
            with lock, seeds_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({k: rec.get(k) for k in ("kind", "text", "language", "ts")},
                                   ensure_ascii=False) + "\n")
            wake.set()
            self.send_response(204); self.end_headers()

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
