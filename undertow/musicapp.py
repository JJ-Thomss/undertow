"""Small macOS jobs: notifications and the Music permission check."""

from __future__ import annotations

import subprocess
import sys


def available() -> bool:
    return sys.platform == "darwin"


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript(script: str, timeout: int = 120) -> tuple[bool, str]:
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return p.returncode == 0, (p.stdout if p.returncode == 0 else p.stderr).strip()


def ping() -> tuple[bool, str]:
    """Ask the Music app one trivial question. Its only purpose is to make macOS
    show the "allow control of Music" prompt while someone is there to answer."""
    return _osascript('tell application "Music" to get name of library playlist 1', timeout=120)


def notify(title: str, message: str) -> None:
    if available():
        _osascript(f"display notification {_quote(message)} with title {_quote(title)}", timeout=15)
