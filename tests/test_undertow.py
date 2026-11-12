import random
from datetime import datetime, timedelta, timezone

import pytest

from undertow import library, taste as taste_mod
from undertow.applemusic import AppleMusic, AppleMusicError, NotSignedIn
from undertow.config import Config
from undertow.library import Track, norm_artist, norm_title, track_key
from undertow.recommend import Pick, Plan, Recommender, is_variant, weighted_sample
from undertow.rotate import apply_rotation
from undertow.schedule import build_plist
from undertow.sources import Song
from undertow.state import State

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


# --- names ---------------------------------------------------------------

def test_normalisation_matches_the_same_song_across_spellings():
    assert norm_artist("Beyoncé feat. JAY-Z") == "beyonce"
    assert norm_artist("The National") == norm_artist("National")
    assert norm_title("Creep (2011 Remaster)") == "creep"
    assert norm_title("Song 2 - 2012 Remaster") == "song 2"
    assert track_key("Sigur Rós", "Hoppípolla") == "sigur ros|hoppipolla"
    assert norm_artist("Simon & Garfunkel") == "simon and garfunkel"


def test_variant_filter_only_reads_decorations():
    assert not is_variant("Live Forever")
    assert not is_variant("Mixed Emotions")
    assert is_variant("Creep (Live)")
    assert is_variant("Song - Radio Edit")
    assert is_variant("Live at Leeds")
    assert not is_variant("Blue (Deluxe Edition)")


def test_parse_dump_keeps_songs_only_and_reads_dates():
    dump = {
        "name": ["A", "B", "C"], "artist": ["X", "Y", ""], "albumArtist": ["X", "Y", ""],
        "album": ["Al", "", ""], "genre": ["", "", ""], "playedCount": [3, None, 1],
        "skippedCount": [0, 0, 0], "favorited": [True, False, False], "disliked": None,
        "dateAdded": ["2026-01-02T10:00:00.000Z", None, None], "playedDate": [None, None, None],
        "mediaKind": ["song", "music video", "song"],
    }
    tracks = library.parse_dump(dump)
    assert [t.name for t in tracks] == ["A"]  # video and artist-less row dropped
    assert tracks[0].favorited and tracks[0].plays == 3
    assert tracks[0].added.year == 2026 and tracks[0].added.tzinfo


# --- taste ---------------------------------------------------------------

def make_taste():
    recent = NOW - timedelta(days=10)
    tracks = [
        Track("Loved One", "Alpha", "First", plays=40, favorited=True, last_played=recent),
        Track("Other", "Alpha", "First", plays=10, last_played=recent),
        Track("Okay", "Beta", "B1", plays=5, last_played=recent),
        Track("Old Phase", "Gamma", "G1", plays=40, last_played=NOW - timedelta(days=1500)),
        Track("Nope", "Hated", "H1", disliked=True),
        Track("Nope 2", "Hated", "H1", disliked=True),
    ]
    return taste_mod.build(tracks, NOW)


def test_taste_ranks_recent_favourites_first_and_blocks_dislikes():
    t = make_taste()
    ranked = [a for a, _ in t.top_artists(10)]
    assert ranked[0] == "alpha"
    assert t.affinity["alpha"] > t.affinity["beta"] > t.affinity["gamma"] > 0
    # 40 plays four years ago count for less than 40 plays last week.
    assert t.affinity["gamma"] < taste_mod.track_score(
        Track("x", "y", plays=40, last_played=NOW), NOW)
    assert "hated" in t.blocked and "hated" not in ranked
    assert t.has("ALPHA", "Loved One (Remastered)")


def test_weighted_sample_has_no_repeats_and_skips_zero_weight():
    got = weighted_sample(list("abcd"), [1, 1, 0, 5], 10, random.Random(1))
    assert sorted(got) == ["a", "b", "d"]


# --- recommendations -----------------------------------------------------

