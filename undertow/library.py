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
  // Errors must surface (a refused permission would otherwise look like an
  // empty library). Only the favourite flag may be missing: its name changed.
  const get = (prop) => tracks[prop]();
  const opt = (prop) => { try { return tracks[prop](); } catch (e) { return null; } };
  return JSON.stringify({
    name: get('name'),
    artist: get('artist'),
    albumArtist: get('albumArtist'),
    album: get('album'),
    genre: get('genre'),
    playedCount: get('playedCount'),
    skippedCount: get('skippedCount'),
    favorited: opt('favorited') || opt('loved'),
    disliked: opt('disliked'),
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


# A feature credit: "(feat. X)", "(with X)" in brackets, or " feat. X" after the
# name. A bare "with" is an ordinary word ("Stay With Me") and is left alone.
_FEAT = re.compile(
    r"(?<=\S)\s*[\(\[]\s*(feat\.?|featuring|ft\.?|with)\b.*$"
    r"|(?<=\S)\s+(feat\.?|featuring|ft\.?)\s.*$",
    re.I,
)
_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_DASH_SUFFIX = re.compile(
    r"\s+-\s+.*\b(remaster(ed)?|version|edit|mix|mono|stereo|single|deluxe|bonus|anniversary)\b.*$",
    re.I,
)
_NON_ALNUM = re.compile(r"[\W_]+")  # Unicode-aware: keeps letters of every script
_LETTERS = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ð": "d", "đ": "d", "ł": "l",
                          "ß": "ss", "þ": "th"})


def _fold(text: str) -> str:
    """Lower-case and strip accents from Latin letters only, so 'Beyoncé' matches
    'Beyonce' while Japanese, Cyrillic and other scripts keep their letters."""
    out = []
    for ch in unicodedata.normalize("NFC", text or "").casefold():
        base = unicodedata.normalize("NFD", ch)
        out.append(base[0] if len(base) > 1 and "a" <= base[0] <= "z" else ch)
    return "".join(out).translate(_LETTERS).replace("&", " and ")


def _clean(text: str, original: str) -> str:
    cleaned = _NON_ALNUM.sub(" ", text).strip()
    # Names made only of symbols ("!!!") must not all collapse to nothing.
    return cleaned or " ".join((original or "").casefold().split())


def norm_artist(name: str) -> str:
    """Comparable artist name: 'Beyoncé feat. JAY-Z' -> 'beyonce'."""
    text = _FEAT.sub("", _fold(name))
    text = re.sub(r"^the\s+", "", text.strip())
    return _clean(text, name)


def norm_title(title: str) -> str:
    """Comparable song title: 'Creep (2011 Remaster)' -> 'creep'."""
    text = _fold(title)
    text = _DASH_SUFFIX.sub("", text)
    text = _BRACKETS.sub("", text)
    text = _FEAT.sub("", text)
    return _clean(text, title)


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
                " Allow it under System Settings > Privacy & Security > Automation: tick "
                "Music for Terminal (or for python, if this was the scheduled run), then "
                "run this again."
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
