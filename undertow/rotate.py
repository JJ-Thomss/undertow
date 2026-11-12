"""One rotation: learn from last time, pick songs, create the new playlists,
then delete the old ones."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .applemusic import AppleMusic, AppleMusicError, NotSignedIn
from .config import PLAYLIST_BLURBS, Config
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
    kept: list[str] = field(default_factory=list)  # old playlists with no replacement
    new_saves: int = 0
    plans: list[Plan] = field(default_factory=list)


def day_label(now: datetime) -> str:
    return f"{now:%b} {now.day}"


def plan_rotation(taste: Taste, cfg: Config, state: State, deezer, itunes,
                  now: datetime, progress=None, reroll: int = 0) -> list[Plan]:
    # Same picks if rerun on the same day, different every rotation.
    rng = random.Random(f"{state.data['salt']}:{now:%Y-%m-%d}:{reroll}")
    return Recommender(taste, cfg, state, deezer, itunes, now, rng, progress).run()


def _create(plan: Plan, name: str, description: str, folder_id: str, generation: dict,
            state: State, client: AppleMusic) -> dict:
    """Create one playlist. Its id is written to disk the moment it exists, so
    nothing Undertow makes can be lost track of, whatever fails afterwards."""
    pid = client.create_playlist(name, description)
    entry = {"id": pid, "name": name, "kind": plan.kind, "tracks": len(plan.picks),
             "confirmed": None, "failed": True}
    generation["playlists"].append(entry)
    state.save()
    try:
        client.move_to_folder(pid, folder_id)
        client.add_songs(pid, [p.song.song_id for p in plan.picks])
    except AppleMusicError:
        # Half-made: remove it. If even that fails it stays on the books,
        # flagged, and the next rotation deletes it.
        try:
            client.delete_playlist(pid)
            generation["playlists"].remove(entry)
        except AppleMusicError:
            pass
        state.save()
        raise
    entry["failed"] = False
    entry["confirmed"] = client.confirm_tracks(pid, len(plan.picks))
    return entry


def apply_rotation(plans: list[Plan], cfg: Config, state: State, client: AppleMusic,
                   now: datetime) -> Outcome:
    out = Outcome(plans=plans)
    label = now.strftime("%Y-%m-%d %H:%M:%S")
    folder_id = client.ensure_folder(cfg.folder)
    generation = {"label": label, "created": now.isoformat(), "playlists": []}
    state.data["generations"].append(generation)

    def drop_generation_if_empty() -> None:
        if not generation["playlists"] and generation in state.data["generations"]:
            state.data["generations"].remove(generation)
        state.save()

    for plan in plans:
        if len(plan.picks) < min(MIN_TRACKS, cfg.tracks_per_playlist):
            out.skipped.append(f"{plan.title} (only {len(plan.picks)} songs found)")
            continue
        name = f"{plan.title} · {day_label(now)}"
        description = f"{PLAYLIST_BLURBS[plan.kind]} Undertow, {day_label(now)}."
        try:
            entry = _create(plan, name, description, folder_id, generation, state, client)
        except NotSignedIn:
            drop_generation_if_empty()
            raise  # the same failure would hit every remaining playlist
        except AppleMusicError as e:
            out.skipped.append(f"{plan.title} ({e})")
            continue
        out.created.append(entry)
        for p in plan.picks:
            state.data["history"].append({
                "key": p.key,
                "artist_key": p.artist_key,
                "song_id": p.song.song_id,
                "artist": p.song.artist,
                "title": p.song.title,
                "kind": plan.kind,
                "seeds": p.seeds,
                "date": now.isoformat(),
                "generation": label,
            })
        state.save()

    if not out.created:
        # Nothing new was made, so last rotation's playlists stay where they are.
        drop_generation_if_empty()
        return out

    # An old playlist goes only when its replacement exists (or its kind has been
    # switched off in settings, or it was a half-made leftover). Deletion is by
    # the recorded id and nothing else.
    replaced = {p["kind"] for p in out.created}
    signed_out = None
    for old in [g for g in state.data["generations"] if g is not generation]:
        remaining = []
        for pl in old["playlists"]:
            wanted_gone = pl.get("failed") or pl["kind"] in replaced or pl["kind"] not in cfg.playlists
            if not wanted_gone:
                remaining.append(pl)
                out.kept.append(pl["name"])
                continue
            if signed_out:
                remaining.append(pl)
                out.undeleted.append(pl["name"])
                continue
            try:
                client.delete_playlist(pl["id"])
                out.deleted.append(pl["name"])
            except NotSignedIn as e:
                signed_out = e
                remaining.append(pl)
                out.undeleted.append(pl["name"])
            except AppleMusicError:
                remaining.append(pl)
                out.undeleted.append(pl["name"])
        old["playlists"] = remaining
        state.close_generation(old["label"])
    state.data["generations"] = [
        g for g in state.data["generations"] if g is generation or g["playlists"]
    ]
    state.data["last_rotation"] = now.isoformat()
    state.save()
    if signed_out:
        raise signed_out
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
                         ("Kept from last time (no replacement was made)", out.kept),
                         ("Could not delete (will retry)", out.undeleted)):
        if items:
            lines += [f"## {title}", ""] + [f"- {i}" for i in items] + [""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
