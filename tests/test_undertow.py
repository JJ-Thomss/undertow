import random
from datetime import datetime, timedelta, timezone

import pytest

from undertow import library, taste as taste_mod
from undertow.applemusic import AppleMusic, AppleMusicError, NotSignedIn
from undertow.config import Config
from undertow.library import Track, norm_artist, norm_title, track_key
from undertow.recommend import (Pick, Plan, Recommender, is_seasonal, is_variant,
                                is_variant_album, weighted_sample)
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


def test_titles_with_ordinary_words_survive_normalisation():
    assert norm_title("With or Without You") == "with or without you"
    assert norm_title("Stay With Me") != norm_title("Stay")
    assert norm_title("Song (with Friend)") == norm_title("Song (feat. X)") == "song"
    assert norm_artist("With Confidence") == "with confidence"


def test_non_latin_and_symbol_names_keep_their_identity():
    assert norm_artist("米津玄師") == "米津玄師"
    assert norm_artist("Молчат Дома") == "молчат дома"
    assert norm_artist("!!!") == "!!!" != norm_artist("???")
    assert norm_artist("MØ") == "mo"


def test_variant_filter_only_reads_decorations():
    assert not is_variant("Live Forever")
    assert not is_variant("Mixed Emotions")
    assert not is_variant("Live in the Moment")
    assert is_variant("Creep (Live)")
    assert is_variant("Song - Radio Edit")
    assert is_variant_album("Live at Leeds")
    assert is_variant_album("MTV Unplugged (Live)")
    # Often the only edition Apple carries; its songs are the normal recordings.
    assert not is_variant_album("Blue (Deluxe Version)")
    assert not is_variant_album("1989 (Taylor's Version)")
    assert is_seasonal("On This Christmas", "") and not is_seasonal("Holiday", "Blue")


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
    """Stands in for Apple Music. `fail` maps a step name to playlist-name
    fragments (or ids, for delete) that should fail at that step."""

    def __init__(self, **fail):
        self.calls, self.live = [], {"stranger": "My Own Playlist"}
        self.fail = {k: set(v) for k, v in fail.items()}
        self.error = AppleMusicError
        self.n = 0

    def _maybe_fail(self, step, subject):
        if any(f in subject for f in self.fail.get(step, ())):
            raise self.error(f"{step} failed")

    def storefront(self):
        return "ca"

    def ensure_folder(self, name):
        self.calls.append(("folder", name))
        return "f1"

    def create_playlist(self, name, description):
        self._maybe_fail("create", name)
        self.n += 1
        pid = f"p{self.n}"
        self.live[pid] = name
        self.calls.append(("create", pid, name))
        return pid

    def move_to_folder(self, pid, folder_id):
        self._maybe_fail("move", self.live[pid])
        self.calls.append(("move", pid, folder_id))

    def add_songs(self, pid, song_ids):
        self._maybe_fail("add", self.live[pid])
        self.calls.append(("add", pid, tuple(song_ids)))

    def confirm_tracks(self, pid, expected):
        return expected

    def delete_playlist(self, pid):
        self._maybe_fail("delete", pid)
        self.calls.append(("delete", pid))
        self.live.pop(pid, None)


def plans_for(*kinds, n=6):
    titles = {"kindred": "Kindred", "fresh": "Fresh"}
    return [Plan(k, titles[k], [Pick(song(f"{k}{i}", f"T{i}"), "why", ["alpha"], f"{k}{i}|t{i}", f"{k}{i}")
                                for i in range(n)]) for k in kinds]


def recorded_ids(state):
    return [p["id"] for g in state.data["generations"] for p in g["playlists"]]


