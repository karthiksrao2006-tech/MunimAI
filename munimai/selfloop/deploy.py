"""
Export the real LedgerAgent prompts and ship the selfloop bundle to the
supercomputer (~/munimai/selfloop). Run from munimai/:  python -m selfloop.deploy
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from backend.agents.ledger import _SCHEMA_HINT, _SYSTEM

HERE = Path(__file__).resolve().parent
REMOTE = "admin@100.86.188.31"


def main() -> None:
    system = (f"{_SYSTEM}\n\nRespond with ONLY a valid JSON object matching this "
              f"shape: {_SCHEMA_HINT}. No prose, no code fences.")
    (HERE / "prompts.json").write_text(json.dumps({"system": system}, indent=2), encoding="utf-8")
    (HERE / "__init__.py").touch()
    subprocess.run(["ssh", REMOTE, "mkdir -p ~/munimai/selfloop ~/munimai/selfloop_data"], check=True)
    files = [str(p) for p in HERE.glob("*") if p.suffix in {".py", ".json"}]
    subprocess.run(["scp", "-q", *files, f"{REMOTE}:munimai/selfloop/"], check=True)
    print(f"deployed {len(files)} files")


if __name__ == "__main__":
    main()
