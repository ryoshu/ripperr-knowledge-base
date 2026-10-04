"""Write user-specific launchd jobs for the Mac Pro indexer, MCP and ingest servers.

Set KB_MCP_ALLOWED_HOSTS (comma-separated, e.g. the `tailscale serve` name)
when running this; it is written into the MCP job. No secrets go in a plist.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
PREFIX = "com.ryoshu.ripperr-knowledge-base"
LOG_DIR = HOME / ".local/state/ripperr-knowledge-base"


def main() -> None:
    executable = ROOT / ".venv/bin/kb"
    if not executable.is_file():
        raise SystemExit(f"install dependencies first: {executable} is absent")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    jobs = {
        "indexer": ("serve-indexer", {}),
        "ingest": ("serve-ingest", {}),
        "mcp": ("serve-mcp", {"KB_MCP_ALLOWED_HOSTS": os.environ.get("KB_MCP_ALLOWED_HOSTS", "")}),
    }
    for name, (command, env) in jobs.items():
        label = f"{PREFIX}.{name}"
        plist = HOME / "Library/LaunchAgents" / f"{label}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        job = {
            "Label": label,
            "ProgramArguments": [str(executable), command],
            "WorkingDirectory": str(ROOT),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(LOG_DIR / f"{name}.log"),
            "StandardErrorPath": str(LOG_DIR / f"{name}.log"),
        }
        if env:
            job["EnvironmentVariables"] = env
        plist.write_bytes(plistlib.dumps(job))
        print(plist)


if __name__ == "__main__":
    main()