def test_rotation_creates_new_then_deletes_only_its_own_old_playlists(tmp_path):
    cfg, client, state = Config(), FakeClient(), State(tmp_path / "s.json")
    first = apply_rotation(plans_for("kindred", "fresh"), cfg, state, client, NOW)
    assert [p["name"] for p in first.created] == ["Kindred · Nov 12", "Fresh · Nov 12"]
    assert first.deleted == []
    assert ("move", "p1", "f1") in client.calls and len(client.calls[3][2]) == 6

    later = NOW + timedelta(days=7)
    client.calls.clear()
    second = apply_rotation(plans_for("kindred", "fresh"), cfg, state, client, later)
    steps = [c[0] for c in client.calls]
    assert steps.index("delete") > max(i for i, s in enumerate(steps) if s == "add")  # new first
    assert sorted(second.deleted) == ["Fresh · Nov 12", "Kindred · Nov 12"]
    assert sorted(client.live.values()) == ["Fresh · Nov 19", "Kindred · Nov 19", "My Own Playlist"]
    assert recorded_ids(state) == ["p3", "p4"]
    assert state.last_rotation() == later
    old = [h for h in state.data["history"] if h["date"] == NOW.isoformat()]
    assert len(old) == 12 and all(h["closed"] for h in old)
    assert {c[1] for c in client.calls if c[0] == "delete"} == {"p1", "p2"}  # never the stranger


def test_failed_rotation_keeps_last_weeks_playlists(tmp_path):
    cfg, state, client = Config(), State(tmp_path / "s.json"), FakeClient()
    apply_rotation(plans_for("kindred"), cfg, state, client, NOW)
    client.fail = {"create": {"Kindred"}}
    out = apply_rotation(plans_for("kindred"), cfg, state, client, NOW + timedelta(days=7))
    assert not out.created and not out.deleted
    assert "Kindred · Nov 12" in client.live.values()
    assert state.last_rotation() == NOW  # still due, so it retries tomorrow
    assert recorded_ids(state) == ["p1"]


def test_only_replaced_playlists_are_deleted_on_partial_success(tmp_path):
    cfg, state, client = Config(), State(tmp_path / "s.json"), FakeClient()
    apply_rotation(plans_for("kindred", "fresh"), cfg, state, client, NOW)
    client.fail = {"create": {"Fresh"}}
    out = apply_rotation(plans_for("kindred", "fresh"), cfg, state, client, NOW + timedelta(days=7))
    assert out.deleted == ["Kindred · Nov 12"]
    assert out.kept == ["Fresh · Nov 12"]           # no replacement, so it stays
    assert sorted(client.live.values()) == ["Fresh · Nov 12", "Kindred · Nov 19", "My Own Playlist"]
    # Next week Fresh is made again and the two-week-old one finally goes.
    client.fail = {}
    out = apply_rotation(plans_for("kindred", "fresh"), cfg, state, client, NOW + timedelta(days=14))
    assert sorted(out.deleted) == ["Fresh · Nov 12", "Kindred · Nov 19"]


def test_half_made_playlist_is_removed_and_never_orphaned(tmp_path):
    cfg, state = Config(), State(tmp_path / "s.json")
    client = FakeClient(add={"Kindred"})
    out = apply_rotation(plans_for("kindred", "fresh"), cfg, state, client, NOW)
    assert [p["kind"] for p in out.created] == ["fresh"]
    assert "Kindred · Nov 12" not in client.live.values()   # created, then cleaned up
    assert recorded_ids(state) == ["p2"]
    assert not any(h["kind"] == "kindred" for h in state.data["history"])

    # If the clean-up delete fails too, the id stays recorded and goes next time.
    state2 = State(tmp_path / "s2.json")
    client2 = FakeClient(add={"Kindred"}, delete={"p1"})
    apply_rotation(plans_for("kindred", "fresh"), cfg, state2, client2, NOW)
    assert recorded_ids(state2) == ["p1", "p2"]
    assert State(tmp_path / "s2.json").data["generations"][0]["playlists"][0]["failed"]
    client2.fail = {}
    out = apply_rotation(plans_for("fresh"), cfg, state2, client2, NOW + timedelta(days=7))
    assert "Kindred · Nov 12" in out.deleted and "p1" not in client2.live


