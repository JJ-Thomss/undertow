"""One rotation: learn from last time, pick songs, create the new playlists,
then delete the old ones."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import musicapp
from .applemusic import AppleMusic, AppleMusicError
from .config import PLAYLIST_BLURBS, Config
from .library import norm_artist, track_key
from .recommend import Plan, Recommender
from .state import State
from .taste import Taste

MIN_TRACKS = 5  # a playlist thinner than this is not worth creating


@dataclass
class Outcome:
    created: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    undeleted: list[str] = field(default_factory=list)
    new_saves: int = 0
    plans: list[Plan] = field(default_factory=list)


def day_label(now: datetime) -> str:
    return f"{now:%b} {now.day}"


def plan_rotation(taste: Taste, cfg: Config, state: State, deezer, itunes,
                  now: datetime, progress=None, reroll: int = 0) -> list[Plan]:
    # Same picks if rerun on the same day, different every rotation.
    rng = random.Random(f"{state.data['salt']}:{now:%Y-%m-%d}:{reroll}")
    return Recommender(taste, cfg, state, deezer, itunes, now, rng, progress).run()


def apply_rotation(plans: list[Plan], cfg: Config, state: State, client: AppleMusic,
                   now: datetime, delete_by_name=musicapp.delete_playlists_named) -> Outcome:
    out = Outcome(plans=plans)
    label = now.strftime("%Y-%m-%d %H:%M")
    folder_id = client.ensure_folder(cfg.folder)
    generation = {"label": label, "created": now.isoformat(), "playlists": []}
    state.data["generations"].append(generation)

    for plan in plans:
        if len(plan.picks) < min(MIN_TRACKS, cfg.tracks_per_playlist):
            out.skipped.append(f"{plan.title} (only {len(plan.picks)} songs found)")
            continue
        name = f"{plan.title} · {day_label(now)}"
        description = f"{PLAYLIST_BLURBS[plan.kind]} Undertow, {day_label(now)}."
        try:
            pid, confirmed = client.create_playlist(
                name, description, folder_id, [p.song.song_id for p in plan.picks]
            )
        except AppleMusicError as e:
            out.skipped.append(f"{plan.title} ({e})")
            continue
        entry = {"id": pid, "name": name, "kind": plan.kind,
                 "tracks": len(plan.picks), "confirmed": confirmed}
        generation["playlists"].append(entry)
        out.created.append(entry)
        for p in plan.picks:
            state.data["history"].append({
                "key": track_key(p.song.artist, p.song.title),
                "artist_key": norm_artist(p.song.artist),
                "song_id": p.song.song_id,
                "artist": p.song.artist,
                "title": p.song.title,
                "kind": plan.kind,
                "seeds": p.seeds,
                "date": now.isoformat(),
                "generation": label,
            })
        state.save()  # record each playlist as soon as it exists

    if not out.created:
        # Nothing new was made, so last rotation's playlists stay where they are.
        state.data["generations"].remove(generation)
        state.save()
        return out

    new_names = {p["name"] for p in generation["playlists"]}
    for old in [g for g in state.data["generations"] if g is not generation]:
        remaining = []
        for pl in old["playlists"]:
            try:
                client.delete_playlist(pl["id"])
                out.deleted.append(pl["name"])
            except AppleMusicError:
                # Backstop through the Music app, never for a name just created.
                ok = False
                if musicapp.available() and pl["name"] not in new_names:
                    ok, _ = delete_by_name(pl["name"])
                if ok:
                    out.deleted.append(pl["name"])
                else:
                    remaining.append(pl)
                    out.undeleted.append(pl["name"])
        old["playlists"] = remaining
        state.close_generation(old["label"])
    state.data["generations"] = [
        g for g in state.data["generations"] if g is generation or g["playlists"]
    ]
    state.data["last_rotation"] = now.isoformat()
    state.save()
    return out


def write_report(out: Outcome, state: State, path: Path, now: datetime) -> None:
    lines = [f"# Undertow, {day_label(now)} {now.year}", ""]
    if out.new_saves:
        lines += [f"Since last rotation you saved {out.new_saves} recommended song(s).", ""]
    rates = state.save_rates()
    if rates:
        lines += ["Saved so far, by playlist: " + ", ".join(
            f"{k} {s}/{t}" for k, (s, t) in sorted(rates.items())), ""]
    for plan in out.plans:
        lines += [f"## {plan.title} ({len(plan.picks)})", ""]
        lines += [f"- {p.song.artist}, *{p.song.title}*. {p.reason}." for p in plan.picks]
        lines.append("")
    for title, items in (("Not created", out.skipped), ("Deleted", out.deleted),
                         ("Could not delete", out.undeleted)):
        if items:
            lines += [f"## {title}", ""] + [f"- {i}" for i in items] + [""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
