"""
Mopidy client.

Two separate MPD connections:
  _ctrl  – polled every 0.5 s for status / current song
  _browse – used on-demand for library queries (avoids blocking the poll lock)

Album art / images come from the Mopidy HTTP JSON-RPC API because it handles
all back-ends (local files, Spotify, etc.) transparently.
"""
from __future__ import annotations
import base64
import os
import threading
import time
import io
import logging
import shutil
import subprocess
import urllib.parse

import mpd
import requests
from PIL import Image

from config import (
    MOPIDY_HOST, MOPIDY_MPD_PORT, MOPIDY_HTTP_PORT, MOPIDY_PASSWORD,
    VOLUME_BACKEND, MUSIC_DIR,
)

log = logging.getLogger(__name__)


def _detect_volume_backend() -> str:
    """Return whichever system audio tool is available, or 'mpd' as fallback."""
    for tool in ("wpctl", "pactl", "amixer"):
        if shutil.which(tool):
            return tool
    return "mpd"


def _sys_setvol(backend: str, vol: int):
    """Set system volume (0-100) via the given backend. Raises on failure."""
    if backend == "wpctl":
        subprocess.run(
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{vol}%"],
            check=True, timeout=2,
        )
    elif backend == "pactl":
        subprocess.run(
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{vol}%"],
            check=True, timeout=2,
        )
    elif backend == "amixer":
        subprocess.run(
            ["amixer", "set", "Master", f"{vol}%"],
            check=True, timeout=2,
        )
    else:
        raise ValueError(f"unknown backend: {backend}")


def _make_client() -> mpd.MPDClient:
    c = mpd.MPDClient()
    c.timeout = 8
    return c


def _connect(client: mpd.MPDClient) -> bool:
    try:
        client.connect(MOPIDY_HOST, MOPIDY_MPD_PORT)
        if MOPIDY_PASSWORD:
            client.password(MOPIDY_PASSWORD)
        return True
    except Exception as e:
        log.warning("MPD connect failed: %s", e)
        return False


_MOPIDY_CONF = os.path.expanduser("~/.config/mopidy/mopidy.conf")

class _SpotifyWebAPI:
    """Minimal Spotify Web API client using Client Credentials auth.

    Used as a last-resort fallback when mopidy-spotify can't fetch tracks for
    an album URI. Reads credentials from mopidy.conf so they stay in sync.
    """
    _TOKEN_URL = "https://accounts.spotify.com/api/token"
    _API_BASE  = "https://api.spotify.com/v1"

    def __init__(self):
        self._token: str       = ""
        self._token_expires: float = 0.0
        self._lock = threading.Lock()

    def _credentials(self) -> tuple[str, str]:
        client_id = client_secret = ""
        try:
            in_spotify = False
            with open(_MOPIDY_CONF) as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("["):
                        in_spotify = s == "[spotify]"
                    elif in_spotify:
                        if s.startswith("client_id"):
                            client_id = s.split("=", 1)[1].strip()
                        elif s.startswith("client_secret"):
                            client_secret = s.split("=", 1)[1].strip()
        except Exception as e:
            log.warning("Could not read Spotify credentials: %s", e)
        return client_id, client_secret

    def _ensure_token(self):
        if time.monotonic() < self._token_expires:
            return
        client_id, client_secret = self._credentials()
        if not client_id or not client_secret:
            log.warning("Spotify Web API: no credentials found in mopidy.conf")
            return
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        try:
            r = requests.post(
                self._TOKEN_URL,
                headers={"Authorization": f"Basic {creds}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials"},
                timeout=10,
            )
            data = r.json()
            if "access_token" in data:
                self._token = data["access_token"]
                self._token_expires = time.monotonic() + data.get("expires_in", 3600) - 60
                log.info("Spotify Web API token acquired (expires in %ss)", data.get("expires_in"))
            else:
                log.warning("Spotify Web API token error (status %s): %r", r.status_code, data)
        except Exception as e:
            log.warning("Spotify token request failed: %s", e)

    def album_track_uris(self, album_id: str) -> list[str]:
        """Return spotify:track:xxx URIs for every track in the album."""
        with self._lock:
            self._ensure_token()
            if not self._token:
                return []
            uris: list[str] = []
            url = f"{self._API_BASE}/albums/{album_id}/tracks"
            params: dict = {"limit": 50}
            while url:
                try:
                    r = requests.get(url, headers={"Authorization": f"Bearer {self._token}"},
                                     params=params, timeout=10)
                    data = r.json()
                    if "error" in data:
                        log.warning("Spotify API error for %s: %s", album_id, data["error"])
                        break
                    for item in data.get("items", []):
                        if item and item.get("uri"):
                            uris.append(item["uri"])
                    url    = data.get("next")  # pagination
                    params = {}                 # already in the next URL
                except Exception as e:
                    log.warning("Spotify API request failed: %s", e)
                    break
            return uris


