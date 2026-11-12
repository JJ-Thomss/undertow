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


def _first_id(response) -> str:
    """The id of the first item in a response, or a clear error if Apple's
    answer is not shaped as expected."""
    try:
        return response.json()["data"][0]["id"]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise AppleMusicError(
            f"Apple Music answered in an unexpected format: {(response.text or '')[:200]}"
        ) from e


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
        # A POST that may already have reached Apple is never sent twice: a
        # repeat could create a second playlist or add the songs again.
        resend_safe = method != "POST"
        wait = 20.0
        for attempt in range(4):
            last = attempt == 3
            try:
                r = self.session.request(method, url, headers=self._headers(), timeout=30, **kwargs)
            except requests.RequestException as e:
                not_sent = isinstance(e, requests.exceptions.ConnectTimeout)
                if last or not (resend_safe or not_sent):
                    raise AppleMusicError(f"Apple Music is unreachable: {e}") from e
                self.sleep(wait)
                continue
            if r.status_code in ok:
                return r
            if r.status_code == 401:
                raise NotSignedIn(
                    "Apple Music rejected the saved sign-in (it expires every few months). "
                    "Run: undertow login"
                )
            retryable = r.status_code == 429 or (r.status_code >= 500 and resend_safe)
            if retryable and not last:
                self.sleep(wait)
                wait *= 2
                continue
            hint = " If this keeps happening, run: undertow login" if r.status_code == 403 else ""
            raise AppleMusicError(
                f"{method} {path} failed with HTTP {r.status_code}: {(r.text or '')[:200]}.{hint}"
            )
        raise AppleMusicError(f"{method} {path} kept failing")

    def _paged(self, path: str, empty_on_404: bool = False) -> list[dict]:
        out, url, first = [], path, True
        while url:
            r = self._request("GET", url, ok=(200, 404) if first and empty_on_404 else (200,))
            if r.status_code == 404:
                return []
            data = r.json()
            out.extend(data.get("data", []))
            url, first = data.get("next"), False
        return out

    # --- reads ------------------------------------------------------------

    def storefront(self) -> str:
        return _first_id(self._request("GET", "/v1/me/storefront"))

    def children(self, folder_id: str = ROOT_FOLDER) -> list[dict]:
        """Folders and playlists directly inside a folder: [{id, type, name}]."""
        items = self._paged(
            f"/v1/me/library/playlist-folders/{folder_id}/children", empty_on_404=True
        )
        return [
            {"id": c["id"], "type": c.get("type"), "name": c.get("attributes", {}).get("name", "")}
            for c in items
        ]

    def playlists(self) -> list[dict]:
        items = self._paged("/v1/me/library/playlists?limit=100")
        return [{"id": p["id"], "name": p.get("attributes", {}).get("name", "")} for p in items]

    def track_count(self, playlist_id: str) -> int | None:
        """Songs Apple shows in the playlist, or None if it is not visible yet
        (Apple answers 404 for a playlist whose songs have not appeared)."""
        try:
            items = self._paged(
                f"/v1/me/library/playlists/{playlist_id}/tracks?limit=100", empty_on_404=True
            )
        except NotSignedIn:
            raise
        except AppleMusicError:
            return None
        return len(items) or None

    # --- writes -----------------------------------------------------------

    def ensure_folder(self, name: str) -> str:
        for c in self.children(ROOT_FOLDER):
            if c["type"] == FOLDER_TYPE and c["name"].strip().lower() == name.strip().lower():
                return c["id"]
        return _first_id(self._request(
            "POST", "/v1/me/library/playlist-folders",
            json={
                "attributes": {"name": name},
                "relationships": {"parent": {"data": [{"id": ROOT_FOLDER, "type": FOLDER_TYPE}]}},
            },
        ))

    # Creating a playlist is three separate calls so the caller can write the
    # new id down the moment it exists, before anything else can go wrong.

    def create_playlist(self, name: str, description: str) -> str:
        return _first_id(self._request(
            "POST", "/v1/me/library/playlists",
            json={"attributes": {"name": name, "description": description}},
        ))

    def move_to_folder(self, playlist_id: str, folder_id: str) -> None:
        self._request(
            "PUT", f"/v1/me/library/playlists/{playlist_id}/parent",
            json={"data": [{"id": folder_id, "type": FOLDER_TYPE}]},
        )

    def add_songs(self, playlist_id: str, song_ids: list[str]) -> None:
        self._request(
            "POST", f"/v1/me/library/playlists/{playlist_id}/tracks",
            json={"data": [{"id": str(s), "type": "songs"} for s in song_ids]},
        )

    def confirm_tracks(self, playlist_id: str, expected: int) -> int | None:
        """Wait briefly for the library to show the new songs. None = not visible yet."""
        seen = None
        for attempt in range(4):
            seen = self.track_count(playlist_id)
            if seen and seen >= expected:
                break
            if attempt < 3:
                self.sleep(3)
        return seen

    def delete_playlist(self, playlist_id: str) -> None:
        """Delete a playlist. One that is already gone counts as deleted."""
        self._request("DELETE", f"/v1/me/library/playlists/{playlist_id}", ok=(200, 202, 204, 404))
