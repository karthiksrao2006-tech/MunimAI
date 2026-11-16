"""
probe_bhashini.py
-----------------
Diagnostic probe. We have a Udyat key and an Inference key (Bhashini's newer
console scheme), but the existing client was written for the older ULCA scheme
(userID + ulcaApiKey). Rather than guess which header format is current, this
script tries each plausible combination against the pipeline-config endpoints
and reports what each one actually returns.

Run:  python probe_bhashini.py
It prints status codes and a short body snippet per attempt. Nothing is
hardcoded; credentials come from .env.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

UDYAT = os.environ.get("BHASHINI_UDYAT_KEY", "").strip()
INFERENCE = os.environ.get("BHASHINI_INFERENCE_KEY", "").strip()

if not UDYAT or not INFERENCE:
    raise SystemExit("Set BHASHINI_UDYAT_KEY and BHASHINI_INFERENCE_KEY in .env")

PIPELINE_ID_MEITY = "64392f96daac500b55c543cd"

CONFIG_ENDPOINTS = [
    "https://meity-auth.ulcacontrib.org/ulca/apis/v0/model/getModelsPipeline",
    "https://meity-auth.ulcacontrib.org/ulca/apis/v0/model/getModelsPipeline/",
]

CONFIG_BODY: dict[str, Any] = {
    "pipelineTasks": [
        {"taskType": "asr", "config": {"language": {"sourceLanguage": "hi"}}}
    ],
    "pipelineRequestConfig": {"pipelineId": PIPELINE_ID_MEITY},
}

# Each candidate is (label, headers dict)
HEADER_CANDIDATES = [
    ("udyat as ulcaApiKey, no userID", {"ulcaApiKey": UDYAT}),
    ("udyat as ulcaApiKey + userID=udyat", {"ulcaApiKey": UDYAT, "userID": UDYAT}),
    ("Authorization: <udyat>", {"Authorization": UDYAT}),
    ("Authorization: Bearer <udyat>", {"Authorization": f"Bearer {UDYAT}"}),
    ("udyat-key header", {"udyat-key": UDYAT}),
    ("udyatKey header", {"udyatKey": UDYAT}),
    ("Authorization: <inference>", {"Authorization": INFERENCE}),
    ("ulcaApiKey=inference", {"ulcaApiKey": INFERENCE}),
]


def snippet(text: str, n: int = 300) -> str:
    text = " ".join(text.split())
    return text[:n] + ("..." if len(text) > n else "")


def probe() -> None:
    print("=" * 72)
    print("BHASHINI CONFIG-CALL PROBE")
    print("=" * 72)
    for url in CONFIG_ENDPOINTS:
        print(f"\n### endpoint: {url}")
        for label, extra_headers in HEADER_CANDIDATES:
            headers = {"Content-Type": "application/json", **extra_headers}
            try:
                resp = requests.post(
                    url, json=CONFIG_BODY, headers=headers, timeout=25
                )
            except requests.RequestException as exc:
                print(f"  [{label:38}] NETWORK ERROR: {exc}")
                continue

            marker = "  <-- WORKS" if resp.ok else ""
            print(f"  [{label:38}] HTTP {resp.status_code}{marker}")
            if resp.ok:
                try:
                    data = resp.json()
                    print("      keys:", list(data.keys()))
                    print("      body:", snippet(json.dumps(data), 600))
                except ValueError:
                    print("      non-JSON body:", snippet(resp.text))
            else:
                print("      body:", snippet(resp.text, 200))


if __name__ == "__main__":
    probe()
