"""What Undertow remembers between rotations: the playlists it created (the only
ones it will ever delete), everything it has recommended, and what got saved."""

from __future__ import annotations

import json
import os
import secrets
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = {
            "version": 1,
            "salt": secrets.token_hex(4),
            "last_rotation": None,
            "generations": [],  # live playlist sets, oldest first
            "history": [],      # every recommendation ever made
        }
        if path.exists():
            self.data.update(json.loads(path.read_text()))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)

    # --- rotation timing -------------------------------------------------

    def last_rotation(self) -> datetime | None:
        raw = self.data.get("last_rotation")
        return datetime.fromisoformat(raw) if raw else None

    def due(self, now: datetime, rotation_days: int) -> bool:
        last = self.last_rotation()
        # Half a day of slack so a job that runs at the same hour each week
        # is not skipped for being a few minutes early.
        return last is None or now - last >= timedelta(days=rotation_days) - timedelta(hours=12)

    # --- history and feedback ---------------------------------------------

    def recommended_keys(self) -> set[str]:
        return {h["key"] for h in self.data["history"]}

    def recent_artists(self, now: datetime, weeks: int) -> set[str]:
        cutoff = (now - timedelta(weeks=weeks)).isoformat()
        return {h["artist_key"] for h in self.data["history"] if h["date"] >= cutoff}

    def recent_seeds(self, now: datetime, weeks: int) -> dict[str, int]:
        """Seed artist -> how many of the last `weeks` rotations used it."""
        cutoff = (now - timedelta(weeks=weeks)).isoformat()
        seen: dict[str, set[str]] = defaultdict(set)
        for h in self.data["history"]:
            if h["date"] >= cutoff:
                for s in h.get("seeds", []):
                    seen[s].add(h["date"])
        return {s: len(d) for s, d in seen.items()}

    def mark_saved(self, library_keys: set[str]) -> tuple[int, list[str]]:
        """Flag past recommendations that now sit in the library.

        Returns (new saves, suspicious generations). If nearly a whole rotation
        shows up in the library at once, the Music app is auto-adding playlist
        songs to the library, so those are not counted as deliberate saves.
        """
        by_gen: dict[str, list[dict]] = defaultdict(list)
        for h in self.data["history"]:
            by_gen[h.get("generation", "")].append(h)
        new, suspicious = 0, []
        for gen, items in by_gen.items():
            hits = [h for h in items if h["key"] in library_keys]
            if len(items) >= 10 and len(hits) > 0.8 * len(items):
                suspicious.append(gen)
                continue
            for h in hits:
                if not h.get("saved"):
                    h["saved"] = True
                    new += 1
        return new, suspicious

    def seed_multipliers(self) -> dict[str, float]:
        """Seeds whose recommendations get saved steer more; duds steer a bit less.

        Only rotated-out recommendations count as misses, so a song is never
        held against a seed while it is still sitting in a live playlist.
        """
        saves: dict[str, int] = defaultdict(int)
        misses: dict[str, int] = defaultdict(int)
        for h in self.data["history"]:
            for s in h.get("seeds", []):
                if h.get("saved"):
                    saves[s] += 1
                elif h.get("closed"):
                    misses[s] += 1
        return {
            s: min(max(1 + 0.4 * saves[s] - 0.04 * misses[s], 0.5), 3.0)
            for s in set(saves) | set(misses)
        }

    def save_rates(self) -> dict[str, tuple[int, int]]:
        """Playlist kind -> (saved, total) over rotated-out recommendations."""
        out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for h in self.data["history"]:
            if h.get("closed"):
                out[h["kind"]][1] += 1
                out[h["kind"]][0] += 1 if h.get("saved") else 0
        return {k: (v[0], v[1]) for k, v in out.items()}

    def close_generation(self, label: str) -> None:
        for h in self.data["history"]:
            if h.get("generation") == label:
                h["closed"] = True