class FakeDeezer:
    RELATED = {
        "Alpha": ["Newcomer", "Shared", "Beta", "Hated"],
        "Beta": ["Shared", "Other New"],
        "Gamma": ["Shared"],
        "Newcomer": ["Distant", "Alpha"],
        "Shared": ["Distant", "Remote"],
        "Other New": ["Remote"],
    }

    def artist_id(self, name):
        return name

    def related(self, artist_id):
        return [{"id": n, "name": n, "fans": 1000} for n in self.RELATED.get(artist_id, [])]


def song(artist, title, rank=0, album="Album", days_old=2000, explicit=False, dur=200):
    return Song(f"{artist}-{title}", title, artist, album, NOW - timedelta(days=days_old),
                explicit, dur, "Alt", rank)


class FakeITunes:
    def __init__(self):
        self.catalogue = {
            "Alpha": [song("Alpha", "Loved One"), song("Alpha", "Unheard", album="First"),
                      song("Alpha", "Brand New", days_old=20)],
            "Beta": [song("Beta", "Okay (Live)"), song("Beta", "Beta Deep")],
            "Gamma": [song("Gamma", "Gamma Deep")],
            "Newcomer": [song("Newcomer", "Hit (Live)"), song("Newcomer", "Hit"),
                         song("Newcomer", "Too Short", dur=30)],
            "Shared": [song("Shared", "Dirty", explicit=True), song("Shared", "Clean")],
            "Other New": [song("Other New", "Past Pick"), song("Other New", "Still New")],
            "Distant": [song("Distant", "Far Song")],
            "Remote": [song("Remote", "Remote Song")],
            "Hated": [song("Hated", "Never")],
        }

    def artist_id(self, name):
        return name if name in self.catalogue else None

    def songs(self, artist_id, recent=False):
        songs = self.catalogue[artist_id]
        return sorted(songs, key=lambda s: s.released, reverse=True) if recent else songs


def run_recommender(tmp_path, cfg=None, history=None):
    state = State(tmp_path / "state.json")
    state.data["history"] = history or []
    cfg = cfg or Config(tracks_per_playlist=5, seeds_per_run=5)
    rec = Recommender(make_taste(), cfg, state, FakeDeezer(), FakeITunes(), NOW, random.Random(7))
    return {p.kind: p for p in rec.run()}, state


def test_recommendations_are_new_unique_and_explained(tmp_path):
    plans, _ = run_recommender(tmp_path)
    picks = [p for plan in plans.values() for p in plan.picks]
    keys = [track_key(p.song.artist, p.song.title) for p in picks]
    artists = [norm_artist(p.song.artist) for p in picks]
    taste = make_taste()
    assert picks
    assert not set(keys) & taste.track_keys           # nothing already in the library
    assert len(set(keys)) == len(keys)                # no song twice
    assert len(set(artists)) == len(artists)          # no artist twice in a rotation
    assert "hated" not in artists                     # disliked artists never appear
    assert all("Live" not in p.song.title and p.song.duration_s >= 90 for p in picks)
    assert all(p.reason and p.seeds for p in picks)

    kindred = {p.song.artist for p in plans["kindred"].picks}
    assert kindred <= {"Newcomer", "Shared", "Other New"}   # only artists not in the library
    assert "Beta" not in kindred
    # "Shared" is similar to three seeds, so it outranks single-seed artists.
    assert plans["kindred"].picks[0].song.artist == "Shared"
    assert {p.song.artist for p in plans["far_shore"].picks} <= {"Distant", "Remote"}
    assert [p.song.title for p in plans["fresh"].picks] == ["Brand New"]
    deep = {p.song.artist for p in plans["deep_cuts"].picks}
    assert deep and deep <= {"Beta", "Gamma"}         # Alpha was used by "fresh"


def test_past_recommendations_are_never_repeated(tmp_path):
    old = (NOW - timedelta(weeks=30)).isoformat()
    history = [{"key": track_key("Other New", "Past Pick"), "artist_key": "other new",
                "kind": "kindred", "seeds": ["beta"], "date": old, "generation": "g0"}]
    plans, _ = run_recommender(tmp_path, history=history)
    titles = {p.song.title for plan in plans.values() for p in plan.picks}
    assert "Past Pick" not in titles and "Still New" in titles


