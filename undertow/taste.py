"""Turn the library into a picture of what the listener likes."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .library import Track, norm_artist, norm_title, track_key

RECENCY_HALF_LIFE_DAYS = 365.0
NEW_ADD_WINDOW_DAYS = 120.0


def track_score(t: Track, now: datetime) -> float:
    """How much one library track says 'I like this'.

    Plays count on a log scale (the 50th play says less than the 5th) and fade
    with time since the last play, so current taste outweighs an old phase.
    A favourite is worth several plays; skips and dislikes pull the other way.
    """
    if t.disliked:
        return -3.0
    recency = 1.0
    if t.last_played:
        age = max((now - t.last_played).days, 0)
        recency = max(0.5 ** (age / RECENCY_HALF_LIFE_DAYS), 0.2)
    score = math.log1p(t.plays) * recency
    if t.favorited:
        score += 2.5
    if t.added:
        age = max((now - t.added).days, 0)
        if age < NEW_ADD_WINDOW_DAYS:
            score += 1.0 - age / NEW_ADD_WINDOW_DAYS
    score -= 0.5 * math.log1p(t.skips)
    # Simply owning a track is a weak positive signal; it also keeps libraries
    # with no play history (a new Mac) from scoring everything zero.
    return score + 0.15


@dataclass
class Taste:
    affinity: dict[str, float] = field(default_factory=dict)      # norm artist -> score
    display: dict[str, str] = field(default_factory=dict)         # norm artist -> shown name
    track_keys: set[str] = field(default_factory=set)             # every library song
    albums: dict[str, set[str]] = field(default_factory=dict)     # norm artist -> norm albums
    blocked: set[str] = field(default_factory=set)                # artists the listener rejects

    def knows(self, artist: str) -> bool:
        return norm_artist(artist) in self.display

    def has(self, artist: str, title: str) -> bool:
        return track_key(artist, title) in self.track_keys

    def top_artists(self, n: int) -> list[tuple[str, float]]:
        ranked = sorted(self.affinity.items(), key=lambda kv: -kv[1])
        return [(a, s) for a, s in ranked if s > 0 and a not in self.blocked][:n]


def build(tracks: list[Track], now: datetime | None = None) -> Taste:
    now = now or datetime.now(timezone.utc)
    taste = Taste()
    scores: dict[str, float] = defaultdict(float)
    names: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    dislikes: dict[str, int] = defaultdict(int)
    for t in tracks:
        artist = norm_artist(t.artist)
        if not artist:
            continue
        scores[artist] += track_score(t, now)
        names[artist][t.artist] += 1
        taste.track_keys.add(track_key(t.artist, t.name))
        if t.album:
            taste.albums.setdefault(artist, set()).add(norm_title(t.album))
        if t.disliked:
            dislikes[artist] += 1
    for artist, variants in names.items():
        taste.display[artist] = max(variants.items(), key=lambda kv: kv[1])[0]
    taste.affinity = dict(scores)
    taste.blocked = {a for a, s in scores.items() if s < 0 or dislikes[a] >= 2}
    return taste
