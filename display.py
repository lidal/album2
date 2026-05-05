"""
Album2 — main UI.

View state machine
──────────────────
  GRID      album grid; mini-player bar at top when album loaded normally,
            OR album art strip at top when peeking (_peeking=True)
  ALBUM     full album art, no text overlay
  TRACKLIST album art slides UP (stays visible in top strip), tracklist below

_album_y animation
───────────────────
   H  =  720  → album panel fully off-screen below (normal GRID)
   0          → album panel fully covering screen (ALBUM)
  _TL_ALBUM_Y → art shifted up so bottom 1/5 shows at top (TRACKLIST or PEEK)

Touch gestures (ALBUM)
  single tap   → toggle controls overlay
  double tap   → play / pause
  swipe up     → open tracklist (art slides up, bottom strip visible)
  swipe down   → peek to grid (art slides up to top strip, grid below)
  swipe L/R    → next / previous track

Touch gestures (TRACKLIST)
  tap art strip (top strip)  → close tracklist back to ALBUM
  tap track row              → play that track
  swipe down                 → close tracklist
  vertical drag in list area → scroll

Touch gestures (GRID / peek)
  tap album strip (peeking)  → return to ALBUM view
  swipe up (peeking)         → return to ALBUM view
  tap album cell             → load album (paused), open ALBUM view
  tap mini-player            → re-open ALBUM view
  vertical drag              → scroll grid
"""

from __future__ import annotations
import bisect
import concurrent.futures
import hashlib
import logging
import math
import os
import re
import pygame.gfxdraw
import threading
import time
from enum import IntEnum

import settings

import pygame
from PIL import Image

from config import (
    SCREEN_WIDTH, SCREEN_HEIGHT, DISPLAY_WIDTH, DISPLAY_HEIGHT,
    GRID_COLS, GRID_PAD, GRID_TEXT_H,
    MINI_H, TRACKLIST_ART_H, TRACK_ROW_H, CTRL_BAR_H, PROGRESS_H, SCRUB_LEEWAY,
    ANIM_SPEED, CTRL_FADE_SPEED,
    SWIPE_V_MIN, SWIPE_H_MIN, TAP_MAX_MOVE, TAP_MAX_MS, DOUBLE_TAP_MS, DRAG_THRESH,
    VOLUME_BADGE_MS, CTRL_TIMEOUT_MS, SCROLL_FRICTION, FPS,
    BTN_MARGIN, BTN_RADIUS, BTN_GAP, CTRL_ICON_SM, CTRL_ICON_LG, CTRL_TEXT_GAP,
    TOGGLE_W, TOGGLE_H, TRACK_PAD, MINI_PAD, MINI_ICON_SIZE, VOL_BADGE_PAD,
    LONG_PRESS_MS,
    COL_BG, COL_GRID_BG, COL_CELL_BG, COL_MINI_BG, COL_TL_BG, COL_SEP,
    COL_TEXT_TITLE, COL_TEXT_ARTIST, COL_TEXT_ALBUM,
    COL_TRACK_PLAYING, COL_TRACK_NORMAL, COL_TRACK_NUM, COL_TRACK_DUR,
    COL_PROGRESS_BG, COL_PROGRESS_FG,
    COL_HIGHLIGHT,
    COL_VOLUME_FG,
    FONT_PATH, FONT_SZ_TITLE, FONT_SZ_ARTIST, FONT_SZ_ALBUM,
    FONT_SZ_GRID, FONT_SZ_GRID_SM, FONT_SZ_MINI, FONT_SZ_TRACK, FONT_SZ_TRACK_SM,
    FONT_SZ_LYRICS, THUMB_WORKERS, MUSIC_DIR, THUMB_CACHE_DIR,
)

log = logging.getLogger(__name__)

_LRC_TS_RE = re.compile(r'^\[(\d+):(\d+(?:\.\d+)?)\]')   # capture mm:ss.xx
_LRC_META_RE = re.compile(r'^\[\w+:[^\]]*\]')             # metadata like [ti:...]

W, H = SCREEN_WIDTH, SCREEN_HEIGHT

_THUMB_CACHE_DIR = (THUMB_CACHE_DIR
                    or os.path.join(os.path.expanduser("~"), ".cache", "album2", "thumbs"))
os.makedirs(_THUMB_CACHE_DIR, exist_ok=True)

# Icon colour for buttons drawn on top of COL_HIGHLIGHT
_BTN_ICON_COL = (0, 0, 0) if sum(COL_HIGHLIGHT[:3]) / 3 > 127.5 else (255, 255, 255)

# album_y target when tracklist is open: art shifts up so only top strip shows
_TL_ALBUM_Y = TRACKLIST_ART_H - H   # e.g. 224 - 720 = -496

# grid geometry
_CELL_W = (W - GRID_PAD * (GRID_COLS + 1)) // GRID_COLS
_CELL_H = _CELL_W + GRID_TEXT_H
_ROW_H  = _CELL_H + GRID_PAD


# ── helpers ───────────────────────────────────────────────────────────────────

def _lerp(a: float, b: float, k: float) -> float:
    return a + (b - a) * k


def _font(size: int) -> pygame.font.Font:
    if FONT_PATH:
        try:
            return pygame.font.Font(FONT_PATH, size)
        except Exception:
            pass
    return pygame.font.Font(None, size)


def _pil_to_surf(img: Image.Image) -> pygame.Surface:
    return pygame.image.fromstring(img.tobytes(), img.size, "RGB").convert()


_SURF_CACHE: dict = {}
_SURF_CACHE_MAX = 800

def _render_text(font, text: str, colour, max_w: int = 0) -> pygame.Surface:
    key = (id(font), text, colour, max_w)
    s = _SURF_CACHE.get(key)
    if s is None:
        s = font.render(text, True, colour)
        if max_w and s.get_width() > max_w:
            while len(text) > 1 and font.size(text + "…")[0] > max_w:
                text = text[:-1]
            s = font.render(text + "…", True, colour)
        if len(_SURF_CACHE) >= _SURF_CACHE_MAX:
            _SURF_CACHE.clear()
        _SURF_CACHE[key] = s
    return s