def test_recently_recommended_artists_rest(tmp_path):
    recent = (NOW - timedelta(weeks=1)).isoformat()
    history = [{"key": "shared|something", "artist_key": "shared", "kind": "kindred",
                "seeds": ["alpha"], "date": recent, "generation": "g0"}]
    plans, _ = run_recommender(tmp_path, history=history)
    assert "Shared" not in {p.song.artist for plan in plans.values() for p in plan.picks}


def test_explicit_songs_can_be_excluded(tmp_path):
    cfg = Config(tracks_per_playlist=5, seeds_per_run=5, allow_explicit=False)
    plans, _ = run_recommender(tmp_path, cfg=cfg)
    assert "Dirty" not in {p.song.title for plan in plans.values() for p in plan.picks}


# --- memory --------------------------------------------------------------

def entry(key, gen, seeds=("alpha",), **extra):
    return {"key": key, "artist_key": key.split("|")[0], "kind": "kindred", "seeds": list(seeds),
            "date": NOW.isoformat(), "generation": gen, **extra}


def test_saves_boost_seeds_and_misses_cool_them(tmp_path):
    state = State(tmp_path / "s.json")
    state.data["history"] = [entry(f"a{i}|t", "g1", closed=True) for i in range(12)]
    state.data["history"] += [entry("b|t", "g1", seeds=("beta",), closed=True)]
    new, suspicious = state.mark_saved({"b|t"})
    assert (new, suspicious) == (1, [])
    mult = state.seed_multipliers()
    assert mult["beta"] > 1 > mult["alpha"] >= 0.5
    assert state.save_rates() == {"kindred": (1, 13)}


def test_whole_rotation_in_library_is_not_counted_as_saves(tmp_path):
    state = State(tmp_path / "s.json")
    state.data["history"] = [entry(f"a{i}|t", "g1") for i in range(12)]
    new, suspicious = state.mark_saved({f"a{i}|t" for i in range(12)})
    assert new == 0 and suspicious == ["g1"]


def test_rotation_is_due_weekly_and_state_survives_reload(tmp_path):
    state = State(tmp_path / "s.json")
    assert state.due(NOW, 7)
    state.data["last_rotation"] = (NOW - timedelta(days=3)).isoformat()
    assert not state.due(NOW, 7)
    state.data["last_rotation"] = (NOW - timedelta(days=7, minutes=-5)).isoformat()
    assert state.due(NOW, 7)
    state.save()
    assert State(tmp_path / "s.json").data["salt"] == state.data["salt"]


# --- rotation ------------------------------------------------------------

class FakeClient:
    def __init__(self, fail_create=(), fail_delete=()):
        self.calls, self.live = [], {"stranger": "My Own Playlist"}
        self.fail_create, self.fail_delete = set(fail_create), set(fail_delete)
        self.n = 0

    def ensure_folder(self, name):
        self.calls.append(("folder", name))
        return "f1"

    def create_playlist(self, name, description, folder_id, song_ids):
        if any(f in name for f in self.fail_create):
            raise AppleMusicError("boom")
        self.n += 1
        pid = f"p{self.n}"
        self.live[pid] = name
        self.calls.append(("create", name, folder_id, tuple(song_ids)))
        return pid, len(song_ids)

    def delete_playlist(self, pid):
        if pid in self.fail_delete:
            raise AppleMusicError("nope")
        self.calls.append(("delete", pid))
        self.live.pop(pid, None)


def plans_for(*kinds, n=6):
    titles = {"kindred": "Kindred", "fresh": "Fresh"}
    return [Plan(k, titles[k], [Pick(song(f"{k}{i}", f"T{i}"), "why", ["alpha"]) for i in range(n)])
            for k in kinds]


