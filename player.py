"""
Mopidy client.

Two separate MPD connections:
  _ctrl  – polled every 0.5 s for status / current song
  _browse – used on-demand for library queries (avoids blocking the poll lock)

Album art / images come from the Mopidy HTTP JSON-RPC API because it handles
all back-ends (local files, Spotify, etc.) transparently.
"""
from __future__ import annotations
import threading
import time
import io
import logging
import shutil
import subprocess

import mpd
import requests
from PIL import Image

from config import (
    MOPIDY_HOST, MOPIDY_MPD_PORT, MOPIDY_HTTP_PORT, MOPIDY_PASSWORD,
    VOLUME_BACKEND,
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

        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()

    # ── internal: status poll ─────────────────────────────────────────────────

    def _poll_loop(self):
        while True:
            if not self._ctrl_ok:
                time.sleep(3)
                with self._ctrl_lock:
                    self._ctrl_ok = _connect(self._ctrl)
                continue
            try:
                with self._ctrl_lock:
                    self._status = self._ctrl.status()
                    self._song   = self._ctrl.currentsong()
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
        status = self.get_status()
        # Mopidy doesn't expose "duration"; parse total from "time" ("elapsed:total")
        time_str = status.get("time", "")
        parts = time_str.split(":") if time_str else []
        dur = float(parts[1]) if len(parts) >= 2 else float(status.get("duration", 0) or 0)
        if dur > 0:
            self._cmd("seekcur", int(fraction * dur))

    def play_track_in_queue(self, pos: int, track: dict | None = None):
        """Play by position in the current queue (0-indexed) via HTTP RPC."""
        with self._ctrl_lock:
            self._status["state"] = "play"
            if track:
                self._song = {
                    "title":  track.get("title", ""),
                    "artist": track.get("artist") or track.get("albumartist", ""),
                    "album":  track.get("album", ""),
                    "file":   track.get("file", ""),
                }

        def _do():
            tl_tracks = self._rpc("core.tracklist.get_tl_tracks")
            if tl_tracks and 0 <= pos < len(tl_tracks):
                self._rpc("core.playback.play", tlid=tl_tracks[pos]["tlid"])
            else:
                log.warning("play_track_in_queue: pos=%d out of range (queue len=%s)",
                            pos, len(tl_tracks) if tl_tracks else None)

        threading.Thread(target=_do, daemon=True).start()

    def set_song_optimistic(self, song: dict):
        """Immediately update the current-song cache without waiting for the poll loop."""
        with self._ctrl_lock:
            self._song = dict(song)

    # ── library browsing ──────────────────────────────────────────────────────

    def get_albums(self) -> list[dict]:
        """
        Return a list of dicts sorted by artist/album:
          {name, artist, track_uri}   (track_uri = first track, for art fetching)
        """
        def _q(c):
            raw = c.list("album")   # [{album: "..."}, ...]
            result = []
            for item in raw:
                name = item.get("album", "").strip()
                if not name:
                    continue
                tracks = c.find("album", name)
                if not tracks:
                    continue
                t0 = tracks[0]
                artist = (
                    t0.get("albumartist")
                    or t0.get("artist")
                    or ""
                ).strip()
                date = t0.get("date", "") or ""
                try:
                    year = int(str(date).split("-")[0])
                except (ValueError, IndexError):
                    year = 9999
                result.append({
                    "name":      name,
                    "artist":    artist,
                    "year":      year,
                    "track_uri": t0.get("file", ""),
                    "tracks":    None,   # populated on demand
                    "thumb":     None,
                })
            return sorted(result, key=lambda x: (x["artist"].casefold(), x["year"], x["name"].casefold()))

        return self._browse(_q) or []

    def get_album_tracks(self, album: dict) -> list[dict]:
        """Return MPD song dicts for every track in *album*, sorted by disc/track."""
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
                # MPD can return "3/12" (track/total) — take only the first part
                return int(str(val or default).split("/")[0] or default)

            def sort_key(t):
                disc  = _parse_num(t.get("disc"),  1)
                track = _parse_num(t.get("track"), 0)
                return (disc, track)

            return sorted(tracks, key=sort_key)

        return self._browse(_q) or []

    def load_album(self, tracks: list[dict], track_index: int = 0):
        """Replace queue with *tracks*, seek to *track_index*, and pause."""
        if not tracks:
            return
        uris = [t["file"] for t in tracks if "file" in t]

        def _q(c):
            c.clear()
            for u in uris:
                c.add(u)

        self._browse(_q)
        # Use RPC to start the track then pause it.  We must wait for mopidy to
        # reach PLAYING state before pausing — the GStreamer pipeline takes a
        # moment to start, so an immediate pause() call races and loses.
        tl_tracks = self._rpc("core.tracklist.get_tl_tracks")
        if tl_tracks and 0 <= track_index < len(tl_tracks):
            self._rpc("core.playback.play", tlid=tl_tracks[track_index]["tlid"])
            for _ in range(20):
                time.sleep(0.05)
                if self._rpc("core.playback.get_state") == "playing":
                    break
            self._rpc("core.playback.pause")
        with self._ctrl_lock:
            self._status["state"] = "pause"

    def play_album(self, tracks: list[dict], track_index: int = 0):
        """Replace queue with *tracks* and start playing from *track_index*."""
        if not tracks:
            return
        uris = [t["file"] for t in tracks if "file" in t]

        def _q(c):
            c.clear()
            for u in uris:
                c.add(u)

        self._browse(_q)
        tl_tracks = self._rpc("core.tracklist.get_tl_tracks")
        if tl_tracks and 0 <= track_index < len(tl_tracks):
            self._rpc("core.playback.play", tlid=tl_tracks[track_index]["tlid"])
        with self._ctrl_lock:
            self._status["state"] = "play"

    # ── album art ─────────────────────────────────────────────────────────────

    def get_album_art(self, uri: str) -> Image.Image | None:
        """Fetch cover art for *uri* via Mopidy's images API. Returns PIL Image or None."""
        if not uri:
            return None
        result = self._rpc("core.library.get_images", uris=[uri])
        images = (result or {}).get(uri, [])
        if not images:
            return None
        img_uri = images[0]["uri"]
        if not img_uri.startswith("http"):
            img_uri = f"http://{MOPIDY_HOST}:{MOPIDY_HTTP_PORT}{img_uri}"
        try:
            r = requests.get(img_uri, timeout=10)
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception as e:
            log.warning("Art download failed: %s", e)
            return None

    # ── cleanup ───────────────────────────────────────────────────────────────

    def disconnect(self):
        try:
            self._ctrl.disconnect()
        except Exception:
            pass
