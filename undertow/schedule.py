"""Run Undertow automatically with launchd (macOS's built-in scheduler).

The job fires every morning and `undertow run` exits straight away unless a
rotation is due, so a Mac that was off on rotation day catches up the next day.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import home

LABEL = "com.undertow.rotate"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(hour: int = 6, minute: int = 30) -> dict:
    log = str(home() / "logs" / "run.log")
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "undertow", "run"],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "UNDERTOW_HOME": str(home()),
        },
        "ProcessType": "Background",
    }


def _launchctl(*args: str) -> tuple[bool, str]:
    p = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def install(hour: int = 6, minute: int = 30) -> str:
    if sys.platform != "darwin":
        raise RuntimeError("Scheduling uses launchd, which only exists on macOS.")
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(build_plist(hour, minute)))
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")  # replace any earlier copy
    ok, msg = _launchctl("bootstrap", domain, str(path))
    if not ok:
        raise RuntimeError(f"launchctl could not load the job: {msg}")
    return str(path)


def remove() -> bool:
    path = plist_path()
    if sys.platform == "darwin":
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def installed() -> bool:
    return plist_path().exists()