_spotify_web = _SpotifyWebAPI()


class MopidyPlayer:
    # ── init ──────────────────────────────────────────────────────────────────

    def __init__(self):
        # Resolve volume backend once at startup
        if VOLUME_BACKEND == "auto":
            self._vol_backend = _detect_volume_backend()
        else:
            self._vol_backend = VOLUME_BACKEND
        log.info("Volume backend: %s", self._vol_backend)

        # Control connection (polled for status)
        self._ctrl = _make_client()
        self._ctrl_ok = _connect(self._ctrl)
        self._ctrl_lock = threading.Lock()

        # Dedicated MPD volume connection — only used when backend == "mpd"
        self._vol = _make_client()
        self._vol_ok   = _connect(self._vol)
        self._vol_lock = threading.Lock()
        self._vol_pending: int | None = None
        self._vol_sending: bool       = False

        self._status: dict = {}
        self._song: dict   = {}
        # Set to a future monotonic time while play_album / play_album_fast is
        # rebuilding the queue.  The auto-advance in _poll_loop must not fire
        # during this window — the tracklist.clear causes a spurious stop-state.
        self._queue_rebuild_until: float = 0.0
        # Full sorted track list for the currently loaded album, used by the
        # recovery path when Mopidy-Spotify clears the queue (qlen=0) after a
        # clicked track ends.
        self._active_tracks: list[dict] = []
        self._active_album_uri: str = ""   # set by play_album_fast for fast recovery
        self._recovery_in_progress: bool = False

        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()

    # ── internal: status poll ─────────────────────────────────────────────────

    def _poll_loop(self):
        _prev_had_next  = False
        _stop_since     = 0.0     # monotonic time when "stop/pause" state first seen
        _stopped_song: dict = {}  # track URI captured when stop first detected
        _last_play_song: dict = {}
        _prev_state     = ""
        while True:
            if not self._ctrl_ok:
                time.sleep(3)
                with self._ctrl_lock:
                    self._ctrl_ok = _connect(self._ctrl)
                continue
            try:
                with self._ctrl_lock:
                    status = self._ctrl.status()
                    song   = self._ctrl.currentsong()
                state = status.get("state", "")
                if state == "play":
                    if _prev_state != "play":
                        log.info("poll: → play  qlen=%s  single=%s  nextsong=%s  song=%s",
                                 status.get("playlistlength", "—"),
                                 status.get("single", "0"),
                                 status.get("nextsong", "—"),
                                 song.get("file", "—"))
                    _last_play_song = dict(song)
                    _prev_had_next  = "nextsong" in status
                    _stop_since     = 0.0
                    # Mopidy-Spotify sets single=1 internally when
                    # core.playback.play(tlid=X) is called, which hides nextsong
                    # and disables auto-advance.  Reset it immediately.
                    if status.get("single", "0") != "0":
                        log.info("poll: detected single=1 during play — resetting")
                        self._cmd("single", 0)
                elif state in ("stop", "pause"):
                    if state != "stop":
                        # Capture song from pause state too — currentsong() is
                        # populated during pause, unlike stop.
                        if song.get("file"):
                            _last_play_song = dict(song)
                        if _stop_since == 0.0:
                            log.info("poll: → pause  qlen=%s  song=%s",
                                     status.get("playlistlength", "—"),
                                     song.get("file", "—"))
                            _stop_since = time.monotonic()  # suppress subsequent polls
                    elif time.monotonic() < self._queue_rebuild_until:
                        # Queue is being rebuilt — ignore stop state entirely.
                        if _stop_since == 0.0:
                            log.info("poll: state=stop  suppressed (rebuild)  qlen=%s",
                                     status.get("playlistlength", "—"))
                        _prev_had_next = False
                        _stop_since    = 0.0
                    elif _stop_since == 0.0:
                        _stop_since   = time.monotonic()
                        # currentsong() is often empty at stop time; use the last
                        # song we saw while state was "play" or "pause" instead.
                        _stopped_song = _last_play_song
                        log.info("poll: → stop  had_next=%s  qlen=%s  active=%d  song=%s",
                                 _prev_had_next,
                                 status.get("playlistlength", "—"),
                                 len(self._active_tracks),
                                 _last_play_song.get("file", "—"))
                    # Only act after a 1 s debounce — avoids firing during brief
                    # stop-states that occur when seeking or rebuilding the queue.
                    elif time.monotonic() - _stop_since > 1.0:
                        qlen = int(status.get("playlistlength", "0") or "0")
                        if not self._active_tracks:
                            # No album loaded — nothing to recover; silence the spam
                            # by resetting the debounce timer.
                            _stop_since = 0.0
                        elif qlen == 0 and not self._recovery_in_progress:
                            # Mopidy-Spotify cleared the queue after a clicked track
                            # ended (single-track context mode).  _prev_had_next is
                            # False because Spotify reports no nextsong in this mode,
                            # so we trigger recovery on qlen=0 unconditionally.
                            _had = _prev_had_next
                            _prev_had_next = False
                            _stop_since    = 0.0
                            _uri = _stopped_song.get("file", "")
                            log.warning("poll: qlen=0 after stop  had_next=%s  from=%s — recovering",
                                        _had, _uri or "—")
                            self._recovery_in_progress = True
                            threading.Thread(
                                target=self._recover_next, args=(_uri,), daemon=True
                            ).start()
                        elif qlen > 0:
                            # Queue intact — Mopidy stopped without advancing.
                            # Happens when single=1 was set (nextsong absent, so
                            # _prev_had_next may be False).  Use MPD next+play to
                            # advance regardless.
                            _had = _prev_had_next
                            _prev_had_next = False
                            _stop_since    = 0.0
                            log.warning("poll: qlen=%d  had_next=%s — forcing next+play",
                                        qlen, _had)
                            with self._ctrl_lock:
                                try:
                                    self._ctrl.next()
                                    self._ctrl.play()
                                    status = self._ctrl.status()
                                    song   = self._ctrl.currentsong()
                                except Exception as e:
                                    log.warning("auto-advance failed: %s", e)
                _prev_state = state
                with self._ctrl_lock:
                    self._status = status
                    self._song   = song
            except Exception as e:
                log.warning("Poll error: %s", e)
                self._ctrl_ok = False
            time.sleep(0.5)

    # ── internal: control command ─────────────────────────────────────────────

    def _cmd(self, name: str, *args):
        if not self._ctrl_ok:
            return
        try:
            with self._ctrl_lock:
                getattr(self._ctrl, name)(*args)
        except Exception as e:
            log.warning("Command %s failed: %s", name, e)
            self._ctrl_ok = False

    # ── internal: one-shot browse connection ──────────────────────────────────

    def _browse(self, fn):
        """
        Open a temporary MPD connection, call fn(client), disconnect.
        Returns fn's return value, or None on error.
        Separate from _ctrl so browsing never blocks the poll loop.
        """
        c = _make_client()
        try:
            if not _connect(c):
                return None
            result = fn(c)
            c.disconnect()
            return result
        except Exception as e:
            log.warning("Browse error: %s", e)
            try:
                c.disconnect()
            except Exception:
                pass
            return None

    # ── internal: HTTP/RPC helper ─────────────────────────────────────────────

    def _rpc(self, method: str, **params):
        try:
            r = requests.post(
                f"http://{MOPIDY_HOST}:{MOPIDY_HTTP_PORT}/mopidy/rpc",
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=8,
            )
            body = r.json()
            if "error" in body:
                log.warning("RPC %s error: %s", method, body["error"])
            return body.get("result")
        except Exception as e:
            log.warning("RPC %s: %s", method, e)
            return None

    # ── status accessors ──────────────────────────────────────────────────────

    def get_status(self) -> dict:
        with self._ctrl_lock:
            return dict(self._status)

    def get_current_song(self) -> dict:
        with self._ctrl_lock:
            return dict(self._song)

    @property
    def is_playing(self) -> bool:
        return self.get_status().get("state") == "play"

    # ── playback commands ─────────────────────────────────────────────────────

    def play(self):
        self._cmd("play")
        with self._ctrl_lock:
            self._status["state"] = "play"

    def pause(self):
        self._cmd("pause", 1)
        with self._ctrl_lock:
            self._status["state"] = "pause"

    def toggle(self):
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def stop(self):
        self._cmd("stop")
        self._cmd("clear")
        with self._ctrl_lock:
            self._status["state"] = "stop"
            self._song = {}

    def _reset_tracklist_options(self):
        """Ensure single/repeat/random/consume are off before starting album playback."""
        self._cmd("single", 0)
        self._cmd("repeat", 0)
        self._cmd("random", 0)
        self._cmd("consume", 0)

    def next(self):
        self._cmd("next")

    def previous(self):
        self._cmd("previous")

    def set_volume(self, vol: int):
        with self._vol_lock:
            self._vol_pending = max(0, min(100, int(vol)))
            if not self._vol_sending:
                self._vol_sending = True
                threading.Thread(target=self._send_volume, daemon=True).start()

    def _send_volume(self):
        while True:
            with self._vol_lock:
                v = self._vol_pending
                self._vol_pending = None
            if v is None:
                with self._vol_lock:
                    self._vol_sending = False
                return
            # All calls happen outside the lock so set_volume never blocks
            try:
                if self._vol_backend == "mpd":
                    if self._vol_ok:
                        self._vol.setvol(v)
                    else:
                        self._cmd("setvol", v)
                else:
                    _sys_setvol(self._vol_backend, v)
            except Exception as e:
                log.warning("setvol failed (%s): %s", self._vol_backend, e)

    def seek(self, fraction: float):
        status   = self.get_status()
        time_str = status.get("time", "")
        parts    = time_str.split(":") if time_str else []
        dur_s    = float(parts[1]) if len(parts) >= 2 else 0.0
        if dur_s <= 0:
            song  = self.get_current_song()
            dur_s = float(song.get("time", 0) or 0)
        if dur_s > 0:
            self._cmd("seekcur", str(fraction * dur_s))

    def play_track_in_queue(self, pos: int, track: dict | None = None):
        """Play the given track in the current queue.

        Looks up the track by URI via Mopidy JSON-RPC so the tlid (not
        position) is used — avoids mismatches when the UI sort order differs
        from the MPD playlist order. Falls back to position-based play.
        """
        with self._ctrl_lock:
            self._status["state"] = "play"
            if track:
                self._song = {
                    "title":  track.get("title", ""),
                    "artist": track.get("artist") or track.get("albumartist", ""),
                    "album":  track.get("album", ""),
                    "file":   track.get("file", ""),
                }
        uri = track.get("file", "") if track else ""
        log.info("play_track_in_queue: pos=%d  uri=%s", pos, uri or "—")
        def _play(uri=uri, pos=pos):
            if uri:
                tl_tracks = self._rpc("core.tracklist.get_tl_tracks") or []
                for tlt in tl_tracks:
                    t = (tlt.get("track") or {}) if isinstance(tlt, dict) else {}
                    if t.get("uri") == uri:
                        log.info("play_track_in_queue: found tlid=%s", tlt["tlid"])
                        self._rpc("core.playback.play", tlid=tlt["tlid"])
                        return
                log.warning("play_track_in_queue: uri not found in queue, falling back to pos=%d", pos)
            self._cmd("play", str(pos))
        threading.Thread(target=_play, daemon=True).start()

    def set_song_optimistic(self, song: dict):
        """Immediately update the current-song cache without waiting for the poll loop."""
        with self._ctrl_lock:
            self._song = dict(song)

    # ── library browsing ──────────────────────────────────────────────────────

    def get_albums(self, library: str = "local") -> list[dict]:
        """Return albums from the given library ('local' or 'spotify')."""
        if library == "spotify":
            return self._get_albums_spotify()
        return self._get_albums_local()

    def _get_albums_local(self) -> list[dict]:
        """Return local library albums via Mopidy RPC browse, sorted by artist/year/album."""
        refs = self._rpc("core.library.browse", uri="local:directory:") or []
        album_uris = [r["uri"] for r in refs if r.get("type") == "album" and r.get("uri")]
        if not album_uris:
            log.warning("Local: no albums found in local:directory:")
            return []
        lookup = self._rpc("core.library.lookup", uris=album_uris) or {}
        result = []
        for ref in refs:
            uri  = ref.get("uri", "")
            name = ref.get("name", "").strip()
            if not uri or not name:
                continue
            tracks = lookup.get(uri, [])
            artist, year = "", 9999
            if tracks:
                t        = tracks[0]
                alb_obj  = t.get("album") if isinstance(t.get("album"), dict) else {}
                artists  = alb_obj.get("artists", []) or t.get("artists", [])
                artist   = artists[0].get("name", "") if artists else ""
                date     = t.get("date") or alb_obj.get("date", "")
                try:
                    year = int(str(date).split("-")[0])
                except (ValueError, IndexError):
                    year = 9999
            result.append({
                "name":      name,
                "artist":    artist,
                "year":      year,
                "track_uri": uri,   # local:album:md5:xxx — works with core.library.get_images
                "tracks":    None,
                "thumb":     None,
            })
        return sorted(result, key=lambda x: (x["artist"].casefold(), x["year"], x["name"].casefold()))

    def _get_albums_spotify(self) -> list[dict]:
        """Return user's saved Spotify albums via Mopidy RPC, sorted by artist/album."""
        refs = self._rpc("core.library.browse", uri="spotify:your:albums") or []
        result = []
        for ref in refs:
            if ref.get("type") != "album":
                continue
            uri      = ref.get("uri", "")
            raw_name = ref.get("name", "").strip()
            if not uri or not raw_name:
                continue
            # Browse returns "Artist - Album Name"; split on first " - "
            if " - " in raw_name:
                artist, album_name = raw_name.split(" - ", 1)
            else:
                artist, album_name = "", raw_name
            result.append({
                "name":      album_name,
                "artist":    artist,
                "year":      0,
                "track_uri": uri,   # spotify:album:xxx — works with core.library.get_images
                "tracks":    None,
                "thumb":     None,
            })
        if not result:
            log.warning("Spotify: no saved albums found (check credentials and saved library)")
        return sorted(result, key=lambda x: (x["artist"].casefold(), x["name"].casefold()))

    def get_album_tracks(self, album: dict) -> list[dict]:
        """Return track dicts for every track in *album*, sorted by disc/track."""
        uri = album.get("track_uri", "")
        if uri.startswith("spotify:album:"):
            return self._get_album_tracks_spotify(uri)
        if uri.startswith("local:album:"):
            return self._get_album_tracks_local_rpc(uri)
        return self._get_album_tracks_local(album)

    def _get_album_tracks_local(self, album: dict) -> list[dict]:
        name   = album["name"]
        artist = album["artist"]

        def _q(c):
            if artist:
                tracks = c.find("album", name, "albumartist", artist)
                if not tracks:
                    tracks = c.find("album", name, "artist", artist)
            else:
                tracks = c.find("album", name)

            def _parse_num(val, default):
                return int(str(val or default).split("/")[0] or default)

            return sorted(tracks, key=lambda t: (
                _parse_num(t.get("disc"),  1),
                _parse_num(t.get("track"), 0),
            ))

        return self._browse(_q) or []

    def _get_album_tracks_local_rpc(self, album_uri: str) -> list[dict]:
        """Return track dicts for a local:album: URI via RPC lookup, sorted by disc/track."""
        lookup = self._rpc("core.library.lookup", uris=[album_uri]) or {}
        track_list = lookup.get(album_uri, [])
        result = []
        for t in track_list:
            uri     = t.get("uri", "")
            artists = t.get("artists", [])
            alb_obj = t.get("album") if isinstance(t.get("album"), dict) else {}
            result.append({
                "file":     uri,
                "title":    t.get("name", ""),
                "artist":   artists[0].get("name", "") if artists else "",
                "album":    alb_obj.get("name", "") if isinstance(alb_obj, dict) else "",
                "track":    t.get("track_no") or 0,
                "disc":     t.get("disc_no")  or 1,
                "duration": (t.get("length") or 0) // 1000,
            })
        def _sort_key(t):
            try:
                return (int(t["disc"]), int(t["track"]))
            except (ValueError, TypeError):
                return (1, 0)
        return sorted(result, key=_sort_key)

    def _get_album_tracks_spotify(self, album_uri: str) -> list[dict]:
        """Return track dicts for a Spotify album URI, sorted by disc/track."""
        lookup = self._rpc("core.library.lookup", uris=[album_uri]) or {}
        track_list = lookup.get(album_uri, [])
        if not track_list:
            refs = self._rpc("core.library.browse", uri=album_uri) or []
            track_uris = [r["uri"] for r in refs
                          if r.get("type") == "track" and r.get("uri")]
            if track_uris:
                lookup2 = self._rpc("core.library.lookup", uris=track_uris) or {}
                for uri in track_uris:
                    track_list.extend(lookup2.get(uri, []))
        if not track_list:
            # Last resort: fetch track URIs directly from the Spotify Web API
            # (mopidy-spotify can't handle some albums via lookup/browse).
            album_id  = album_uri.split(":")[-1]
            web_uris  = _spotify_web.album_track_uris(album_id)
            if web_uris:
                log.info("Spotify Web API returned %d tracks for %s", len(web_uris), album_uri)
                lookup3 = self._rpc("core.library.lookup", uris=web_uris) or {}
                for uri in web_uris:
                    track_list.extend(lookup3.get(uri, []))
                if not track_list:
                    # mopidy lookup of individual tracks also failed — build stubs
                    # so the tracklist at least shows something playable.
                    for i, uri in enumerate(web_uris, 1):
                        track_list.append({"uri": uri, "name": "", "track_no": i, "disc_no": 1,
                                           "artists": [], "album": None})
        result = []
        for t in track_list:
            uri     = t.get("uri", "")
            artists = t.get("artists", [])
            result.append({
                "file":     uri,
                "title":    t.get("name", ""),
                "artist":   artists[0].get("name", "") if artists else "",
                "album":    (t.get("album") or {}).get("name", ""),
                "track":    t.get("track_no") or 0,
                "disc":     t.get("disc_no")  or 1,
                "duration": (t.get("length") or 0) // 1000,
            })
        def _sort_key(t):
            try:
                return (int(t["disc"]), int(t["track"]))
            except (ValueError, TypeError):
                return (1, 0)
        return sorted(result, key=_sort_key)

    def play_album_fast(self, album_uri: str) -> list[dict]:
        """Add *album_uri* to tracklist and start playing immediately.

        Uses a single core.tracklist.add(uri=...) call which is faster than
        a separate library.lookup + N individual adds. Returns track dicts
        parsed from the response so the caller can populate the UI.
        """
        log.info("play_album_fast: %s", album_uri)
        self._queue_rebuild_until = time.monotonic() + 30.0
        self._reset_tracklist_options()
        self._rpc("core.tracklist.clear")
        tl_tracks = self._rpc("core.tracklist.add", uris=[album_uri]) or []
        if not tl_tracks:
            refs = self._rpc("core.library.browse", uri=album_uri) or []
            track_uris = [r["uri"] for r in refs
                          if r.get("type") == "track" and r.get("uri")]
            if track_uris:
                tl_tracks = self._rpc("core.tracklist.add", uris=track_uris) or []
        if not tl_tracks:
            album_id  = album_uri.split(":")[-1]
            web_uris  = _spotify_web.album_track_uris(album_id)
            if web_uris:
                log.info("Spotify Web API returned %d tracks for %s", len(web_uris), album_uri)
                tl_tracks = self._rpc("core.tracklist.add", uris=web_uris) or []
        tracks = []
        for tlt in tl_tracks:
            t       = tlt.get("track", {}) if isinstance(tlt, dict) else {}
            artists = t.get("artists", [])
            tracks.append({
                "file":     t.get("uri", ""),
                "title":    t.get("name", ""),
                "artist":   artists[0].get("name", "") if artists else "",
                "album":    (t.get("album") or {}).get("name", ""),
                "track":    t.get("track_no") or 0,
                "disc":     t.get("disc_no")  or 1,
                "duration": (t.get("length") or 0) // 1000,
            })
        if tl_tracks:
            log.info("play_album_fast: loaded %d tracks, playing tlid=%s",
                     len(tl_tracks), tl_tracks[0]["tlid"])
            self._rpc("core.playback.play", tlid=tl_tracks[0]["tlid"])
            with self._ctrl_lock:
                self._status["state"] = "play"
        else:
            log.warning("play_album_fast: no tracks returned for %s", album_uri)
        self._queue_rebuild_until = 0.0
        def _sort_key(t):
            try:
                return (int(t["disc"]), int(t["track"]))
            except (ValueError, TypeError):
                return (1, 0)
        sorted_tracks = sorted(tracks, key=_sort_key)
        self._active_tracks    = sorted_tracks
        self._active_album_uri = album_uri
        return sorted_tracks

    def _recover_next(self, from_uri: str):
        """Reload the queue and play the track after *from_uri*.
        Uses the album URI (fast) when available, individual URIs otherwise."""
        try:
            tracks    = self._active_tracks
            album_uri = self._active_album_uri
            idx = next((i for i, t in enumerate(tracks) if t.get("file") == from_uri), -1)
            if idx < 0:
                log.warning("recover_next: %r not found in active tracks", from_uri)
                return
            next_idx = idx + 1
            if next_idx >= len(tracks):
                log.info("recover_next: %r was last track", from_uri)
                return
            next_uri = tracks[next_idx].get("file", "")
            log.info("recover_next: reloading from track %d/%d uri=%s",
                     next_idx + 1, len(tracks), next_uri)
            if album_uri:
                # Fast path: single add for the whole album (~1-2s vs ~12s for individual URIs)
                self._queue_rebuild_until = time.monotonic() + 10.0
                self._reset_tracklist_options()
                self._rpc("core.tracklist.clear")
                tl_tracks = self._rpc("core.tracklist.add", uris=[album_uri]) or []
                tlid = None
                for tlt in tl_tracks:
                    t = tlt.get("track", {}) if isinstance(tlt, dict) else {}
                    if t.get("uri") == next_uri:
                        tlid = tlt["tlid"]
                        break
                if tlid is None and tl_tracks:
                    log.warning("recover_next_fast: %r not found by URI, using index", next_uri)
                    tlid = tl_tracks[min(next_idx, len(tl_tracks) - 1)]["tlid"]
                if tlid is not None:
                    self._rpc("core.playback.play", tlid=tlid)
                self._queue_rebuild_until = 0.0
                self._active_tracks       = tracks   # keep existing sorted list
                with self._ctrl_lock:
                    self._status["state"] = "play"
            else:
                self.play_album(tracks, next_idx, start_uri=next_uri)
        except Exception as e:
            log.warning("recover_next failed: %s", e)
        finally:
            self._recovery_in_progress = False

    def load_album(self, tracks: list[dict], track_index: int = 0):
        """Replace queue with *tracks*, seek to *track_index*, and pause."""
        if not tracks:
            return
        self._reset_tracklist_options()
        uris = [t["file"] for t in tracks if "file" in t]
        self._rpc("core.tracklist.clear")
        tl_tracks = self._rpc("core.tracklist.add", uris=uris) or []
        if tl_tracks and 0 <= track_index < len(tl_tracks):
            self._rpc("core.playback.play", tlid=tl_tracks[track_index]["tlid"])
            for _ in range(20):
                time.sleep(0.05)
                if self._rpc("core.playback.get_state") == "playing":
                    break
            self._rpc("core.playback.pause")
        with self._ctrl_lock:
            self._status["state"] = "pause"

    def play_album(self, tracks: list[dict], track_index: int = 0, start_uri: str = ""):
        """Replace queue with *tracks* and start playing from *track_index*.

        If *start_uri* is given, find the matching track in tl_tracks by URI
        (robust against tracks with missing URIs shifting the positional index).
        """
        if not tracks:
            return
        start = tracks[track_index].get("file", "—") if 0 <= track_index < len(tracks) else "—"
        log.info("play_album: %d tracks  idx=%d  start=%s", len(tracks), track_index, start_uri or start)
        self._queue_rebuild_until = time.monotonic() + 30.0
        self._reset_tracklist_options()
        uris = [t["file"] for t in tracks if "file" in t]
        self._rpc("core.tracklist.clear")
        tl_tracks = self._rpc("core.tracklist.add", uris=uris) or []
        tlid = None
        if start_uri:
            for tlt in tl_tracks:
                if isinstance(tlt, dict):
                    t = (tlt.get("track") or {})
                    if t.get("uri") == start_uri:
                        tlid = tlt["tlid"]
                        break
        if tlid is None and tl_tracks and 0 <= track_index < len(tl_tracks):
            tlid = tl_tracks[track_index]["tlid"]
        if tlid is not None:
            self._rpc("core.playback.play", tlid=tlid)
        self._queue_rebuild_until = 0.0
        self._active_tracks    = list(tracks)
        self._active_album_uri = ""   # individual-URI path — no album URI available
        with self._ctrl_lock:
            self._status["state"] = "play"

    # ── album art ─────────────────────────────────────────────────────────────

    def get_album_art(self, uri: str) -> Image.Image | None:
        """Fetch cover art for *uri*. Tries Mopidy images API first, then cover.jpg on disk."""
        if not uri:
            return None

        # 1. Mopidy images API (works for embedded art and some backends)
        result = self._rpc("core.library.get_images", uris=[uri])
        images = (result or {}).get(uri, [])
        if images:
            img_uri = images[0]["uri"]
            if not img_uri.startswith("http"):
                img_uri = f"http://{MOPIDY_HOST}:{MOPIDY_HTTP_PORT}{img_uri}"
            try:
                r = requests.get(img_uri, timeout=10)
                return Image.open(io.BytesIO(r.content)).convert("RGB")
            except Exception as e:
                log.warning("Art download failed: %s", e)

        # 2. Filesystem fallback: look for cover.jpg / folder.jpg next to the track
        cover = self._cover_from_uri(uri)
        if cover:
            return cover

        return None

    _COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png",
                    "folder.jpg", "folder.jpeg", "front.jpg", "front.jpeg")

    def _cover_from_uri(self, uri: str) -> Image.Image | None:
        """Resolve a local:track: URI to a cover image on disk."""
        if not uri.startswith("local:track:"):
            return None
        media_dir = MUSIC_DIR or os.path.expanduser("~/Music")
        rel = urllib.parse.unquote(uri[len("local:track:"):])
        track_path = os.path.join(media_dir, rel)
        folder = os.path.dirname(track_path)
        for name in self._COVER_NAMES:
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                try:
                    return Image.open(candidate).convert("RGB")
                except Exception as e:
                    log.warning("Cover load failed %s: %s", candidate, e)
        return None

    # ── cleanup ───────────────────────────────────────────────────────────────

    def disconnect(self):
        try:
            self._ctrl.disconnect()
        except Exception:
            pass
