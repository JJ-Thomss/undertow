"""Small jobs done through the Music app itself on macOS."""

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


def delete_playlists_named(name: str) -> tuple[bool, str]:
    """Backstop for deletion: remove playlists with exactly this name."""
    q = _quote(name)
    return _osascript(
        f'tell application "Music"\n'
        f"  set n to count of (every user playlist whose name is {q})\n"
        f"  if n > 0 then delete (every user playlist whose name is {q})\n"
        f"  return n\n"
        f"end tell"
    )


def notify(title: str, message: str) -> None:
    if available():
        _osascript(f"display notification {_quote(message)} with title {_quote(title)}", timeout=15)
