#!/bin/bash
# One-step setup for Undertow on a Mac. Safe to run again: each step either
# repeats harmlessly or picks up where it stopped.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
say() { printf '\n== %s\n' "$1"; }

if [ "$(uname)" != "Darwin" ]; then
  echo "Undertow reads your library from the Music app, so this has to run on a Mac."
  exit 1
fi

export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (runs Undertow with its own Python; nothing else on your Mac changes)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

say "Installing Undertow"
uv tool install --force "$here"

cat <<'EOF'

== Before signing in, do this once in Safari:
   1. Open https://music.apple.com and sign in to Apple Music.
   2. Safari > Settings > Advanced > tick "Show features for web developers".
   3. In the Develop menu, tick "Allow JavaScript from Apple Events".
   (You can untick it again once sign-in has worked.)
EOF
read -r -p "Press Return when that is done. "

say "Signing in to Apple Music"
undertow login

say "Checking everything (macOS will ask whether Terminal may control Music: allow it)"
if ! undertow doctor; then
  echo
  echo "Something above is marked FAIL. Fix it, then run this script again."
  exit 1
fi

echo
echo "Look at the favourite artists listed above. If they look like you, carry on."
read -r -p "Create the first Undertow playlists now? This takes half an hour or more. [y/N] " go
case "$go" in
  y|Y|yes|Yes) ;;
  *) echo "Stopped before creating anything. Run 'undertow run' whenever you are ready."; exit 0 ;;
esac

say "Building the first playlists (leave this window open)"
undertow run

say "Switching on the weekly rotation"
undertow schedule install

echo
echo "Done. Open the Undertow folder in Apple Music."
