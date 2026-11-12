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

## Setup (once, on your Mac, about 10 minutes)

1. Install the `uv` tool runner, then Undertow:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

   ```bash
   git clone https://github.com/JJ-Thomss/undertow.git ~/undertow-src
   ```

   ```bash
   uv tool install ~/undertow-src
   ```

2. In Safari, sign in at [music.apple.com](https://music.apple.com). Then turn
   on Safari > Settings > Advanced > "Show features for web developers", and in
   the Develop menu tick "Allow JavaScript from Apple Events". You can untick
   it again after the next step.

3. Sign Undertow in and check everything:

   ```bash
   undertow login
   ```

   ```bash
   undertow doctor
   ```

   The first time, macOS asks whether Terminal may control Music. Allow it.
   `doctor` prints the artists it thinks are your favourites; if that list
   looks wrong, stop and tell me before going further.

4. Look before anything is created:

   ```bash
   undertow preview
   ```

5. Create the first set and switch on the weekly rotation:

   ```bash
   undertow run
   ```

   ```bash
   undertow schedule install
   ```

The first `preview` or `run` takes 15 to 30 minutes, because Apple's free
catalogue lookup allows about 20 requests a minute. Results are saved, so
later runs are quicker.

## Everyday use

| Command | What it does |
|---|---|
| `undertow status` | Last rotation, live playlists, how many recommendations you have saved |
| `undertow run --force` | Rotate now instead of waiting |
| `undertow preview --reroll 2` | See a different selection without changing anything |
| `undertow cleanup` | List Undertow playlists left by earlier setups; add `--delete` to remove them |
| `undertow schedule remove` | Stop the automatic rotation |

Settings are in `~/.undertow/config.toml`: rotation length, playlist size,
which playlists to build, explicit songs on or off, country. A plain-language
report of each rotation, with the reason for every song, is written to
`~/.undertow/reports/`.

## How deletion stays safe

- Undertow deletes only playlists it created itself. It records each
  playlist's ID when it makes it and deletes by that ID, never by searching
  for a name.
- New playlists are created first. The old set is deleted only after that
  succeeds, so a failed run leaves last week's playlists in place.
- A playlist that could not be deleted is reported and retried next time.
- Playlists from earlier setups are never touched unless you run
  `undertow cleanup --delete` and type `delete` to confirm.

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
