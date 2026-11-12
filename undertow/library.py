"""Read the Music app's library (play counts, favourites, skips) on macOS."""

from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# One bulk Apple Event per property, so even a large library reads in seconds.
# "favorited" is the current property name; "loved" is what older macOS calls it.
_DUMP_JXA = r"""
function run() {
  const music = Application('Music');
  const tracks = music.libraryPlaylists[0].tracks;
  const get = (prop) => { try { return tracks[prop](); } catch (e) { return null; } };
  return JSON.stringify({
    name: get('name'),
    artist: get('artist'),
    albumArtist: get('albumArtist'),
    album: get('album'),
    genre: get('genre'),
    playedCount: get('playedCount'),
    skippedCount: get('skippedCount'),
    favorited: get('favorited') || get('loved'),
    disliked: get('disliked'),
    dateAdded: get('dateAdded'),
    playedDate: get('playedDate'),
    mediaKind: get('mediaKind'),
  });
}
"""


@dataclass
class Track:
    name: str
    artist: str
    album: str = ""
    genre: str = ""
    plays: int = 0
    skips: int = 0
    favorited: bool = False
    disliked: bool = False
    added: datetime | None = None
    last_played: datetime | None = None


class LibraryError(RuntimeError):
    pass


_FEAT = re.compile(r"\s*[\(\[]?\s*\b(feat\.?|featuring|ft\.?|with)\b.*$", re.I)
_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_DASH_SUFFIX = re.compile(
    r"\s+-\s+.*\b(remaster(ed)?|version|edit|mix|mono|stereo|single|deluxe|bonus|anniversary)\b.*$",
    re.I,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return text.replace("&", " and ")


def norm_artist(name: str) -> str:
    """Comparable artist name: 'Beyoncé feat. JAY-Z' -> 'beyonce'."""
    text = _FEAT.sub("", _fold(name))
    text = re.sub(r"^the\s+", "", text.strip())
    return _NON_ALNUM.sub(" ", text).strip()


def norm_title(title: str) -> str:
    """Comparable song title: 'Creep (2011 Remaster)' -> 'creep'."""
    text = _fold(title)
    text = _DASH_SUFFIX.sub("", text)
    text = _BRACKETS.sub("", text)
    text = _FEAT.sub("", text)
    return _NON_ALNUM.sub(" ", text).strip()


def track_key(artist: str, title: str) -> str:
    return f"{norm_artist(artist)}|{norm_title(title)}"


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_dump(dump: dict) -> list[Track]:
    """Turn the column-oriented dump into Track rows, songs only."""
    names = dump.get("name") or []
    n = len(names)

    def col(key, default):
        values = dump.get(key)
        return values if isinstance(values, list) and len(values) == n else [default] * n

    kinds = col("mediaKind", "song")
    album_artists = col("albumArtist", "")
    cols = {
        k: col(k, d)
        for k, d in [
            ("artist", ""), ("album", ""), ("genre", ""), ("playedCount", 0),
            ("skippedCount", 0), ("favorited", False), ("disliked", False),
            ("dateAdded", None), ("playedDate", None),
        ]
    }
    tracks = []
    for i in range(n):
        if kinds[i] not in (None, "song", "music"):
            continue
        artist = cols["artist"][i] or album_artists[i] or ""
        if not names[i] or not artist:
            continue
        tracks.append(
            Track(
                name=names[i],
                artist=artist,
                album=cols["album"][i] or "",
                genre=cols["genre"][i] or "",
                plays=int(cols["playedCount"][i] or 0),
                skips=int(cols["skippedCount"][i] or 0),
                favorited=bool(cols["favorited"][i]),
                disliked=bool(cols["disliked"][i]),
                added=_parse_date(cols["dateAdded"][i]),
                last_played=_parse_date(cols["playedDate"][i]),
            )
        )
    return tracks


def read_music_app(timeout: int = 600) -> list[Track]:
    """Ask the Music app for the whole library. macOS only."""
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _DUMP_JXA],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        raise LibraryError(
            "osascript was not found. Undertow reads your library from the Music app, "
            "so it has to run on a Mac."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise LibraryError("The Music app did not answer in time.") from e
    if proc.returncode != 0:
        err = proc.stderr.strip()
        hint = ""
        if "-1743" in err or "Not authorized" in err:
            hint = (
                " Allow it under System Settings > Privacy & Security > Automation "
                "(tick Music for your terminal), then run this again."
            )
        raise LibraryError(f"Could not read the Music library: {err}.{hint}")
    try:
        return parse_dump(json.loads(proc.stdout))
    except json.JSONDecodeError as e:
        raise LibraryError("The Music app returned something unreadable.") from e


def read(library_json: Path | None = None) -> list[Track]:
    """Library from the Music app, or from a saved dump (for testing off a Mac)."""
    if library_json:
        return parse_dump(json.loads(Path(library_json).read_text()))
    return read_music_app()