def _draw_triangle(surf, col, cx, cy, size, direction):
    h = int(size * 0.9)
    w = int(size * 0.65)
    if direction == "right":
        pts = [(cx - w // 2, cy - h // 2),
               (cx - w // 2, cy + h // 2),
               (cx + w // 2, cy)]
    else:
        pts = [(cx + w // 2, cy - h // 2),
               (cx + w // 2, cy + h // 2),
               (cx - w // 2, cy)]
    pygame.gfxdraw.filled_polygon(surf, pts, col)
    pygame.gfxdraw.aapolygon(surf, pts, col)


def _draw_pause(surf, col, cx, cy, size):
    bw = max(3, size // 4)
    bh = int(size * 0.9)
    gap = max(2, size // 6)
    pygame.draw.rect(surf, col, (cx - bw - gap // 2, cy - bh // 2, bw, bh))
    pygame.draw.rect(surf, col, (cx + gap // 2,       cy - bh // 2, bw, bh))


def _draw_play(surf, col, cx, cy, size):
    h = int(size * 0.95)
    pts = [(cx - int(size * 0.35), cy - h // 2),
           (cx - int(size * 0.35), cy + h // 2),
           (cx + int(size * 0.42), cy)]
    pygame.gfxdraw.filled_polygon(surf, pts, col)
    pygame.gfxdraw.aapolygon(surf, pts, col)


def _wrap_text(font, text: str, max_w: int) -> list[str]:
    """Split text into lines that each fit within max_w pixels."""
    if not text:
        return []
    words = text.split()
    if not words:
        return [text]
    result, line = [], ""
    for word in words:
        test = (line + " " + word).strip()
        if font.size(test)[0] <= max_w:
            line = test
        else:
            if line:
                result.append(line)
            line = word
    if line:
        result.append(line)
    return result or [text]


class View(IntEnum):
    GRID      = 0
    ALBUM     = 1
    TRACKLIST = 2
    SETTINGS  = 3
    SCAN      = 4
    CALIBRATE = 5


_SETTINGS_ITEMS = [
    (None,        "PLAYBACK"),
    ("autoplay",  "Autoplay when opening album"),
    ("lyrics",    "Show lyrics"),
    (None,        "PERFORMANCE"),
    ("idle_fps",  "Reduce FPS when idle"),
    ("skip_draw", "Skip redraw when nothing changed"),
]

# Keyboard rows: list of (label, weight).  Special labels: SHIFT BACK OK SPACE SYM ABC
# Numbers row is always the first row on every page.
_KB_ROWS: dict[str, list] = {
    "alpha": [
        [("1",1),("2",1),("3",1),("4",1),("5",1),("6",1),("7",1),("8",1),("9",1),("0",1)],
        [("Q",1),("W",1),("E",1),("R",1),("T",1),("Y",1),("U",1),("I",1),("O",1),("P",1)],
        [("A",1),("S",1),("D",1),("F",1),("G",1),("H",1),("J",1),("K",1),("L",1)],
        [("SHIFT",1.5),("Z",1),("X",1),("C",1),("V",1),("B",1),("N",1),("M",1),("BACK",1.5)],
        [("SYM",1.5),("SPACE",7),("OK",1.5)],
    ],
    "sym": [
        [("1",1),("2",1),("3",1),("4",1),("5",1),("6",1),("7",1),("8",1),("9",1),("0",1)],
        [("-",1),("/",1),(":",1),(";",1),("(",1),(")",1),("$",1),("&",1),("@",1),('"',1)],
        [(".",1),(",",1),("?",1),("!",1),("'",1),("`",1),("~",1),("<",1),(">",1),("\\",1)],
        [("SHIFT",1.5),("#",1),("%",1),("^",1),("*",1),("+",1),("=",1),("_",1),("BACK",1.5)],
        [("ABC",1.5),("SPACE",7),("OK",1.5)],
    ],
    "sym2": [   # shift layer of sym page  (iOS #+=  style)
        [("1",1),("2",1),("3",1),("4",1),("5",1),("6",1),("7",1),("8",1),("9",1),("0",1)],
        [("[",1),("]",1),("{",1),("}",1),("#",1),("%",1),("^",1),("*",1),("+",1),("=",1)],
        [("_",1),("\\",1),("|",1),("~",1),("<",1),(">",1),("€",1),("£",1),("¥",1),("•",1)],
        [("SHIFT",1.5),("©",1),("®",1),("™",1),("°",1),("±",1),("÷",1),("×",1),("BACK",1.5)],
        [("ABC",1.5),("SPACE",7),("OK",1.5)],
    ],
}
_KB_FACE = {"SHIFT": "⇧", "BACK": "⌫", "OK": "↵", "SPACE": " ",
            "SYM": "#+=", "ABC": "ABC"}
_KB_SPECIAL = frozenset(_KB_FACE)


# ══════════════════════════════════════════════════════════════════════════════
class App:
# ══════════════════════════════════════════════════════════════════════════════

    def __init__(self, screen: pygame.Surface, player, volume_ctrl, bt=None, wifi=None, audio=None):
        self.screen = screen
        self.player = player
        self.vc     = volume_ctrl
        self.bt     = bt
        self.wifi   = wifi
        self.audio  = audio

        # fonts
        self._f_title    = _font(FONT_SZ_TITLE)
        self._f_artist   = _font(FONT_SZ_ARTIST)
        self._f_album_f  = _font(FONT_SZ_ALBUM)
        self._f_grid     = _font(FONT_SZ_GRID)
        self._f_grid_sm  = _font(FONT_SZ_GRID_SM)
        self._f_mini     = _font(FONT_SZ_MINI)
        self._f_track    = _font(FONT_SZ_TRACK)
        self._f_track_sm = _font(FONT_SZ_TRACK_SM)
        self._f_lyrics   = _font(FONT_SZ_LYRICS)

        # reusable surfaces
        self._overlay_surf    = pygame.Surface((W, H), pygame.SRCALPHA)
        _fs = W // 2   # 2× supersampling scratch surface for flash icon (W//4 * 2)
        self._flash_icon_surf = pygame.Surface((_fs, _fs), pygame.SRCALPHA)

        # animation state
        self._view    = View.GRID
        self._album_y = float(H)   # current (lerped)
        self._album_y_t = float(H) # target
        self._ctrl_a  = 0.0        # controls overlay alpha (current)
        self._ctrl_a_t = 0.0       # target

        # album art
        self._art:    pygame.Surface | None = None
        self._art_uri = ""

        # default background
        self._default_bg = self._make_default_bg()

        # data
        self._albums:  list[dict] = []
        self._cur_idx: int | None = None
        self._tracks:  list[dict] = []

        # peek mode: album art strip at top of grid view
        self._peeking = False

        # settings return target
        self._settings_return: View = View.GRID
        self._settings_return_ctrl: bool = False

        # cache clear feedback
        self._cache_cleared_ms: int = 0   # ticks when last cleared (0 = not recently)

        # audio output popup
        self._audio_popup_open: bool = False
        self._audio_sinks: list[dict] = []
        self._audio_busy_id: str | None = None

        # bluetooth — paired devices
        self._bt_devices: list[dict] = []
        self._bt_action_addr: str | None = None
        self._bt_menu_dev: dict | None = None
        self._bt_last_refresh: int = 0
        self._bt_refreshing: bool = False

        # wifi — visible networks
        self._wifi_networks: list[dict] = []
        self._wifi_action_name: str | None = None
        self._wifi_refreshing: bool = False
        self._wifi_menu_net: dict | None = None

        # touch calibration
        self._cal_points:      list[tuple[int,int]] = []
        self._cal_raw:         list[tuple[float,float]] = []
        self._cal_step:        int = 0
        self._cal_raw_pending: tuple[float,float] | None = None

        # on-screen keyboard (for WiFi password entry)
        self._kb_ssid:         str | None = None   # None = hidden
        self._kb_text:         str = ""
        self._kb_page:         str = "alpha"
        self._kb_shift:        bool = False
        self._kb_caps:         bool = False
        self._kb_shift_tap_ms: int  = 0
        self._kb_show_pw:      bool = False
        self._kb_error:        bool = False   # True when last connect_new failed

        # lyrics
        self._lyrics:          str | None = None
        self._lyrics_parsed:   tuple | None = None   # (lines, times|None)
        self._lyrics_uri:      str  = ""
        self._lyrics_loading:  bool = False
        self._lyrics_scroll:   float = 0.0   # manual scroll offset (visual rows, no-timestamp mode)
        self._lyrics_drag:     bool  = False

        # action flash feedback
        self._flash_icon:     str | None = None
        self._flash_alpha:    float = 0.0
        self._flash_start_ms: int   = 0

        # bluetooth — scan
        self._scan_devices: list[dict] = []
        self._scan_action_addr: str | None = None
        self._scan_last_refresh: int = 0
        self._scan_refreshing: bool = False

        # scroll offsets
        self._grid_scroll:     float = 0.0
        self._tl_scroll:       float = 0.0
        self._settings_scroll: float = 0.0

        # scroll momentum (px/s, decays each frame)
        self._grid_vel:     float = 0.0
        self._tl_vel:       float = 0.0
        self._settings_vel: float = 0.0

        # touch state
        self._t_start_pos  = None
        self._t_start_ms   = 0
        self._t_prev_pos   = None
        self._t_dragging   = False
        self._t_long_pressed = False

        # panel drag state
        self._panel_touch       = False  # touch started on the album panel
        self._panel_drag_base_y = 0.0   # _album_y when drag started
        self._panel_drag_start  = 0     # screen y when drag started

        # double-tap detection
        self._last_tap_ms    = 0
        self._pending_tap    = False
        self._pending_tap_ms = 0
        self._pending_tap_pos = None

        # controls overlay auto-hide
        self._ctrl_shown_ms = 0        # ticks when controls were last shown

        # progress bar scrubbing
        self._scrub_active: bool  = False
        self._scrub_frac:   float = 0.0

        # redraw gating
        self._dirty: bool = True

        # FPS tracking (exponential moving average)
        self._fps_avg: float = 0.0

        # last user-input timestamp — keep full FPS for a settling window after input
        self._last_input_ms: int = 0

        # mini-player art cache (avoid smoothscale every frame)
        self._mini_art:     pygame.Surface | None = None
        self._mini_art_src: object = None   # identity of self._art when cached

        # lyrics visual-row cache (avoid re-wrapping every frame)
        self._lyrics_row_cache: tuple | None = None  # (parsed_ref, rows, logical, first_vis)

        # volume badge
        self._vol_until_ms = 0
        if volume_ctrl.available:
            volume_ctrl.set_on_change(self._on_volume)

        # status cache
        self._status: dict = {}
        self._song:   dict = {}
        self._last_poll = 0.0
        self._last_tick_ms = pygame.time.get_ticks()
        # elapsed interpolation: base value + wall-clock time since that poll
        self._elapsed_base   = 0.0
        self._elapsed_base_t = 0.0
        self._last_progress_px = -1   # last drawn progress bar pixel; avoids spurious dirty

        # thumbnail loader
        self._thumb_pool    = concurrent.futures.ThreadPoolExecutor(max_workers=THUMB_WORKERS)
        self._thumb_queued:   set[int] = set()
        self._thumbs_pending: int      = 0   # number of thumbs still in flight

        threading.Thread(target=self._load_albums, daemon=True).start()

    # ── default background ────────────────────────────────────────────────────

    def _make_default_bg(self) -> pygame.Surface:
        s = pygame.Surface((W, H))
        s.fill(COL_BG)
        cx, cy = W // 2, H // 2
        for r in range(28, 345, 20):
            a = max(18, 70 - r // 5)
            pygame.gfxdraw.aacircle(s, cx, cy, r, (a, a, a + 8))
        pygame.gfxdraw.filled_circle(s, cx, cy, 26, (35, 35, 45))
        pygame.gfxdraw.aacircle(s, cx, cy, 26, (35, 35, 45))
        return s

    # ── album list + thumbnails ───────────────────────────────────────────────

    def _load_albums(self):
        self._albums          = self.player.get_albums()
        self._thumbs_pending  = len(self._albums)
        self._dirty           = True
        log.info("Loaded %d albums", len(self._albums))
        for i in range(len(self._albums)):
            self._queue_thumb(i)

    def _queue_thumb(self, idx: int):
        if idx in self._thumb_queued:
            return
        album = self._albums[idx]
        if album.get("thumb") is not None or album.get("thumb_loading"):
            return
        album["thumb_loading"] = True
        self._thumb_queued.add(idx)
        self._thumb_pool.submit(self._fetch_thumb, idx)

    def _fetch_thumb(self, idx: int):
        album = self._albums[idx]
        uri   = album["track_uri"]
        key   = hashlib.md5(uri.encode()).hexdigest()
        path  = os.path.join(_THUMB_CACHE_DIR, f"{key}_{_CELL_W}.png")
        try:
            if os.path.exists(path):
                img = Image.open(path).convert("RGB")
            else:
                img = self.player.get_album_art(uri)
                if img:
                    w, h = img.size
                    side = min(w, h)
                    img  = img.crop(((w - side) // 2, (h - side) // 2,
                                      (w + side) // 2, (h + side) // 2))
                    img  = img.resize((_CELL_W, _CELL_W), Image.LANCZOS)
                    img.save(path, "PNG")
            album["thumb"] = _pil_to_surf(img) if img else None
        except Exception as e:
            log.debug("Thumb %d: %s", idx, e)
            album["thumb"] = None
        album["thumb_loading"]  = False
        self._thumbs_pending    = max(0, self._thumbs_pending - 1)
        self._dirty             = True

    # ── album art ─────────────────────────────────────────────────────────────

    def _load_art(self, uri: str):
        def _bg():
            key  = hashlib.md5(uri.encode()).hexdigest()
            path = os.path.join(_THUMB_CACHE_DIR, f"{key}_{DISPLAY_WIDTH}.png")
            try:
                if os.path.exists(path):
                    img = Image.open(path).convert("RGB")
                else:
                    img = self.player.get_album_art(uri)
                    if img:
                        dw, dh = img.size
                        side   = min(dw, dh)
                        img    = img.crop(((dw - side) // 2, (dh - side) // 2,
                                           (dw + side) // 2, (dh + side) // 2))
                        img    = img.resize((DISPLAY_WIDTH, DISPLAY_HEIGHT), Image.LANCZOS)
                        img.save(path, "PNG")
                if img:
                    if img.size != (W, H):
                        img = img.resize((W, H), Image.LANCZOS)
                    self._pending_art = _pil_to_surf(img)
                else:
                    self._pending_art = None
            except Exception as e:
                log.warning("Art load failed for %s: %s", uri, e)
                self._pending_art = None
        self._pending_art = "loading"
        threading.Thread(target=_bg, daemon=True).start()

    # ── volume ────────────────────────────────────────────────────────────────

    def _on_volume(self, vol: int):
        self.player.set_volume(vol)
        self._vol_until_ms = pygame.time.get_ticks() + VOLUME_BADGE_MS

    # ── update ────────────────────────────────────────────────────────────────

    def update(self):
        now_ms = pygame.time.get_ticks()
        dt     = (now_ms - self._last_tick_ms) / 1000.0
        self._last_tick_ms = now_ms
        k_pan = min(1.0, ANIM_SPEED * dt)
        k_ctl = min(1.0, CTRL_FADE_SPEED * dt)

        self._album_y = _lerp(self._album_y, self._album_y_t, k_pan)
        self._ctrl_a  = _lerp(self._ctrl_a,  self._ctrl_a_t,  k_ctl)

        if dt > 0:
            self._fps_avg = self._fps_avg * 0.9 + (1.0 / dt) * 0.1

        if self._t_start_pos is None:   # only coast when finger is up
            friction = SCROLL_FRICTION ** (dt * FPS)
            if abs(self._grid_vel) > 0.5:
                self._grid_scroll = max(0.0, self._grid_scroll + self._grid_vel * dt)
                self._grid_vel   *= friction
                self._dirty = True
            else:
                self._grid_vel = 0.0
            if abs(self._tl_vel) > 0.5:
                self._tl_scroll = max(0.0, self._tl_scroll + self._tl_vel * dt)
                self._tl_vel   *= friction
                self._dirty = True
            else:
                self._tl_vel = 0.0
            if abs(self._settings_vel) > 0.5:
                self._settings_scroll = max(0.0, self._settings_scroll + self._settings_vel * dt)
                self._settings_vel   *= friction
                self._dirty = True
            else:
                self._settings_vel = 0.0

        # pick up finished art load
        pending = getattr(self, "_pending_art", "loading")
        if pending != "loading":
            del self._pending_art
            self._art = pending   # may be None → use default
            self._dirty = True

        # mark dirty for ongoing animations/playback BEFORE resolving taps/events
        # so that pending_tap=True is captured before it gets cleared below
        if not self._dirty:
            self._dirty = (
                abs(self._album_y - self._album_y_t) > 0.5
                or abs(self._ctrl_a - self._ctrl_a_t) > 0.5

                or self._flash_alpha > 0
                or self._pending_tap
                or self._progress_px_changed()
                or now_ms < self._vol_until_ms
                or self._bt_refreshing
                or self._wifi_refreshing
                or self._scan_refreshing
                or self._lyrics_loading
                or self._scrub_active
                or abs(self._grid_vel) > 0.5
                or abs(self._tl_vel) > 0.5
                or abs(self._settings_vel) > 0.5
                or self._view in (View.SCAN, View.CALIBRATE)
                or self._kb_ssid is not None
                or self._thumbs_pending > 0
                or (self._cache_cleared_ms
                    and now_ms - self._cache_cleared_ms < 2000)
            )

        # auto-hide controls overlay after 30 s
        if (CTRL_TIMEOUT_MS > 0 and self._ctrl_a_t > 0 and self._ctrl_shown_ms > 0
                and now_ms - self._ctrl_shown_ms >= CTRL_TIMEOUT_MS):
            self._hide_controls()

        # resolve pending single-tap (double-tap window expired)
        if self._pending_tap and now_ms - self._pending_tap_ms >= DOUBLE_TAP_MS:
            self._pending_tap = False
            self._dirty = True   # state is about to change
            self._exec_single_tap(self._pending_tap_pos)

        # decay action flash (150 ms hold, then 500 ms fade)
        if self._flash_alpha > 0:
            elapsed = now_ms - self._flash_start_ms
            if elapsed < 150:
                self._flash_alpha = 255.0
            else:
                self._flash_alpha = max(0.0, 255.0 * (1.0 - (elapsed - 150) / 500))

        # long-press detection: fire when held >= LONG_PRESS_MS (not on release)
        if (self._t_start_pos is not None
                and not self._t_dragging
                and not self._t_long_pressed
                and self._view == View.SETTINGS
                and self._bt_menu_dev is None
                and self._wifi_menu_net is None):
            held = now_ms - self._t_start_ms
            if held >= LONG_PRESS_MS:
                self._t_long_pressed = True
                if self.bt and self.bt.available:
                    dev = self._bt_device_at(self._t_start_pos)
                    if dev:
                        self._bt_menu_dev = dict(dev)
                if self._bt_menu_dev is None and self.wifi and self.wifi.available:
                    net = self._wifi_network_at(self._t_start_pos)
                    if net:
                        self._wifi_menu_net = dict(net)

        # refresh paired BT devices every 5 s while settings is open
        if (self._view == View.SETTINGS
                and self.bt and self.bt.available
                and not self._bt_refreshing
                and self._bt_action_addr is None
                and now_ms - self._bt_last_refresh >= 5000):
            self._bt_last_refresh = now_ms
            self._bt_refreshing   = True
            def _refresh_bt():
                self._bt_devices    = self.bt.get_devices()
                self._bt_refreshing = False
                self._dirty         = True
            threading.Thread(target=_refresh_bt, daemon=True).start()

        # refresh scan results (every 2 s while in scan view)
        if self._view == View.SCAN and self.bt and self.bt.available and not self._scan_refreshing:
            if now_ms - self._scan_last_refresh >= 2000:
                self._scan_last_refresh = now_ms
                self._scan_refreshing   = True
                paired_addrs = {d["address"] for d in self._bt_devices}
                def _refresh(pa=paired_addrs):
                    all_devs = self.bt.get_all_devices()
                    self._scan_devices    = [d for d in all_devs if d["address"] not in pa]
                    self._scan_refreshing = False
                    self._dirty           = True
                threading.Thread(target=_refresh, daemon=True).start()

        # poll mopidy (every 0.5 s)
        now = time.monotonic()
        if now - self._last_poll >= 0.5:
            self._last_poll = now
            status = self.player.get_status()
            song   = self.player.get_current_song()
            self._status = status
            # Snapshot elapsed for smooth interpolation between polls
            el = float(status.get("elapsed", 0) or 0)
            self._elapsed_base   = el
            self._elapsed_base_t = now
            # Only overwrite _song if MPD returned something — don't clobber
            # our optimistic data while the track is still being queued.
            if song:
                self._song = song
            new_uri = self._song.get("file", "")
            # re-fetch art if track changed while album view is open
            if (new_uri and new_uri != self._art_uri
                    and self._view in (View.ALBUM, View.TRACKLIST)):
                self._art_uri = new_uri
                self._load_art(new_uri)
            # reload lyrics whenever the track changes
            if new_uri and new_uri != self._lyrics_uri and not self._lyrics_loading:
                self._lyrics_uri     = new_uri
                self._lyrics         = None
                self._lyrics_parsed  = None
                self._lyrics_scroll  = 0.0
                self._lyrics_loading = True
                def _fetch_lyr(u=new_uri):
                    text = self._load_lyrics_for_uri(u)
                    self._lyrics         = text
                    self._lyrics_parsed  = self._parse_lyrics(text) if text else None
                    self._lyrics_loading = False
                    self._dirty          = True
                threading.Thread(target=_fetch_lyr, daemon=True).start()

        # mark dirty for ongoing animations/playback — never clear it here;
        # only draw() clears _dirty after actually rendering a frame
        if not self._dirty:
            now_ms = self._last_tick_ms
            self._dirty = (
                abs(self._album_y - self._album_y_t) > 0.5
                or abs(self._ctrl_a - self._ctrl_a_t) > 0.5

                or self._flash_alpha > 0
                or self._pending_tap
                or self._progress_px_changed()
                or now_ms < self._vol_until_ms
                or self._bt_refreshing
                or self._wifi_refreshing
                or self._scan_refreshing
                or self._lyrics_loading
                or self._scrub_active
                or abs(self._grid_vel) > 0.5
                or abs(self._tl_vel) > 0.5
                or abs(self._settings_vel) > 0.5
                or self._view == View.SCAN   # animated dots
                or self._thumbs_pending > 0
            )

    # ── draw ──────────────────────────────────────────────────────────────────

    def _progress_px_changed(self) -> bool:
        """True only when the progress bar pixel position has moved since last check."""
        if self._status.get("state") != "play":
            self._last_progress_px = -1
            return False
        time_str = self._status.get("time", "")
        parts = time_str.split(":") if time_str else []
        if len(parts) < 2:
            return True
        dur = float(parts[1])
        if dur <= 0:
            return True
        el  = self._elapsed_base + (time.monotonic() - self._elapsed_base_t)
        px  = int(W * min(1.0, el / dur))
        if px != self._last_progress_px:
            self._last_progress_px = px
            return True
        return False

    def target_fps(self) -> int:
        if not settings.get("idle_fps"):
            return FPS
        if pygame.time.get_ticks() - self._last_input_ms < 5000:
            return FPS
        return FPS if self._dirty else 10

    def draw(self) -> bool:
        kb_active = self._kb_ssid is not None
        if not self._dirty and settings.get("skip_draw") and not settings.get("debug") and not kb_active:
            return False
        self._dirty = False
        if self._view == View.SETTINGS:
            self._draw_settings()
            if self._bt_menu_dev is not None:
                self._draw_bt_menu()
            if self._wifi_menu_net is not None:
                self._draw_wifi_menu()
            self._draw_volume_badge()
            if settings.get("debug"):
                self._draw_debug_overlays()
            if kb_active:
                self._draw_keyboard()
            return True
        if self._view == View.CALIBRATE:
            self._draw_calibrate()
            return True
        if self._view == View.SCAN:
            self._draw_scan()
            self._draw_volume_badge()
            if settings.get("debug"):
                self._draw_debug_overlays()
            return True

        ay = self._album_y

        # ── layer 1: grid (when album is off-screen or peeking above) ───────────
        if ay > 2 or self._peeking:
            self._draw_grid()
        else:
            self.screen.fill(COL_GRID_BG)

        # ── layer 2: tracklist (drawn behind album art so art slides over it) ─
        if ay < -2 and not self._peeking:
            self._draw_tracklist()

        # ── layer 3: album art panel ──────────────────────────────────────────
        if ay < H - 1:
            self._draw_album_panel(int(ay))

        # ── layer 4: controls overlay (ALBUM only, art fully down) ────────────
        ca = int(self._ctrl_a)
        if ca > 0 and self._view == View.ALBUM:
            self._draw_controls_overlay(ca)

        # ── layer 4.5: action flash ───────────────────────────────────────────
        if self._flash_alpha > 0 and self._flash_icon:
            self._draw_flash()

        # ── layer 5: always-on-top ────────────────────────────────────────────
        self._draw_progress()
        self._draw_volume_badge()
        if settings.get("debug"):
            self._draw_debug_overlays()
        return True

    # ── draw: grid ────────────────────────────────────────────────────────────

    def _draw_grid(self):
        self.screen.fill(COL_GRID_BG)

        grid_top = 0

        if not self._albums or self._thumbs_pending > 0:
            loaded = len(self._albums) - self._thumbs_pending
            if not self._albums:
                msg_text = "Loading library…"
            else:
                msg_text = f"Loading art… {loaded} / {len(self._albums)}"
            msg = _render_text(self._f_artist, msg_text, COL_TEXT_ALBUM)
            self.screen.blit(msg, ((W - msg.get_width()) // 2,
                                    grid_top + (H - grid_top) // 2))
            return

        grid_bottom = (H - TRACKLIST_ART_H) if self._peeking else H

        total_rows = (len(self._albums) + GRID_COLS - 1) // GRID_COLS
        max_scroll = max(0, total_rows * _ROW_H + GRID_PAD - (grid_bottom - grid_top))
        self._grid_scroll = max(0.0, min(self._grid_scroll, float(max_scroll)))

        old_clip = self.screen.get_clip()
        try:
            self.screen.set_clip(0, grid_top, W, grid_bottom - grid_top)

            for i, album in enumerate(self._albums):
                row = i // GRID_COLS
                col = i % GRID_COLS
                x   = GRID_PAD + col * (_CELL_W + GRID_PAD)
                y   = grid_top + GRID_PAD + row * _ROW_H - int(self._grid_scroll)

                # preload thumbnails two rows outside the visible area
                if (y + _CELL_H >= grid_top - _ROW_H * 2
                        and y <= H + _ROW_H * 2):
                    self._queue_thumb(i)

                if y + _CELL_H < grid_top or y > H:
                    continue

                thumb = album.get("thumb")
                if thumb:
                    self.screen.blit(thumb, (x, y))
                else:
                    pygame.draw.rect(self.screen, COL_CELL_BG,
                                      (x, y, _CELL_W, _CELL_W), border_radius=4)

                ty = y + _CELL_W + 4
                sn = _render_text(self._f_grid,    album["name"],   COL_TEXT_TITLE,  _CELL_W)
                sa = _render_text(self._f_grid_sm, album["artist"], COL_TEXT_ARTIST, _CELL_W)
                self.screen.blit(sn, (x, ty))
                self.screen.blit(sa, (x, ty + sn.get_height() + 2))
        finally:
            self.screen.set_clip(old_clip)

    def _draw_mini_player(self):
        pygame.draw.rect(self.screen, COL_MINI_BG, (0, 0, W, MINI_H))
        pygame.draw.aaline(self.screen, COL_SEP, (0, MINI_H - 1), (W, MINI_H - 1))

        art_sz = MINI_H - MINI_PAD * 2

        # art — cache the scaled-down version so we don't smoothscale every frame
        thumb_surf = None
        if self._art:
            if self._mini_art_src is not self._art:
                self._mini_art     = pygame.transform.smoothscale(self._art, (art_sz, art_sz))
                self._mini_art_src = self._art
            thumb_surf = self._mini_art
        elif self._cur_idx is not None:
            th = self._albums[self._cur_idx].get("thumb")
            if th:
                thumb_surf = pygame.transform.smoothscale(th, (art_sz, art_sz))
        if thumb_surf:
            self.screen.blit(thumb_surf, (MINI_PAD, MINI_PAD))
        else:
            pygame.draw.rect(self.screen, COL_CELL_BG, (MINI_PAD, MINI_PAD, art_sz, art_sz), border_radius=3)

        # text
        info_x = MINI_PAD + art_sz + MINI_PAD
        info_w  = W - info_x - MINI_ICON_SIZE * 3
        title   = self._song.get("title") or (
            self._albums[self._cur_idx]["name"] if self._cur_idx is not None else "—")
        artist  = self._song.get("artist") or (
            self._albums[self._cur_idx]["artist"] if self._cur_idx is not None else "")

        text_y = (MINI_H - FONT_SZ_MINI - FONT_SZ_GRID_SM - MINI_PAD) // 2
        s = _render_text(self._f_mini, title, COL_TEXT_TITLE, info_w)
        self.screen.blit(s, (info_x, text_y))
        s = _render_text(self._f_grid_sm, artist, COL_TEXT_ARTIST, info_w)
        self.screen.blit(s, (info_x, text_y + FONT_SZ_MINI + MINI_PAD // 2))

        # play/pause icon
        playing = self._status.get("state") == "play"
        if playing:
            _draw_pause(self.screen, COL_TEXT_TITLE, W - MINI_ICON_SIZE - MINI_PAD, MINI_H // 2, MINI_ICON_SIZE)
        else:
            _draw_play(self.screen, COL_TEXT_TITLE, W - MINI_ICON_SIZE - MINI_PAD, MINI_H // 2, MINI_ICON_SIZE)

        # progress line
        el  = float(self._status.get("elapsed",  0) or 0)
        dur = float(self._status.get("duration", 0) or 0)
        if dur > 0:
            fw = int(W * el / dur)
            pygame.draw.rect(self.screen, COL_PROGRESS_BG, (0, MINI_H - 3, W, 3))
            pygame.draw.rect(self.screen, COL_PROGRESS_FG, (0, MINI_H - 3, fw, 3))

    # ── draw: album panel ─────────────────────────────────────────────────────

    def _draw_album_panel(self, ay: int):
        art = self._art or self._default_bg
        # SDL2 maps set_alpha(255) → plain byte-copy (no NEON, ~8× slower on
        # Cortex-A53). Using 254 keeps the NEON alpha-blend path active.
        art.set_alpha(254)
        self.screen.blit(art, (0, ay))

    # ── draw: controls overlay ────────────────────────────────────────────────

    def _draw_controls_overlay(self, alpha: int):
        # Album art is always at y=0 in ALBUM state.
        ov = self._overlay_surf
        ov.fill((0, 0, 0, min(200, int(alpha * 0.82))))
        self.screen.blit(ov, (0, 0))

        title  = self._song.get("title", "") or "—"
        artist = self._song.get("artist", "")
        album  = self._song.get("album",  "")

        # controls are vertically centred; build text surfaces and stack them above
        ctrl_cy = H // 2
        playing = self._status.get("state") == "play"

        lines = []
        if artist:
            lines.append(_render_text(self._f_artist, artist.upper(), COL_TEXT_ARTIST, W - 60))
        lines.append(_render_text(self._f_title, title, COL_TEXT_TITLE, W - 40))
        if album:
            lines.append(_render_text(self._f_album_f, album, COL_TEXT_ALBUM, W - 60))

        text_h = sum(s.get_height() for s in lines) + CTRL_TEXT_GAP * (len(lines) - 1)
        cy = ctrl_cy - CTRL_BAR_H // 2 - CTRL_TEXT_GAP * 2 - text_h

        for s in lines:
            self.screen.blit(s, ((W - s.get_width()) // 2, cy))
            cy += s.get_height() + CTRL_TEXT_GAP
        col = (200, 200, 200)
        big = (235, 235, 235)

        # offset so the two triangles in each pair touch exactly at the midpoint
        off = int(CTRL_ICON_SM * 0.65) // 2
        _draw_triangle(self.screen, col, W // 6 - off, ctrl_cy, CTRL_ICON_SM, "left")
        _draw_triangle(self.screen, col, W // 6 + off, ctrl_cy, CTRL_ICON_SM, "left")

        if playing:
            _draw_pause(self.screen, big, W // 2, ctrl_cy, CTRL_ICON_LG)
        else:
            _draw_play(self.screen, big, W // 2, ctrl_cy, CTRL_ICON_LG)

        _draw_triangle(self.screen, col, 5 * W // 6 - off, ctrl_cy, CTRL_ICON_SM, "right")
        _draw_triangle(self.screen, col, 5 * W // 6 + off, ctrl_cy, CTRL_ICON_SM, "right")

        # gear (settings) button — left of close button
        gx = W - BTN_RADIUS - BTN_MARGIN - 2 * BTN_RADIUS - BTN_GAP
        gy = BTN_MARGIN + BTN_RADIUS
        pygame.gfxdraw.filled_circle(self.screen, gx, gy, BTN_RADIUS, COL_HIGHLIGHT)
        pygame.gfxdraw.aacircle(self.screen, gx, gy, BTN_RADIUS, COL_HIGHLIGHT)
        self._draw_gear_icon(gx, gy, BTN_RADIUS - max(1, BTN_RADIUS * 5 // 12))

        # close button (top-right) — hides controls only
        bx = W - BTN_RADIUS - BTN_MARGIN
        by = BTN_MARGIN + BTN_RADIUS
        pygame.gfxdraw.filled_circle(self.screen, bx, by, BTN_RADIUS, COL_HIGHLIGHT)
        pygame.gfxdraw.aacircle(self.screen, bx, by, BTN_RADIUS, COL_HIGHLIGHT)
        d = max(1, BTN_RADIUS * 11 // 32)
        xc = _BTN_ICON_COL
        pygame.draw.aaline(self.screen, xc, (bx - d, by - d), (bx + d, by + d))
        pygame.draw.aaline(self.screen, xc, (bx - d + 1, by - d), (bx + d + 1, by + d))
        pygame.draw.aaline(self.screen, xc, (bx + d, by - d), (bx - d, by + d))
        pygame.draw.aaline(self.screen, xc, (bx + d + 1, by - d), (bx - d + 1, by + d))

        # stop button (top-left) — stops playback and returns to grid
        sx = BTN_RADIUS + BTN_MARGIN
        sy = BTN_MARGIN + BTN_RADIUS
        pygame.gfxdraw.filled_circle(self.screen, sx, sy, BTN_RADIUS, COL_HIGHLIGHT)
        pygame.gfxdraw.aacircle(self.screen, sx, sy, BTN_RADIUS, COL_HIGHLIGHT)
        sq = max(1, BTN_RADIUS * 13 // 32)
        pygame.draw.rect(self.screen, _BTN_ICON_COL,
                         (sx - sq, sy - sq, sq * 2, sq * 2))

        # speaker button — right of stop button
        if self.audio and self.audio.available:
            spx = sx + 2 * BTN_RADIUS + BTN_GAP
            spy = sy
            bg  = COL_HIGHLIGHT if self._audio_popup_open else (55, 55, 65)
            pygame.gfxdraw.filled_circle(self.screen, spx, spy, BTN_RADIUS, bg)
            pygame.gfxdraw.aacircle(self.screen, spx, spy, BTN_RADIUS, bg)
            self._draw_speaker_icon(spx, spy, BTN_RADIUS - max(1, BTN_RADIUS * 5 // 12))
            if self._audio_popup_open:
                self._draw_audio_popup(spx, spy)

        if settings.get("lyrics"):
            self._draw_lyrics(alpha)

    # ── draw: tracklist ───────────────────────────────────────────────────────

    def _draw_tracklist(self):
        # The tracklist always lives at y=TRACKLIST_ART_H to y=H.
        # The album art (drawn later) naturally covers only y=0..TRACKLIST_ART_H
        # because _album_y == _TL_ALBUM_Y when fully open.
        clip_h = H - TRACKLIST_ART_H - PROGRESS_H

        pygame.draw.rect(self.screen, COL_TL_BG,
                          (0, TRACKLIST_ART_H, W, clip_h + PROGRESS_H))
        pygame.draw.aaline(self.screen, COL_SEP,
                           (0, TRACKLIST_ART_H), (W, TRACKLIST_ART_H))

        if not self._tracks:
            msg = _render_text(self._f_artist, "Loading tracks…", COL_TEXT_ALBUM)
            self.screen.blit(msg, ((W - msg.get_width()) // 2,
                                    TRACKLIST_ART_H + 40))
            return

        max_sc = max(0, len(self._tracks) * TRACK_ROW_H - clip_h)
        self._tl_scroll = max(0.0, min(self._tl_scroll, float(max_sc)))

        cur_file = self._song.get("file", "")

        old_clip = self.screen.get_clip()
        try:
            self.screen.set_clip(0, TRACKLIST_ART_H, W, clip_h)

            for i, track in enumerate(self._tracks):
                ty = TRACKLIST_ART_H + i * TRACK_ROW_H - int(self._tl_scroll)
                if ty + TRACK_ROW_H < TRACKLIST_ART_H or ty > H:
                    continue
                self._draw_track_row(i, track, ty, cur_file)
        finally:
            self.screen.set_clip(old_clip)

    def _draw_track_row(self, idx: int, track: dict, y: int, cur_file: str):
        is_playing = track.get("file", "") == cur_file
        if is_playing:
            pygame.draw.rect(self.screen, (24, 22, 14), (0, y, W, TRACK_ROW_H))

        col_title = COL_TRACK_PLAYING if is_playing else COL_TRACK_NORMAL

        num = str(track.get("track", idx + 1) or idx + 1).split("/")[0].zfill(2)
        sn  = _render_text(self._f_track_sm, num, COL_TRACK_NUM)
        self.screen.blit(sn, (TRACK_PAD, y + (TRACK_ROW_H - sn.get_height()) // 2))

        raw_dur = track.get("duration") or track.get("time", 0)
        dur_s   = int(float(raw_dur or 0))
        dur_str = f"{dur_s // 60}:{dur_s % 60:02d}"
        sd = _render_text(self._f_track_sm, dur_str, COL_TRACK_DUR)
        self.screen.blit(sd, (W - TRACK_PAD - sd.get_width(),
                                y + (TRACK_ROW_H - sd.get_height()) // 2))

        title   = track.get("title") or track.get("name") or "—"
        title_x = TRACK_PAD + sn.get_width() + TRACK_PAD * 3 // 4
        title_w = W - title_x - sd.get_width() - TRACK_PAD * 2
        st = _render_text(self._f_track, title, col_title, title_w)
        self.screen.blit(st, (title_x, y + (TRACK_ROW_H - st.get_height()) // 2))

        pygame.draw.aaline(self.screen, COL_SEP,
                           (TRACK_PAD, y + TRACK_ROW_H - 1), (W - TRACK_PAD, y + TRACK_ROW_H - 1))

    # ── draw: progress bar + volume badge ─────────────────────────────────────

    def _progress_bar_y(self) -> int:
        """Return the screen y coordinate of the progress bar's top edge."""
        ay = self._album_y
        y  = min(H - PROGRESS_H, int(ay) + H - PROGRESS_H)
        if ay < 0 and self._tracks:
            cur_file = self._song.get("file", "")
            for i, track in enumerate(self._tracks):
                if track.get("file", "") == cur_file:
                    row_y  = TRACKLIST_ART_H + i * TRACK_ROW_H - int(self._tl_scroll)
                    target = min(H - PROGRESS_H, row_y + TRACK_ROW_H - PROGRESS_H)
                    y = max(target, y)
                    break
        return y

    def _draw_progress(self):
        self.screen.set_clip(None)   # defensive: ensure no stale clip hides the bar
        # mopidy's MPD status has no "duration" key — parse total from "time" ("pos:total")
        time_str = self._status.get("time", "")
        parts = time_str.split(":") if time_str else []
        dur = float(parts[1]) if len(parts) >= 2 else 0.0
        el  = self._elapsed_base
        if self._status.get("state") == "play":
            el = min(dur, el + (time.monotonic() - self._elapsed_base_t))

        y = self._progress_bar_y()

        if self._scrub_active:
            bar_h = max(PROGRESS_H * 3, 12)
            cr = bar_h // 2
            by2 = y - bar_h + PROGRESS_H
            pygame.draw.rect(self.screen, COL_PROGRESS_BG, (0, by2, W, bar_h))
            fw = int(W * self._scrub_frac)
            if fw > 0:
                pygame.draw.rect(self.screen, COL_PROGRESS_FG, (0, by2, fw, bar_h),
                                 border_top_left_radius=0, border_bottom_left_radius=0,
                                 border_top_right_radius=cr, border_bottom_right_radius=cr)
        else:
            cr = PROGRESS_H // 2
            pygame.draw.rect(self.screen, COL_PROGRESS_BG, (0, y, W, PROGRESS_H))
            if dur > 0:
                fw = int(W * el / dur)
                if fw > 0:
                    pygame.draw.rect(self.screen, COL_PROGRESS_FG, (0, y, fw, PROGRESS_H),
                                     border_top_left_radius=0, border_bottom_left_radius=0,
                                     border_top_right_radius=cr, border_bottom_right_radius=cr)

    def _draw_volume_badge(self):
        if not self.vc.available:
            return
        now = pygame.time.get_ticks()
        if now > self._vol_until_ms:
            return
        vol  = self.vc.volume
        fade = min(300, self._vol_until_ms - now)
        alpha = fade / 300  # 0.0–1.0

        bar_h  = H - VOL_BADGE_PAD * 2
        fill_h = int(bar_h * vol / 100)
        cr     = PROGRESS_H // 2   # radius = half width → pill ends

        # background track
        bg_col = tuple(int(c * alpha) for c in COL_PROGRESS_BG)
        pygame.draw.rect(self.screen, bg_col,
                         (0, VOL_BADGE_PAD, PROGRESS_H, bar_h), border_radius=cr)
        # filled portion (from bottom up)
        if fill_h > 0:
            fg_col = tuple(int(c * alpha) for c in COL_VOLUME_FG)
            pygame.draw.rect(self.screen, fg_col,
                             (0, VOL_BADGE_PAD + bar_h - fill_h, PROGRESS_H, fill_h),
                             border_radius=cr)

    # ── navigation ────────────────────────────────────────────────────────────

    def _go_album(self, idx: int):
        if idx < 0 or idx >= len(self._albums):
            return
        self._cur_idx   = idx
        self._peeking   = False
        album           = self._albums[idx]
        self._tracks    = []
        self._ctrl_a    = 0.0
        self._ctrl_a_t  = 0.0
        self._album_y_t = 0.0
        self._view      = View.ALBUM
        self._tl_scroll = 0.0

        # Immediately show album-level info (title will refine once tracks load)
        self.player.set_song_optimistic({
            "title":  "",
            "artist": album["artist"],
            "album":  album["name"],
            "file":   album["track_uri"],
        })
        self._song = self.player.get_current_song()

        # start art loading immediately from cached track_uri
        uri = album["track_uri"]
        if uri != self._art_uri:
            self._art_uri = uri
            self._load_art(uri)

        # load tracks + pause at track 0 in background
        def _load():
            tracks = self.player.get_album_tracks(album)
            album["tracks"] = tracks
            self._tracks = tracks
            if tracks:
                t0 = tracks[0]
                # Set title as soon as we know the first track — before load_album
                # so controls overlay shows it immediately
                self.player.set_song_optimistic({
                    "title":  t0.get("title", ""),
                    "artist": t0.get("artist") or t0.get("albumartist", album["artist"]),
                    "album":  t0.get("album", album["name"]),
                    "file":   t0.get("file", ""),
                })
                if settings.get("autoplay"):
                    self.player.play_album(tracks, 0)
                else:
                    self.player.load_album(tracks, 0)

        threading.Thread(target=_load, daemon=True).start()

    def _go_grid(self):
        self._peeking  = False
        self.player.stop()
        self._cur_idx  = None
        self._tracks   = []
        self._art     = None
        self._art_uri = ""
        self._album_y_t = float(H)
        self._ctrl_a    = 0.0
        self._ctrl_a_t  = 0.0
        self._view     = View.GRID

    def _peek_to_grid(self):
        """Swipe-down from ALBUM: slide art down so only the top strip shows at the bottom."""
        self._peeking   = True
        self._album_y_t = float(H - TRACKLIST_ART_H)   # 576 → top 144px of art at screen bottom
        self._ctrl_a    = 0.0
        self._ctrl_a_t  = 0.0
        self._view      = View.GRID

    def _unpeek(self):
        """Return from peek mode back to full ALBUM view."""
        self._peeking   = False
        self._album_y_t = 0.0
        self._view      = View.ALBUM

    def _open_tracklist(self):
        self._album_y_t = float(_TL_ALBUM_Y)   # art slides up
        self._ctrl_a    = 0.0
        self._ctrl_a_t  = 0.0
        self._view      = View.TRACKLIST

    def _close_tracklist(self):
        self._album_y_t = 0.0                  # art slides back down
        self._ctrl_a_t  = 0.0
        self._view      = View.ALBUM

    def _open_settings(self):
        self._settings_return      = self._view
        self._settings_return_ctrl = self._ctrl_a > 0
        self._ctrl_a      = 0.0
        self._ctrl_a_t    = 0.0
        self._settings_scroll = 0.0
        self._settings_vel    = 0.0
        self._view        = View.SETTINGS
        if self.bt and self.bt.available and not self._bt_refreshing:
            self._bt_refreshing   = True
            self._bt_last_refresh = pygame.time.get_ticks()
            def _refresh_bt():
                self._bt_devices    = self.bt.get_devices()
                self._bt_refreshing = False
            threading.Thread(target=_refresh_bt, daemon=True).start()
        if self.wifi and self.wifi.available and not self._wifi_refreshing:
            self._wifi_refreshing = True
            def _refresh_wifi():
                self._wifi_networks  = self.wifi.get_networks()
                self._wifi_refreshing = False
                self._dirty = True
            threading.Thread(target=_refresh_wifi, daemon=True).start()

    def _close_settings(self):
        self._view = self._settings_return
        if self._settings_return_ctrl:
            self._show_controls()

    def _open_scan(self):
        self._scan_devices    = []
        self._scan_action_addr = None
        self._scan_last_refresh = 0
        self._scan_refreshing  = False
        self._view = View.SCAN
        if self.bt and self.bt.available:
            self.bt.start_scan()

    def _close_scan(self):
        if self.bt and self.bt.available:
            self.bt.stop_scan()
            self._bt_devices = self.bt.get_devices()
        self._view = View.SETTINGS

    def _clear_thumb_cache(self):
        try:
            for fname in os.listdir(_THUMB_CACHE_DIR):
                if fname.endswith(".png"):
                    os.remove(os.path.join(_THUMB_CACHE_DIR, fname))
        except Exception as e:
            log.warning("clear cache: %s", e)
        self._cache_cleared_ms = pygame.time.get_ticks()
        self._dirty = True

    # ── draw: settings ────────────────────────────────────────────────────────

    def _show_flash(self, icon: str):
        self._flash_icon     = icon
        self._flash_alpha    = 255.0
        self._flash_start_ms = pygame.time.get_ticks()

    def _draw_flash(self):
        alpha = int(self._flash_alpha)
        cx, cy = W // 2, H // 2
        size   = W // 4
        icon   = self._flash_icon
        surf   = self._overlay_surf
        # ── pass 1: shadow (alpha already baked in) ──────────────────────────
        surf.fill((0, 0, 0, 0))
        r_outer = int(size * 0.92)
        r_inner = int(size * 0.42)
        steps   = 40
        for i in range(steps):
            r = r_outer - (r_outer - r_inner) * i // steps
            a = 160 * (i + 1) * alpha // (steps * 255)
            pygame.gfxdraw.filled_circle(surf, cx, cy, r, (0, 0, 0, a))
        self.screen.blit(surf, (0, 0))

        # ── pass 2: icon via 2× supersampling (no aapolygon colour fringe) ──
        # Draw filled_polygon at 2× resolution, smoothscale down; the resize
        # provides clean AA without gfxdraw edge-blending artefacts.
        ss   = self._flash_icon_surf   # W//2 × W//2
        s2   = W // 2                  # 2× "size"
        sc   = s2 // 2                 # centre in ss coords
        white = (255, 255, 255, 255)
        ss.fill((0, 0, 0, 0))

        if icon == "play":
            h = int(s2 * 0.95)
            pts = [(sc - int(s2 * 0.35), sc - h // 2),
                   (sc - int(s2 * 0.35), sc + h // 2),
                   (sc + int(s2 * 0.42), sc)]
            pygame.gfxdraw.filled_polygon(ss, pts, white)
        elif icon == "pause":
            bw  = max(3, s2 // 4)
            bh  = int(s2 * 0.9)
            gap = max(2, s2 // 6)
            pygame.gfxdraw.box(ss, (sc - bw - gap // 2, sc - bh // 2, bw, bh), white)
            pygame.gfxdraw.box(ss, (sc + gap // 2,       sc - bh // 2, bw, bh), white)
        elif icon in ("next", "prev"):
            # each triangle is half the surface width so the pair fits exactly
            w   = s2 // 2
            h   = w * 18 // 13   # same h/w ratio as control skip buttons (0.9/0.65)
            off = w // 2
            for ox in (-off, off) if icon == "next" else (off, -off):
                tx = sc + ox
                if icon == "next":
                    pts = [(tx - w // 2, sc - h // 2),
                           (tx - w // 2, sc + h // 2),
                           (tx + w // 2, sc)]
                else:
                    pts = [(tx + w // 2, sc - h // 2),
                           (tx + w // 2, sc + h // 2),
                           (tx - w // 2, sc)]
                pygame.gfxdraw.filled_polygon(ss, pts, white)

        scaled = pygame.transform.smoothscale(ss, (size, size))
        scaled.fill((255, 255, 255, alpha), special_flags=pygame.BLEND_RGBA_MULT)
        self.screen.blit(scaled, (cx - size // 2, cy - size // 2))

    # ── lyrics ────────────────────────────────────────────────────────────────

    def _resolve_music_path(self, uri: str) -> str | None:
        """Convert an MPD/Mopidy file URI to an absolute filesystem path."""
        from urllib.parse import unquote
        path = uri
        if path.startswith("file://"):
            return unquote(path[7:])
        if path.startswith("local:track:"):
            rel = unquote(path[len("local:track:"):])
        elif not os.path.isabs(path):
            rel = path
        else:
            return path
        base = MUSIC_DIR or os.path.expanduser("~/Music")
        return os.path.join(base, rel)

    def _load_lyrics_for_uri(self, uri: str) -> str | None:
        path = self._resolve_music_path(uri)

        # 1. embedded tags (FLAC / MP3 via mutagen)
        if path and path.lower().endswith(".flac"):
            try:
                from mutagen.flac import FLAC
                f = FLAC(path)
                for tag in ("LYRICS", "UNSYNCEDLYRICS", "lyrics", "unsyncedlyrics"):
                    if tag in f:
                        return "\n".join(f[tag])
            except ImportError:
                pass
            except Exception as e:
                log.debug("lyrics embedded load: %s", e)

        # 2. sidecar .lrc file (same dir, same stem)
        if path:
            try:
                lrc_path = os.path.splitext(path)[0] + ".lrc"
                if os.path.exists(lrc_path):
                    with open(lrc_path, encoding="utf-8", errors="replace") as fh:
                        return fh.read()
            except Exception as e:
                log.debug("lyrics lrc load: %s", e)

        # 3. lyrics.ovh API (free, no key required)
        song   = self._song
        artist = (song.get("artist") or song.get("albumartist") or "").strip()
        title  = (song.get("title") or "").strip()
        if artist and title:
            try:
                import urllib.parse
                url = ("https://api.lyrics.ovh/v1/"
                       + urllib.parse.quote(artist) + "/"
                       + urllib.parse.quote(title))
                r = __import__("requests").get(url, timeout=10)
                if r.status_code == 200:
                    text = r.json().get("lyrics", "").strip()
                    if text:
                        log.info("Lyrics fetched from lyrics.ovh for %s – %s", artist, title)
                        return text
                else:
                    log.debug("lyrics.ovh: %s %s/%s", r.status_code, artist, title)
            except Exception as e:
                log.debug("lyrics.ovh fetch failed: %s", e)

        return None

    def _parse_lyrics(self, text: str) -> tuple:
        """Return (lines, times) where times is list[float] (seconds) or None."""
        timed: list[tuple[float, str]] = []
        plain: list[str] = []
        has_ts = False

        for raw in text.splitlines():
            m = _LRC_TS_RE.match(raw)
            if m:
                has_ts = True
                t = int(m.group(1)) * 60 + float(m.group(2))
                # strip all [mm:ss] tags from line
                line_text = _LRC_TS_RE.sub('', raw).strip()
                timed.append((t, line_text))
            elif _LRC_META_RE.match(raw):
                continue
            else:
                stripped = raw.strip()
                if stripped:
                    plain.append(stripped)

        if has_ts and timed:
            timed.sort(key=lambda x: x[0])
            return ([t[1] for t in timed], [t[0] for t in timed])
        return (plain, None)

    def _draw_lyrics(self, alpha: int):
        parsed = self._lyrics_parsed
        if not parsed:
            return
        lines, times = parsed
        if not lines:
            return

        fnt    = self._f_lyrics
        line_h = fnt.get_linesize() + max(2, fnt.get_linesize() // 6)

        y_start = H // 2 + CTRL_BAR_H // 2 + CTRL_TEXT_GAP * 2
        y_end   = H - PROGRESS_H - CTRL_TEXT_GAP
        avail_h = y_end - y_start
        if avail_h < line_h:
            return

        max_w = W - BTN_MARGIN * 6
        total = len(lines)

        # expand logical lines into visual rows (word-wrap) — cached
        cache = self._lyrics_row_cache
        if cache is None or cache[0] is not self._lyrics_parsed:
            visual_rows    = []
            visual_logical = []
            first_visual   = {}
            for li, text in enumerate(lines):
                first_visual[li] = len(visual_rows)
                for chunk in (_wrap_text(fnt, text, max_w) or [""]):
                    visual_rows.append(chunk)
                    visual_logical.append(li)
            self._lyrics_row_cache = (self._lyrics_parsed, visual_rows, visual_logical, first_visual)
        else:
            _, visual_rows, visual_logical, first_visual = cache

        n_vis_v   = avail_h // line_h
        total_vis = len(visual_rows)

        # elapsed time
        time_str = self._status.get("time", "")
        parts    = time_str.split(":") if time_str else []
        dur      = float(parts[1]) if len(parts) >= 2 else 0.0
        el       = self._elapsed_base
        if self._status.get("state") == "play":
            el = min(dur, el + (time.monotonic() - self._elapsed_base_t))

        # current logical line (float, fractional between lines)
        if times:
            idx = bisect.bisect_right(times, el) - 1
            idx = max(0, min(total - 1, idx))
            if idx + 1 < total:
                span = times[idx + 1] - times[idx]
                frac = (el - times[idx]) / span if span > 0 else 0.0
                frac = max(0.0, min(1.0, frac))
            else:
                frac = 0.0
            cur_logical = float(idx) + frac

            # map fractional logical position to fractional visual row position
            li_a    = int(cur_logical)
            li_b    = min(total - 1, li_a + 1)
            cur_vis = (first_visual.get(li_a, 0)
                       + (first_visual.get(li_b, 0) - first_visual.get(li_a, 0))
                         * (cur_logical - li_a))
            float_start = cur_vis - n_vis_v * 0.3
            float_start = max(0.0, min(float(max(0, total_vis - n_vis_v)), float_start))
            focus_li    = int(cur_logical)
        else:
            # manual scroll: clamp scroll offset and derive focus from position
            self._lyrics_scroll = max(0.0, min(float(max(0, total_vis - n_vis_v)),
                                               self._lyrics_scroll))
            float_start = self._lyrics_scroll
            focus_vi    = min(total_vis - 1, int(float_start + n_vis_v * 0.3))
            focus_li    = visual_logical[max(0, focus_vi)]

        v_start = int(float_start)
        y_sub   = -int((float_start - v_start) * line_h)

        old_clip = self.screen.get_clip()
        self.screen.set_clip(0, y_start, W, avail_h)

        a_foc   = alpha / 255
        a_dim   = alpha * 0.35 / 255
        for i in range(-1, n_vis_v + 2):
            vi = v_start + i
            if vi < 0 or vi >= total_vis:
                continue
            li      = visual_logical[vi]
            focused = (li == focus_li)
            # bake alpha into colour so _render_text can cache the surface
            af  = a_foc if focused else a_dim
            col = (int(235 * af), int(235 * af), int(235 * af)) if focused \
                  else (int(160 * af), int(160 * af), int(160 * af))
            s   = _render_text(fnt, visual_rows[vi], col)
            y   = y_start + y_sub + i * line_h
            self.screen.blit(s, (BTN_MARGIN * 3, y))

        self.screen.set_clip(old_clip)

    def _any_settings_row_hit(self, pos) -> bool:
        """True if pos lands on any tappable row in the settings view.
        Add new hit-test methods here and the debug overlay picks them up automatically."""
        return bool(
            self._settings_item_at(pos)
            or self._bt_device_at(pos)
            or self._settings_scan_btn_at(pos)
            or self._wifi_network_at(pos)
            or self._settings_clear_cache_btn_at(pos)
            or self._settings_debug_btn_at(pos)
            or self._settings_calibrate_btn_at(pos)
            or self._settings_reset_cal_btn_at(pos)
            or self._settings_restart_btn_at(pos)
            or self._settings_shutdown_btn_at(pos)
        )

    def _draw_debug_overlays(self):
        dc = (255, 0, 0)   # debug red
        lw = 2

        fps_surf = _render_text(self._f_track_sm, f"{self._fps_avg:.0f} fps", (255, 255, 0))
        self.screen.blit(fps_surf, (BTN_MARGIN, BTN_MARGIN))

        def rect(x, y, w, h):
            pygame.draw.rect(self.screen, dc, (x, y, w, h), lw)

        def circle(cx, cy, r):
            pygame.draw.circle(self.screen, dc, (cx, cy), r, lw)

        # scrub zone (every view)
        bar_y = self._progress_bar_y()
        rect(0, bar_y - SCRUB_LEEWAY, W, SCRUB_LEEWAY * 2)

        # back / close / stop / gear buttons (shared positions)
        bx, by = W - BTN_RADIUS - BTN_MARGIN, BTN_MARGIN + BTN_RADIUS
        gx, gy = W - BTN_RADIUS - BTN_MARGIN - 2 * BTN_RADIUS - BTN_GAP, BTN_MARGIN + BTN_RADIUS
        sx, sy = BTN_RADIUS + BTN_MARGIN, BTN_MARGIN + BTN_RADIUS

        if self._view == View.ALBUM:
            if int(self._ctrl_a) > 10:
                circle(bx, by, BTN_RADIUS + 10)   # close
                circle(sx, sy, BTN_RADIUS + 10)   # stop
                circle(gx, gy, BTN_RADIUS + 10)   # gear
                if self.audio and self.audio.available:
                    spx, spy = self._speaker_btn_pos()
                    circle(spx, spy, BTN_RADIUS + 10)   # speaker
                ctrl_y1, ctrl_y2 = H // 4, 3 * H // 4
                rect(0,           ctrl_y1, W // 3, ctrl_y2 - ctrl_y1)   # prev
                rect(W // 3,      ctrl_y1, W // 3, ctrl_y2 - ctrl_y1)   # play
                rect(2 * W // 3,  ctrl_y1, W // 3, ctrl_y2 - ctrl_y1)   # next
                # lyrics drag zone
                if (self._lyrics_parsed and self._lyrics_parsed[1] is None):
                    y_lyr = H // 2 + CTRL_BAR_H // 2 + CTRL_TEXT_GAP * 2
                    rect(0, y_lyr, W, H - y_lyr - PROGRESS_H)

        elif self._view == View.SETTINGS:
            circle(bx, by, BTN_RADIUS + 10)
            bt_rows   = (2 + len(self._bt_devices)) if (self.bt and self.bt.available) else 0
            wifi_rows = (1 + len(self._wifi_networks)) if (self.wifi and self.wifi.available) else 0
            total_rows = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 9
            for i in range(total_rows):
                probe = (W // 2, self._settings_row_y(i) + TRACK_ROW_H // 2)
                if self._any_settings_row_hit(probe):
                    rect(0, self._settings_row_y(i), W, TRACK_ROW_H)
            if self._bt_menu_dev is not None:
                px, py, pw, _ = self._bt_menu_rect()
                rect(px, py + TRACK_ROW_H,     pw, TRACK_ROW_H)
                rect(px, py + 2 * TRACK_ROW_H, pw, TRACK_ROW_H)
            if self._wifi_menu_net is not None:
                px, py, pw, ph = self._wifi_menu_rect()
                n_rows = ph // TRACK_ROW_H
                for mi in range(1, n_rows):   # skip header row (not tappable)
                    rect(px, py + mi * TRACK_ROW_H, pw, TRACK_ROW_H)

        elif self._view == View.SCAN:
            circle(bx, by, BTN_RADIUS + 10)
            y0 = 2 * (BTN_MARGIN + BTN_RADIUS)
            for i in range(len(self._scan_devices)):
                rect(0, y0 + i * TRACK_ROW_H, W, TRACK_ROW_H)

        elif self._view == View.TRACKLIST:
            rect(0, 0, W, TRACKLIST_ART_H)   # art strip → close tracklist
            for i in range(len(self._tracks)):
                ty = TRACKLIST_ART_H + i * TRACK_ROW_H - int(self._tl_scroll)
                if ty + TRACK_ROW_H > TRACKLIST_ART_H and ty < H:
                    rect(0, ty, W, TRACK_ROW_H)

        elif self._view == View.GRID:
            for i in range(len(self._albums)):
                row = i // GRID_COLS
                c   = i % GRID_COLS
                x   = GRID_PAD + c * (_CELL_W + GRID_PAD)
                y   = GRID_PAD + row * _ROW_H - int(self._grid_scroll)
                if y + _CELL_H > 0 and y < H:
                    rect(x, y, _CELL_W, _CELL_H)
            if self._peeking:
                rect(0, H - TRACKLIST_ART_H, W, TRACKLIST_ART_H)   # peek strip → unpeek

        # current touch position
        if self._t_start_pos is not None and self._t_prev_pos is not None:
            tx, ty = self._t_prev_pos
            pygame.gfxdraw.filled_circle(self.screen, tx, ty, 28, (255, 0, 0, 80))
            pygame.gfxdraw.aacircle(self.screen, tx, ty, 28, (255, 0, 0, 200))

    def _draw_speaker_icon(self, cx, cy, r):
        col = _BTN_ICON_COL
        # speaker body (trapezoid)
        bw = max(2, r // 3)
        bh = max(2, r // 2)
        pts = [
            (cx - r // 2,     cy - bh),
            (cx - r // 2 + bw, cy - bh),
            (cx + r // 3,      cy - r),
            (cx + r // 3,      cy + r),
            (cx - r // 2 + bw, cy + bh),
            (cx - r // 2,      cy + bh),
        ]
        pygame.gfxdraw.filled_polygon(self.screen, pts, col)
        pygame.gfxdraw.aapolygon(self.screen, pts, col)
        # sound wave arcs
        for scale in (0.5, 0.85):
            ar = int(r * scale)
            pygame.draw.arc(self.screen, col,
                            (cx + r // 6, cy - ar, ar, ar * 2),
                            -math.pi / 3, math.pi / 3, max(1, r // 8))

    def _draw_audio_popup(self, btn_x, btn_y):
        if not self._audio_sinks:
            return
        row_h = TRACK_ROW_H
        pw    = min(W - BTN_MARGIN * 2, max(260, W * 2 // 3))
        ph    = len(self._audio_sinks) * row_h
        px    = max(BTN_MARGIN, btn_x - pw // 2)
        # open downward from the button
        py    = btn_y + BTN_RADIUS + 8
        if py + ph > H - PROGRESS_H:
            py = btn_y - BTN_RADIUS - 8 - ph   # flip upward if no room

        ov = self._overlay_surf
        ov.fill((0, 0, 0, 0))
        self.screen.blit(ov, (0, 0))

        pygame.draw.rect(self.screen, COL_CELL_BG, (px, py, pw, ph), border_radius=8)
        pygame.draw.rect(self.screen, COL_SEP,     (px, py, pw, ph), width=1, border_radius=8)

        dot_r = max(3, row_h // 10)
        for i, sink in enumerate(self._audio_sinks):
            ry  = py + i * row_h
            busy = self._audio_busy_id == sink["id"]
            col  = COL_TRACK_NUM if busy else COL_TRACK_NORMAL
            sl   = _render_text(self._f_track, sink["name"], col, pw - BTN_MARGIN * 2 - dot_r * 3)
            self.screen.blit(sl, (px + BTN_MARGIN, ry + (row_h - sl.get_height()) // 2))
            if sink["active"] and not busy:
                dot_col = COL_HIGHLIGHT
                dot_cx  = px + pw - BTN_MARGIN - dot_r
                dot_cy  = ry + row_h // 2
                pygame.gfxdraw.filled_circle(self.screen, dot_cx, dot_cy, dot_r, dot_col)
                pygame.gfxdraw.aacircle(self.screen, dot_cx, dot_cy, dot_r, dot_col)
            if i < len(self._audio_sinks) - 1:
                pygame.draw.aaline(self.screen, COL_SEP,
                                   (px, ry + row_h - 1), (px + pw, ry + row_h - 1))

    def _draw_gear_icon(self, cx, cy, r):
        col = _BTN_ICON_COL
        teeth, R_out = 8, r
        R_in = max(1, round(r * 0.68))
        hole = max(1, round(r * 0.38))
        pts = []
        step = 2 * math.pi / teeth
        tip = step * 0.38
        for i in range(teeth):
            base = i * step
            for a, rad in [(base - tip, R_in), (base - tip * 0.5, R_out),
                           (base + tip * 0.5, R_out), (base + tip, R_in)]:
                pts.append((cx + round(rad * math.cos(a)), cy + round(rad * math.sin(a))))
        pygame.gfxdraw.filled_polygon(self.screen, pts, col)
        pygame.gfxdraw.aapolygon(self.screen, pts, col)
        pygame.gfxdraw.filled_circle(self.screen, cx, cy, hole, COL_HIGHLIGHT)
        pygame.gfxdraw.aacircle(self.screen, cx, cy, hole, COL_HIGHLIGHT)

    def _draw_toggle(self, x, y, on):
        pw, ph = TOGGLE_W, TOGGLE_H
        cr = ph // 2
        bg = COL_HIGHLIGHT if on else (55, 55, 65)
        pygame.draw.rect(self.screen, bg, (x + cr, y, pw - 2 * cr, ph))
        pygame.gfxdraw.filled_circle(self.screen, x + cr,        y + cr, cr, bg)
        pygame.gfxdraw.aacircle(self.screen, x + cr,             y + cr, cr, bg)
        pygame.gfxdraw.filled_circle(self.screen, x + pw - cr,   y + cr, cr, bg)
        pygame.gfxdraw.aacircle(self.screen, x + pw - cr,        y + cr, cr, bg)
        tx = x + pw - cr if on else x + cr
        knob_r = max(1, cr - max(1, cr // 4))
        pygame.gfxdraw.filled_circle(self.screen, tx, y + cr, knob_r, (240, 240, 240))
        pygame.gfxdraw.aacircle(self.screen, tx,  y + cr, knob_r, (240, 240, 240))

    def _draw_settings(self):
        self.screen.fill(COL_BG)
        # Back button (top-right) — matches controls X position exactly
        bx, by = W - BTN_RADIUS - BTN_MARGIN, BTN_MARGIN + BTN_RADIUS
        d = max(1, BTN_RADIUS * 11 // 32)
        pygame.draw.aaline(self.screen, COL_HIGHLIGHT, (bx - d, by - d), (bx + d, by + d))
        pygame.draw.aaline(self.screen, COL_HIGHLIGHT, (bx - d + 1, by - d), (bx + d + 1, by + d))
        pygame.draw.aaline(self.screen, COL_HIGHLIGHT, (bx + d, by - d), (bx - d, by + d))
        pygame.draw.aaline(self.screen, COL_HIGHLIGHT, (bx + d + 1, by - d), (bx - d + 1, by + d))

        sep_y  = 2 * (BTN_MARGIN + BTN_RADIUS)
        clip_y = sep_y
        clip_h = H - PROGRESS_H - BTN_MARGIN * 2 - clip_y   # viewport stops well above scrub bar
        hdr = _render_text(self._f_title, "Settings", COL_TEXT_TITLE)
        self.screen.blit(hdr, ((W - hdr.get_width()) // 2, (sep_y - hdr.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP, (0, sep_y), (W, sep_y))

        # compute total content height from row count (independent of scroll)
        bt_rows   = (2 + len(self._bt_devices)) if (self.bt and self.bt.available) else 0
        wifi_rows = (1 + len(self._wifi_networks)) if (self.wifi and self.wifi.available) else 0
        total_rows = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 9   # SYSTEM(hdr+cache+debug) + TOUCH(hdr+cal+reset) + POWER(hdr+restart+shutdown)
        content_h  = total_rows * TRACK_ROW_H + BTN_MARGIN * 2
        max_scroll  = max(0.0, float(content_h - clip_h))
        self._settings_scroll = max(0.0, min(self._settings_scroll, max_scroll))
        self._settings_vel    = 0.0 if self._settings_scroll >= max_scroll else self._settings_vel

        scroll   = int(self._settings_scroll)
        old_clip = self.screen.get_clip()
        self.screen.set_clip(0, clip_y, W, H - clip_y)   # clip top only, open bottom

        y = clip_y - scroll   # content origin, offset by scroll
        for key, label in _SETTINGS_ITEMS:
            if key is None:
                # section header
                sh = _render_text(self._f_track_sm, label, COL_TEXT_ALBUM)
                self.screen.blit(sh, (BTN_MARGIN, y + (TRACK_ROW_H - sh.get_height()) // 2))
            else:
                sl = _render_text(self._f_track, label, COL_TRACK_NORMAL)
                self.screen.blit(sl, (BTN_MARGIN, y + (TRACK_ROW_H - sl.get_height()) // 2))
                self._draw_toggle(W - BTN_MARGIN - TOGGLE_W, y + (TRACK_ROW_H - TOGGLE_H) // 2, settings.get(key))
            pygame.draw.aaline(self.screen, COL_SEP,
                               (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
            y += TRACK_ROW_H

        if self.bt and self.bt.available:
            sh = _render_text(self._f_track_sm, "BLUETOOTH", COL_TEXT_ALBUM)
            self.screen.blit(sh, (BTN_MARGIN, y + (TRACK_ROW_H - sh.get_height()) // 2))
            pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
            y += TRACK_ROW_H

            dot_r = max(2, TRACK_ROW_H // 8)
            dot_x = W - BTN_MARGIN - dot_r
            for dev in self._bt_devices:
                busy = self._bt_action_addr == dev["address"]
                col  = COL_TRACK_NUM if busy else COL_TRACK_NORMAL
                sl   = _render_text(self._f_track, dev["name"], col)
                self.screen.blit(sl, (BTN_MARGIN, y + (TRACK_ROW_H - sl.get_height()) // 2))
                dot_col = COL_HIGHLIGHT if dev["connected"] and not busy else COL_SEP
                dot_cy  = y + TRACK_ROW_H // 2
                pygame.gfxdraw.filled_circle(self.screen, dot_x, dot_cy, dot_r, dot_col)
                pygame.gfxdraw.aacircle(self.screen, dot_x, dot_cy, dot_r, dot_col)
                pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
                y += TRACK_ROW_H

            sc = _render_text(self._f_track, "Search for new devices", COL_HIGHLIGHT)
            self.screen.blit(sc, (BTN_MARGIN, y + (TRACK_ROW_H - sc.get_height()) // 2))
            pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
            y += TRACK_ROW_H

        if self.wifi and self.wifi.available:
            sh = _render_text(self._f_track_sm, "WI-FI", COL_TEXT_ALBUM)
            self.screen.blit(sh, (BTN_MARGIN, y + (TRACK_ROW_H - sh.get_height()) // 2))
            pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
            y += TRACK_ROW_H

            # signal bar geometry
            bar_w  = max(3, TRACK_ROW_H // 8)
            bar_gap = max(1, bar_w // 3)
            bar_max = TRACK_ROW_H * 2 // 5
            n_bars  = 4
            sig_w   = n_bars * bar_w + (n_bars - 1) * bar_gap
            lock_w  = max(10, TRACK_ROW_H // 4)

            for net in self._wifi_networks:
                busy = self._wifi_action_name == net["name"]
                col  = COL_TRACK_NUM if busy else COL_TRACK_NORMAL
                sl   = _render_text(self._f_track, net["name"], col)
                cy   = y + TRACK_ROW_H // 2

                # signal bars (right-aligned)
                sig   = net.get("signal", 0) or 0
                thres = [1, 30, 55, 75]
                sx    = W - BTN_MARGIN - sig_w
                for i in range(n_bars):
                    bh  = bar_max * (i + 1) // n_bars
                    bx  = sx + i * (bar_w + bar_gap)
                    by  = cy + bar_max // 2 - bh
                    lit = sig >= thres[i]
                    bcol = (COL_HIGHLIGHT if net["connected"] else COL_TRACK_NORMAL) if lit else COL_SEP
                    pygame.draw.rect(self.screen, bcol, (bx, by, bar_w, bh), border_radius=1)

                # lock icon for unsaved networks
                if not net["saved"]:
                    lx = sx - BTN_MARGIN // 2 - lock_w
                    lh = int(lock_w * 0.55)
                    body_y = cy - lh // 2 + lock_w // 4
                    pygame.draw.rect(self.screen, COL_TRACK_NUM,
                                     (lx, body_y, lock_w, lh), border_radius=2)
                    pygame.draw.arc(self.screen, COL_TRACK_NUM,
                                    (lx + lock_w // 5, body_y - lock_w // 2,
                                     lock_w * 3 // 5, lock_w // 2),
                                    0, math.pi, max(1, lock_w // 8))

                self.screen.blit(sl, (BTN_MARGIN, cy - sl.get_height() // 2))
                pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
                y += TRACK_ROW_H

        # ── SYSTEM ────────────────────────────────────────────────────────────
        sh = _render_text(self._f_track_sm, "SYSTEM", COL_TEXT_ALBUM)
        self.screen.blit(sh, (BTN_MARGIN, y + (TRACK_ROW_H - sh.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        now_ms = pygame.time.get_ticks()
        recently_cleared = self._cache_cleared_ms and now_ms - self._cache_cleared_ms < 2000
        cc_label = "Cache cleared" if recently_cleared else "Clear cache"
        cc_col   = COL_TEXT_ALBUM if recently_cleared else COL_HIGHLIGHT
        cc = _render_text(self._f_track, cc_label, cc_col)
        self.screen.blit(cc, (BTN_MARGIN, y + (TRACK_ROW_H - cc.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        dbg_s = _render_text(self._f_track, "Debug mode", COL_TRACK_NORMAL)
        self.screen.blit(dbg_s, (BTN_MARGIN, y + (TRACK_ROW_H - dbg_s.get_height()) // 2))
        self._draw_toggle(W - BTN_MARGIN - TOGGLE_W, y + (TRACK_ROW_H - TOGGLE_H) // 2, settings.get("debug"))
        pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        # ── TOUCH ─────────────────────────────────────────────────────────────
        sh = _render_text(self._f_track_sm, "TOUCH", COL_TEXT_ALBUM)
        self.screen.blit(sh, (BTN_MARGIN, y + (TRACK_ROW_H - sh.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        cal_s = _render_text(self._f_track, "Calibrate touch", COL_HIGHLIGHT)
        self.screen.blit(cal_s, (BTN_MARGIN, y + (TRACK_ROW_H - cal_s.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        is_default = (settings.get("cal_sx") == 1.0 and settings.get("cal_ox") == 0.0
                      and settings.get("cal_sy") == 1.0 and settings.get("cal_oy") == 0.0)
        rst_col = COL_TEXT_ALBUM if is_default else COL_HIGHLIGHT
        rst_s   = _render_text(self._f_track, "Reset calibration", rst_col)
        self.screen.blit(rst_s, (BTN_MARGIN, y + (TRACK_ROW_H - rst_s.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        # ── POWER ─────────────────────────────────────────────────────────────
        sh = _render_text(self._f_track_sm, "POWER", COL_TEXT_ALBUM)
        self.screen.blit(sh, (BTN_MARGIN, y + (TRACK_ROW_H - sh.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        rb_s = _render_text(self._f_track, "Restart", (220, 160, 60))
        self.screen.blit(rb_s, (BTN_MARGIN, y + (TRACK_ROW_H - rb_s.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        sd_s = _render_text(self._f_track, "Shut down", (220, 80, 80))
        self.screen.blit(sd_s, (BTN_MARGIN, y + (TRACK_ROW_H - sd_s.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))

        self.screen.set_clip(old_clip)
        self._draw_progress()

    def _draw_scan(self):
        self.screen.fill(COL_BG)
        # Stop/back button — same X position as settings
        bx, by = W - BTN_RADIUS - BTN_MARGIN, BTN_MARGIN + BTN_RADIUS
        d = max(1, BTN_RADIUS * 11 // 32)
        pygame.draw.aaline(self.screen, COL_HIGHLIGHT, (bx - d, by - d), (bx + d, by + d))
        pygame.draw.aaline(self.screen, COL_HIGHLIGHT, (bx - d + 1, by - d), (bx + d + 1, by + d))
        pygame.draw.aaline(self.screen, COL_HIGHLIGHT, (bx + d, by - d), (bx - d, by + d))
        pygame.draw.aaline(self.screen, COL_HIGHLIGHT, (bx + d + 1, by - d), (bx - d + 1, by + d))

        sep_y = 2 * (BTN_MARGIN + BTN_RADIUS)
        dots  = "." * (1 + (pygame.time.get_ticks() // 500) % 3)
        hdr   = _render_text(self._f_title, f"Scanning{dots}", COL_TEXT_TITLE)
        self.screen.blit(hdr, ((W - hdr.get_width()) // 2, (sep_y - hdr.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP, (0, sep_y), (W, sep_y))

        y = sep_y
        if not self._scan_devices:
            hint = _render_text(self._f_track_sm, "Put device in pairing mode", COL_TEXT_ALBUM)
            self.screen.blit(hint, ((W - hint.get_width()) // 2,
                                    y + TRACK_ROW_H + (TRACK_ROW_H - hint.get_height()) // 2))
        else:
            for dev in self._scan_devices:
                busy  = self._scan_action_addr == dev["address"]
                col   = COL_TRACK_NUM if busy else COL_TRACK_NORMAL
                label = "Pairing…" if busy else dev["name"]
                sl    = _render_text(self._f_track, label, col)
                self.screen.blit(sl, (BTN_MARGIN, y + (TRACK_ROW_H - sl.get_height()) // 2))
                pygame.draw.aaline(self.screen, COL_SEP,
                                   (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
                y += TRACK_ROW_H

        self._draw_progress()

    # ── touch calibration ─────────────────────────────────────────────────────

    # 5 target points as fractions of screen size (margin, centre)
    _CAL_TARGETS = [
        (0.1, 0.1), (0.9, 0.1), (0.5, 0.5), (0.1, 0.9), (0.9, 0.9),
    ]

    def _open_calibrate(self):
        self._cal_points = [
            (int(fx * W), int(fy * H)) for fx, fy in self._CAL_TARGETS
        ]
        self._cal_raw  = []
        self._cal_step = 0
        self._view     = View.CALIBRATE
        self._dirty    = True

    def _finish_calibrate(self):
        pts = self._cal_points
        raw = self._cal_raw
        n   = len(pts)
        # least-squares fit: screen = s * raw + o  (per axis independently)
        sx_raw = sum(r[0] for r in raw) / n
        sx_scr = sum(p[0] for p in pts) / n
        sx_cov = sum((raw[i][0] - sx_raw) * (pts[i][0] - sx_scr) for i in range(n))
        sx_var = sum((raw[i][0] - sx_raw) ** 2 for i in range(n))
        sx = sx_cov / sx_var if sx_var else 1.0
        ox = sx_scr - sx * sx_raw

        sy_raw = sum(r[1] for r in raw) / n
        sy_scr = sum(p[1] for p in pts) / n
        sy_cov = sum((raw[i][1] - sy_raw) * (pts[i][1] - sy_scr) for i in range(n))
        sy_var = sum((raw[i][1] - sy_raw) ** 2 for i in range(n))
        sy = sy_cov / sy_var if sy_var else 1.0
        oy = sy_scr - sy * sy_raw

        settings.set("cal_sx", sx); settings.set("cal_ox", ox)
        settings.set("cal_sy", sy); settings.set("cal_oy", oy)
        self._view  = View.SETTINGS
        self._dirty = True

    def _draw_calibrate(self):
        self.screen.fill(COL_BG)
        if self._cal_step >= len(self._cal_points):
            return
        px, py = self._cal_points[self._cal_step]
        total  = len(self._cal_points)

        # instructions
        hdr = _render_text(self._f_title, "Touch Calibration", COL_TEXT_TITLE)
        self.screen.blit(hdr, ((W - hdr.get_width()) // 2, H // 6 - hdr.get_height() // 2))
        sub = _render_text(self._f_track_sm,
                           f"Tap the crosshair  ({self._cal_step + 1} / {total})",
                           COL_TEXT_ALBUM)
        self.screen.blit(sub, ((W - sub.get_width()) // 2, H // 6 + hdr.get_height()))

        # crosshair — leave a gap around the centre dot so it reads clearly
        arm = max(18, W // 20)
        dot_r = 5
        gap   = dot_r + 4
        pygame.draw.aaline(self.screen, COL_HIGHLIGHT, (px - arm, py), (px - gap, py))
        pygame.draw.aaline(self.screen, COL_HIGHLIGHT, (px + gap, py), (px + arm, py))
        pygame.draw.aaline(self.screen, COL_HIGHLIGHT, (px, py - arm), (px, py - gap))
        pygame.draw.aaline(self.screen, COL_HIGHLIGHT, (px, py + gap), (px, py + arm))
        pygame.gfxdraw.filled_circle(self.screen, px, py, dot_r, COL_HIGHLIGHT)
        pygame.gfxdraw.aacircle(self.screen, px, py, dot_r, COL_HIGHLIGHT)

        # cancel hint
        hint = _render_text(self._f_track_sm, "Swipe down to cancel", COL_SEP)
        self.screen.blit(hint, ((W - hint.get_width()) // 2, H * 5 // 6))

    def _settings_back_btn_hit(self, pos) -> bool:
        bx, by = W - BTN_RADIUS - BTN_MARGIN, BTN_MARGIN + BTN_RADIUS
        return abs(pos[0] - bx) <= BTN_RADIUS and abs(pos[1] - by) <= BTN_RADIUS

    def _settings_row_y(self, row_index: int) -> int:
        """Screen y of the nth settings row (0-based), accounting for scroll."""
        sep_y = 2 * (BTN_MARGIN + BTN_RADIUS)
        return sep_y + row_index * TRACK_ROW_H - int(self._settings_scroll)

    def _settings_row_hit(self, pos, row_index: int) -> bool:
        sep_y  = 2 * (BTN_MARGIN + BTN_RADIUS)
        clip_h = H - PROGRESS_H - BTN_MARGIN * 2 - sep_y
        ry = self._settings_row_y(row_index)
        return (ry <= pos[1] < ry + TRACK_ROW_H
                and sep_y <= pos[1] < sep_y + clip_h)

    def _settings_item_at(self, pos) -> str | None:
        for i, (key, _) in enumerate(_SETTINGS_ITEMS):
            if key is not None and self._settings_row_hit(pos, i):
                return key
        return None

    def _bt_device_at(self, pos) -> dict | None:
        if not (self.bt and self.bt.available and self._bt_devices):
            return None
        base = len(_SETTINGS_ITEMS) + 1   # +1 for BT section header
        for i, dev in enumerate(self._bt_devices):
            if self._settings_row_hit(pos, base + i):
                return dev
        return None

    def _settings_scan_btn_at(self, pos) -> bool:
        if not (self.bt and self.bt.available):
            return False
        row = len(_SETTINGS_ITEMS) + 1 + len(self._bt_devices)
        return self._settings_row_hit(pos, row)

    def _wifi_network_at(self, pos) -> dict | None:
        if not (self.wifi and self.wifi.available and self._wifi_networks):
            return None
        bt_rows = (2 + len(self._bt_devices)) if (self.bt and self.bt.available) else 0
        base = len(_SETTINGS_ITEMS) + bt_rows + 1   # +1 for wifi header
        for i, net in enumerate(self._wifi_networks):
            if self._settings_row_hit(pos, base + i):
                return net
        return None

    def _settings_clear_cache_btn_at(self, pos) -> bool:
        bt_rows   = (2 + len(self._bt_devices)) if (self.bt and self.bt.available) else 0
        wifi_rows = (1 + len(self._wifi_networks)) if (self.wifi and self.wifi.available) else 0
        row = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 1   # +0 = SYSTEM header
        return self._settings_row_hit(pos, row)

    def _settings_debug_btn_at(self, pos) -> bool:
        bt_rows   = (2 + len(self._bt_devices)) if (self.bt and self.bt.available) else 0
        wifi_rows = (1 + len(self._wifi_networks)) if (self.wifi and self.wifi.available) else 0
        row = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 2
        return self._settings_row_hit(pos, row)

    def _settings_calibrate_btn_at(self, pos) -> bool:
        bt_rows   = (2 + len(self._bt_devices)) if (self.bt and self.bt.available) else 0
        wifi_rows = (1 + len(self._wifi_networks)) if (self.wifi and self.wifi.available) else 0
        row = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 4   # +3 = TOUCH header
        return self._settings_row_hit(pos, row)

    def _settings_reset_cal_btn_at(self, pos) -> bool:
        bt_rows   = (2 + len(self._bt_devices)) if (self.bt and self.bt.available) else 0
        wifi_rows = (1 + len(self._wifi_networks)) if (self.wifi and self.wifi.available) else 0
        row = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 5
        return self._settings_row_hit(pos, row)

    def _settings_restart_btn_at(self, pos) -> bool:
        bt_rows   = (2 + len(self._bt_devices)) if (self.bt and self.bt.available) else 0
        wifi_rows = (1 + len(self._wifi_networks)) if (self.wifi and self.wifi.available) else 0
        row = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 7   # +6 = POWER header
        return self._settings_row_hit(pos, row)

    def _settings_shutdown_btn_at(self, pos) -> bool:
        bt_rows   = (2 + len(self._bt_devices)) if (self.bt and self.bt.available) else 0
        wifi_rows = (1 + len(self._wifi_networks)) if (self.wifi and self.wifi.available) else 0
        row = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 8
        return self._settings_row_hit(pos, row)

    def _scan_device_at(self, pos) -> dict | None:
        y0 = 2 * (BTN_MARGIN + BTN_RADIUS)
        for i, dev in enumerate(self._scan_devices):
            row_top = y0 + i * TRACK_ROW_H
            if row_top <= pos[1] < row_top + TRACK_ROW_H:
                return dev
        return None

    # ── BT context menu ───────────────────────────────────────────────────────

    def _bt_menu_rect(self):
        pw = W * 2 // 3
        ph = 3 * TRACK_ROW_H
        px = (W - pw) // 2
        py = (H - ph) // 2
        return px, py, pw, ph

    def _draw_bt_menu(self):
        dev = self._bt_menu_dev
        if dev is None:
            return
        ov = self._overlay_surf
        ov.fill((0, 0, 0, 160))
        self.screen.blit(ov, (0, 0))

        px, py, pw, ph = self._bt_menu_rect()
        pygame.draw.rect(self.screen, COL_CELL_BG, (px, py, pw, ph), border_radius=8)
        pygame.draw.rect(self.screen, COL_SEP, (px, py, pw, ph), width=1, border_radius=8)

        sy = py
        # row 0: device name header
        name = _render_text(self._f_track_sm, dev["name"], COL_TEXT_ALBUM, pw - BTN_MARGIN * 2)
        self.screen.blit(name, (px + (pw - name.get_width()) // 2,
                                sy + (TRACK_ROW_H - name.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP,
                           (px, sy + TRACK_ROW_H - 1), (px + pw, sy + TRACK_ROW_H - 1))
        sy += TRACK_ROW_H

        # row 1: Connect / Disconnect
        action1 = "Disconnect" if dev["connected"] else "Connect"
        s1 = _render_text(self._f_track, action1, COL_TRACK_NORMAL)
        self.screen.blit(s1, (px + (pw - s1.get_width()) // 2,
                               sy + (TRACK_ROW_H - s1.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP,
                           (px, sy + TRACK_ROW_H - 1), (px + pw, sy + TRACK_ROW_H - 1))
        sy += TRACK_ROW_H

        # row 2: Forget device
        s2 = _render_text(self._f_track, "Forget device", (200, 80, 80))
        self.screen.blit(s2, (px + (pw - s2.get_width()) // 2,
                               sy + (TRACK_ROW_H - s2.get_height()) // 2))

    def _bt_menu_item_at(self, pos) -> str | None:
        if self._bt_menu_dev is None:
            return None
        px, py, pw, ph = self._bt_menu_rect()
        if not (px <= pos[0] < px + pw and py <= pos[1] < py + ph):
            return "dismiss"
        row = (pos[1] - py) // TRACK_ROW_H
        if row == 1: return "connect"
        if row == 2: return "forget"
        return None  # row 0 is header

    def _exec_bt_menu_tap(self, action: str):
        dev = self._bt_menu_dev
        if dev is None:
            return
        addr = dev["address"]

        # All other actions close the menu
        self._bt_menu_dev = None

        if action == "connect":
            self._bt_action_addr = addr
            def _do(a=addr, c=dev["connected"]):
                if c:
                    self.bt.disconnect(a)
                else:
                    self.bt.connect(a)
                self._bt_devices     = self.bt.get_devices()
                self._bt_action_addr = None
                self._dirty          = True
            threading.Thread(target=_do, daemon=True).start()
        elif action == "forget":
            def _do(a=addr):
                self.bt.forget(a)
                self._bt_devices = self.bt.get_devices()
                self._dirty      = True
            threading.Thread(target=_do, daemon=True).start()

    def _wifi_menu_rect(self):
        n_rows = 4 if self._wifi_menu_net and not self._wifi_menu_net["saved"] else 3
        pw = W * 2 // 3
        ph = n_rows * TRACK_ROW_H
        px = (W - pw) // 2
        py = (H - ph) // 2
        return px, py, pw, ph

    def _draw_wifi_menu(self):
        net = self._wifi_menu_net
        if net is None:
            return
        ov = self._overlay_surf
        ov.fill((0, 0, 0, 160))
        self.screen.blit(ov, (0, 0))

        px, py, pw, ph = self._wifi_menu_rect()
        pygame.draw.rect(self.screen, COL_CELL_BG, (px, py, pw, ph), border_radius=8)
        pygame.draw.rect(self.screen, COL_SEP, (px, py, pw, ph), width=1, border_radius=8)

        sy = py
        name = _render_text(self._f_track_sm, net["name"], COL_TEXT_ALBUM, pw - BTN_MARGIN * 2)
        self.screen.blit(name, (px + (pw - name.get_width()) // 2,
                                sy + (TRACK_ROW_H - name.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP,
                           (px, sy + TRACK_ROW_H - 1), (px + pw, sy + TRACK_ROW_H - 1))
        sy += TRACK_ROW_H

        action1 = "Disconnect" if net["connected"] else "Connect"
        s1 = _render_text(self._f_track, action1, COL_TRACK_NORMAL)
        self.screen.blit(s1, (px + (pw - s1.get_width()) // 2,
                               sy + (TRACK_ROW_H - s1.get_height()) // 2))
        pygame.draw.aaline(self.screen, COL_SEP,
                           (px, sy + TRACK_ROW_H - 1), (px + pw, sy + TRACK_ROW_H - 1))
        sy += TRACK_ROW_H

        if net["saved"]:
            s2 = _render_text(self._f_track, "Forget network", (200, 80, 80))
            self.screen.blit(s2, (px + (pw - s2.get_width()) // 2,
                                   sy + (TRACK_ROW_H - s2.get_height()) // 2))
        else:
            # unsaved: offer password entry
            s2 = _render_text(self._f_track, "Enter password", COL_TRACK_NORMAL)
            self.screen.blit(s2, (px + (pw - s2.get_width()) // 2,
                                   sy + (TRACK_ROW_H - s2.get_height()) // 2))
            pygame.draw.aaline(self.screen, COL_SEP,
                               (px, sy + TRACK_ROW_H - 1), (px + pw, sy + TRACK_ROW_H - 1))
            sy += TRACK_ROW_H
            s3 = _render_text(self._f_track, "Forget network", (200, 80, 80))
            self.screen.blit(s3, (px + (pw - s3.get_width()) // 2,
                                   sy + (TRACK_ROW_H - s3.get_height()) // 2))

    def _wifi_menu_item_at(self, pos) -> str | None:
        if self._wifi_menu_net is None:
            return None
        px, py, pw, ph = self._wifi_menu_rect()
        if not (px <= pos[0] < px + pw and py <= pos[1] < py + ph):
            return "dismiss"
        row = (pos[1] - py) // TRACK_ROW_H
        if self._wifi_menu_net["saved"]:
            if row == 1: return "connect"
            if row == 2: return "forget"
        else:
            if row == 1: return "connect"
            if row == 2: return "password"
            if row == 3: return "forget"
        return None  # header row

    def _exec_wifi_menu_tap(self, action: str):
        net = self._wifi_menu_net
        if net is None:
            return
        name = net["name"]
        self._wifi_menu_net = None

        if action == "connect":
            if net["connected"]:
                self._wifi_action_name = name
                def _do(n=name):
                    self.wifi.disconnect(n)
                    self._wifi_networks    = self.wifi.get_networks()
                    self._wifi_action_name = None
                    self._dirty = True
                threading.Thread(target=_do, daemon=True).start()
            elif net["saved"]:
                self._wifi_action_name = name
                def _do(n=name):
                    self.wifi.connect(n)
                    self._wifi_networks    = self.wifi.get_networks()
                    self._wifi_action_name = None
                    self._dirty = True
                threading.Thread(target=_do, daemon=True).start()
            else:
                self._kb_ssid  = name
                self._kb_text  = ""
                self._kb_page  = "alpha"
                self._kb_shift = False
                self._kb_caps  = False
                self._kb_show_pw = False
                self._dirty    = True
        elif action == "password":
            self._kb_ssid    = name
            self._kb_text    = ""
            self._kb_page    = "alpha"
            self._kb_shift   = False
            self._kb_caps    = False
            self._kb_show_pw = False
            self._dirty      = True
        elif action == "forget":
            def _do(n=name):
                import subprocess
                subprocess.run(["nmcli", "connection", "delete", n],
                               capture_output=True, timeout=8)
                self._wifi_networks = self.wifi.get_networks()
                self._dirty = True
            threading.Thread(target=_do, daemon=True).start()

    # ── hit testing ───────────────────────────────────────────────────────────

    def _cell_at(self, pos) -> int | None:
        x, y  = pos
        gt    = 0
        if y < gt:
            return None
        gy    = y - gt + int(self._grid_scroll)
        row   = (gy - GRID_PAD) // _ROW_H
        if row < 0 or (gy - GRID_PAD) % _ROW_H > _CELL_H:
            return None
        col   = (x - GRID_PAD) // (_CELL_W + GRID_PAD)
        if col < 0 or col >= GRID_COLS:
            return None
        idx   = row * GRID_COLS + col
        return idx if idx < len(self._albums) else None

    def _track_at(self, pos) -> int | None:
        x, y = pos
        if y < TRACKLIST_ART_H:
            return None
        row = int((y - TRACKLIST_ART_H + int(self._tl_scroll)) // TRACK_ROW_H)
        return row if 0 <= row < len(self._tracks) else None

    def _close_btn_hit(self, pos) -> bool:
        bx, by = W - BTN_RADIUS - BTN_MARGIN, BTN_MARGIN + BTN_RADIUS
        dx, dy = pos[0] - bx, pos[1] - by
        return dx * dx + dy * dy <= (BTN_RADIUS + 10) ** 2

    def _stop_btn_hit(self, pos) -> bool:
        bx, by = BTN_RADIUS + BTN_MARGIN, BTN_MARGIN + BTN_RADIUS
        dx, dy = pos[0] - bx, pos[1] - by
        return dx * dx + dy * dy <= (BTN_RADIUS + 10) ** 2

    def _gear_btn_hit(self, pos) -> bool:
        gx, gy = W - BTN_RADIUS - BTN_MARGIN - 2 * BTN_RADIUS - BTN_GAP, BTN_MARGIN + BTN_RADIUS
        dx, dy = pos[0] - gx, pos[1] - gy
        return dx * dx + dy * dy <= (BTN_RADIUS + 10) ** 2

    def _speaker_btn_pos(self):
        sx = BTN_RADIUS + BTN_MARGIN
        sy = BTN_MARGIN + BTN_RADIUS
        return sx + 2 * BTN_RADIUS + BTN_GAP, sy

    def _speaker_btn_hit(self, pos) -> bool:
        if not (self.audio and self.audio.available):
            return False
        spx, spy = self._speaker_btn_pos()
        dx, dy = pos[0] - spx, pos[1] - spy
        return dx * dx + dy * dy <= (BTN_RADIUS + 10) ** 2

    def _audio_popup_sink_at(self, pos) -> dict | None:
        if not self._audio_popup_open or not self._audio_sinks:
            return None
        spx, spy = self._speaker_btn_pos()
        row_h = TRACK_ROW_H
        pw    = min(W - BTN_MARGIN * 2, max(260, W * 2 // 3))
        ph    = len(self._audio_sinks) * row_h
        px    = max(BTN_MARGIN, spx - pw // 2)
        py    = spy + BTN_RADIUS + 8
        if py + ph > H - PROGRESS_H:
            py = spy - BTN_RADIUS - 8 - ph
        if not (px <= pos[0] < px + pw and py <= pos[1] < py + ph):
            return None
        i = (pos[1] - py) // row_h
        return self._audio_sinks[i] if 0 <= i < len(self._audio_sinks) else None

    def _ctrl_zone(self, pos) -> str | None:
        x, y = pos
        # Controls sit in the middle third of the screen vertically
        if y < H // 4 or y > 3 * H // 4:
            return None
        if x < W // 3:     return "prev"
        if x > 2 * W // 3: return "next"
        return "play"

    # ── tap execution ─────────────────────────────────────────────────────────

    def _exec_single_tap(self, pos):
        if pos is None:
            return

        # On-screen keyboard intercepts all taps
        if self._kb_ssid is not None:
            if self._kb_show_pw_rect().collidepoint(pos):
                self._kb_show_pw = not self._kb_show_pw
                self._dirty = True
                return
            key = self._kb_key_at(pos)
            if key:
                self._exec_kb_tap(key)
            else:
                _, _, _, kb_top = self._kb_dims()
                if pos[1] < kb_top:   # tap above panel → dismiss
                    self._kb_ssid = None
                    self._kb_text = ""
                    self._dirty   = True
            return

        view = self._view

        if view == View.GRID:
            if self._peeking and pos[1] >= H - TRACKLIST_ART_H:
                # tap the album strip at bottom → return to full album view
                self._unpeek()
                return
            idx = self._cell_at(pos)
            if idx is not None:
                    self._go_album(idx)

        elif view == View.SETTINGS:
            if self._bt_menu_dev is not None:
                action = self._bt_menu_item_at(pos)
                if action and action != "dismiss":
                    self._exec_bt_menu_tap(action)
                else:
                    self._bt_menu_dev = None
                return
            if self._wifi_menu_net is not None:
                action = self._wifi_menu_item_at(pos)
                if action and action != "dismiss":
                    self._exec_wifi_menu_tap(action)
                else:
                    self._wifi_menu_net = None
                return
            if self._settings_back_btn_hit(pos):
                self._close_settings()
                return
            key = self._settings_item_at(pos)
            if key:
                settings.toggle(key)
                return
            dev = self._bt_device_at(pos)
            if dev and self._bt_action_addr is None:
                addr = dev["address"]
                was_connected = dev["connected"]
                self._bt_action_addr = addr
                def _do(a=addr, c=was_connected):
                    if c:
                        self.bt.disconnect(a)
                    else:
                        self.bt.connect(a)
                    self._bt_devices     = self.bt.get_devices()
                    self._bt_action_addr = None
                    self._dirty          = True
                threading.Thread(target=_do, daemon=True).start()
                return
            net = self._wifi_network_at(pos)
            if net and self._wifi_action_name is None:
                name = net["name"]
                if net["connected"]:
                    self._wifi_action_name = name
                    def _do_wifi(n=name):
                        self.wifi.disconnect(n)
                        self._wifi_networks    = self.wifi.get_networks()
                        self._wifi_action_name = None
                        self._dirty = True
                    threading.Thread(target=_do_wifi, daemon=True).start()
                elif net["saved"]:
                    self._wifi_action_name = name
                    def _do_wifi(n=name):
                        self.wifi.connect(n)
                        self._wifi_networks    = self.wifi.get_networks()
                        self._wifi_action_name = None
                        self._dirty = True
                    threading.Thread(target=_do_wifi, daemon=True).start()
                else:
                    # unsaved — open password keyboard
                    self._kb_ssid  = name
                    self._kb_text  = ""
                    self._kb_page  = "alpha"
                    self._kb_shift = False
                    self._dirty    = True
                return
            if self._settings_scan_btn_at(pos):
                self._open_scan()
                return
            if self._settings_clear_cache_btn_at(pos):
                self._clear_thumb_cache()
                return
            if self._settings_debug_btn_at(pos):
                settings.toggle("debug")
                self._dirty = True
                return
            if self._settings_calibrate_btn_at(pos):
                self._open_calibrate()
                return
            if self._settings_reset_cal_btn_at(pos):
                settings.set("cal_sx", 1.0); settings.set("cal_ox", 0.0)
                settings.set("cal_sy", 1.0); settings.set("cal_oy", 0.0)
                self._dirty = True
                return
            if self._settings_restart_btn_at(pos):
                import subprocess
                subprocess.Popen(["sudo", "reboot"])
                return
            if self._settings_shutdown_btn_at(pos):
                import subprocess
                subprocess.Popen(["sudo", "shutdown", "-h", "now"])
            return

        elif view == View.CALIBRATE:
            # raw position before calibration — read directly from _t_start_pos
            # which was set via _epos; we need the pre-calibration coords instead.
            # We stored them in _cal_raw_pending during FINGERDOWN/MOUSEDOWN.
            raw = getattr(self, "_cal_raw_pending", None)
            if raw is not None:
                self._cal_raw.append(raw)
                self._cal_step += 1
                if self._cal_step >= len(self._cal_points):
                    self._finish_calibrate()
                self._dirty = True
            return

        elif view == View.SCAN:
            if self._settings_back_btn_hit(pos):
                self._close_scan()
                return
            dev = self._scan_device_at(pos)
            if dev and self._scan_action_addr is None:
                addr = dev["address"]
                self._scan_action_addr = addr
                def _pair(a=addr):
                    self.bt.pair(a)
                    self.bt.trust(a)
                    self.bt.connect(a)
                    self._bt_devices       = self.bt.get_devices()
                    self._scan_action_addr = None
                    self.bt.stop_scan()
                    self._view  = View.SETTINGS
                    self._dirty = True
                threading.Thread(target=_pair, daemon=True).start()
            return

        elif view == View.ALBUM:
            if int(self._ctrl_a) > 10:
                # controls are showing — handle button taps

                # audio popup sink selection (check before other buttons)
                sink = self._audio_popup_sink_at(pos)
                if sink is not None:
                    sid = sink["id"]
                    self._audio_busy_id = sid
                    self._dirty = True
                    def _switch(s=sid):
                        self.audio.set_sink(s)
                        self._audio_sinks   = self.audio.get_sinks()
                        self._audio_busy_id = None
                        self._audio_popup_open = False
                        self._dirty = True
                    threading.Thread(target=_switch, daemon=True).start()
                    return

                # dismiss popup on tap outside it
                if self._audio_popup_open:
                    self._audio_popup_open = False
                    self._dirty = True
                    return

                if self._close_btn_hit(pos):
                    self._hide_controls()
                    return
                if self._stop_btn_hit(pos):
                    self._go_grid()
                    return
                if self._speaker_btn_hit(pos):
                    self._audio_popup_open = not self._audio_popup_open
                    if self._audio_popup_open:
                        self._audio_sinks = self.audio.get_sinks()
                    self._dirty = True
                    return
                if self._gear_btn_hit(pos):
                    self._open_settings()
                    return
                zone = self._ctrl_zone(pos)
                if zone == "prev":
                    self.player.previous(); self._reset_elapsed()
                elif zone == "next":
                    self.player.next(); self._reset_elapsed()
                elif zone == "play":
                    self.player.toggle()
                else:
                    self._hide_controls()
            else:
                self._show_controls()

        elif view == View.TRACKLIST:
            if pos[1] < TRACKLIST_ART_H:
                self._close_tracklist()
            else:
                idx = self._track_at(pos)
                if idx is not None and idx < len(self._tracks):
                    track = self._tracks[idx]
                    self.player.play_track_in_queue(idx, track)
                    self._reset_elapsed()
                    self._song = self.player.get_current_song()

    def _exec_double_tap(self):
        if self._view in (View.ALBUM, View.TRACKLIST):
            playing = self._status.get("state") == "play"
            self.player.toggle()
            self._show_flash("pause" if playing else "play")

    def _show_controls(self):
        self._ctrl_a = 255.0; self._ctrl_a_t = 255.0
        self._ctrl_shown_ms = pygame.time.get_ticks()
    def _hide_controls(self):
        self._ctrl_a = 0.0; self._ctrl_a_t = 0.0
        self._audio_popup_open = False

    def _reset_elapsed(self):
        self._elapsed_base   = 0.0
        self._elapsed_base_t = time.monotonic()

    def _in_scrub_zone(self, pos) -> bool:
        bar_y = self._progress_bar_y()
        return bar_y - SCRUB_LEEWAY <= pos[1] <= bar_y + SCRUB_LEEWAY

    def _is_lyrics_drag_target(self, pos) -> bool:
        """True when a touch in the lyrics zone should scroll lyrics manually."""
        if self._view != View.ALBUM:
            return False
        if self._ctrl_a < 10:
            return False
        parsed = self._lyrics_parsed
        if not parsed or parsed[1] is not None:
            return False   # timestamps present — auto-scroll only
        y_start = H // 2 + CTRL_BAR_H // 2 + CTRL_TEXT_GAP * 2
        return pos[1] >= y_start

    def _is_panel_touch(self, pos) -> bool:
        """True if pos lands on the currently visible part of the album panel."""
        if self._view in (View.SETTINGS, View.SCAN, View.CALIBRATE):
            return False
        ay = self._album_y
        if ay >= H - 2:
            return False          # panel fully off-screen (GRID without peek)
        if self._peeking:
            return pos[1] >= H - TRACKLIST_ART_H   # only bottom strip visible
        if ay < -2:
            return pos[1] < TRACKLIST_ART_H         # only top strip visible
        return True               # ALBUM: full panel

    def _snap_panel(self, total_y: float):
        """Snap _album_y_t to the nearest snap point, biased by drag direction."""
        snaps = [
            (float(_TL_ALBUM_Y),          View.TRACKLIST, False),
            (0.0,                          View.ALBUM,     False),
            (float(H - TRACKLIST_ART_H),   View.GRID,      True),
        ]
        # Dragging down from peek → go to grid (same as stop button)
        started_at_peek = self._panel_drag_base_y >= H - TRACKLIST_ART_H - 2
        if started_at_peek and total_y >= SWIPE_V_MIN:
            self._go_grid()
            return

        ay = self._album_y
        if abs(total_y) >= SWIPE_V_MIN:
            # Prefer a snap in the direction of movement
            candidates = [s for s in snaps
                          if (total_y < 0 and s[0] <= ay) or (total_y > 0 and s[0] >= ay)]
            target = min(candidates or snaps, key=lambda s: abs(s[0] - ay))
        else:
            target = min(snaps, key=lambda s: abs(s[0] - ay))
        snap_y, new_view, new_peeking = target
        self._album_y_t = snap_y
        self._view      = new_view
        self._peeking   = new_peeking
        if new_view != View.ALBUM:
            self._ctrl_a   = 0.0
            self._ctrl_a_t = 0.0

    # ── on-screen keyboard ────────────────────────────────────────────────────

    def _kb_dims(self):
        """Return (key_h, inp_h, panel_h, kb_top) for the current screen."""
        key_h   = max(44, H // 12)
        inp_h   = key_h + 8
        panel_h = inp_h + 5 * key_h + 8
        kb_top  = H - panel_h
        return key_h, inp_h, panel_h, kb_top

    def _kb_show_pw_rect(self):
        """Return the pygame.Rect of the show/hide checkbox."""
        key_h, inp_h, _, kb_top = self._kb_dims()
        mid  = kb_top + inp_h // 2
        cb_s = max(14, key_h // 4)
        # extend hit area leftward to include the "Show" label
        lbl_s = _render_text(self._f_track_sm, "Show", COL_TEXT_ALBUM)
        x = W - BTN_MARGIN - cb_s - lbl_s.get_width() - 6
        return pygame.Rect(x, mid + 2, W - BTN_MARGIN - x, cb_s)

    def _kb_row_rects(self, row, y, key_h, pad=6, gap=5):
        """Return list of (pygame.Rect, label) for one keyboard row."""
        total_w = sum(w for _, w in row)
        avail   = W - 2 * pad - (len(row) - 1) * gap
        unit    = avail / total_w
        rects, x = [], pad
        for i, (label, weight) in enumerate(row):
            kw = W - pad - x if i == len(row) - 1 else int(round(unit * weight))
            rects.append((pygame.Rect(x, y, kw, key_h), label))
            x += kw + gap
        return rects

    def _draw_keyboard(self):
        if self._kb_ssid is None:
            return
        key_h, inp_h, panel_h, kb_top = self._kb_dims()

        # dim overlay above panel
        ov = pygame.Surface((W, kb_top), pygame.SRCALPHA)
        ov.fill((0, 0, 0, 140))
        self.screen.blit(ov, (0, 0))

        # panel background
        pygame.draw.rect(self.screen, (18, 18, 24), (0, kb_top, W, panel_h))
        pygame.draw.line(self.screen, COL_SEP, (0, kb_top), (W, kb_top))

        # SSID label (or error message) above password field
        mid = kb_top + inp_h // 2
        if self._kb_error:
            top_s = _render_text(self._f_track_sm, "Wrong password — try again", (220, 80, 80))
        else:
            top_s = _render_text(self._f_track_sm, self._kb_ssid, COL_TEXT_ALBUM)
        self.screen.blit(top_s, (BTN_MARGIN, mid - top_s.get_height() - 2))

        # show/hide checkbox — right side of input row
        cb_s   = max(14, key_h // 4)
        cb_x   = W - BTN_MARGIN - cb_s
        cb_y   = mid + 2
        cb_col = COL_HIGHLIGHT if self._kb_show_pw else COL_SEP
        pygame.draw.rect(self.screen, cb_col, (cb_x, cb_y, cb_s, cb_s), border_radius=3)
        if self._kb_show_pw:
            mx, my = cb_x + cb_s // 4, cb_y + cb_s // 2
            pygame.draw.line(self.screen, COL_BG, (mx, my), (cb_x + cb_s * 2 // 5, cb_y + cb_s * 3 // 4), 2)
            pygame.draw.line(self.screen, COL_BG, (cb_x + cb_s * 2 // 5, cb_y + cb_s * 3 // 4),
                             (cb_x + cb_s - cb_s // 4, cb_y + cb_s // 5), 2)
        else:
            pygame.draw.rect(self.screen, (18, 18, 24), (cb_x + 2, cb_y + 2, cb_s - 4, cb_s - 4), border_radius=2)
        lbl_s  = _render_text(self._f_track_sm, "Show", COL_TEXT_ALBUM)
        self.screen.blit(lbl_s, (cb_x - lbl_s.get_width() - 6, cb_y + (cb_s - lbl_s.get_height()) // 2))

        cursor = "|" if (pygame.time.get_ticks() // 500) % 2 == 0 else ""
        pw_display = self._kb_text if self._kb_show_pw else "●" * len(self._kb_text)
        pw_s   = _render_text(self._f_track, (pw_display or " ") + cursor, COL_TEXT_TITLE)
        self.screen.blit(pw_s, (BTN_MARGIN, mid + 2))

        # key rows
        page = _KB_ROWS.get(self._kb_page, _KB_ROWS["alpha"])
        y = kb_top + inp_h + 2
        for row in page:
            for rect, label in self._kb_row_rects(row, y, key_h - 5):
                is_special = label in _KB_SPECIAL
                caps_lit   = label == "SHIFT" and self._kb_caps
                shift_lit  = label == "SHIFT" and self._kb_shift and not self._kb_caps
                if caps_lit:
                    bg = (240, 200, 80)   # brighter gold for caps lock
                elif shift_lit:
                    bg = COL_HIGHLIGHT
                elif is_special:
                    bg = (32, 32, 44)
                else:
                    bg = COL_CELL_BG
                pygame.draw.rect(self.screen, bg, rect, border_radius=6)
                if label == "SHIFT":
                    if self._kb_caps:
                        face = "⇪"
                    elif self._kb_page == "sym":
                        face = "[{"   # preview of sym2 layer
                    elif self._kb_page == "sym2":
                        face = "?!"   # preview of sym layer
                    else:
                        face = "⇧"
                else:
                    face = _KB_FACE.get(label, label.upper() if self._kb_shift else label.lower())
                fc = COL_BG if (shift_lit or caps_lit) else COL_TRACK_NORMAL
                ts = _render_text(self._f_track_sm, face, fc)
                self.screen.blit(ts, (rect.centerx - ts.get_width() // 2,
                                      rect.centery - ts.get_height() // 2))
            y += key_h

    def _kb_key_at(self, pos) -> str | None:
        if self._kb_ssid is None:
            return None
        key_h, inp_h, _, kb_top = self._kb_dims()
        y    = kb_top + inp_h + 2
        page = _KB_ROWS.get(self._kb_page, _KB_ROWS["alpha"])
        for row in page:
            for rect, label in self._kb_row_rects(row, y, key_h - 5):
                if rect.collidepoint(pos):
                    return label
            y += key_h
        return None

    def _exec_kb_tap(self, key: str):
        if key == "BACK":
            self._kb_text  = self._kb_text[:-1]
            self._kb_error = False
        elif key == "OK":
            ssid, pw = self._kb_ssid, self._kb_text
            self._kb_ssid  = None
            self._kb_text  = ""
            self._kb_error = False
            if ssid and self.wifi:
                self._wifi_action_name = ssid
                def _connect(s=ssid, p=pw):
                    ok = self.wifi.connect_new(s, p)
                    self._wifi_networks    = self.wifi.get_networks()
                    self._wifi_action_name = None
                    if not ok:
                        # reopen keyboard so user can retry; flag the error
                        self._kb_ssid  = s
                        self._kb_text  = ""
                        self._kb_error = True
                    self._dirty = True
                threading.Thread(target=_connect, daemon=True).start()
        elif key == "SHIFT":
            if self._kb_page == "sym":
                self._kb_page  = "sym2"
                self._kb_shift = True
            elif self._kb_page == "sym2":
                self._kb_page  = "sym"
                self._kb_shift = False
            else:
                now = pygame.time.get_ticks()
                if self._kb_caps:
                    # third tap → everything off
                    self._kb_shift = False
                    self._kb_caps  = False
                elif self._kb_shift and now - self._kb_shift_tap_ms < DOUBLE_TAP_MS:
                    # double-tap → caps lock
                    self._kb_caps = True
                else:
                    # single tap → one-shot shift
                    self._kb_shift = not self._kb_shift
                if self._kb_shift:
                    self._kb_shift_tap_ms = now
        elif key == "SYM":
            self._kb_page  = "sym"
            self._kb_shift = False
        elif key == "ABC":
            self._kb_page  = "alpha"
            self._kb_shift = False
        elif key == "SPACE":
            self._kb_text  += " "
            self._kb_error  = False
            if self._kb_page == "alpha":
                self._kb_shift = False
        elif len(key) == 1:
            self._kb_error = False
            if self._kb_page == "alpha":
                self._kb_text += key.upper() if self._kb_shift else key.lower()
                if not self._kb_caps:
                    self._kb_shift = False   # one-shot; caps lock keeps it on
            else:
                self._kb_text += key      # sym chars are already the right symbol
        self._dirty = True

    # ── event handling ────────────────────────────────────────────────────────

    def _handle_wheel(self, px: float):
        """Move scroll by px immediately, no momentum."""
        self._dirty = True
        if self._view == View.GRID:
            self._grid_scroll = max(0.0, self._grid_scroll + px)
            self._grid_vel = 0.0
        elif self._view == View.TRACKLIST:
            self._tl_scroll = max(0.0, self._tl_scroll + px)
            self._tl_vel = 0.0
        elif self._view == View.SETTINGS:
            self._settings_scroll = max(0.0, self._settings_scroll + px)
            self._settings_vel = 0.0

    def handle_event(self, event):
        self._dirty = True
        self._last_input_ms = pygame.time.get_ticks()

        # When on-screen keyboard is active, bypass all drag/swipe logic
        if self._kb_ssid is not None:
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_BACKSPACE:
                    self._kb_text = self._kb_text[:-1]
                elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    self._exec_kb_tap("OK")
                elif event.key == pygame.K_ESCAPE:
                    self._kb_ssid = None
                    self._kb_text = ""
                elif event.unicode and event.unicode.isprintable():
                    self._kb_text += event.unicode
            elif event.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
                self._t_start_pos = self._epos(event)
                self._t_start_ms  = pygame.time.get_ticks()
            elif event.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP):
                if self._t_start_pos is not None:
                    pos = self._epos(event)
                    dx  = pos[0] - self._t_start_pos[0]
                    dy  = pos[1] - self._t_start_pos[1]
                    ms  = pygame.time.get_ticks() - self._t_start_ms
                    if abs(dx) < TAP_MAX_MOVE and abs(dy) < TAP_MAX_MOVE and ms < TAP_MAX_MS:
                        self._exec_single_tap(self._t_start_pos)
                    self._t_start_pos = None
            return

        if event.type == pygame.MOUSEWHEEL:
            dy = getattr(event, "precise_y", float(event.y))
            self._handle_wheel(-dy * TRACK_ROW_H * 0.2)
            return
        if event.type == pygame.MOUSEBUTTONDOWN and event.button in (4, 5):
            direction = 1 if event.button == 5 else -1
            self._handle_wheel(direction * TRACK_ROW_H * 0.2)
            return

        if event.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
            # capture uncalibrated raw coords for touch calibration screen
            if event.type in (pygame.FINGERDOWN, pygame.FINGERUP, pygame.FINGERMOTION):
                self._cal_raw_pending = (event.x * W, event.y * H)
            else:
                self._cal_raw_pending = (float(event.pos[0]), float(event.pos[1]))
            pos = self._epos(event)
            self._t_start_pos  = pos
            self._t_prev_pos   = pos
            self._t_start_ms   = pygame.time.get_ticks()
            self._t_dragging   = False
            self._grid_vel     = 0.0   # grab stops any ongoing momentum
            self._tl_vel       = 0.0
            # scrub bar takes top priority
            if self._in_scrub_zone(pos):
                self._scrub_active = True
                self._scrub_frac   = max(0.0, min(1.0, pos[0] / W))
                self._panel_touch  = False
                self._lyrics_drag  = False
            # lyrics manual scroll takes priority over panel drag
            elif self._is_lyrics_drag_target(pos):
                self._scrub_active = False
                self._lyrics_drag  = True
                self._panel_touch  = False
            else:
                self._scrub_active = False
                self._lyrics_drag  = False
                self._panel_touch  = self._is_panel_touch(pos)
                if self._panel_touch:
                    self._panel_drag_base_y = self._album_y
                    self._panel_drag_start  = pos[1]

        elif event.type in (pygame.MOUSEMOTION, pygame.FINGERMOTION):
            if self._t_start_pos is None:
                return
            pos  = self._epos(event)
            prev = self._t_prev_pos or pos
            dy   = pos[1] - prev[1]

            total_dx = abs(pos[0] - self._t_start_pos[0])
            total_dy = abs(pos[1] - self._t_start_pos[1])
            if total_dx > DRAG_THRESH or total_dy > DRAG_THRESH:
                self._t_dragging = True

            if self._t_dragging:
                if self._scrub_active:
                    self._scrub_frac = max(0.0, min(1.0, pos[0] / W))
                elif self._lyrics_drag:
                    self._lyrics_scroll -= dy / (self._f_lyrics.get_linesize()
                                                  + max(2, self._f_lyrics.get_linesize() // 6))
                elif self._panel_touch and total_dy >= total_dx:
                    new_y = self._panel_drag_base_y + (pos[1] - self._panel_drag_start)
                    new_y = max(float(_TL_ALBUM_Y), min(float(H - TRACKLIST_ART_H), new_y))
                    self._album_y   = new_y
                    self._album_y_t = new_y
                    # Update view/peeking continuously so grid/tracklist draw correctly
                    if new_y < 0:
                        self._view    = View.TRACKLIST
                        self._peeking = False
                    elif new_y > 0:
                        self._view    = View.GRID
                        self._peeking = True
                    else:
                        self._view    = View.ALBUM
                        self._peeking = False
                elif self._view == View.GRID:
                    peek_top = H - TRACKLIST_ART_H
                    in_peek_strip = self._peeking and (self._t_start_pos or pos)[1] >= peek_top
                    if not in_peek_strip:
                        self._grid_scroll = max(0.0, self._grid_scroll - dy)
                        self._grid_vel    = -dy * 60   # px/s estimate
                        self._tl_vel      = 0.0
                elif self._view == View.TRACKLIST and pos[1] >= TRACKLIST_ART_H:
                    self._tl_scroll = max(0.0, self._tl_scroll - dy)
                    self._tl_vel    = -dy * 60
                    self._grid_vel  = 0.0
                elif self._view == View.SETTINGS:
                    self._settings_scroll = max(0.0, self._settings_scroll - dy)
                    self._settings_vel    = -dy * 60
                    self._grid_vel        = 0.0

            self._t_prev_pos = pos

        elif event.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP):
            if self._t_start_pos is None:
                return
            pos     = self._epos(event)
            now_ms  = pygame.time.get_ticks()
            total_x = pos[0] - self._t_start_pos[0]
            total_y = pos[1] - self._t_start_pos[1]
            held_ms = now_ms - self._t_start_ms

            if self._scrub_active:
                frac = max(0.0, min(1.0, pos[0] / W))
                self._scrub_frac   = frac
                self._scrub_active = False
                threading.Thread(target=lambda f=frac: self.player.seek(f),
                                 daemon=True).start()
                # optimistically snap elapsed so the bar doesn't jump back
                time_str = self._status.get("time", "")
                parts    = time_str.split(":") if time_str else []
                dur      = float(parts[1]) if len(parts) >= 2 else 0.0
                self._elapsed_base   = frac * dur
                self._elapsed_base_t = time.monotonic()
                self._t_start_pos    = None
                self._t_prev_pos     = None
                self._t_dragging     = False
                self._t_long_pressed = False
                self._panel_touch    = False
                self._lyrics_drag    = False
                return

            swipe_h = abs(total_x) >= SWIPE_H_MIN and abs(total_x) > abs(total_y)

            # horizontal swipe on album art bypasses panel snap
            if self._panel_touch and self._t_dragging and not (swipe_h and self._view == View.ALBUM):
                self._snap_panel(total_y)
            else:
                is_tap  = (not self._t_dragging
                            and abs(total_x) < TAP_MAX_MOVE
                            and abs(total_y) < TAP_MAX_MOVE
                            and held_ms      < TAP_MAX_MS)

                if self._view == View.CALIBRATE:
                    if total_y >= SWIPE_V_MIN:
                        self._view  = View.SETTINGS
                        self._dirty = True
                    elif is_tap:
                        self._exec_single_tap(self._t_start_pos)

                elif swipe_h and self._view == View.GRID and total_x > 0:
                    self._open_settings()

                elif swipe_h and self._view == View.SETTINGS and total_x < 0:
                    self._close_settings()

                elif swipe_h and self._view == View.ALBUM:
                    if total_x < 0:
                        self.player.next(); self._reset_elapsed()
                        self._show_flash("next")
                    else:
                        self.player.previous(); self._reset_elapsed()
                        self._show_flash("prev")

                elif is_tap:
                    if self._view not in (View.ALBUM, View.CALIBRATE):
                        self._exec_single_tap(self._t_start_pos)
                    elif now_ms - self._last_tap_ms < DOUBLE_TAP_MS:
                        self._pending_tap = False
                        self._last_tap_ms = 0
                        self._exec_double_tap()
                    else:
                        self._last_tap_ms     = now_ms
                        self._pending_tap     = True
                        self._pending_tap_ms  = now_ms
                        self._pending_tap_pos = self._t_start_pos

            self._t_start_pos    = None
            self._t_prev_pos     = None
            self._t_dragging     = False
            self._t_long_pressed = False
            self._panel_touch    = False
            self._lyrics_drag    = False

    def _epos(self, event) -> tuple[int, int]:
        if event.type in (pygame.FINGERDOWN, pygame.FINGERUP, pygame.FINGERMOTION):
            rx, ry = event.x * W, event.y * H
        else:
            rx, ry = float(event.pos[0]), float(event.pos[1])
        sx = settings.get("cal_sx"); ox = settings.get("cal_ox")
        sy = settings.get("cal_sy"); oy = settings.get("cal_oy")
        return (int(rx * sx + ox), int(ry * sy + oy))


AlbumDisplay = App