def test_undeletable_playlist_is_kept_on_the_books_for_retry(tmp_path):
    cfg, state, client = Config(), State(tmp_path / "s.json"), FakeClient()
    apply_rotation(plans_for("kindred"), cfg, state, client, NOW)
    client.fail = {"delete": {"p1"}}
    out = apply_rotation(plans_for("kindred"), cfg, state, client, NOW + timedelta(days=7))
    assert out.undeleted == ["Kindred · Nov 12"]
    assert recorded_ids(state) == ["p1", "p2"]
    client.fail = {}
    out = apply_rotation(plans_for("kindred"), cfg, state, client, NOW + timedelta(days=14))
    assert sorted(out.deleted) == ["Kindred · Nov 12", "Kindred · Nov 19"]


def test_expired_sign_in_stops_the_run_and_says_so(tmp_path):
    cfg, state, client = Config(), State(tmp_path / "s.json"), FakeClient()
    apply_rotation(plans_for("kindred"), cfg, state, client, NOW)
    client.fail, client.error = {"create": {"Kindred"}}, NotSignedIn
    with pytest.raises(NotSignedIn):
        apply_rotation(plans_for("kindred"), cfg, state, client, NOW + timedelta(days=7))
    assert recorded_ids(state) == ["p1"] and "p1" in client.live   # nothing lost, nothing deleted
    assert State(tmp_path / "s.json").last_rotation() == NOW


def test_playlist_type_switched_off_in_settings_is_cleared_out(tmp_path):
    state, client = State(tmp_path / "s.json"), FakeClient()
    apply_rotation(plans_for("kindred", "fresh"), Config(), state, client, NOW)
    cfg = Config(playlists=["kindred"])
    out = apply_rotation(plans_for("kindred"), cfg, state, client, NOW + timedelta(days=7))
    assert sorted(out.deleted) == ["Fresh · Nov 12", "Kindred · Nov 12"]


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
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def client_with(script):
    session = FakeSession(script)
    return AppleMusic(tokens=lambda: ("WEB", "USER"), session=session, sleep=lambda s: None), session


def test_playlist_is_created_moved_and_filled_with_the_tested_requests():
    client, session = client_with([
        Resp(200, {"data": [{"id": "fX", "type": "library-playlist-folders",
                             "attributes": {"name": "undertow"}}]}),
        Resp(201, {"data": [{"id": "p.new"}]}), Resp(204), Resp(204),
        Resp(404), Resp(200, {"data": [{}, {}]}),
    ])
    folder = client.ensure_folder("Undertow")  # found case-insensitively, not recreated
    pid = client.create_playlist("K", "desc")
    client.move_to_folder(pid, folder)
    client.add_songs(pid, ["11", "22"])
    assert client.confirm_tracks(pid, 2) == 2          # first look 404s: not visible yet
    assert session.log[1] == ("POST", "/v1/me/library/playlists",
                              {"attributes": {"name": "K", "description": "desc"}})
    assert session.log[2] == ("PUT", "/v1/me/library/playlists/p.new/parent",
                              {"data": [{"id": "fX", "type": "library-playlist-folders"}]})
    assert session.log[3] == ("POST", "/v1/me/library/playlists/p.new/tracks",
                              {"data": [{"id": "11", "type": "songs"}, {"id": "22", "type": "songs"}]})


def test_folder_is_created_when_missing():
    client, session = client_with([Resp(404), Resp(201, {"data": [{"id": "fNew"}]})])
    assert client.ensure_folder("Undertow") == "fNew"
    assert session.log[1][2]["attributes"] == {"name": "Undertow"}


