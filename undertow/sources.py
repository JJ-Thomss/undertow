"""Free, no-login music data: Deezer for 'who sounds like whom', iTunes for the
Apple Music catalogue (its track ids are Apple Music song ids)."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from .library import norm_artist

DAY = 86400


class SourceError(RuntimeError):
    pass


class SourceDown(SourceError):
    """A data source stayed unreachable through every retry. The run should
    stop rather than grind through hundreds more lookups that will also fail."""


class Cache:
    """Small JSON-on-disk cache so reruns and later weeks reuse lookups."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / (hashlib.sha1(key.encode()).hexdigest() + ".json")

    def get(self, key: str, max_age: float):
        p = self._path(key)
        try:
            if time.time() - p.stat().st_mtime <= max_age:
                return json.loads(p.read_text())["v"]
        except (OSError, ValueError, KeyError):
            pass
        return None

    def put(self, key: str, value) -> None:
        self._path(key).write_text(json.dumps({"k": key, "v": value}))


class Http:
    """GET with a minimum gap between calls per host and patient retries.

    Apple's catalogue lookup blocks a caller for several minutes (HTTP 403)
    after a burst of roughly 140 requests. That is a pause, not a failure: wait
    it out, then carry on more slowly for the rest of the run.
    """

    WAITS = (60, 120, 240, 300)

    def __init__(self, session=None, sleep=time.sleep, clock=time.monotonic):
        self._own_session = session is None
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock
        self._last: dict[str, float] = {}
        self._slowdown: dict[str, float] = {}
        self.calls = 0

    def _pause(self, host_key: str, why: str, seconds: float) -> None:
        print(f"  {host_key} {why}; waiting {seconds:.0f}s", file=sys.stderr, flush=True)
        self.sleep(seconds)
        self._slowdown[host_key] = min(self._slowdown.get(host_key, 1.0) * 1.5, 4.0)
        if self._own_session:  # a fresh connection sometimes clears a block sooner
            self.session.close()
            self.session = requests.Session()

    def get_json(self, host_key: str, url: str, params: dict, interval: float) -> dict:
        for attempt, backoff in enumerate((*self.WAITS, None)):
            gap = interval * self._slowdown.get(host_key, 1.0)
            wait = self._last.get(host_key, -1e9) + gap - self.clock()
            if wait > 0:
                self.sleep(wait)
            self._last[host_key] = self.clock()
            self.calls += 1
            try:
                r = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                if backoff is None:
                    raise SourceDown(f"{host_key} is unreachable: {e}") from e
                self._pause(host_key, "is unreachable", min(backoff, 60))
                continue
            data = None
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    data = None
                # Deezer reports rate limiting inside a 200 response (error code 4).
                limited = isinstance(data, dict) and (data.get("error") or {}).get("code") == 4
                if isinstance(data, dict) and not limited:
                    return data
            elif r.status_code not in (403, 429) and r.status_code < 500:
                raise SourceError(f"{host_key} answered HTTP {r.status_code} for {url}")
            if backoff is None:
                break
            # Deezer's limit resets within seconds; Apple's takes minutes.
            self._pause(host_key, f"is rate limiting (HTTP {r.status_code})",
                        5 * (attempt + 1) if host_key == "deezer" else backoff)
        raise SourceDown(
            f"{host_key} kept refusing requests. Nothing was changed; lookups so far are "
            f"saved, so trying again later picks up where this stopped."
        )


@dataclass
class Song:
    song_id: str
    title: str
    artist: str
    album: str
    released: datetime | None
    explicit: bool
    duration_s: int
    genre: str
    rank: int  # position in Apple's popularity order for the artist, 0 = top


class Deezer:
    BASE = "https://api.deezer.com"

    def __init__(self, http: Http, cache: Cache):
        self.http, self.cache = http, cache

    def _get(self, path: str, params: dict, max_age: float):
        key = f"deezer:{path}:{sorted(params.items())}"
        hit = self.cache.get(key, max_age)
        if hit is not None:
            return hit
        data = self.http.get_json("deezer", self.BASE + path, params, interval=0.15)
        if "error" in data:  # a one-off error must not be remembered for weeks
            raise SourceError(f"Deezer could not answer {path}: {data['error']}")
        self.cache.put(key, data)
        return data

    def artist_id(self, name: str) -> int | None:
        data = self._get("/search/artist", {"q": name, "limit": 5}, 90 * DAY)
        want = norm_artist(name)
        for item in data.get("data", []):
            if norm_artist(item.get("name", "")) == want:
                return item["id"]
        return None

    def related(self, artist_id: int) -> list[dict]:
        """Similar artists, most similar first: [{id, name, fans}]."""
        data = self._get(f"/artist/{artist_id}/related", {"limit": 20}, 30 * DAY)
        return [
            {"id": a["id"], "name": a["name"], "fans": a.get("nb_fan", 0)}
            for a in data.get("data", [])
            if a.get("name")
        ]


class ITunes:
    BASE = "https://itunes.apple.com"

    def __init__(self, http: Http, cache: Cache, storefront: str = "ca", interval: float = 3.1):
        self.http, self.cache = http, cache
        self.storefront, self.interval = storefront, interval

    def _get(self, path: str, params: dict, max_age: float):
        params = {**params, "country": self.storefront}
        key = f"itunes:{path}:{sorted(params.items())}"
        hit = self.cache.get(key, max_age)
        if hit is not None:
            return hit
        data = self.http.get_json("itunes", self.BASE + path, params, interval=self.interval)
        if data.get("results"):  # an empty answer may be a blip; ask again next time
            self.cache.put(key, data)
        return data

    def artist_id(self, name: str) -> int | None:
        data = self._get(
            "/search", {"term": name, "entity": "musicArtist", "limit": 5}, 180 * DAY
        )
        want = norm_artist(name)
        for item in data.get("results", []):
            if norm_artist(item.get("artistName", "")) == want:
                return item.get("artistId")
        return None

    def songs(self, artist_id: int, recent: bool = False) -> list[Song]:
        """An artist's streamable songs: Apple's popularity order, or newest first.

        One lookup serves both orders (the newest-first list is sorted here), so
        every playlist shares the same saved answer for an artist.
        """
        data = self._get("/lookup", {"id": artist_id, "entity": "song", "limit": 200}, 6 * DAY)
        out = []
        for item in data.get("results", []):
            if item.get("wrapperType") != "track" or item.get("kind") != "song":
                continue
            if item.get("isStreamable") is False or item.get("artistId") != artist_id:
                continue
            released = None
            try:
                released = datetime.fromisoformat(item["releaseDate"].replace("Z", "+00:00"))
            except (KeyError, ValueError, AttributeError):
                pass
            if released and released > datetime.now(timezone.utc):
                continue  # announced but not out yet
            out.append(
                Song(
                    song_id=str(item["trackId"]),
                    title=item.get("trackName", ""),
                    artist=item.get("artistName", ""),
                    album=item.get("collectionName", ""),
                    released=released,
                    explicit=item.get("trackExplicitness") == "explicit",
                    duration_s=int(item.get("trackTimeMillis", 0) / 1000),
                    genre=item.get("primaryGenreName", ""),
                    rank=len(out),
                )
            )
        if recent:
            floor = datetime.min.replace(tzinfo=timezone.utc)
            out.sort(key=lambda song: song.released or floor, reverse=True)
        return out