def test_rotation_creates_new_then_deletes_only_its_own_old_playlists(tmp_path):
    cfg, client, state = Config(), FakeClient(), State(tmp_path / "s.json")
    first = apply_rotation(plans_for("kindred", "fresh"), cfg, state, client, NOW)
    assert [p["name"] for p in first.created] == ["Kindred · Nov 12", "Fresh · Nov 12"]
    assert first.deleted == []

    later = NOW + timedelta(days=7)
    client.calls.clear()
    second = apply_rotation(plans_for("kindred", "fresh"), cfg, state, client, later)
    kinds = [c[0] for c in client.calls]
    assert kinds == ["folder", "create", "create", "delete", "delete"]  # new before old goes
    assert sorted(second.deleted) == ["Fresh · Nov 12", "Kindred · Nov 12"]
    assert sorted(client.live.values()) == ["Fresh · Nov 19", "Kindred · Nov 19", "My Own Playlist"]
    assert len(state.data["generations"]) == 1
    assert state.last_rotation() == later
    old = [h for h in state.data["history"] if h["date"] == NOW.isoformat()]
    assert len(old) == 12 and all(h["closed"] for h in old)
    assert all("stranger" != c[1] for c in client.calls if c[0] == "delete")


def test_failed_rotation_keeps_last_weeks_playlists(tmp_path):
    cfg, state = Config(), State(tmp_path / "s.json")
    client = FakeClient()
    apply_rotation(plans_for("kindred"), cfg, state, client, NOW)
    client.fail_create = {"Kindred"}
    out = apply_rotation(plans_for("kindred"), cfg, state, client, NOW + timedelta(days=7))
    assert not out.created and not out.deleted
    assert "Kindred · Nov 12" in client.live.values()
    assert state.last_rotation() == NOW  # still due, so it retries tomorrow


def test_undeletable_playlist_is_kept_on_the_books_for_retry(tmp_path):
    cfg, state = Config(), State(tmp_path / "s.json")
    client = FakeClient()
    apply_rotation(plans_for("kindred"), cfg, state, client, NOW)
    client.fail_delete = {"p1"}
    out = apply_rotation(plans_for("kindred"), cfg, state, client, NOW + timedelta(days=7),
                         delete_by_name=lambda name: (False, ""))
    assert out.undeleted == ["Kindred · Nov 12"]
    assert [g["playlists"][0]["id"] for g in state.data["generations"]] == ["p1", "p2"]
    client.fail_delete = set()
    out = apply_rotation(plans_for("kindred"), cfg, state, client, NOW + timedelta(days=14))
    assert sorted(out.deleted) == ["Kindred · Nov 12", "Kindred · Nov 19"]


def test_thin_playlists_are_skipped(tmp_path):
    out = apply_rotation(plans_for("kindred", n=2) + plans_for("fresh"), Config(),
                         State(tmp_path / "s.json"), FakeClient(), NOW)
    assert [p["kind"] for p in out.created] == ["fresh"]
    assert "Kindred" in out.skipped[0]


# --- Apple Music client --------------------------------------------------

class Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data, self.text = status, data or {}, str(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, script):
        self.script, self.log = list(script), []

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.log.append((method, url.replace("https://amp-api.music.apple.com", ""), kw.get("json")))
        assert headers["Authorization"] == "Bearer WEB" and headers["Music-User-Token"] == "USER"
        assert headers["Origin"] == "https://music.apple.com"
        return self.script.pop(0)


def client_with(script):
    session = FakeSession(script)
    return AppleMusic(tokens=lambda: ("WEB", "USER"), session=session, sleep=lambda s: None), session


def test_create_playlist_goes_into_the_folder_with_its_songs():
    client, session = client_with([
        Resp(200, {"data": [{"id": "fX", "type": "library-playlist-folders",
                             "attributes": {"name": "undertow"}}]}),
        Resp(201, {"data": [{"id": "p.new"}]}),
        Resp(204),
        Resp(200, {"data": [{"id": "p.new", "type": "library-playlists", "attributes": {"name": "K"}}]}),
        Resp(200, {"data": [{}, {}]}),
    ])
    folder = client.ensure_folder("Undertow")  # found case-insensitively, not recreated
    pid, confirmed = client.create_playlist("K", "desc", folder, ["11", "22"])
    assert (folder, pid, confirmed) == ("fX", "p.new", 2)
    method, path, body = session.log[1]
    assert (method, path) == ("POST", "/v1/me/library/playlists")
    assert body["relationships"]["parent"]["data"] == [{"id": "fX", "type": "library-playlist-folders"}]
    assert session.log[2][1] == "/v1/me/library/playlists/p.new/tracks"
    assert session.log[2][2] == {"data": [{"id": "11", "type": "songs"}, {"id": "22", "type": "songs"}]}