def test_a_post_is_never_sent_twice_but_reads_and_deletes_retry():
    import requests

    client, session = client_with([Resp(502)])
    with pytest.raises(AppleMusicError):
        client.create_playlist("K", "d")
    assert len(session.log) == 1                       # a 502 may mean it was created
    client, session = client_with([requests.exceptions.ReadTimeout()])
    with pytest.raises(AppleMusicError):
        client.add_songs("p1", ["1"])
    assert len(session.log) == 1
    client, session = client_with([Resp(429), Resp(201, {"data": [{"id": "p9"}]})])
    assert client.create_playlist("K", "d") == "p9"   # 429 means it was refused: safe to resend
    client, session = client_with([Resp(503), Resp(204)])
    client.delete_playlist("p1")
    assert len(session.log) == 2


def test_delete_treats_missing_as_done_and_bad_answers_are_explicit():
    client, session = client_with([Resp(404), Resp(401), Resp(201, {"oops": 1})])
    client.delete_playlist("p.gone")
    assert session.log[0][:2] == ("DELETE", "/v1/me/library/playlists/p.gone")
    with pytest.raises(NotSignedIn, match="undertow login"):
        client.delete_playlist("p.x")
    with pytest.raises(AppleMusicError, match="unexpected format"):
        client.create_playlist("K", "d")


def test_settings_reject_wrong_types(tmp_path):
    from undertow import config

    path = tmp_path / "config.toml"
    path.write_text('rotation_days = "7"\n')
    with pytest.raises(ValueError, match="rotation_days"):
        config.load(path)
    path.write_text("playlists = []\n")
    with pytest.raises(ValueError, match="at least one"):
        config.load(path)
    path.write_text("rotation_days = 14\nitunes_interval = 4\n")
    assert config.load(path).rotation_days == 14


def test_schedule_runs_this_install_daily():
    plist = build_plist(6, 30)
    assert plist["ProgramArguments"][1:] == ["-m", "undertow", "run"]
    assert plist["StartCalendarInterval"] == {"Hour": 6, "Minute": 30}
    assert plist["EnvironmentVariables"]["UNDERTOW_SCHEDULED"] == "1"


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
    added = [c for c in client.calls if c[0] == "add"]
    assert len(added) == 1 and len(added[0][2]) == 3
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


# --- fixes from review -----------------------------------------------------

def test_owned_collaboration_is_not_recommended_under_the_solo_name(tmp_path):
    taste = make_taste()
    taste.track_keys.add(track_key("Beta & Friend", "Joint Song"))
    state = State(tmp_path / "s.json")
    state.data["history"] = [{"key": "x|y", "artist_key": "x", "kind": "kindred", "seeds": [],
                              "song_id": "Beta-Seen Before", "date": "2020-01-01", "generation": "g"}]
    rec = Recommender(taste, Config(), state, FakeDeezer(), FakeITunes(), NOW, random.Random(1))
    joint = song("Beta", "Joint Song")
    joint.artist = "Beta & Friend"                       # how Apple credits it
    seen = song("Beta", "Seen Before")                   # same catalogue id as in history
    fine = song("Beta", "Fine")
    assert [s.title for s in rec.eligible([joint, seen, fine], "Beta")] == ["Fine"]


def test_a_dead_data_source_stops_the_run_instead_of_grinding_on(tmp_path):
    from undertow.sources import SourceDown

    class DeadITunes(FakeITunes):
        def artist_id(self, name):
            raise SourceDown("itunes kept refusing requests")

    state = State(tmp_path / "s.json")
    rec = Recommender(make_taste(), Config(seeds_per_run=5), state, FakeDeezer(), DeadITunes(),
                      NOW, random.Random(1))
    with pytest.raises(SourceDown):
        rec.run()


def test_library_read_errors_are_not_mistaken_for_an_empty_library():
    # Every property except the favourite/dislike flags must raise on failure.
    script = library._DUMP_JXA
    assert "const get = (prop) => tracks[prop]();" in script
    assert "favorited: opt('favorited') || opt('loved')" in script
    assert "playedCount: get('playedCount')" in script
