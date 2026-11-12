"""Choose this rotation's songs.

The approach, in order:
  1. Pick a rotating set of "seed" artists from the ones the listener plays most.
  2. Ask Deezer who is similar to each seed. An artist similar to several seeds
     scores highest: independent agreement is the best predictor of a save.
  3. Pull each chosen artist's songs from the Apple catalogue and keep only
     songs the listener does not have and has never been recommended.
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import PLAYLIST_TITLES, Config
from .library import norm_artist, norm_title, track_key
from .sources import Deezer, ITunes, Song, SourceError
from .state import State
from .taste import Taste

SEED_POOL = 60
MAX_ARTISTS_TRIED = 60  # per playlist, bounds catalogue lookups

# Alternate takes are rarely what someone wants from a discovery playlist. The
# words only count inside brackets or after a dash, so "Live Forever" and
# "Mixed Emotions" pass while "Creep (Live)" and "Song - Radio Edit" do not.
_VARIANT = re.compile(
    r"\b(live|karaoke|commentary|instrumental|demo|remix(ed)?|sped up|slowed|skit|interlude|"
    r"voice memo|tribute|made famous|session|acapella|a cappella|rehearsal|acoustic|"
    r"mix|version|edit|reprise|medley|mono|bonus)\b",
    re.I,
)
_DECORATION = re.compile(r"[\(\[]([^\)\]]*)[\)\]]|\s[-\u2013]\s(.*)$")
_ALWAYS = re.compile(r"\b(karaoke|tribute to|made famous|live (at|in|from|on)\b|commentary)", re.I)


def is_variant(text: str) -> bool:
    if _ALWAYS.search(text):
        return True
    return any(_VARIANT.search(a or b or "") for a, b in _DECORATION.findall(text))


@dataclass
class Pick:
    song: Song
    reason: str
    seeds: list[str]  # normalised seed artists credited if this gets saved


@dataclass
class Plan:
    kind: str
    title: str
    picks: list[Pick] = field(default_factory=list)


@dataclass
class Candidate:
    name: str
    deezer_id: int
    fans: int
    score: float = 0.0
    seeds: dict[str, float] = field(default_factory=dict)
    via: str = ""


def weighted_sample(items: list, weights: list[float], k: int, rng: random.Random) -> list:
    """Sample k items without replacement, likelier for heavier weights
    (Efraimidis-Spirakis: sort by u^(1/w))."""
    keyed = [
        (rng.random() ** (1.0 / w), item) for item, w in zip(items, weights) if w > 0
    ]
    keyed.sort(key=lambda kv: -kv[0])
    return [item for _, item in keyed[:k]]


class Recommender:
    def __init__(self, taste: Taste, cfg: Config, state: State, deezer: Deezer,
                 itunes: ITunes, now: datetime, rng: random.Random, progress=None):
        self.taste, self.cfg, self.state = taste, cfg, state
        self.deezer, self.itunes = deezer, itunes
        self.now, self.rng = now, rng
        self.progress = progress or (lambda msg: None)
        self.past_keys = state.recommended_keys()
        self.cooling = state.recent_artists(now, cfg.artist_cooldown_weeks)
        # Known artists come back sooner: there are far fewer of them.
        self.cooling_known = state.recent_artists(now, min(cfg.artist_cooldown_weeks, 2))
        self.used_artists: set[str] = set()
        self.used_keys: set[str] = set()
        self.seeds: list[tuple[str, float]] = []

    # --- seeds and similar artists ----------------------------------------

    def choose_seeds(self) -> list[tuple[str, float]]:
        pool = self.taste.top_artists(SEED_POOL)
        mult = self.state.seed_multipliers()
        recent = self.state.recent_seeds(self.now, 3)
        weights = [
            (score ** 0.75) * mult.get(a, 1.0) * (0.5 ** recent.get(a, 0))
            for a, score in pool
        ]
        chosen = weighted_sample(pool, weights, self.cfg.seeds_per_run, self.rng)
        total = sum(s for _, s in chosen) or 1.0
        self.seeds = [(a, s / total) for a, s in chosen]
        return self.seeds

    def similar_artists(self) -> dict[str, Candidate]:
        cands: dict[str, Candidate] = {}
        for seed, weight in self.seeds:
            name = self.taste.display[seed]
            self.progress(f"similar to {name}")
            try:
                did = self.deezer.artist_id(name)
                related = self.deezer.related(did) if did else []
            except SourceError:
                continue
            for i, rel in enumerate(related):
                key = norm_artist(rel["name"])
                if not key or key in self.taste.blocked:
                    continue
                c = cands.setdefault(key, Candidate(rel["name"], rel["id"], rel["fans"]))
                contribution = weight * (1 - i / 25)
                c.seeds[seed] = c.seeds.get(seed, 0) + contribution
        for c in cands.values():
            # Agreement between seeds matters more than one strong link, and
            # household names are damped: the listener has already heard them.
            agreement = 1 + 0.6 * (len(c.seeds) - 1)
            familiarity = 1 + math.log10(1 + c.fans / 1_000_000)
            c.score = sum(c.seeds.values()) * agreement / familiarity
        return cands

    def second_hop(self, first: dict[str, Candidate]) -> dict[str, Candidate]:
        """Artists similar to the best first-hop artists, favouring smaller ones."""
        parents = sorted(
            (c for k, c in first.items() if not self.taste.knows(c.name)),
            key=lambda c: -c.score,
        )[:14]
        out: dict[str, Candidate] = {}
        for parent in parents:
            self.progress(f"further out from {parent.name}")
            try:
                related = self.deezer.related(parent.deezer_id)
            except SourceError:
                continue
            for i, rel in enumerate(related):
                key = norm_artist(rel["name"])
                if not key or key in first or key in self.taste.blocked or self.taste.knows(rel["name"]):
                    continue
                niche = 1 / (1 + math.log10(1 + rel["fans"] / 50_000))
                score = parent.score * (1 - i / 25) * niche
                c = out.get(key)
                if c is None or score > c.score:
                    out[key] = Candidate(rel["name"], rel["id"], rel["fans"], score,
                                         dict(parent.seeds), via=parent.name)
        return out

    # --- songs ------------------------------------------------------------

    def eligible(self, songs: list[Song], artist: str) -> list[Song]:
        seen_titles: set[str] = set()
        out = []
        for s in songs:
            title = norm_title(s.title)
            key = track_key(artist, s.title)
            if not title or title in seen_titles:
                continue
            if is_variant(s.title) or is_variant(s.album):
                continue
            if not 90 <= s.duration_s <= 600:
                continue
            if s.explicit and not self.cfg.allow_explicit:
                continue
            if key in self.taste.track_keys or key in self.past_keys or key in self.used_keys:
                continue
            seen_titles.add(title)
            out.append(s)
        return out

    def songs_for(self, artist: str, recent: bool = False) -> list[Song]:
        try:
            aid = self.itunes.artist_id(artist)
            return self.eligible(self.itunes.songs(aid, recent=recent), artist) if aid else []
        except SourceError:
            return []

    def take(self, plan: Plan, artist: str, song: Song, reason: str, seeds: list[str]) -> None:
        plan.picks.append(Pick(song, reason, seeds))
        self.used_artists.add(norm_artist(artist))
        self.used_keys.add(track_key(artist, song.title))

    def full(self, plan: Plan) -> bool:
        return len(plan.picks) >= self.cfg.tracks_per_playlist

    def pick_top(self, songs: list[Song]) -> Song:
        """One of the artist's five best-known eligible songs. Not always the
        biggest hit: that is the one the listener is likeliest to know already."""
        top = songs[:5]
        return self.rng.choices(top, weights=[0.3, 0.25, 0.2, 0.15, 0.1][: len(top)])[0]

    def seed_names(self, seeds: dict[str, float], n: int = 2) -> str:
        ranked = sorted(seeds.items(), key=lambda kv: -kv[1])[:n]
        return " and ".join(self.taste.display.get(s, s) for s, _ in ranked)

    # --- the four playlists -----------------------------------------------

    def known_artists(self, n: int) -> list[str]:
        return [
            a for a, _ in self.taste.top_artists(n)
            if a not in self.cooling_known and a not in self.used_artists
        ]

    def build_fresh(self, plan: Plan, similar: dict[str, Candidate]) -> None:
        cutoff = self.now - timedelta(days=self.cfg.fresh_days)
        for artist in self.known_artists(120)[:MAX_ARTISTS_TRIED]:
            if self.full(plan):
                break
            name = self.taste.display[artist]
            self.progress(f"new releases: {name}")
            songs = [s for s in self.songs_for(name, recent=True) if s.released and s.released >= cutoff]
            if songs:
                s = songs[0]
                self.take(plan, name, s, f"New from {name} ({s.released:%b %Y})", [artist])
        # Top up with new releases from the closest artists not in the library.
        ranked = sorted(similar.items(), key=lambda kc: -kc[1].score)
        for key, c in ranked[:30]:
            if self.full(plan):
                break
            if self.taste.knows(c.name) or key in self.cooling or key in self.used_artists:
                continue
            self.progress(f"new releases: {c.name}")
            songs = [s for s in self.songs_for(c.name, recent=True) if s.released and s.released >= cutoff]
            if songs:
                s = songs[0]
                reason = f"New ({s.released:%b %Y}) from {c.name}, close to {self.seed_names(c.seeds)}"
                self.take(plan, c.name, s, reason, list(c.seeds))

    def build_deep_cuts(self, plan: Plan) -> None:
        pool = self.known_artists(120)
        weights = [self.taste.affinity[a] ** 0.5 for a in pool]
        order = weighted_sample(pool, weights, MAX_ARTISTS_TRIED, self.rng)
        for artist in order:
            if self.full(plan):
                break
            name = self.taste.display[artist]
            self.progress(f"deep cuts: {name}")
            songs = self.songs_for(name)[:40]
            if not songs:
                continue
            owned_albums = self.taste.albums.get(artist, set())
            same_album = [s for s in songs if norm_title(s.album) in owned_albums]
            if same_album:
                s = same_album[0]
                reason = f"From {s.album}, an album by {name} you already have songs from"
            else:
                s = self.rng.choice(songs[:8])
                reason = f"More from {name}"
            self.take(plan, name, s, reason, [artist])

    def build_from_candidates(self, plan: Plan, cands: dict[str, Candidate], far: bool) -> None:
        ranked = sorted(
            (
                (k, c) for k, c in cands.items()
                if not self.taste.knows(c.name) and k not in self.cooling and k not in self.used_artists
            ),
            key=lambda kc: -kc[1].score,
        )
        for key, c in ranked[:MAX_ARTISTS_TRIED]:
            if self.full(plan):
                break
            self.progress(f"{plan.title.lower()}: {c.name}")
            songs = self.songs_for(c.name)
            if not songs:
                continue
            s = self.pick_top(songs)
            if far:
                reason = f"Two steps from {self.seed_names(c.seeds, 1)}, by way of {c.via}"
            else:
                reason = f"Close to {self.seed_names(c.seeds)}"
            self.take(plan, c.name, s, reason, list(c.seeds))

    def run(self) -> list[Plan]:
        self.choose_seeds()
        plans = {k: Plan(k, PLAYLIST_TITLES[k]) for k in self.cfg.playlists}
        need_similar = {"kindred", "far_shore", "fresh"} & set(plans)
        first = self.similar_artists() if need_similar else {}
        # Scarcest first, so "fresh" gets first call on artists it shares with the others.
        if "fresh" in plans:
            self.build_fresh(plans["fresh"], first)
        if "deep_cuts" in plans:
            self.build_deep_cuts(plans["deep_cuts"])
        if "kindred" in plans:
            self.build_from_candidates(plans["kindred"], first, far=False)
        if "far_shore" in plans:
            self.build_from_candidates(plans["far_shore"], self.second_hop(first), far=True)
        return [plans[k] for k in self.cfg.playlists]
