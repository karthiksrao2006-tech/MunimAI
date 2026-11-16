"""
OCR backend: a bill photo -> raw text.

EasyOCR runs on the supercomputer (it is installed in the `munimocr` conda
env there, and the box has 48 CPU cores). The laptop sends the image over
Tailscale SSH and gets text back. This keeps the OCR dependency off the laptop
and on the machine that already has it.

The LedgerAgent depends only on the OCRBackend protocol (read(path) -> str),
so this can be swapped for a local EasyOCR, a cloud OCR, or a vision model
later without touching the agent.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

REMOTE_HOST = "admin@100.86.188.31"
# The worker script lives on the box (scripts/ocr_worker.py). We feed ONLY the
# base64 image on stdin — the script itself is a file, so stdin stays free for
# the image (an earlier version passed the script on stdin too, which collided).
# Call the env's python directly, NOT `conda run` — conda run does not pass
# stdin through reliably, which silently delivered an empty image.
_REMOTE_CMD = (
    "/home/apps/miniconda3/envs/munimocr/bin/python "
    "/home/admin/munimai/scripts/ocr_worker.py"
)


class RemoteEasyOCR:
    """Runs EasyOCR on the supercomputer via SSH. One image per call."""

    def __init__(self, host: str = REMOTE_HOST, timeout_s: int = 120) -> None:
        self.host = host
        self.timeout_s = timeout_s

    def read(self, image_path: str) -> str:
        img = Path(image_path).read_bytes()
        b64 = base64.b64encode(img).decode("ascii")
        try:
            proc = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=15", self.host, _REMOTE_CMD],
                input=b64, capture_output=True, text=True, timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"OCR timed out after {self.timeout_s}s") from exc
        if proc.returncode != 0:
            raise RuntimeError(f"OCR failed: {proc.stderr[-400:]}")
        return proc.stdout.strip()


class StubOCR:
    """For tests / offline: returns preset text for a given path."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    def read(self, image_path: str) -> str:
        return self.mapping.get(image_path, "")
