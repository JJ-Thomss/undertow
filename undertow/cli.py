"""The `undertow` command."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, config, library, musicapp, schedule, taste as taste_mod
from .applemusic import AppleMusic, AppleMusicError, NotSignedIn
from .rotate import apply_rotation, day_label, plan_rotation, write_report
from .sources import Cache, Deezer, Http, ITunes, SourceError
from .state import State


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _sources(cfg: config.Config, home: Path):
    http, cache = Http(), Cache(home / "cache")
    return Deezer(http, cache), ITunes(http, cache, cfg.storefront, cfg.itunes_interval)


def _print_plans(plans) -> None:
    for plan in plans:
        _say(f"\n{plan.title} ({len(plan.picks)} songs)")
        for p in plan.picks:
            _say(f"  {p.song.artist} - {p.song.title}   [{p.reason}]")


def _progress(msg: str) -> None:
    if sys.stdout.isatty():
        print(f"\r\033[K  {msg[:70]}", end="", flush=True)
    else:
        print(f"  {msg}", flush=True)  # keeps the scheduled run's log readable


def _build(args, cfg, home, state):
    tracks = library.read(args.library_json)
    if not tracks:
        raise library.LibraryError("The Music library came back empty.")
    taste = taste_mod.build(tracks)
    _say(f"Read {len(tracks)} songs by {len(taste.display)} artists from your library.")
    new_saves, suspicious = state.mark_saved(taste.track_keys)
    if suspicious:
        _say(
            "Note: almost every song from a past rotation is in your library, so the Music app "
            "is adding playlist songs to your library automatically. Those are not counted as "
            "saves. To fix it, turn off Music > Settings > Advanced > "
            "'Add songs to Library when adding to playlists'."
        )
    _say("Choosing songs. The first run takes 15 to 30 minutes because the catalogue "
         "lookups are rate limited; later runs reuse saved lookups.")
    deezer, itunes = _sources(cfg, home)
    plans = plan_rotation(taste, cfg, state, deezer, itunes, _now(), _progress,
                          reroll=getattr(args, "reroll", 0))
    if sys.stdout.isatty():
        print("\r\033[K", end="")
    return plans, new_saves


def cmd_preview(args) -> int:
    home = config.ensure_home()
    cfg = config.load()
    state = State(home / "state.json")
    plans, _ = _build(args, cfg, home, state)
    _print_plans(plans)
    _say("\nPreview only: nothing was created or deleted.")
    return 0


def cmd_run(args) -> int:
    home = config.ensure_home()
    cfg = config.load()
    state = State(home / "state.json")
    now = _now()
    if not args.force and not state.due(now, cfg.rotation_days):
        _say(f"Not due yet (last rotation {state.last_rotation():%b %d}, "
             f"every {cfg.rotation_days} days). Use --force to rotate now.")
        return 0
    client = AppleMusic()
    try:
        store = client.storefront()  # fail on a dead sign-in before the slow part
        if store != cfg.storefront:
            _say(f"Warning: your Apple Music country is '{store}' but config says "
                 f"'{cfg.storefront}'. Set storefront = \"{store}\" in {home / 'config.toml'}.")
        plans, new_saves = _build(args, cfg, home, state)
        out = apply_rotation(plans, cfg, state, client, now)
    except NotSignedIn as e:
        _say(str(e))
        musicapp.notify("Undertow needs you to sign in", "Open Terminal and run: undertow login")
        return 2
    except (AppleMusicError, library.LibraryError, SourceError) as e:
        _say(f"Rotation failed: {e}")
        musicapp.notify("Undertow could not rotate", str(e)[:120])
        return 1
    out.new_saves = new_saves
    report = home / "reports" / f"{now:%Y-%m-%d}.md"
    write_report(out, state, report, now)
    for pl in out.created:
        note = "" if pl["confirmed"] == pl["tracks"] else f" (Apple confirmed {pl['confirmed']})"
        _say(f"Created  {pl['name']}: {pl['tracks']} songs{note}")
    for item in out.skipped:
        _say(f"Skipped  {item}")
    for name in out.deleted:
        _say(f"Deleted  {name}")
    for name in out.undeleted:
        _say(f"COULD NOT DELETE  {name} (will retry next rotation)")
    if not out.created:
        _say("No playlists were created, so the previous ones were left in place.")
        musicapp.notify("Undertow could not rotate", "No playlists were created. See the log.")
        return 1
    _say(f"Report: {report}")
    musicapp.notify("Undertow", f"{len(out.created)} new playlists are in your {cfg.folder} folder.")
    return 0


def cmd_login(args) -> int:
    _say("Signing in through Safari. If this stops with instructions, follow them and rerun.")
    return subprocess.call([sys.executable, "-m", "applemusic_mcp", "login"])


def cmd_status(args) -> int:
    home = config.ensure_home()
    cfg = config.load()
    state = State(home / "state.json")
    last = state.last_rotation()
    _say(f"Undertow {__version__}   settings: {home / 'config.toml'}")
    _say(f"Last rotation: {last:%a %b %d %Y %H:%M}" if last else "Last rotation: never")
    _say(f"Schedule: {'installed' if schedule.installed() else 'not installed'}"
         f" (rotates every {cfg.rotation_days} days)")
    for gen in state.data["generations"]:
        for pl in gen["playlists"]:
            _say(f"  live: {pl['name']} ({pl['tracks']} songs)")
    _say(f"Songs recommended so far: {len(state.data['history'])}")
    for kind, (saved, total) in sorted(state.save_rates().items()):
        _say(f"  {kind}: {saved} of {total} saved")
    return 0


def cmd_doctor(args) -> int:
    home = config.ensure_home()
    cfg = config.load()
    ok = True

    def check(name: str, fn) -> None:
        nonlocal ok
        try:
            _say(f"[ok]   {name}: {fn()}")
        except Exception as e:  # each check reports its own failure
            ok = False
            _say(f"[FAIL] {name}: {e}")

    def lib():
        t = taste_mod.build(library.read(args.library_json))
        top = ", ".join(t.display[a] for a, _ in t.top_artists(8))
        return f"{len(t.track_keys)} songs. Your top artists look like: {top}"

    def signin():
        store = AppleMusic().storefront()
        hint = "" if store == cfg.storefront else f" (config says '{cfg.storefront}', change it)"
        return f"signed in, country '{store}'{hint}"

    def data():
        deezer, itunes = _sources(cfg, home)
        return (f"Deezer id {deezer.artist_id('Radiohead')}, "
                f"Apple catalogue id {itunes.artist_id('Radiohead')}")

    check("Music library", lib)
    check("Apple Music sign-in", signin)
    check("Catalogue data", data)
    _say(f"[{'ok' if schedule.installed() else '--'}]   Schedule: "
         f"{'installed' if schedule.installed() else 'not installed (undertow schedule install)'}")
    return 0 if ok else 1


def cmd_schedule(args) -> int:
    if args.action == "install":
        path = schedule.install(args.hour, args.minute)
        _say(f"Installed {path}. Undertow checks every day at "
             f"{args.hour:02d}:{args.minute:02d} and rotates when due.")
    elif args.action == "remove":
        _say("Schedule removed." if schedule.remove() else "No schedule was installed.")
    else:
        _say("installed" if schedule.installed() else "not installed")
    return 0


def cmd_cleanup(args) -> int:
    """Find playlists Undertow did not create that look like earlier Undertow runs."""
    home = config.ensure_home()
    cfg = config.load()
    state = State(home / "state.json")
    client = AppleMusic()
    mine = {p["id"] for g in state.data["generations"] for p in g["playlists"]}
    found = {}
    for c in client.children():
        if c["type"] == "library-playlist-folders" and c["name"].strip().lower() == cfg.folder.lower():
            for child in client.children(c["id"]):
                if child["type"] == "library-playlists":
                    found[child["id"]] = child["name"] + f"   (in the {cfg.folder} folder)"
    for p in client.playlists():
        if "undertow" in p["name"].lower():
            found.setdefault(p["id"], p["name"])
    found = {i: n for i, n in found.items() if i not in mine}
    if not found:
        _say("No leftover Undertow playlists found.")
        return 0
    _say("Playlists that look like earlier Undertow runs (not created by this install):")
    for name in found.values():
        _say(f"  {name}")
    if not args.delete:
        _say("\nNothing deleted. To delete them, run: undertow cleanup --delete")
        return 0
    if input(f"\nDelete these {len(found)} playlists for good? Type 'delete' to confirm: ") != "delete":
        _say("Left alone.")
        return 0
    for pid, name in found.items():
        try:
            client.delete_playlist(pid)
            _say(f"Deleted  {name}")
        except AppleMusicError as e:
            _say(f"Could not delete {name}: {e}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="undertow", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def with_library(p):
        p.add_argument("--library-json", type=Path, help=argparse.SUPPRESS)
        p.add_argument("--reroll", type=int, default=0,
                       help="any number; gives a different selection for the same day")

    p = sub.add_parser("doctor", help="check that everything Undertow needs is working")
    p.add_argument("--library-json", type=Path, help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_doctor)
    sub.add_parser("login", help="one-time Apple Music sign-in via Safari").set_defaults(fn=cmd_login)
    p = sub.add_parser("preview", help="show what would be recommended; changes nothing")
    with_library(p)
    p.set_defaults(fn=cmd_preview)
    p = sub.add_parser("run", help="rotate: create new playlists, delete the old ones")
    with_library(p)
    p.add_argument("--force", action="store_true", help="rotate even if not due yet")
    p.set_defaults(fn=cmd_run)
    sub.add_parser("status", help="last rotation, live playlists, save rate").set_defaults(fn=cmd_status)
    p = sub.add_parser("schedule", help="run automatically")
    p.add_argument("action", choices=["install", "remove", "status"])
    p.add_argument("--hour", type=int, default=6)
    p.add_argument("--minute", type=int, default=30)
    p.set_defaults(fn=cmd_schedule)
    p = sub.add_parser("cleanup", help="list (or delete) Undertow playlists from earlier setups")
    p.add_argument("--delete", action="store_true")
    p.set_defaults(fn=cmd_cleanup)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (NotSignedIn, AppleMusicError, library.LibraryError, SourceError, ValueError,
            RuntimeError) as e:
        _say(f"Error: {e}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
