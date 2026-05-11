"""
Spotify album browser — Spotify Web API via spotipy (no libspotify needed).
Handles OAuth 2.0 Authorization Code + PKCE flow with a local callback server.
"""
from __future__ import annotations
import hashlib
import io
import logging
import os
import socket
import threading
import urllib.parse

log = logging.getLogger(__name__)

_SCOPES        = "user-library-read user-read-playback-state user-modify-playback-state"
_REDIRECT_PORT = 8888
_REDIRECT_HOST = "raspberrypi.local"
_REDIRECT_URI  = f"http://{_REDIRECT_HOST}:{_REDIRECT_PORT}/callback"
_CACHE_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".spotify_token")


class SpotifyBrowser:

    def __init__(self):
        self._lock     = threading.Lock()
        self._sp       = None   # authenticated spotipy.Spotify instance
        self._oauth    = None   # SpotifyOAuth manager
        self._auth_event = threading.Event()
        self._auth_code: str | None = None
        self._server_thread: threading.Thread | None = None

        try:
            import spotipy
            import spotipy.oauth2 as oauth2
            self._spotipy = spotipy
            self._oauth2  = oauth2
            self.available = True
        except ImportError:
            self._spotipy = None
            self._oauth2  = None
            self.available = False
            log.warning("spotipy not installed — Spotify support disabled")

    # ── credentials / authentication ─────────────────────────────────────────

    def configure(self, client_id: str, client_secret: str):
        """Call on startup and whenever credentials change. Restores cached token."""
        if not self.available or not client_id:
            self._sp = None
            self._oauth = None
            return
        try:
            self._oauth = self._oauth2.SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=_REDIRECT_URI,
                scope=_SCOPES,
                cache_handler=self._oauth2.CacheFileHandler(_CACHE_PATH),
                open_browser=False,
            )
            token = self._oauth.get_cached_token()
            if token:
                if self._oauth.is_token_expired(token):
                    token = self._oauth.refresh_access_token(token["refresh_token"])
                if token:
                    self._sp = self._spotipy.Spotify(auth=token["access_token"])
                    log.info("Spotify: token restored from cache")
        except Exception as e:
            log.warning("Spotify configure: %s", e)
            self._sp = None

    def is_authenticated(self) -> bool:
        return self._sp is not None

    def get_auth_url(self) -> str:
        """Start the local callback server and return the Spotify authorization URL."""
        if not self._oauth:
            return ""
        self._auth_event.clear()
        self._auth_code = None
        if self._server_thread is None or not self._server_thread.is_alive():
            self._server_thread = threading.Thread(
                target=self._run_callback_server, daemon=True,
            )
            self._server_thread.start()
        return self._oauth.get_authorize_url()

    def wait_for_auth(self, timeout: float = 300.0) -> bool:
        """Block until the user completes authorization or timeout expires."""
        if not self._auth_event.wait(timeout):
            return False
        code = self._auth_code
        if not code or not self._oauth:
            return False
        try:
            token = self._oauth.get_access_token(code, as_dict=True, check_cache=False)
            if token:
                self._sp = self._spotipy.Spotify(auth=token["access_token"])
                log.info("Spotify: authorized successfully")
                return True
        except Exception as e:
            log.warning("Spotify code exchange: %s", e)
        return False

    def disconnect(self):
        """Remove cached token and clear authenticated state."""
        self._sp = None
        try:
            os.remove(_CACHE_PATH)
        except FileNotFoundError:
            pass

    def _run_callback_server(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("", _REDIRECT_PORT))
                srv.listen(1)
                srv.settimeout(310.0)
                conn, _ = srv.accept()
                with conn:
                    data = conn.recv(4096).decode("utf-8", errors="replace")
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n"
                        b"<h1>Authorized! You can close this tab.</h1>"
                    )
                first_line = data.split("\r\n")[0]
                parts = first_line.split(" ")
                path  = parts[1] if len(parts) > 1 else ""
                qs    = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
                self._auth_code = qs.get("code", [None])[0]
        except Exception as e:
            log.warning("Spotify callback server: %s", e)
        finally:
            self._auth_event.set()

    def _ensure_token(self):
        """Refresh access token if expired."""
        if not self._oauth or not self._sp:
            return
        try:
            token = self._oauth.get_cached_token()
            if token and self._oauth.is_token_expired(token):
                token = self._oauth.refresh_access_token(token["refresh_token"])
                if token:
                    self._sp = self._spotipy.Spotify(auth=token["access_token"])
        except Exception as e:
            log.warning("Spotify token refresh: %s", e)

    # ── library browsing ─────────────────────────────────────────────────────

    def get_saved_albums(self) -> list[dict]:
        """Return saved albums in the same format as MopidyPlayer.get_albums()."""
        if not self._sp:
            return []
        self._ensure_token()
        result = []
        offset = 0
        try:
            while True:
                page = self._sp.current_user_saved_albums(limit=50, offset=offset)
                if not page or not page.get("items"):
                    break
                for item in page["items"]:
                    alb    = item["album"]
                    name   = alb.get("name", "").strip()
                    artist = ", ".join(a["name"] for a in alb.get("artists", []))
                    try:
                        year = int(alb.get("release_date", "9999")[:4])
                    except (ValueError, TypeError):
                        year = 9999
                    uri   = alb.get("uri", "")   # spotify:album:xxx
                    imgs  = alb.get("images", [])
                    result.append({
                        "name":      name,
                        "artist":    artist,
                        "year":      year,
                        "track_uri": uri,
                        "tracks":    None,
                        "thumb":     None,
                        # Spotify-specific extras
                        "_spotify":  True,
                        "_album_id": alb.get("id", ""),
                        "_thumb_url": imgs[0]["url"] if imgs else "",
                    })
                if not page.get("next"):
                    break
                offset += 50
        except Exception as e:
            log.warning("Spotify get_saved_albums: %s", e)
        return sorted(result, key=lambda x: (
            x["artist"].casefold(), x["year"], x["name"].casefold()
        ))

    def get_album_tracks(self, album: dict) -> list[dict]:
        """Return track dicts in the same format as MopidyPlayer.get_album_tracks()."""
        if not self._sp or not album.get("_album_id"):
            return []
        self._ensure_token()
        result = []
        offset = 0
        try:
            while True:
                page = self._sp.album_tracks(album["_album_id"], limit=50, offset=offset)
                if not page or not page.get("items"):
                    break
                for t in page["items"]:
                    result.append({
                        "file":   t.get("uri", ""),   # spotify:track:xxx
                        "title":  t.get("name", ""),
                        "artist": ", ".join(a["name"] for a in t.get("artists", [])),
                        "album":  album["name"],
                        "track":  str(t.get("track_number", 0)),
                        "disc":   str(t.get("disc_number", 1)),
                        "time":   str(t.get("duration_ms", 0) // 1000),
                    })
                if not page.get("next"):
                    break
                offset += 50
        except Exception as e:
            log.warning("Spotify get_album_tracks: %s", e)
        return result

    def fetch_art(self, album: dict) -> "Image.Image | None":
        """Download and return full-size album art from the Spotify CDN."""
        url = album.get("_thumb_url", "")
        if not url:
            return None
        try:
            import requests
            from PIL import Image
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception as e:
            log.warning("Spotify art fetch: %s", e)
            return None

    # ── playback (requires Spotify Premium + a Spotify Connect device) ────────

    def get_devices(self) -> list[dict]:
        if not self._sp:
            return []
        self._ensure_token()
        try:
            result = self._sp.devices()
            return (result or {}).get("devices", [])
        except Exception as e:
            log.warning("Spotify get_devices: %s", e)
            return []

    def _active_device_id(self) -> str | None:
        devs = self.get_devices()
        # prefer whichever is already active, else take first
        for d in devs:
            if d.get("is_active"):
                return d["id"]
        return devs[0]["id"] if devs else None

    def play_album(self, album: dict, track_index: int = 0):
        if not self._sp:
            return
        self._ensure_token()
        album_uri = album.get("track_uri", "")
        if not album_uri.startswith("spotify:album:"):
            return
        dev_id = self._active_device_id()
        try:
            kwargs: dict = {"context_uri": album_uri}
            if track_index > 0:
                tracks = self.get_album_tracks(album)
                if 0 <= track_index < len(tracks):
                    kwargs["offset"] = {"uri": tracks[track_index]["file"]}
            if dev_id:
                kwargs["device_id"] = dev_id
            self._sp.start_playback(**kwargs)
        except Exception as e:
            log.warning("Spotify play_album: %s", e)

    def toggle(self):
        if not self._sp:
            return
        self._ensure_token()
        dev_id = self._active_device_id()
        try:
            pb = self._sp.current_playback()
            if pb and pb.get("is_playing"):
                self._sp.pause_playback(device_id=dev_id)
            else:
                self._sp.start_playback(device_id=dev_id)
        except Exception as e:
            log.warning("Spotify toggle: %s", e)

    def next_track(self):
        if not self._sp:
            return
        self._ensure_token()
        try:
            self._sp.next_track(device_id=self._active_device_id())
        except Exception as e:
            log.warning("Spotify next: %s", e)

    def previous_track(self):
        if not self._sp:
            return
        self._ensure_token()
        try:
            self._sp.previous_track(device_id=self._active_device_id())
        except Exception as e:
            log.warning("Spotify prev: %s", e)
