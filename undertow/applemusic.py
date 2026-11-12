"""Write to the Apple Music library: folder, playlists, deletion.

The Music app has no scripting command for putting a song you don't own into a
playlist, and Apple's public API cannot delete playlists. Both work through the
service the music.apple.com web player talks to, using the listener's own
signed-in session (see `undertow login`). This is the same route the
applemusic-mcp project uses; it is not an officially documented interface, so
every failure here is reported plainly rather than papered over.
"""

from __future__ import annotations

import os
import time

import requests

AMP = "https://amp-api.music.apple.com"
ORIGIN = "https://music.apple.com"
ROOT_FOLDER = "p.playlistsroot"
FOLDER_TYPE = "library-playlist-folders"


class AppleMusicError(RuntimeError):
    pass


class NotSignedIn(AppleMusicError):
    pass


def stored_tokens() -> tuple[str, str]:
    """(web token, music user token) from the environment or the saved sign-in."""
    web, user = os.environ.get("UNDERTOW_WEB_TOKEN"), os.environ.get("UNDERTOW_USER_TOKEN")
    if web and user:
        return web, user
    try:
        from applemusic_mcp import auth
    except ImportError as e:
        raise NotSignedIn("The sign-in helper is not installed. Reinstall Undertow.") from e
    try:
        return auth.resolve_web_token(), auth.get_user_token()
    except FileNotFoundError as e:
        raise NotSignedIn("Not signed in to Apple Music yet. Run: undertow login") from e
    except Exception as e:
        raise AppleMusicError(f"Could not load the Apple Music sign-in: {e}") from e


class AppleMusic:
    def __init__(self, tokens=stored_tokens, session=None, sleep=time.sleep):
        self._tokens = tokens
        self.session = session or requests.Session()
        self.sleep = sleep

    def _headers(self) -> dict:
        web, user = self._tokens()
        return {
            "Authorization": f"Bearer {web}",
            "Music-User-Token": user,
            "Content-Type": "application/json",
            "Origin": ORIGIN,
        }

    def _request(self, method: str, path: str, ok=(200, 201, 202, 204), **kwargs):
        url = path if path.startswith("http") else AMP + path
        wait = 20.0
        for attempt in range(4):
            try:
                r = self.session.request(method, url, headers=self._headers(), timeout=30, **kwargs)
            except requests.RequestException as e:
                if attempt == 3:
                    raise AppleMusicError(f"Apple Music is unreachable: {e}") from e
                self.sleep(wait)
                continue
            if r.status_code in ok:
                return r
            if r.status_code in (401, 403):
                raise NotSignedIn(
                    "Apple Music rejected the saved sign-in (it expires every few months). "
                    "Run: undertow login"
                )
            if r.status_code == 429 or r.status_code >= 500:
                self.sleep(wait)
                wait *= 2
                continue
            raise AppleMusicError(
                f"{method} {path} failed with HTTP {r.status_code}: {(r.text or '')[:200]}"
            )
        raise AppleMusicError(f"{method} {path} kept failing (rate limited or Apple outage)")

    def _paged(self, path: str) -> list[dict]:
        out, url = [], path
        while url:
            data = self._request("GET", url).json()
            out.extend(data.get("data", []))
            url = data.get("next")
        return out

    # --- reads ------------------------------------------------------------

    def storefront(self) -> str:
        data = self._request("GET", "/v1/me/storefront").json()
        return data["data"][0]["id"]

    def children(self, folder_id: str = ROOT_FOLDER) -> list[dict]:
        """Folders and playlists directly inside a folder: [{id, type, name}]."""
        items = self._paged(f"/v1/me/library/playlist-folders/{folder_id}/children")
        return [
            {"id": c["id"], "type": c.get("type"), "name": c.get("attributes", {}).get("name", "")}
            for c in items
        ]

    def playlists(self) -> list[dict]:
        items = self._paged("/v1/me/library/playlists?limit=100")
        return [{"id": p["id"], "name": p.get("attributes", {}).get("name", "")} for p in items]

    def track_count(self, playlist_id: str) -> int | None:
        r = self._request(
            "GET", f"/v1/me/library/playlists/{playlist_id}/tracks?limit=100", ok=(200, 404)
        )
        return None if r.status_code == 404 else len(r.json().get("data", []))

    # --- writes -----------------------------------------------------------

    def ensure_folder(self, name: str) -> str:
        for c in self.children(ROOT_FOLDER):
            if c["type"] == FOLDER_TYPE and c["name"].strip().lower() == name.strip().lower():
                return c["id"]
        r = self._request(
            "POST", "/v1/me/library/playlist-folders",
            json={
                "attributes": {"name": name},
                "relationships": {"parent": {"data": [{"id": ROOT_FOLDER, "type": FOLDER_TYPE}]}},
            },
        )
        return r.json()["data"][0]["id"]

    def create_playlist(self, name: str, description: str, folder_id: str,
                        song_ids: list[str]) -> tuple[str, int | None]:
        """Create a playlist in the folder and fill it. Returns (id, tracks confirmed)."""
        r = self._request(
            "POST", "/v1/me/library/playlists",
            json={
                "attributes": {"name": name, "description": description},
                "relationships": {"parent": {"data": [{"id": folder_id, "type": FOLDER_TYPE}]}},
            },
        )
        playlist_id = r.json()["data"][0]["id"]
        self._request(
            "POST", f"/v1/me/library/playlists/{playlist_id}/tracks",
            json={"data": [{"id": str(s), "type": "songs"} for s in song_ids]},
        )
        if not any(c["id"] == playlist_id for c in self.children(folder_id)):
            self._request(
                "PUT", f"/v1/me/library/playlists/{playlist_id}/parent",
                json={"data": [{"id": folder_id, "type": FOLDER_TYPE}]},
            )
        # The library can take a moment to show new tracks.
        confirmed = None
        for _ in range(4):
            confirmed = self.track_count(playlist_id)
            if confirmed and confirmed >= len(song_ids):
                break
            self.sleep(3)
        return playlist_id, confirmed

    def delete_playlist(self, playlist_id: str) -> None:
        """Delete a playlist. One that is already gone counts as deleted."""
        self._request("DELETE", f"/v1/me/library/playlists/{playlist_id}", ok=(200, 202, 204, 404))
