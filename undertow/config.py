"""Settings. Defaults live here; ~/.undertow/config.toml overrides them."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG_TOML = """\
# Undertow settings. Delete a line to go back to its default.

folder = "Undertow"        # playlist folder in Apple Music
storefront = "ca"          # Apple Music country code
rotation_days = 7          # how often the playlists are replaced
tracks_per_playlist = 25
allow_explicit = true

# Which playlists to build each rotation, in the order they appear.
#   kindred    best-known songs by artists similar to several of your favourites
#   deep_cuts  songs you don't have yet by artists you already play
#   fresh      releases from the last few months by your artists
#   far_shore  smaller artists two steps away from your taste
playlists = ["kindred", "deep_cuts", "fresh", "far_shore"]

fresh_days = 240           # how recent a release must be for "fresh"
seeds_per_run = 24         # how many of your artists steer each rotation
artist_cooldown_weeks = 6  # a recommended artist is not repeated for this long
"""

PLAYLIST_TITLES = {
    "kindred": "Kindred",
    "deep_cuts": "Deep Cuts",
    "fresh": "Fresh",
    "far_shore": "Far Shore",
}

PLAYLIST_BLURBS = {
    "kindred": "Artists close to several of your favourites.",
    "deep_cuts": "Songs you haven't saved by artists you already play.",
    "fresh": "Recent releases from artists in your library.",
    "far_shore": "Smaller artists two steps out from your taste.",
}


def home() -> Path:
    return Path(os.environ.get("UNDERTOW_HOME", Path.home() / ".undertow"))


@dataclass
class Config:
    folder: str = "Undertow"
    storefront: str = "ca"
    rotation_days: int = 7
    tracks_per_playlist: int = 25
    allow_explicit: bool = True
    playlists: list[str] = field(default_factory=lambda: list(PLAYLIST_TITLES))
    fresh_days: int = 240
    seeds_per_run: int = 24
    artist_cooldown_weeks: int = 6
    # Seconds between iTunes catalogue calls. Apple documents roughly 20 calls
    # a minute for this API, so 3.1 stays under it.
    itunes_interval: float = 3.1

    def validate(self) -> None:
        unknown = [p for p in self.playlists if p not in PLAYLIST_TITLES]
        if unknown:
            raise ValueError(
                f"Unknown playlist type(s) {unknown} in config. Choose from {list(PLAYLIST_TITLES)}."
            )
        if not self.folder.strip():
            raise ValueError("folder must not be empty")
        if self.tracks_per_playlist < 1 or self.rotation_days < 1:
            raise ValueError("tracks_per_playlist and rotation_days must be at least 1")


def load(path: Path | None = None) -> Config:
    path = path or home() / "config.toml"
    cfg = Config()
    if path.exists():
        data = tomllib.loads(path.read_text())
        known = {f.name for f in fields(Config)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown setting(s) in {path}: {sorted(unknown)}")
        for key, value in data.items():
            setattr(cfg, key, value)
    cfg.validate()
    return cfg


def ensure_home() -> Path:
    h = home()
    for sub in ("", "cache", "logs", "reports"):
        (h / sub).mkdir(parents=True, exist_ok=True)
    cfg = h / "config.toml"
    if not cfg.exists():
        cfg.write_text(DEFAULT_CONFIG_TOML)
    return h