def test_folder_is_created_when_missing():
    client, session = client_with([Resp(200, {"data": []}), Resp(201, {"data": [{"id": "fNew"}]})])
    assert client.ensure_folder("Undertow") == "fNew"
    assert session.log[1][2]["attributes"] == {"name": "Undertow"}


def test_delete_treats_missing_as_done_and_expired_login_is_explicit():
    client, session = client_with([Resp(404), Resp(401)])
    client.delete_playlist("p.gone")
    assert session.log[0][:2] == ("DELETE", "/v1/me/library/playlists/p.gone")
    with pytest.raises(NotSignedIn, match="undertow login"):
        client.delete_playlist("p.x")


def test_schedule_runs_this_install_daily():
    plist = build_plist(6, 30)
    assert plist["ProgramArguments"][1:] == ["-m", "undertow", "run"]
    assert plist["StartCalendarInterval"] == {"Hour": 6, "Minute": 30}


# --- the whole command, end to end with stand-ins --------------------------

def test_run_command_rotates_and_then_waits_until_due(tmp_path, monkeypatch, capsys):
    import json

    from undertow import cli

    recent = (NOW - timedelta(days=10)).isoformat()
    rows = [("Loved One", "Alpha", "First", 40, True), ("Other", "Alpha", "First", 10, False),
            ("Okay", "Beta", "B1", 5, False), ("Old", "Gamma", "G1", 9, False)]
    dump = {
        "name": [r[0] for r in rows], "artist": [r[1] for r in rows],
        "albumArtist": [r[1] for r in rows], "album": [r[2] for r in rows],
        "genre": [""] * 4, "playedCount": [r[3] for r in rows], "skippedCount": [0] * 4,
        "favorited": [r[4] for r in rows], "disliked": [False] * 4,
        "dateAdded": [recent] * 4, "playedDate": [recent] * 4, "mediaKind": ["song"] * 4,
    }
    lib = tmp_path / "lib.json"
    lib.write_text(json.dumps(dump))
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('playlists = ["kindred"]\ntracks_per_playlist = 3\n')
    monkeypatch.setenv("UNDERTOW_HOME", str(home))

    client = FakeClient()
    client.storefront = lambda: "ca"
    itunes = FakeITunes()
    extras = [f"N{i}" for i in range(4, 12)]  # enough unknown artists for two rotations
    for extra in extras:
        FakeDeezer.RELATED["Alpha"].append(extra)
        itunes.catalogue[extra] = [song(extra, f"{extra} Song")]
    monkeypatch.setattr(cli, "AppleMusic", lambda: client)
    monkeypatch.setattr(cli, "_sources", lambda cfg, h: (FakeDeezer(), itunes))
    monkeypatch.setattr(cli.musicapp, "notify", lambda *a: None)
    try:
        assert cli.main(["run", "--library-json", str(lib)]) == 0
    finally:
        pass
    out = capsys.readouterr().out
    assert "Created  Kindred" in out
    created = [c for c in client.calls if c[0] == "create"]
    assert len(created) == 1 and len(created[0][3]) == 3
    assert list((home / "reports").glob("*.md"))

    assert cli.main(["run", "--library-json", str(lib)]) == 0   # same day: nothing to do
    assert "Not due yet" in capsys.readouterr().out
    assert len([c for c in client.calls if c[0] == "create"]) == 1

    try:
        assert cli.main(["run", "--force", "--library-json", str(lib)]) == 0
    finally:
        del FakeDeezer.RELATED["Alpha"][-len(extras):]
    assert [c[0] for c in client.calls].count("delete") == 1     # the first set was removed
    assert len(client.live) == 2                                 # new playlist + the stranger
