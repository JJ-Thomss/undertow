# Undertow

Undertow reads what you actually play in the Music app on your Mac, finds songs
you don't have yet that fit it, and keeps a folder of fresh playlists in Apple
Music. Every week it builds a new set and deletes the previous one.

No songs are bought. Everything is streamed under your Apple Music subscription.

## What you get

A folder called **Undertow** in Apple Music (Mac, iPhone, everywhere your
library syncs) holding four playlists of 25 songs, dated so you can see they
are current:

| Playlist | What is in it |
|---|---|
| **Kindred** | Artists you have nothing by, who are similar to several of your favourites at once |
| **Deep Cuts** | Songs you haven't saved by artists you already play, starting with albums you partly own |
| **Fresh** | Releases from the last few months by your artists and their closest neighbours |
| **Far Shore** | Smaller artists two steps away from your taste |

## Why the picks should land

- **Taste comes from behaviour.** Each song in your library is scored from its
  play count, how recently you played it, whether you favourited it, and how
  often you skipped it. Recent listening outweighs an old phase.
- **Agreement beats popularity.** An artist similar to three of your favourites
  ranks above an artist similar to one. Household names are marked down, since
  you have already heard them and passed.
- **Nothing repeats.** A song is never recommended if it is in your library or
  was ever recommended before. An artist appears once per week and then rests
  for six weeks.
- **It learns.** When you add a recommended song to your library, the artists
  that led to it steer more of the following weeks. Artists whose
  recommendations you keep ignoring steer less.
- **No alternate takes.** Live versions, remixes, demos, karaoke and similar
  are filtered out.

To keep a song, add it to your library or one of your own playlists before the
week is up. The Undertow playlists themselves are replaced.

## Setup (once, on your Mac)

Open Terminal and run these two commands. The second one walks you through
everything, pausing where it needs you:

```bash
git clone -b claude/peaceful-ride-50sue4 https://github.com/JJ-Thomss/undertow.git ~/undertow-src
```

```bash
~/undertow-src/setup.sh
```

What the script does, in order:

1. Installs `uv` (a tool runner that gives Undertow its own Python) and Undertow.
2. Asks you to sign in at [music.apple.com](https://music.apple.com) in Safari
   and switch on two Safari settings: Settings > Advanced > "Show features for
   web developers", then Develop > "Allow JavaScript from Apple Events". You
   can switch the second one off again afterwards.
3. Runs `undertow login`, then `undertow doctor`. macOS asks whether Terminal
   may control Music: allow it. `doctor` prints the artists it thinks are your
   favourites. If that list looks wrong, answer "n" at the next question.
4. Asks before creating anything, then runs `undertow run` to build the first
   playlists and `undertow schedule install` to switch on the weekly rotation.
   macOS asks once more whether "python" may control Music: that is the
   scheduled job asking for itself. Allow it.

The first run is slow (expect half an hour or more), because Apple's free
catalogue lookup pauses callers for a few minutes after a burst of requests.
Undertow waits those pauses out. Results are saved, so later runs are quicker.
To see the picks without creating anything, run `undertow preview`.

## Everyday use

| Command | What it does |
|---|---|
| `undertow status` | Last rotation, live playlists, how many recommendations you have saved |
| `undertow run --force` | Rotate now instead of waiting |
| `undertow preview --reroll 2` | See a different selection without changing anything |
| `undertow cleanup` | List playlists left by earlier setups; add `--delete` to go through them one by one |
| `undertow schedule remove` | Stop the automatic rotation |

Settings are in `~/.undertow/config.toml`: rotation length, playlist size,
which playlists to build, explicit songs on or off, country. A plain-language
report of each rotation, with the reason for every song, is written to
`~/.undertow/reports/`.

## How deletion stays safe

- Undertow deletes only playlists it created itself. It writes each
  playlist's ID to disk the moment the playlist exists and deletes by that ID,
  never by searching for a name.
- New playlists are created first. An old playlist is deleted only once its
  replacement exists. If a run fails outright, last week's set stays. If only
  some playlists could be made, only those are replaced, and you are told.
- A playlist that fails halfway through being made is removed straight away.
- A playlist that could not be deleted is reported and retried next time.
- Playlists from earlier setups are never touched unless you run
  `undertow cleanup --delete` and answer yes for each one.

## How it works, and its limits

| Job | How |
|---|---|
| Read your taste | The Music app on your Mac (AppleScript) |
| Find similar artists | Deezer's public data, no account |
| Find songs in Apple's catalogue | Apple's public iTunes Search service, no account |
| Create the folder and playlists, delete old ones | Apple Music's web-player service, using your own sign-in |
| Weekly schedule | launchd, built into macOS |

Things to know:

- **The sign-in route is unofficial.** The Music app has no scripting command
  for putting a song you don't own into a playlist, and Apple's official
  developer API costs US$99 a year and cannot delete playlists. Undertow uses
  the same service Apple's own web player uses, through the open-source
  [applemusic-mcp](https://github.com/epheterson/applemusic-mcp) sign-in.
  Apple could change it without notice. If that happens Undertow says so and
  leaves your playlists alone.
- **The sign-in expires** every few months. You get a notification saying to
  run `undertow login` again.
- **The Mac has to be on** some time that day. The job checks every morning at
  6:30 and rotates when a week has passed, so a missed day is caught up the next.
- **Save tracking** assumes Music > Settings > Advanced > "Add songs to Library
  when adding to playlists" does not sweep whole Undertow playlists into your
  library. If it does, Undertow notices and ignores those as saves.

## Development

```bash
pip install -e '.[dev]'
```

```bash
pytest
```
