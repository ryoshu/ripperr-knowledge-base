"""Write a user-specific launchd job for the Mac Pro indexer."""

from __future__ import annotations

import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
LABEL = "com.ryoshu.ripperr-kb.indexer"
LOG_DIR = HOME / ".local/state/ripperr-kb"
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"


def main() -> None:
    executable = ROOT / ".venv/bin/kb-index"
    if not executable.is_file():
        raise SystemExit(f"install dependencies first: {executable} is absent")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    job = {
        "Label": LABEL,
        "ProgramArguments": [str(executable), "serve-indexer"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG_DIR / "indexer.log"),
        "StandardErrorPath": str(LOG_DIR / "indexer.log"),
    }
    PLIST.write_bytes(plistlib.dumps(job))
    print(PLIST)


if __name__ == "__main__":
    main()
