"""
Album2 — main UI.

View state machine
──────────────────
  GRID      album grid; album art strip at bottom when peeking (_peeking=True)
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
  vertical drag              → scroll grid
"""

from __future__ import annotations
import bisect
import collections
import concurrent.futures
import hashlib
import json
import logging
import math
import os
import re
import pygame.gfxdraw
import colorsys
import threading
import time
from enum import IntEnum

import settings
from artwork import ArtworkFetcher

import pygame
from PIL import Image, ImageFilter

from config import (
    SCREEN_WIDTH, SCREEN_HEIGHT, DISPLAY_WIDTH, DISPLAY_HEIGHT,
    GRID_COLS, GRID_PAD, GRID_TEXT_H,
    TRACKLIST_ART_H, TRACK_ROW_H, CTRL_BAR_H, PROGRESS_H, SCRUB_LEEWAY,
    ANIM_SPEED, CTRL_FADE_SPEED,
    SWIPE_V_MIN, SWIPE_H_MIN, TAP_MAX_MOVE, TAP_MAX_MS, DOUBLE_TAP_MS, DRAG_THRESH,
    VOLUME_BADGE_MS, CTRL_TIMEOUT_MS, SCROLL_FRICTION, FPS,
    BTN_MARGIN, BTN_RADIUS, BTN_GAP, CTRL_ICON_SM, CTRL_ICON_LG, CTRL_TEXT_GAP,
    TOGGLE_W, TOGGLE_H, TRACK_PAD, VOL_BADGE_PAD,
    LONG_PRESS_MS,
    COL_BG, COL_GRID_BG, COL_CELL_BG, COL_TL_BG, COL_SEP,
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

_LYRICS_CACHE_DIR  = os.path.expanduser("~/.cache/album2/lyrics")
_LYRICS_INDEX_PATH = os.path.expanduser("~/.cache/album2/lyrics_index.json")

_LRC_TS_RE = re.compile(r'^\[(\d+):(\d+(?:\.\d+)?)\]')   # capture mm:ss.xx
_LRC_META_RE = re.compile(r'^\[\w+:[^\]]*\]')             # metadata like [ti:...]

_ART_DOTS_HOLD_MS = 2000   # how long the page dots stay visible after the last slide

_ALBUM_MENU_ITEMS = [
    ("dl_art",       "Download album art"),
    ("clear_art",    "Clear album art"),
    ("dl_lyrics",    "Download lyrics"),
    ("clear_lyrics", "Clear lyrics"),
]
_REL_ROW_H = int(TRACK_ROW_H * 1.6)   # release-picker rows carry two lines of text

W, H = SCREEN_WIDTH, SCREEN_HEIGHT

_THUMB_CACHE_DIR = (THUMB_CACHE_DIR
                    or os.path.join(os.path.expanduser("~"), ".cache", "album2", "thumbs"))
os.makedirs(_THUMB_CACHE_DIR, exist_ok=True)

# Icon colour for buttons drawn on top of COL_HIGHLIGHT
_BTN_ICON_COL = (0, 0, 0) if sum(COL_HIGHLIGHT[:3]) / 3 > 127.5 else (255, 255, 255)

# album_y target when tracklist is open: art shifts up so only top strip shows
_TL_ALBUM_Y = TRACKLIST_ART_H - H   # e.g. 224 - 720 = -496

# grid geometry
_CELL_W      = (W - GRID_PAD * (GRID_COLS + 1)) // GRID_COLS
_CELL_H      = _CELL_W + GRID_TEXT_H
_ROW_H       = _CELL_H + GRID_PAD

# carousel geometry
_CAR_SIZE         = 350    # all albums same height (px)
_CAR_COMP         = 0.55   # min width ratio for fully-rotated side album (more front-facing)
_CAR_CY           = 250    # y-centre of center album
_CAR_PX_PER_ALBUM = 220    # px of horizontal drag = 1 album step
_CAR_REFL_H       = 90     # reflection strip height below each album
_CAR_CAM_D        = 1500   # virtual camera distance (px) — controls perspective depth
_CAR_PERSP_N      = 32     # vertical strips for trapezoidal perspective simulation
_CAR_SIDE_SCALE   = 0.84   # overall height scale for side albums (tall edge < center SIZE)
# x-centre offsets from W//2 at integer distances 0, 1, 2.
_CAR_AX           = (0, 270, 355)
_CAR_PEEK_SHIFT   = 70     # px carousel + labels shift up when peeking from album panel


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
_SURF_CACHE_MAX = 128

def _lum(r: float, g: float, b: float) -> float:
    """Perceived luminance, 0–1."""
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _on_bg(bg: tuple, strong=True) -> tuple:
    """Return a foreground colour with good contrast against bg.
    strong=True → title weight; False → dimmed weight (track numbers)."""
    l = _lum(*bg[:3])
    if strong:
        return (240, 240, 240) if l < 0.55 else (20, 20, 20)
    else:
        return (195, 195, 195) if l < 0.55 else (80, 80, 80)


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
    CAROUSEL  = 6


_SETTINGS_ITEMS = [
    (None,          "PLAYBACK"),
    ("autoplay",    "Autoplay when opening album"),
    ("lyrics",      "Show lyrics"),
    (None,          "LIBRARY"),
    ("library",     "Library"),
    ("album_sort",  "Sort by"),
    (None,          "SPOTIFY"),
    ("spotify_bitrate", "Bitrate"),
    ("lyrics_cache_all", "Cache all lyrics"),
    (None,          "ARTWORK"),
    ("art_cache_local",   "Fetch artwork (local)"),
    ("art_cache_spotify", "Fetch artwork (Spotify)"),
    (None,          "GRID"),
    ("grid_labels", "Show album & artist names"),
    ("carousel",    "Carousel view"),
    (None,          "PERFORMANCE"),
    ("idle_fps",    "Reduce FPS when idle"),
    ("skip_draw",   "Skip redraw when nothing changed"),
    (None,          "CAROUSEL"),
    ("car_reflections", "Show reflections"),
    ("car_cache",   "Cache pre-rendered album surfaces"),
]

# Keys whose values are string options rather than booleans.
# Tapping cycles to the next option; draw code renders a pill instead of a toggle.
_SETTINGS_SELECTORS: dict[str, tuple[str, ...]] = {
    "library":        ("Local", "Spotify"),
    "album_sort":     ("Artist A→Z", "Artist Z→A", "Year ↑", "Year ↓", "Album A→Z", "Album Z→A"),
    "spotify_bitrate": ("96", "160", "320"),
}

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
        self._f_track    = _font(FONT_SZ_TRACK)
        self._f_track_sm = _font(FONT_SZ_TRACK_SM)
        self._f_lyrics   = _font(FONT_SZ_LYRICS)

        # reusable surfaces
        self._overlay_surf    = pygame.Surface((W, H), pygame.SRCALPHA)
        _fs = W // 2   # 2× supersampling scratch surface for flash icon (W//4 * 2)
        self._flash_icon_surf = pygame.Surface((_fs, _fs), pygame.SRCALPHA)

        # animation state
        self._view    = View.CAROUSEL if settings.get("carousel") else View.GRID
        self._album_y = float(H)   # current (lerped)
        self._album_y_t = float(H) # target
        self._ctrl_a  = 0.0        # controls overlay alpha (current)
        self._ctrl_a_t = 0.0       # target

        # album art
        self._art:           pygame.Surface | None = None
        self._art_uri:       str  = ""   # current playing track URI (dedup)
        self._art_album_uri: str  = ""   # canonical first-track URI used as art key
        self._art_loading:   bool = False
        # LRU in-memory cache: uri → Surface (or None).  Keeps last 8 arts so
        # switching back to a previous album is instant.
        self._art_mem: collections.OrderedDict[str, pygame.Surface | None] = \
            collections.OrderedDict()

        # ── extended artwork carousel (front + back + booklet) ──
        # Image 0 is always the front (self._art, from the player's embedded
        # art); images 1..N are back/booklet pages fetched by ArtworkFetcher.
        self._artwork = ArtworkFetcher()
        self._art_paths:   list[str] = []     # disk paths for images 1..N
        self._art_count:   int   = 1          # total carousel images (>=1)
        self._art_pos:     float = 0.0        # current (lerped) carousel position
        self._art_pos_t:   float = 0.0        # target position (settled index)
        self._art_idx:     int   = 0          # settled index (for prep window)
        self._art_drag:    bool  = False      # horizontal art drag in progress
        self._art_drag_base: float = 0.0      # _art_pos at drag start
        self._art_dots_a:      float = 0.0    # page-dots opacity (0-255), autohides
        self._art_dots_a_t:    float = 0.0    # target opacity
        self._art_dots_seen_ms: int  = 0      # last time the carousel was moving
        self._art_fetching: set[str] = set()  # album_uris with an in-flight fetch
        # Prepared 720² surfaces for extra pages, keyed by (album_uri, idx).
        self._art_page_surf: collections.OrderedDict[tuple, pygame.Surface] = \
            collections.OrderedDict()
        self._ART_PAGE_MAX = 7                # keep a small window in memory
        self._palette_col: dict[str, tuple] = {}        # uri → (bg, accent)
        self._tl_bg_cur:  list  = list(COL_TL_BG)      # current (animated) tl bg colour
        self._tl_bg_t:    tuple = COL_TL_BG             # target tl bg colour
        self._accent_cur: list  = list(COL_HIGHLIGHT)   # current (animated) accent colour
        self._accent_t:   tuple = COL_HIGHLIGHT         # target accent colour

        # default background
        self._default_bg = self._make_default_bg()

        # data
        self._albums:  list[dict] = []
        self._cur_idx: int | None = None
        self._tracks:  list[dict] = []

        # peek mode: album art strip at top of grid view
        self._peeking = False

        # carousel state
        self._carousel_pos:        float = 0.0   # smooth current position (float album index)
        self._carousel_pos_t:      float = 0.0   # snapped target (integer album index)
        self._carousel_drag_start: float = 0.0   # carousel_pos captured at touch-start
        # keyed by (album_idx, w, near_h, far_h, N) →
        #   (surf, [(refl_surf, rel_dx, rel_dy), ...])
        # rel coords are offsets from (x - w//2, floor_y) so cached blits
        # only need the current x position, not recomputed geometry.
        self._car_surf_cache: dict[tuple, tuple] = {}

        # settings return target
        self._settings_return: View = View.GRID
        self._settings_return_ctrl: bool = False

        # cache clear feedback
        self._cache_cleared_ms: int = 0   # ticks when last cleared (0 = not recently)
        self._instrumental_cleared_ms: int = 0
        self._lyrics_album_index: set[str] = self._load_lyrics_index()

        # playback-screen menu: download/clear artwork & lyrics
        self._album_menu_open:    bool = False
        self._art_release_picker: list[dict] | None = None  # candidates, or None if closed
        self._menu_toast:    str | None = None
        self._menu_toast_ms: int = 0

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

        # power state for BT/WiFi header toggles
        self._bt_powered: bool = True
        self._wifi_powered: bool = True

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
        self._lyrics_cache:    dict  = {}    # uri → parsed tuple or None (session cache)
        self._prefetch_gen:    int   = 0    # incremented on each new album; cancels old prefetch
        self._lyrics_bulk_progress: tuple | None = None  # (done, total) while running
        # Extended-artwork bulk fetch: (done, total, library) while running.
        self._art_bulk_progress: tuple | None = None
        self._art_lib_counts: dict[str, tuple[int, int]] = {}  # library → (done, total)
        self._lyrics_anim_vis:   float = 0.0  # animated scroll position (visual rows)
        self._lyrics_target_vis: float = 0.0  # target scroll position
        self._lyrics_prev_idx:   int   = -1   # last known logical line index
        self._lyrics_anim_t:     float = 0.0  # monotonic time of last anim update

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
        self._settings_scroll:   float = 0.0
        self._settings_dropdown: str | None = None   # key of open selector dropdown

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


        # lyrics visual-row cache (avoid re-wrapping every frame)
        self._lyrics_row_cache: tuple | None = None  # (parsed_ref, rows, logical, first_vis)

        # volume badge
        self._vol_until_ms = 0
        if volume_ctrl.available:
            volume_ctrl.set_on_change(self._on_volume)

        self._load_gen: int = 0   # incremented on each library reload; stale threads check this

        # status cache
        self._status: dict = {}
        self._song:   dict = {}
        self._last_poll = 0.0
        self._last_tick_ms = pygame.time.get_ticks()
        # elapsed interpolation: base value + wall-clock time since that poll
        self._elapsed_base   = 0.0
        self._elapsed_base_t = time.monotonic()
        self._last_progress_px = -1   # last drawn progress bar pixel; avoids spurious dirty
        # True while an album is loading — suppresses poll from overwriting elapsed with stale data
        self._loading_album    = False
        # monotonic deadline before which backwards elapsed jumps are ignored (post-seek buffer)
        self._seek_guard_until = 0.0
        # URI we expect after a track tap. Guard stays active (blocking wrong-file
        # updates) until MPD confirms the URI AND bitrate > 0 (audio flowing),
        # or until the 5s hard timeout fires.
        self._song_guard_file: str      = ""
        self._song_guard_timeout: float = 0.0
        # Previous file blocked for a brief window after the guard clears, so
        # a Spotify backend glitch that briefly re-reports the old URI can't
        # flip the display back after we've already confirmed the new track.
        self._song_prev_file: str      = ""
        self._song_block_until: float  = 0.0

        # thumbnail loader
        self._thumb_pool    = concurrent.futures.ThreadPoolExecutor(max_workers=THUMB_WORKERS)
        self._thumb_queued:   set[int] = set()
        self._thumbs_pending: int      = 0   # number of thumbs still in flight

        threading.Thread(target=self._load_albums, args=(self._load_gen,), daemon=True).start()

    # ── default background ────────────────────────────────────────────────────

    def _make_default_bg(self) -> pygame.Surface:
        s = pygame.Surface((W, H))
        s.fill(COL_BG)
        return s

    @staticmethod
    def _palette_colors_from_image(img: Image.Image) -> tuple[tuple, tuple]:
        """Return (bg_col, accent_col) derived from the album art palette.
        bg_col  — dark, for tracklist background.
        accent_col — vivid, replaces COL_HIGHLIGHT for buttons and progress bar.
        """
        small = img.resize((64, 64), Image.LANCZOS).convert("RGB")
        q     = small.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        raw_pal = q.getpalette() or []
        n_cols  = min(8, len(raw_pal) // 3)
        pal     = raw_pal[:n_cols * 3]

        # count pixels per palette entry so coverage drives the choice,
        # not just saturation (avoids small-area accent colours like logos winning)
        counts = [0] * n_cols
        for p in q.getdata():
            if p < n_cols:
                counts[p] += 1
        total = max(1, sum(counts))

        best_col, best_score = None, -1.0
        for i in range(n_cols):
            r, g, b  = pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]
            _, s, v  = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if v < 0.15 or v > 0.95:   # skip near-black and near-white
                continue
            coverage = counts[i] / total
            score    = coverage * (0.5 * s + 0.5 * v)
            if score > best_score:
                best_score = score
                best_col   = (r, g, b)

        if best_col is None:   # all colours were too dark/light — fall back
            best_col = COL_TL_BG

        r, g, b = best_col
        # bg: dark tint
        f = 0.48
        bg = (max(8, int(r * f)), max(8, int(g * f)), max(8, int(b * f)))
        # accent: vivid — same hue, boosted saturation, fixed high brightness
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        s = min(1.0, s + 0.2)
        v = 0.88
        ar, ag, ab = colorsys.hsv_to_rgb(h, s, v)
        accent = (int(ar * 255), int(ag * 255), int(ab * 255))
        return bg, accent

    # ── album list + thumbnails ───────────────────────────────────────────────

    @staticmethod
    def _sort_albums(albums: list[dict]) -> list[dict]:
        sort = (settings.get("album_sort") or "artist a→z").lower()
        if sort == "artist z→a":
            return sorted(albums, key=lambda a: (a["artist"].casefold(), a.get("year") or 9999, a["name"].casefold()), reverse=True)
        if sort == "year ↑":
            return sorted(albums, key=lambda a: (a.get("year") or 9999, a["name"].casefold()))
        if sort == "year ↓":
            return sorted(albums, key=lambda a: (-(a.get("year") or 0), a["name"].casefold()))
        if sort == "album a→z":
            return sorted(albums, key=lambda a: a["name"].casefold())
        if sort == "album z→a":
            return sorted(albums, key=lambda a: a["name"].casefold(), reverse=True)
        # default: artist a→z
        return sorted(albums, key=lambda a: (a["artist"].casefold(), a.get("year") or 9999, a["name"].casefold()))

    def _load_albums(self, gen: int):
        library = settings.get("library") or "local"
        albums = self.player.get_albums(library=library)
        if self._load_gen != gen:   # library switched while we were loading — discard
            return
        albums = self._sort_albums(albums)
        self._albums         = albums
        self._thumbs_pending = len(albums)
        self._dirty          = True
        log.info("Loaded %d albums from %s", len(albums), library)
        for i in range(len(albums)):
            self._queue_thumb(i)

    def _apply_spotify_bitrate(self, bitrate: str):
        """Write bitrate to mopidy.conf and restart the mopidy service."""
        import subprocess
        conf_path = os.path.expanduser("~/.config/mopidy/mopidy.conf")
        try:
            with open(conf_path) as f:
                lines = f.readlines()
            # Update or insert 'bitrate' inside the [spotify] section only.
            in_spotify = False
            found = False
            out = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("["):
                    if in_spotify and not found:
                        out.append(f"bitrate = {bitrate}\n")
                        found = True
                    in_spotify = stripped == "[spotify]"
                if in_spotify and stripped.startswith("bitrate"):
                    out.append(f"bitrate = {bitrate}\n")
                    found = True
                    continue
                out.append(line)
            if in_spotify and not found:
                out.append(f"bitrate = {bitrate}\n")
            with open(conf_path, "w") as f:
                f.writelines(out)
        except Exception as e:
            log.warning("Could not write mopidy.conf: %s", e)
            return
        for cmd in (["systemctl", "--user", "restart", "mopidy"],
                    ["sudo", "systemctl", "restart", "mopidy"]):
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode == 0:
                log.info("mopidy restarted for bitrate=%s", bitrate)
                return
        log.warning("Could not restart mopidy after bitrate change")

    def _reload_library(self):
        """Increment generation (aborts in-flight load) then reload album list."""
        self._load_gen += 1
        gen = self._load_gen
        self._albums = []
        self._thumb_queued.clear()
        self._thumbs_pending = 0
        self._car_surf_cache.clear()
        self._dirty = True
        threading.Thread(target=self._load_albums, args=(gen,), daemon=True).start()

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
        album     = self._albums[idx]
        uri       = album["track_uri"]
        key       = hashlib.md5(uri.encode()).hexdigest()
        thumb_jpg = os.path.join(_THUMB_CACHE_DIR, f"{key}_{_CELL_W}.jpg")
        full_png  = os.path.join(_THUMB_CACHE_DIR, f"{key}_{DISPLAY_WIDTH}.png")
        full_jpg  = os.path.join(_THUMB_CACHE_DIR, f"{key}_{DISPLAY_WIDTH}.jpg")  # legacy
        try:
            full = None
            if os.path.exists(thumb_jpg):
                # Thumbnail already cached — load only the small file, skip full image entirely.
                thumb = Image.open(thumb_jpg).convert("RGB")
            else:
                # Need the full-size image to generate the thumbnail.
                if os.path.exists(full_png):
                    full = Image.open(full_png).convert("RGB")
                elif os.path.exists(full_jpg):
                    full = Image.open(full_jpg).convert("RGB")
                else:
                    raw = self.player.get_album_art(uri)
                    if raw:
                        w, h = raw.size
                        side = min(w, h)
                        full = raw.crop(((w - side) // 2, (h - side) // 2,
                                         (w + side) // 2, (h + side) // 2))
                        full = full.resize((DISPLAY_WIDTH, DISPLAY_HEIGHT), Image.LANCZOS)
                        full.save(full_png, "PNG")
                    else:
                        full = None
                if full and not os.path.exists(thumb_jpg):
                    thumb = full.resize((_CELL_W, _CELL_W), Image.LANCZOS)
                    thumb.save(thumb_jpg, "JPEG", quality=80)
                else:
                    thumb = full

            if thumb:
                th = thumb.resize((_CELL_W, _CELL_W), Image.LANCZOS) if thumb.size != (_CELL_W, _CELL_W) else thumb
                album["thumb"] = _pil_to_surf(th)
            else:
                album["thumb"] = None
        except Exception as e:
            log.debug("Thumb %d: %s", idx, e)
            album["thumb"] = None
        album["thumb_loading"]  = False
        self._thumbs_pending    = max(0, self._thumbs_pending - 1)
        self._dirty             = True
        # Invalidate any cached carousel surfaces for this album.
        for k in [k for k in self._car_surf_cache if k[0] == idx]:
            del self._car_surf_cache[k]

    # ── album art ─────────────────────────────────────────────────────────────

    _ART_MEM_MAX = 1   # full-size art surface kept in memory (~2 MB each)

    def _load_art(self, uri: str):
        # Serve from memory cache instantly when available
        if uri in self._art_mem:
            self._art_mem.move_to_end(uri)
            self._art         = self._art_mem[uri]
            self._art_loading = False
            self._dirty       = True
            cols = self._palette_col.get(uri)
            self._tl_bg_t   = cols[0] if cols else COL_TL_BG
            self._accent_t  = cols[1] if cols else COL_HIGHLIGHT
            return

        self._art         = None   # clear old art so spinner shows
        self._art_loading = True
        self._dirty       = True

        def _bg():
            key      = hashlib.md5(uri.encode()).hexdigest()
            path_png = os.path.join(_THUMB_CACHE_DIR, f"{key}_{DISPLAY_WIDTH}.png")
            path_jpg = os.path.join(_THUMB_CACHE_DIR, f"{key}_{DISPLAY_WIDTH}.jpg")  # legacy
            img = None
            try:
                if os.path.exists(path_png):
                    img = Image.open(path_png).convert("RGB")
                elif os.path.exists(path_jpg):
                    img = Image.open(path_jpg).convert("RGB")
                else:
                    img = self.player.get_album_art(uri)
                    if img:
                        dw, dh = img.size
                        side   = min(dw, dh)
                        img    = img.crop(((dw - side) // 2, (dh - side) // 2,
                                           (dw + side) // 2, (dh + side) // 2))
                        img    = img.resize((DISPLAY_WIDTH, DISPLAY_HEIGHT), Image.LANCZOS)
                        img.save(path_png, "PNG")
                if img:
                    if img.size != (W, H):
                        img = img.resize((W, H), Image.LANCZOS)
                    surf = _pil_to_surf(img)
                    self._palette_col[uri] = self._palette_colors_from_image(img)
                else:
                    surf = None
            except Exception as e:
                log.warning("Art load failed for %s: %s", uri, e)
                surf = None
            # Store in memory LRU cache
            self._art_mem[uri] = surf
            if len(self._art_mem) > self._ART_MEM_MAX:
                self._art_mem.popitem(last=False)
            self._pending_art = surf

        threading.Thread(target=_bg, daemon=True).start()

    # ── extended artwork carousel ─────────────────────────────────────────────

    def _reset_art_carousel(self):
        """Clear carousel state when switching albums."""
        self._art_paths      = []
        self._art_count      = 1
        self._art_pos        = 0.0
        self._art_pos_t      = 0.0
        self._art_idx        = 0
        self._art_drag       = False
        self._art_dots_a     = 0.0
        self._art_dots_a_t   = 0.0
        self._art_dots_seen_ms = 0
        self._art_page_surf.clear()

    def _load_art_set(self, album: dict, allow_fetch: bool = True):
        """Populate the carousel with cached extended art, or fetch it.

        Image 0 (front) comes from the normal _load_art pipeline; this adds the
        back/booklet pages.  If nothing is cached yet and *allow_fetch*, a
        background fetch runs and the carousel is refreshed when it completes.
        """
        album_uri = album.get("track_uri", "")
        if not album_uri:
            return
        cached = self._artwork.cached_images(album_uri)
        if cached is not None:
            self._art_paths = cached
            self._art_count = 1 + len(cached)
            self._ensure_art_window()
            return
        if not allow_fetch:
            return
        # Not fetched yet — kick a background fetch keyed to this album.
        if album_uri in self._art_fetching:
            return
        self._art_fetching.add(album_uri)
        artist = album.get("artist", "")
        name   = album.get("name", "")

        def _on_image(path):
            # Append each page as it arrives so the carousel grows live.
            if album_uri != self._art_album_uri:
                return
            if path not in self._art_paths:
                self._art_paths.append(path)
                self._art_count = 1 + len(self._art_paths)
                self._ensure_art_window()
                self._dirty = True

        def _bg():
            try:
                tracks = album.get("tracks")
                track_count = len(tracks) if tracks else 0
                self._artwork.fetch(album_uri, artist, name, track_count,
                                    on_image=_on_image)
            except Exception as e:
                log.warning("art set fetch failed for %s: %s", album_uri, e)
            finally:
                self._art_fetching.discard(album_uri)

        threading.Thread(target=_bg, daemon=True).start()

    def _art_surface(self, idx: int) -> pygame.Surface | None:
        """Return the 720² surface for carousel index *idx* (0 = front)."""
        if idx <= 0:
            return self._art
        page = idx - 1
        if page >= len(self._art_paths):
            return None
        key = (self._art_album_uri, idx)
        surf = self._art_page_surf.get(key)
        if surf is not None:
            self._art_page_surf.move_to_end(key)
            return surf
        return None   # not prepared yet; _ensure_art_window schedules it

    def _ensure_art_window(self):
        """Prepare surfaces for the current index ±1 in a background thread,
        and evict pages outside a small window to bound memory."""
        want = {i for i in range(self._art_idx - 2, self._art_idx + 3)
                if 1 <= i < self._art_count}
        # Bound memory: LRU-evict the least-recently-used prepared pages.
        while len(self._art_page_surf) > self._ART_PAGE_MAX:
            self._art_page_surf.popitem(last=False)

        missing = [i for i in want
                   if (self._art_album_uri, i) not in self._art_page_surf]
        if not missing:
            return

        album_uri = self._art_album_uri
        paths     = list(self._art_paths)

        def _bg():
            for i in missing:
                page = i - 1
                if page < 0 or page >= len(paths):
                    continue
                key = (album_uri, i)
                if key in self._art_page_surf:
                    continue
                try:
                    img  = Image.open(paths[page]).convert("RGB")
                    # Back covers often carry spine strips on the sides — fill
                    # the height and crop the left/right so the back shows big.
                    mode = ("height" if "_back" in os.path.basename(paths[page]).lower()
                            else "contain")
                    surf = self._fit_art_surface(img, mode)
                    if album_uri == self._art_album_uri:
                        self._art_page_surf[key] = surf
                        self._dirty = True
                except Exception as e:
                    log.debug("art page %d prep failed: %s", i, e)

        threading.Thread(target=_bg, daemon=True).start()

    @staticmethod
    def _blurred_bg(img: Image.Image) -> Image.Image:
        """A darkened, blurred, screen-filling background from *img*.

        Blurs a quarter-size copy (cheap) and upscales — visually the same as a
        big-radius blur of the full image but far faster on the Pi.
        """
        iw, ih = img.size
        cover = max(W / iw, H / ih) / 4.0
        bw, bh = max(1, round(iw * cover)), max(1, round(ih * cover))
        small = img.resize((bw, bh), Image.BILINEAR)
        qw, qh = W // 4, H // 4
        sx, sy = max(0, (bw - qw) // 2), max(0, (bh - qh) // 2)
        small = small.crop((sx, sy, sx + qw, sy + qh)).filter(ImageFilter.GaussianBlur(6))
        bg = small.resize((W, H), Image.BILINEAR)
        return Image.eval(bg, lambda p: int(p * 0.45))

    @staticmethod
    def _fit_art_surface(img: Image.Image, mode: str = "contain") -> pygame.Surface:
        """Fit *img* into the W×H square.

        "contain" (default): whole image visible, centred on a blurred cover.
        "height": scale to full height and centre, cropping the left/right —
        used for back covers so spine strips get cropped off and the back
        fills the screen.
        """
        iw, ih = img.size
        if mode == "height":
            fw = max(1, round(iw * (H / ih)))
            fg = img.resize((fw, H), Image.LANCZOS)
            if fw >= W:                      # fills the screen — no background
                x = (fw - W) // 2
                return _pil_to_surf(fg.crop((x, 0, x + W, H)))
            bg = AlbumDisplay._blurred_bg(img)
            bg.paste(fg, ((W - fw) // 2, 0))
            return _pil_to_surf(bg)

        # contain: whole image visible.
        contain = min(W / iw, H / ih)
        fw, fh  = max(1, round(iw * contain)), max(1, round(ih * contain))
        fg = img.resize((fw, fh), Image.LANCZOS)
        if fw >= W and fh >= H:              # already covers — no background
            return _pil_to_surf(fg)
        bg = AlbumDisplay._blurred_bg(img)
        bg.paste(fg, ((W - fw) // 2, (H - fh) // 2))
        return _pil_to_surf(bg)

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

        self._album_y      = _lerp(self._album_y,      self._album_y_t,      k_pan)
        self._ctrl_a       = _lerp(self._ctrl_a,       self._ctrl_a_t,       k_ctl)
        self._carousel_pos = _lerp(self._carousel_pos, self._carousel_pos_t, k_pan)
        if (self._carousel_pos_t % 1.0 == 0.0
                and abs(self._carousel_pos - self._carousel_pos_t) < 0.005):
            self._carousel_pos = self._carousel_pos_t

        # Art carousel: follow the target unless a finger is actively dragging.
        if not self._art_drag:
            self._art_pos = _lerp(self._art_pos, self._art_pos_t, k_pan)
            if abs(self._art_pos - self._art_pos_t) < 0.004:
                self._art_pos = self._art_pos_t

        # Page dots: full opacity while sliding, hold, then fade out.
        art_moving = self._art_drag or abs(self._art_pos - self._art_pos_t) > 0.004
        if art_moving:
            self._art_dots_a_t     = 255.0
            self._art_dots_seen_ms = now_ms
        elif now_ms - self._art_dots_seen_ms > _ART_DOTS_HOLD_MS:
            self._art_dots_a_t = 0.0
        self._art_dots_a = _lerp(self._art_dots_a, self._art_dots_a_t, k_ctl)

        k_bg = min(1.0, 3.0 * dt)
        self._tl_bg_cur = [_lerp(self._tl_bg_cur[i], self._tl_bg_t[i], k_bg) for i in range(3)]
        self._accent_cur = [_lerp(self._accent_cur[i], self._accent_t[i], k_bg) for i in range(3)]

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
                if self._settings_dropdown:
                    self._settings_dropdown = None
            else:
                self._settings_vel = 0.0

        # pick up finished art load
        pending = getattr(self, "_pending_art", "loading")
        if pending != "loading":
            del self._pending_art
            self._art         = pending   # may be None → use default
            self._art_loading = False
            self._dirty       = True
            cols = self._palette_col.get(self._art_album_uri)
            self._tl_bg_t  = cols[0] if cols else COL_TL_BG
            self._accent_t = cols[1] if cols else COL_HIGHLIGHT

        # mark dirty for ongoing animations/playback BEFORE resolving taps/events
        # so that pending_tap=True is captured before it gets cleared below
        if not self._dirty:
            self._dirty = (
                abs(self._album_y - self._album_y_t) > 0.5
                or abs(self._ctrl_a - self._ctrl_a_t) > 0.5
                or any(abs(self._tl_bg_cur[i] - self._tl_bg_t[i]) > 0.5 for i in range(3))
                or any(abs(self._accent_cur[i] - self._accent_t[i]) > 0.5 for i in range(3))

                or self._flash_alpha > 0
                or self._menu_toast is not None
                or self._pending_tap
                or (self._view not in (View.SETTINGS, View.CALIBRATE) and self._progress_px_changed())
                or now_ms < self._vol_until_ms
                or self._bt_refreshing
                or self._wifi_refreshing
                or self._scan_refreshing
                or self._lyrics_loading
                or self._art_loading
                or self._art_drag
                or abs(self._art_pos - self._art_pos_t) > 0.002
                or abs(self._art_dots_a - self._art_dots_a_t) > 0.5
                or self._scrub_active
                or abs(self._grid_vel) > 0.5
                or abs(self._tl_vel) > 0.5
                or abs(self._settings_vel) > 0.5
                or self._view in (View.SCAN, View.CALIBRATE)
                or self._kb_ssid is not None
                or self._thumbs_pending > 0
                or (self._cache_cleared_ms
                    and now_ms - self._cache_cleared_ms < 2000)
                or (self._instrumental_cleared_ms
                    and now_ms - self._instrumental_cleared_ms < 2000)
                or abs(self._carousel_pos - self._carousel_pos_t) > 0.01
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
                    discovered = self.bt.get_discovered_devices()
                    self._scan_devices    = [d for d in discovered if d["address"] not in pa]
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
            # Snapshot elapsed for smooth interpolation between polls.
            # Suppress while loading a new album (old track's elapsed bleeds in)
            # or when elapsed jumps backwards right after a seek (Spotify rebuffers at 0).
            el = float(status.get("elapsed", 0) or 0)
            if not self._loading_album:
                if el >= self._elapsed_base - 1.0 or now > self._seek_guard_until:
                    self._elapsed_base   = el
                    self._elapsed_base_t = now
            # Only overwrite _song once MPD reports the track we expect.
            # While _song_guard_file is set, ignore updates that don't match —
            # this prevents the display reverting to the old track while
            # core.playback.play is still in flight (however long that takes).
            if song:
                f = song.get("file", "")
                if self._song_guard_file:
                    timed_out = now > self._song_guard_timeout
                    bitrate   = int(status.get("bitrate", 0) or 0)
                    if f == self._song_guard_file:
                        self._song = song
                        if bitrate > 0 or timed_out:
                            # Confirmed: arm a short post-guard block so a
                            # transient backend revert to the old URI can't
                            # flip the display back.
                            self._song_block_until = now + 3.0
                            self._song_guard_file  = ""
                    elif timed_out:
                        # Timed out but MPD still reports the old file — clear
                        # the guard without reverting _song (keep showing the
                        # track we're switching to; poll will catch up when
                        # Spotify actually starts it).
                        self._song_guard_file = ""
                    # else: wrong file, still within timeout → skip
                elif self._song_prev_file and now < self._song_block_until:
                    # Post-guard window: accept any file that isn't the old one.
                    if f != self._song_prev_file:
                        self._song           = song
                        self._song_prev_file = ""
                else:
                    self._song = song
            new_uri = self._song.get("file", "")
            # re-fetch art if track changed while album view is open
            if (new_uri and new_uri != self._art_uri
                    and self._view in (View.ALBUM, View.TRACKLIST)):
                self._art_uri = new_uri
                # Use the album's canonical first-track URI as the art key so
                # track changes within the same album never trigger a reload.
                canonical = (self._albums[self._cur_idx]["track_uri"]
                             if self._cur_idx is not None and self._cur_idx < len(self._albums)
                             else new_uri)
                if canonical != self._art_album_uri:
                    self._art_album_uri = canonical
                    self._load_art(canonical)
            # reload lyrics whenever the track changes
            if new_uri and new_uri != self._lyrics_uri and not self._lyrics_loading:
                self._lyrics_uri    = new_uri
                self._lyrics        = None
                self._lyrics_scroll      = 0.0
                self._lyrics_prev_idx    = -1   # force snap on first draw
                self._lyrics_anim_t      = 0.0  # 0 signals "snap immediately"
                if new_uri in self._lyrics_cache:
                    # Instant cache hit — no network round-trip needed.
                    self._lyrics_parsed = self._lyrics_cache[new_uri]
                    self._dirty         = True
                else:
                    self._lyrics_parsed  = None
                    self._lyrics_loading = True
                    # Snapshot song + status NOW so the thread doesn't race
                    # with poll updates that could change self._song mid-fetch.
                    _snap_song   = dict(self._song)
                    _snap_status = dict(self._status)
                    def _fetch_lyr(u=new_uri, ss=_snap_song, st=_snap_status):
                        text   = self._load_lyrics_for_uri(u, song=ss, status=st)
                        parsed = self._parse_lyrics(text) if text else None
                        self._lyrics_cache[u]  = parsed
                        self._lyrics_parsed    = parsed
                        self._lyrics_loading   = False
                        self._dirty            = True
                    threading.Thread(target=_fetch_lyr, daemon=True).start()

        # mark dirty for ongoing animations/playback — never clear it here;
        # only draw() clears _dirty after actually rendering a frame
        if not self._dirty:
            now_ms = self._last_tick_ms
            self._dirty = (
                abs(self._album_y - self._album_y_t) > 0.5
                or abs(self._ctrl_a - self._ctrl_a_t) > 0.5
                or any(abs(self._tl_bg_cur[i] - self._tl_bg_t[i]) > 0.5 for i in range(3))

                or self._flash_alpha > 0
                or self._menu_toast is not None
                or self._pending_tap
                or (self._view not in (View.SETTINGS, View.CALIBRATE) and self._progress_px_changed())
                or self._bt_refreshing
                or self._wifi_refreshing
                or self._scan_refreshing
                or self._lyrics_loading
                or self._art_loading
                or self._art_drag
                or abs(self._art_pos - self._art_pos_t) > 0.002
                or abs(self._art_dots_a - self._art_dots_a_t) > 0.5
                or self._scrub_active
                or abs(self._grid_vel) > 0.5
                or abs(self._tl_vel) > 0.5
                or abs(self._settings_vel) > 0.5
                or self._view == View.SCAN   # animated dots
                or self._thumbs_pending > 0
                or abs(self._carousel_pos - self._carousel_pos_t) > 0.01
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
        if (self._view == View.ALBUM and self._lyrics_parsed
                and settings.get("lyrics")):
            return FPS
        return FPS if self._dirty else 10

    def draw(self) -> bool:
        kb_active = self._kb_ssid is not None
        if not self._dirty and settings.get("skip_draw") and not settings.get("debug") and not kb_active:
            return False
        self._dirty = False
        if self._view == View.CAROUSEL and self._album_y >= H - 1:
            self._draw_carousel()
            self._draw_progress()
            self._draw_volume_badge()
            if settings.get("debug"):
                self._draw_debug_overlays()
            return True
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

        # ── layer 1: grid/carousel (when album is off-screen or peeking above) ──
        if ay > 2 or self._peeking:
            if self._view == View.CAROUSEL:
                self._draw_carousel()
            else:
                self._draw_grid()

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

        grid_bottom  = (H - TRACKLIST_ART_H) if self._peeking else H
        show_labels  = settings.get("grid_labels")
        label_h      = GRID_TEXT_H if show_labels else 0
        cell_h       = _CELL_W + label_h
        row_h        = cell_h + GRID_PAD

        total_rows = (len(self._albums) + GRID_COLS - 1) // GRID_COLS
        max_scroll = max(0, total_rows * row_h + GRID_PAD - (grid_bottom - grid_top))
        self._grid_scroll = max(0.0, min(self._grid_scroll, float(max_scroll)))

        old_clip = self.screen.get_clip()
        try:
            self.screen.set_clip(0, grid_top, W, grid_bottom - grid_top)

            for i, album in enumerate(self._albums):
                row = i // GRID_COLS
                col = i % GRID_COLS
                x   = GRID_PAD + col * (_CELL_W + GRID_PAD)
                y   = grid_top + GRID_PAD + row * row_h - int(self._grid_scroll)

                # preload thumbnails two rows outside the visible area
                if (y + cell_h >= grid_top - row_h * 2
                        and y <= H + row_h * 2):
                    self._queue_thumb(i)

                if y + cell_h < grid_top or y > H:
                    continue

                thumb = album.get("thumb")
                if thumb:
                    self.screen.blit(thumb, (x, y))
                else:
                    pygame.draw.rect(self.screen, COL_CELL_BG,
                                      (x, y, _CELL_W, _CELL_W), border_radius=4)

                if show_labels:
                    ty = y + _CELL_W + 4
                    sn = _render_text(self._f_grid,    album["name"],   COL_TEXT_TITLE,  _CELL_W)
                    sa = _render_text(self._f_grid_sm, album["artist"], COL_TEXT_ARTIST, _CELL_W)
                    self.screen.blit(sn, (x, ty))
                    self.screen.blit(sa, (x, ty + sn.get_height() + 2))
        finally:
            self.screen.set_clip(old_clip)

    # ── draw: carousel ────────────────────────────────────────────────────────

    def _draw_carousel(self):
        self.screen.fill(COL_BG)
        n = len(self._albums)

        if not n:
            msg = _render_text(self._f_artist, "Loading library…", COL_TEXT_ALBUM)
            self.screen.blit(msg, ((W - msg.get_width()) // 2, H // 2))
            return

        pos        = self._carousel_pos
        center_idx = max(0, min(n - 1, int(round(pos))))

        def _slot(d: float):
            a        = abs(d)
            t        = min(a, 1.0)
            compress = max(_CAR_COMP, 1.0 - (1.0 - _CAR_COMP) * t)
            w        = max(4, int(_CAR_SIZE * compress))
            # overall_scale pulls side albums back in 3D so the tall (outer)
            # edge stays visually smaller than the centre album.
            overall_scale = 1.0 if a < 0.001 else max(_CAR_SIDE_SCALE,
                                1.0 - (1.0 - _CAR_SIDE_SCALE) * min(a, 1.0))
            sin_th   = math.sqrt(max(0.0, 1.0 - compress * compress))
            half_z   = sin_th * _CAR_SIZE * 0.5
            if half_z > 1.0:
                near_h = int(overall_scale * _CAR_SIZE * _CAR_CAM_D / max(1.0, _CAR_CAM_D - half_z))
                far_h  = int(overall_scale * _CAR_SIZE * _CAR_CAM_D / (_CAR_CAM_D + half_z))
            else:
                near_h = far_h = _CAR_SIZE
            ai, af   = int(a), a - int(a)
            n_ax     = len(_CAR_AX)
            if ai < n_ax - 1:
                x_off = _CAR_AX[ai] + (_CAR_AX[ai + 1] - _CAR_AX[ai]) * af
            else:
                x_off = _CAR_AX[-1] + (_CAR_AX[-1] - _CAR_AX[-2]) * (a - n_ax + 1)
            x = W // 2 + int(math.copysign(x_off, d)) if d != 0.0 else W // 2
            return x, w, near_h, far_h, compress

        # All albums share the same floor line (bottom of centre album).
        # When the album panel is peeking up from below, shift the carousel
        # upward so it stays visible above the peek strip.
        peek_frac = 0.0
        if self._peeking:
            peek_frac = max(0.0, min(1.0,
                (H - self._album_y) / TRACKLIST_ART_H))
        y_shift = int(peek_frac * _CAR_PEEK_SHIFT)
        floor_y = _CAR_CY + _CAR_SIZE // 2 - y_shift

        # Furthest albums first → nearer ones blit on top (correct depth order).
        visible = []
        for i in range(n):
            d = i - pos
            a = abs(d)
            if a < 1.9:
                x, w, near_h, far_h, compress = _slot(d)
                # Fade only the very outermost albums (a > 1.4) so core albums
                # are fully opaque; the fade gracefully clips the far-edge album.
                alpha = max(0, int(255 * max(0.0, 1.0 - max(0.0, a - 1.4) / 0.9)))
                visible.append((a, d, i, x, w, near_h, far_h, compress, alpha))
        visible.sort(key=lambda v: v[0], reverse=True)

        # Lazy-build reflection fade mask (transparent → COL_BG, top → bottom).
        if not hasattr(self, "_refl_fade") or self._refl_fade is None:
            fade = pygame.Surface((W, _CAR_REFL_H), pygame.SRCALPHA)
            r, g, b = COL_BG
            for yi in range(_CAR_REFL_H):
                a = int(255 * yi / _CAR_REFL_H)
                pygame.draw.line(fade, (r, g, b, a), (0, yi), (W - 1, yi))
            self._refl_fade = fade

        # Pass 1 — render each album surface + composite reflection below it.
        #
        # Strips are CENTRE-aligned (dst_y = (max_h - col_h)//2) giving a
        # perspective-correct angled top AND angled bottom.
        #
        # All per-column reflection surfaces are composited into one surface per
        # album before blitting (1 blit vs N) to reduce per-call overhead.
        #
        # Cache key includes (d > 0) because left/right albums need opposite
        # t_persp direction for correct perspective; omitting the sign causes
        # albums to show the wrong face after scrolling past centre.
        _settled  = (self._carousel_pos_t % 1.0 == 0.0
                     and self._carousel_pos == self._carousel_pos_t)
        use_cache = settings.get("car_cache") and _settled
        _refl     = settings.get("car_reflections")
        # Animation uses fewer strips and skips reflections for speed.
        N = _CAR_PERSP_N if _settled else max(4, _CAR_PERSP_N // 2)

        surfs = []
        for _, d, i, x, w, near_h, far_h, compress, alpha in visible:
            self._queue_thumb(i)
            thumb  = self._albums[i].get("thumb")
            max_h  = near_h   # near_h >= far_h always
            blit_x = x - w // 2
            # refl composite starts refl_y_offset px ABOVE floor_y so that the
            # far edge (whose bottom sits above floor_y) is included.
            refl_y_off = (near_h - far_h) // 2

            # d_sign distinguishes left/right perspective direction; must be part
            # of the cache key or albums show the wrong face after crossing centre.
            cache_key = (i, w, near_h, far_h, d > 0, _refl)
            if use_cache and thumb and cache_key in self._car_surf_cache:
                surf, refl_comp = self._car_surf_cache[cache_key]
                if _refl:
                    self.screen.blit(refl_comp, (blit_x, floor_y - refl_y_off))
            elif thumb:
                tw, th = thumb.get_size()

                if near_h == far_h:
                    # Centre album: plain scale, no perspective strips.
                    surf = pygame.Surface((w, near_h))
                    surf.fill(COL_BG)
                    surf.blit(pygame.transform.smoothscale(thumb, (w, near_h)), (0, 0))
                    rh = min(near_h, _CAR_REFL_H)
                    if _settled:
                        composite_h = _CAR_REFL_H + refl_y_off
                        refl_comp   = pygame.Surface((w, composite_h))
                        refl_comp.fill(COL_BG)
                        if _refl:
                            piece = pygame.transform.flip(
                                surf.subsurface((0, near_h - rh, w, rh)), False, True)
                            piece.fill((80, 80, 80), special_flags=pygame.BLEND_MULT)
                            refl_comp.blit(piece, (0, 0))
                            self.screen.blit(refl_comp, (blit_x, floor_y - refl_y_off))
                    elif _refl:
                        piece = pygame.transform.flip(
                            surf.subsurface((0, near_h - rh, w, rh)), False, True)
                        piece.fill((80, 80, 80), special_flags=pygame.BLEND_MULT)
                        self.screen.blit(piece, (blit_x, floor_y))
                else:
                    # Side album: N perspective strips.  Animation: fewer strips,
                    # no SRCALPHA (avoids per-pixel alpha cost), no reflections.
                    # Settled: full quality SRCALPHA + reflections; cached surf is
                    # then converted to colorkey for fast subsequent blitting.
                    if _settled:
                        surf = pygame.Surface((w, max_h), pygame.SRCALPHA)
                        r, g, b = COL_BG
                        surf.fill((r, g, b, 0))
                    else:
                        surf = pygame.Surface((w, max_h))
                        surf.fill(COL_BG)
                    composite_h  = _CAR_REFL_H + refl_y_off
                    refl_comp    = pygame.Surface((w, composite_h))
                    refl_comp.fill(COL_BG)
                    album_blit_y = floor_y - max_h
                    for col in range(N):
                        t_persp = (1.0 - col / max(1, N - 1)) if d > 0 \
                                  else (col / max(1, N - 1))
                        col_h   = max(1, int(far_h + (near_h - far_h) * (1.0 - t_persp)))
                        dst_y   = (max_h - col_h) // 2
                        src_x   = int(col * tw / N)
                        src_w   = max(1, int((col + 1) * tw / N) - src_x)
                        dst_x   = int(col * w / N)
                        dst_w   = max(1, int((col + 1) * w / N) - dst_x)
                        strip   = pygame.transform.smoothscale(
                                      thumb.subsurface((src_x, 0, src_w, th)),
                                      (dst_w, col_h))
                        surf.blit(strip, (dst_x, dst_y))
                        if _refl:
                            col_bottom_y = album_blit_y + dst_y + col_h
                            rh_strip = min(col_h, _CAR_REFL_H)
                            flipped = pygame.transform.flip(
                                strip.subsurface((0, col_h - rh_strip, dst_w, rh_strip)),
                                False, True)
                            flipped.fill((80, 80, 80), special_flags=pygame.BLEND_MULT)
                            refl_comp.blit(flipped,
                                           (dst_x, col_bottom_y - floor_y + refl_y_off))
                    if not _settled:
                        surf.set_colorkey(COL_BG)
                    if _refl:
                        if not _settled:
                            refl_comp.set_colorkey(COL_BG)
                        self.screen.blit(refl_comp, (blit_x, floor_y - refl_y_off))

                if use_cache:
                    # Pre-blend SRCALPHA transparency into COL_BG and switch to
                    # colorkey so subsequent blits avoid per-pixel alpha math.
                    if surf.get_flags() & pygame.SRCALPHA:
                        solid = pygame.Surface(surf.get_size())
                        solid.fill(COL_BG)
                        solid.blit(surf, (0, 0))
                        solid.set_colorkey(COL_BG)
                        surf = solid
                    self._car_surf_cache[cache_key] = (surf, refl_comp)
                    if len(self._car_surf_cache) > 50:
                        for k in list(self._car_surf_cache.keys())[:25]:
                            del self._car_surf_cache[k]
            else:
                surf = pygame.Surface((w, near_h))
                surf.fill(COL_CELL_BG)

            surfs.append((surf, alpha, x, max_h))

        # Fade mask erases reflections below floor_y (transparent at top → opaque).
        if _refl:
            self.screen.blit(self._refl_fade, (0, floor_y))

        # Pass 2 — album bodies (far→near).
        # floor_y - max_h positions the near column's bottom exactly at floor_y.
        # Never mutate a cached surface: copy before applying alpha.
        for surf, alpha, x, max_h in surfs:
            if alpha < 255:
                surf = surf.copy()
                surf.set_alpha(alpha)
            self.screen.blit(surf, (x - surf.get_width() // 2, floor_y - max_h))

        # Name + artist centred below the album strip.
        # Compress the gap between reflections and text when peeking.
        c        = self._albums[center_idx]
        text_gap = int(18 - peek_frac * 10)   # 18px → 8px as panel slides up
        ty       = floor_y + _CAR_REFL_H + text_gap
        ns  = _render_text(self._f_title,  c.get("name",   ""), COL_TEXT_TITLE,  W - 80)
        as_ = _render_text(self._f_artist, c.get("artist", ""), COL_TEXT_ARTIST, W - 80)
        self.screen.blit(ns,  ((W - ns.get_width())  // 2, ty))
        self.screen.blit(as_, ((W - as_.get_width()) // 2, ty + ns.get_height() + 6))

        # Settings gear button (same position as in controls overlay, reuses _gear_btn_hit).
        gx = W - BTN_RADIUS - BTN_MARGIN - 2 * BTN_RADIUS - BTN_GAP
        gy = BTN_MARGIN + BTN_RADIUS
        pygame.gfxdraw.filled_circle(self.screen, gx, gy, BTN_RADIUS, (38, 38, 42))
        pygame.gfxdraw.aacircle(self.screen, gx, gy, BTN_RADIUS, (60, 60, 65))
        self._draw_gear_icon(gx, gy, BTN_RADIUS - max(1, BTN_RADIUS * 5 // 12),
                             col=(200, 200, 200), hole_col=(38, 38, 42))

    # ── draw: album panel ─────────────────────────────────────────────────────

    def _draw_album_panel(self, ay: int):
        if self._art_loading:
            self.screen.blit(self._default_bg, (0, ay))
            self._draw_spinner(W // 2, ay + H // 2)
            return
        # Outside full ALBUM view (peek/tracklist strips) show whichever image
        # the carousel is currently on — not always the front.
        if self._view != View.ALBUM:
            surf = self._art_surface(self._art_idx) or self._art or self._default_bg
            self.screen.blit(surf, (0, ay))
            return
        if self._art_count <= 1 and self._art_pos == 0.0:
            self.screen.blit(self._art or self._default_bg, (0, ay))
            return
        pos  = self._art_pos
        base = int(math.floor(pos))
        frac = pos - base
        for idx, x in ((base, round(-frac * W)), (base + 1, round((1.0 - frac) * W))):
            if 0 <= idx < self._art_count:
                surf = self._art_surface(idx) or self._default_bg
                self.screen.blit(surf, (x, ay))
        # Page indicator dots when more than one image and near a settle.
        if self._art_count > 1:
            self._draw_art_dots(ay)

    def _draw_art_dots(self, ay: int):
        n = self._art_count
        if n <= 1:
            return
        alpha = int(self._art_dots_a)
        if alpha <= 2:
            return
        r   = max(2, W // 200)
        gap = r * 4
        total_w = gap * (n - 1)
        cx0 = W // 2 - total_w // 2
        cy  = ay + H - max(16, H // 32)

        # Pill-shaped translucent backdrop — plain dots wash out on light covers.
        pad_x, pad_y = r * 3, int(r * 1.6)
        pill_w = total_w + r * 2 + pad_x * 2
        pill_h = r * 2 + pad_y * 2
        pill = pygame.Surface((pill_w, pill_h), pygame.SRCALPHA)
        pygame.draw.rect(pill, (0, 0, 0, int(120 * alpha / 255)),
                          (0, 0, pill_w, pill_h), border_radius=pill_h // 2)
        self.screen.blit(pill, (W // 2 - pill_w // 2, cy - pill_h // 2))

        cur = int(round(self._art_pos))
        for i in range(n):
            base = (235, 235, 235) if i == cur else (110, 110, 110)
            c = (*base, alpha)
            pygame.gfxdraw.filled_circle(self.screen, cx0 + i * gap, cy, r, c)
            pygame.gfxdraw.aacircle(self.screen, cx0 + i * gap, cy, r, c)

    def _draw_spinner(self, cx: int, cy: int):
        t     = pygame.time.get_ticks() / 1000.0
        r     = W // 9
        n     = 8
        dot_r = max(4, W // 55)
        for i in range(n):
            angle = math.pi * 2 * i / n - t * 4
            ax = cx + int(r * math.cos(angle))
            ay = cy + int(r * math.sin(angle))
            brightness = int(60 + 180 * (i + 1) / n)
            pygame.gfxdraw.filled_circle(self.screen, ax, ay, dot_r,
                                         (brightness, brightness, brightness))

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

        accent   = tuple(int(c) for c in self._accent_cur)
        icon_col = _on_bg(accent, strong=True)
        pressed  = self._pressed_ctrl()

        def _btn(col):
            """Return lightened colour when this button is pressed."""
            return tuple(min(255, c + 55) for c in col[:3]) if True else col

        def _circle(x, y, r, name, base_col):
            c = tuple(min(255, v + 55) for v in base_col[:3]) if pressed == name else base_col
            pygame.gfxdraw.filled_circle(self.screen, x, y, r, c)
            pygame.gfxdraw.aacircle(self.screen, x, y, r, c)

        # play/pause
        _circle(W // 2, ctrl_cy, CTRL_ICON_LG, "play", accent)
        play_icon_col = _on_bg(accent, strong=True)
        if playing:
            _draw_pause(self.screen, play_icon_col, W // 2, ctrl_cy, CTRL_ICON_LG)
        else:
            _draw_play(self.screen, play_icon_col, W // 2, ctrl_cy, CTRL_ICON_LG)

        # prev/next triangles — highlight with brighter col when pressed
        tri_col     = tuple(min(255, c + 55) for c in col) if pressed == "prev" else col
        tri_col_nxt = tuple(min(255, c + 55) for c in col) if pressed == "next" else col
        _draw_triangle(self.screen, tri_col,     W // 6 - off, ctrl_cy, CTRL_ICON_SM, "left")
        _draw_triangle(self.screen, tri_col,     W // 6 + off, ctrl_cy, CTRL_ICON_SM, "left")
        _draw_triangle(self.screen, tri_col_nxt, 5 * W // 6 - off, ctrl_cy, CTRL_ICON_SM, "right")
        _draw_triangle(self.screen, tri_col_nxt, 5 * W // 6 + off, ctrl_cy, CTRL_ICON_SM, "right")

        # gear button
        gx = W - BTN_RADIUS - BTN_MARGIN - 2 * BTN_RADIUS - BTN_GAP
        gy = BTN_MARGIN + BTN_RADIUS
        _circle(gx, gy, BTN_RADIUS, "gear", accent)
        self._draw_gear_icon(gx, gy, BTN_RADIUS - max(1, BTN_RADIUS * 5 // 12), col=icon_col, hole_col=accent)

        # menu button (artwork / lyrics download & clear)
        mx, my = self._menu_btn_pos()
        _circle(mx, my, BTN_RADIUS, "menu", accent)
        self._draw_menu_icon(mx, my, BTN_RADIUS - max(1, BTN_RADIUS * 5 // 12), col=icon_col)

        # close button
        bx = W - BTN_RADIUS - BTN_MARGIN
        by = BTN_MARGIN + BTN_RADIUS
        _circle(bx, by, BTN_RADIUS, "close", accent)
        d = max(1, BTN_RADIUS * 11 // 32)
        pygame.draw.aaline(self.screen, icon_col, (bx - d, by - d), (bx + d, by + d))
        pygame.draw.aaline(self.screen, icon_col, (bx - d + 1, by - d), (bx + d + 1, by + d))
        pygame.draw.aaline(self.screen, icon_col, (bx + d, by - d), (bx - d, by + d))
        pygame.draw.aaline(self.screen, icon_col, (bx + d + 1, by - d), (bx - d + 1, by + d))

        # stop button
        sx = BTN_RADIUS + BTN_MARGIN
        sy = BTN_MARGIN + BTN_RADIUS
        _circle(sx, sy, BTN_RADIUS, "stop", accent)
        sq = max(1, BTN_RADIUS * 13 // 32)
        pygame.draw.rect(self.screen, icon_col, (sx - sq, sy - sq, sq * 2, sq * 2))

        # speaker button
        if self.audio and self.audio.available:
            spx = sx + 2 * BTN_RADIUS + BTN_GAP
            spy = sy
            spk_base = accent if self._audio_popup_open else (55, 55, 65)
            _circle(spx, spy, BTN_RADIUS, "speaker", spk_base)
            self._draw_speaker_icon(spx, spy, BTN_RADIUS - max(1, BTN_RADIUS * 5 // 12), col=_on_bg(spk_base, strong=True))
            if self._audio_popup_open:
                self._draw_audio_popup(spx, spy)

        if settings.get("lyrics"):
            self._draw_lyrics(alpha)

        if self._album_menu_open:
            self._draw_album_menu()
        if self._art_release_picker is not None:
            self._draw_release_picker()
        self._draw_menu_toast()

    # ── draw: tracklist ───────────────────────────────────────────────────────

    def _draw_tracklist(self):
        # The tracklist always lives at y=TRACKLIST_ART_H to y=H.
        # The album art (drawn later) naturally covers only y=0..TRACKLIST_ART_H
        # because _album_y == _TL_ALBUM_Y when fully open.
        clip_h = H - TRACKLIST_ART_H - PROGRESS_H

        pygame.draw.rect(self.screen, tuple(int(c) for c in self._tl_bg_cur),
                          (0, TRACKLIST_ART_H, W, clip_h + PROGRESS_H))
        pygame.draw.line(self.screen, COL_SEP,
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
        row_bg     = tuple(int(c) for c in self._tl_bg_cur)
        if is_playing:
            row_bg = tuple(min(255, int(c * 2.2)) for c in self._tl_bg_cur)
            pygame.draw.rect(self.screen, row_bg, (0, y, W, TRACK_ROW_H))

        col_title = _on_bg(row_bg, strong=True)
        col_dim   = _on_bg(row_bg, strong=False)

        num = str(track.get("track", idx + 1) or idx + 1).split("/")[0].zfill(2)
        sn  = _render_text(self._f_track_sm, num, col_dim)
        self.screen.blit(sn, (TRACK_PAD, y + (TRACK_ROW_H - sn.get_height()) // 2))

        raw_dur = track.get("duration") or track.get("time", 0)
        dur_s   = int(float(raw_dur or 0))
        dur_str = f"{dur_s // 60}:{dur_s % 60:02d}"
        sd = _render_text(self._f_track_sm, dur_str, col_dim)
        self.screen.blit(sd, (W - TRACK_PAD - sd.get_width(),
                                y + (TRACK_ROW_H - sd.get_height()) // 2))

        if not is_playing:
            col_title = COL_TRACK_NORMAL

        title   = track.get("title") or track.get("name") or "—"
        title_x = TRACK_PAD + sn.get_width() + TRACK_PAD * 3 // 4
        title_w = W - title_x - sd.get_width() - TRACK_PAD * 2
        st = _render_text(self._f_track, title, col_title, title_w)
        self.screen.blit(st, (title_x, y + (TRACK_ROW_H - st.get_height()) // 2))

        pygame.draw.line(self.screen, COL_SEP,
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

        pfg = tuple(int(c) for c in self._accent_cur)
        if self._scrub_active:
            bar_h = max(PROGRESS_H * 3, 12)
            cr = bar_h // 2
            by2 = y - bar_h + PROGRESS_H
            pygame.draw.rect(self.screen, COL_PROGRESS_BG, (0, by2, W, bar_h))
            fw = int(W * self._scrub_frac)
            if fw > 0:
                pygame.draw.rect(self.screen, pfg, (0, by2, fw, bar_h),
                                 border_top_left_radius=0, border_bottom_left_radius=0,
                                 border_top_right_radius=cr, border_bottom_right_radius=cr)
        else:
            cr = PROGRESS_H // 2
            pygame.draw.rect(self.screen, COL_PROGRESS_BG, (0, y, W, PROGRESS_H))
            if dur > 0:
                fw = int(W * el / dur)
                if fw > 0:
                    pygame.draw.rect(self.screen, pfg, (0, y, fw, PROGRESS_H),
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
        self._cur_idx        = idx
        self._peeking        = False
        album                = self._albums[idx]
        self._tracks         = []
        self._ctrl_a         = 0.0
        self._ctrl_a_t       = 0.0
        self._album_y_t      = 0.0
        self._view           = View.ALBUM
        self._tl_scroll      = 0.0
        # Suppress poll elapsed updates until the new track is queued at position 0
        self._loading_album  = True
        self._elapsed_base   = 0.0
        self._elapsed_base_t = time.monotonic()

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
        if uri != self._art_album_uri:
            self._art_uri       = uri
            self._art_album_uri = uri
            self._load_art(uri)
            self._reset_art_carousel()
            # Show cached back/booklet instantly; fetching (if needed) happens
            # in _load() once the real track count is known.
            self._load_art_set(album, allow_fetch=False)

        # load tracks + pause at track 0 in background
        def _load():
            try:
                album_uri  = album.get("track_uri", "")
                is_spotify = album_uri.startswith("spotify:album:")
                autoplay   = settings.get("autoplay")

                if is_spotify and autoplay:
                    # Fast path: single RPC add → play, no pre-lookup needed.
                    # core.tracklist.add returns tl_tracks with full metadata so we
                    # parse tracks for the UI from that same response.
                    tracks = self.player.play_album_fast(album_uri)
                else:
                    tracks = self.player.get_album_tracks(album)

                album["tracks"] = tracks
                self._tracks = tracks
                # Extended artwork (back/booklet): fetch now that we know the
                # track count, unless it's already cached.  Populates the
                # carousel progressively as pages arrive.
                if album.get("track_uri", "") == self._art_album_uri:
                    self._load_art_set(album, allow_fetch=True)
                if tracks and settings.get("lyrics"):
                    self._prefetch_album_lyrics(tracks)
                if tracks:
                    t0 = tracks[0]
                    self.player.set_song_optimistic({
                        "title":  t0.get("title", ""),
                        "artist": t0.get("artist") or t0.get("albumartist", album["artist"]),
                        "album":  t0.get("album", album["name"]),
                        "file":   t0.get("file", ""),
                    })
                    if not is_spotify:
                        if autoplay:
                            self.player.play_album(tracks, 0)
                        else:
                            self.player.load_album(tracks, 0)
                    elif not autoplay:
                        self.player.load_album(tracks, 0)
                elif album_uri:
                    log.warning("No tracks returned for album %r (uri=%s)", album.get("name"), album_uri)
            except Exception:
                log.exception("_load() failed for album %r", album.get("name"))
            finally:
                self._loading_album  = False
                self._elapsed_base   = 0.0
                self._elapsed_base_t = time.monotonic()

        threading.Thread(target=_load, daemon=True).start()

    def _browse_view(self) -> View:
        return View.CAROUSEL if settings.get("carousel") else View.GRID

    def _go_grid(self):
        self._peeking  = False
        self.player.stop()
        self._cur_idx  = None
        self._tracks   = []
        self._art           = None
        self._art_uri       = ""
        self._art_album_uri = ""
        self._album_y_t = float(H)
        self._ctrl_a    = 0.0
        self._ctrl_a_t  = 0.0
        self._tl_bg_t   = COL_TL_BG
        self._accent_t  = COL_HIGHLIGHT
        self._view     = self._browse_view()

    def _peek_to_grid(self):
        """Swipe-down from ALBUM: slide art down so only the top strip shows at the bottom."""
        self._peeking   = True
        self._album_y_t = float(H - TRACKLIST_ART_H)   # 576 → top 144px of art at screen bottom
        self._ctrl_a    = 0.0
        self._ctrl_a_t  = 0.0
        self._view      = self._browse_view()

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
        self._refresh_art_counts()
        if self.bt and self.bt.available and not self._bt_refreshing:
            self._bt_refreshing   = True
            self._bt_last_refresh = pygame.time.get_ticks()
            def _refresh_bt():
                self._bt_powered    = self.bt.is_powered()
                self._bt_devices    = self.bt.get_devices()
                self._bt_refreshing = False
                self._dirty         = True
            threading.Thread(target=_refresh_bt, daemon=True).start()
        if self.wifi and self.wifi.available and not self._wifi_refreshing:
            self._wifi_refreshing = True
            def _refresh_wifi():
                self._wifi_powered   = self.wifi.is_enabled()
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
                if fname.endswith(".png") or fname.endswith(".jpg"):
                    os.remove(os.path.join(_THUMB_CACHE_DIR, fname))
        except Exception as e:
            log.warning("clear cache: %s", e)
        # Drop in-memory art cache and reset all album thumbs so they reload
        self._art_mem.clear()
        self._art     = None
        self._art_uri = ""
        self._art_album_uri = ""
        for album in self._albums:
            album["thumb"]         = None
            album["thumb_loading"] = False
        self._thumb_queued.clear()
        self._thumbs_pending = len(self._albums)
        for i in range(len(self._albums)):
            self._queue_thumb(i)
        self._cache_cleared_ms = pygame.time.get_ticks()
        self._dirty = True

    def _clear_instrumental_sentinels(self):
        count = 0
        try:
            for fname in os.listdir(_LYRICS_CACHE_DIR):
                if fname.endswith(".lrc"):
                    fpath = os.path.join(_LYRICS_CACHE_DIR, fname)
                    try:
                        if os.path.getsize(fpath) == 0:
                            os.remove(fpath)
                            count += 1
                    except Exception:
                        pass
        except Exception as e:
            log.warning("clear instrumental: %s", e)
        # also clear session cache entries that resolved to None (no lyrics)
        self._lyrics_cache = {k: v for k, v in self._lyrics_cache.items() if v is not None}
        # clear album index so the counter reflects the removed sentinels
        self._lyrics_album_index.clear()
        self._save_lyrics_index()
        log.info("clear instrumental: removed %d sentinel files", count)
        self._instrumental_cleared_ms = pygame.time.get_ticks()
        self._dirty = True

    def _load_lyrics_index(self) -> set[str]:
        try:
            with open(_LYRICS_INDEX_PATH) as f:
                return set(json.load(f))
        except Exception:
            return set()

    def _save_lyrics_index(self):
        try:
            os.makedirs(os.path.dirname(_LYRICS_INDEX_PATH), exist_ok=True)
            with open(_LYRICS_INDEX_PATH, "w") as f:
                json.dump(list(self._lyrics_album_index), f)
        except Exception as e:
            log.warning("lyrics index save: %s", e)

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

    def _prefetch_album_lyrics(self, tracks: list[dict]):
        """Fetch and cache lyrics for every track in *tracks* sequentially.

        Runs in one background thread so API requests are serialised and we
        don't hammer lrclib.net with a burst of parallel calls.  Already-
        cached tracks are skipped, so re-opening the same album is instant.
        Opening a new album increments _prefetch_gen which causes any
        in-flight prefetch to stop after its current track finishes.
        """
        self._prefetch_gen += 1
        my_gen = self._prefetch_gen

        def _run():
            for track in tracks:
                if self._prefetch_gen != my_gen:
                    log.info("lyrics prefetch: cancelled (new album opened)")
                    return
                uri = track.get("file", "")
                if not uri or uri in self._lyrics_cache:
                    continue
                snap_song = {
                    "title":  track.get("title", ""),
                    "artist": track.get("artist", ""),
                    "album":  track.get("album", ""),
                    "file":   uri,
                }
                try:
                    text   = self._load_lyrics_for_uri(uri, song=snap_song, status={})
                    parsed = self._parse_lyrics(text) if text else None
                except Exception:
                    log.exception("lyrics prefetch failed for %s", uri)
                    parsed = None
                self._lyrics_cache[uri] = parsed
                # If this track is currently playing, apply immediately.
                if self._lyrics_uri == uri and not self._lyrics_loading:
                    self._lyrics_parsed = parsed
                    self._dirty = True
        threading.Thread(target=_run, daemon=True).start()

    def _start_lyrics_bulk_cache(self):
        """Fetch and cache lyrics for every track in the entire library."""
        self._lyrics_bulk_progress = (0, 0)
        self._dirty = True

        def _run():
            albums = list(self._albums)
            total  = len(albums)
            for i, album in enumerate(albums):
                if self._lyrics_bulk_progress is None:
                    return   # cancelled — don't mark this album as complete
                self._lyrics_bulk_progress = (i, total)
                self._dirty = True
                tracks = album.get("tracks") or []
                if not tracks:
                    try:
                        tracks = self.player.get_album_tracks(album)
                        album["tracks"] = tracks
                    except Exception:
                        log.exception("bulk lyrics: failed loading %s", album.get("name"))
                        tracks = []
                for track in tracks:
                    if self._lyrics_bulk_progress is None:
                        return   # cancelled mid-album
                    uri = track.get("file", "")
                    if uri and uri not in self._lyrics_cache:
                        snap = {
                            "title":  track.get("title", ""),
                            "artist": track.get("artist", ""),
                            "album":  track.get("album", ""),
                            "file":   uri,
                        }
                        try:
                            text   = self._load_lyrics_for_uri(uri, song=snap, status={})
                            parsed = self._parse_lyrics(text) if text else None
                        except Exception:
                            log.exception("bulk lyrics: failed for %s", uri)
                            parsed = None
                        self._lyrics_cache[uri] = parsed
                # album fully processed — mark it in the index
                album_uri = album.get("track_uri", "")
                if album_uri:
                    self._lyrics_album_index.add(album_uri)
                    self._save_lyrics_index()
                    self._dirty = True

            self._lyrics_bulk_progress = None
            self._dirty = True
            log.info("bulk lyrics: done — %d albums", total)

        threading.Thread(target=_run, daemon=True).start()

    # ── bulk extended-artwork fetch ───────────────────────────────────────────

    def _refresh_art_counts(self):
        """Background-count how many albums per library already have artwork."""
        def _bg():
            for lib in ("local", "spotify"):
                try:
                    albums = self.player.get_albums(lib) or []
                    done   = sum(1 for a in albums
                                 if self._artwork.is_done(a.get("track_uri", "")))
                    self._art_lib_counts[lib] = (done, len(albums))
                    self._dirty = True
                except Exception as e:
                    log.debug("art counts %s: %s", lib, e)
        threading.Thread(target=_bg, daemon=True).start()

    def _start_art_bulk_cache(self, library: str):
        """Fetch extended artwork for every album in *library* ('local'|'spotify')."""
        if self._art_bulk_progress is not None:
            return   # one bulk run at a time (shared MusicBrainz rate limit)
        self._art_bulk_progress = (0, 0, library)
        self._dirty = True

        def _run():
            try:
                albums = self.player.get_albums(library) or []
            except Exception:
                log.exception("bulk art: failed listing %s", library)
                albums = []
            total = len(albums)
            for i, album in enumerate(albums):
                prog = self._art_bulk_progress
                if prog is None or prog[2] != library:
                    return   # cancelled
                self._art_bulk_progress = (i, total, library)
                self._dirty = True
                uri = album.get("track_uri", "")
                if uri and not self._artwork.is_done(uri):
                    tracks = album.get("tracks") or []
                    tc = len(tracks)
                    try:
                        self._artwork.fetch(uri, album.get("artist", ""),
                                            album.get("name", ""), tc)
                    except Exception:
                        log.exception("bulk art: failed for %s", album.get("name"))
            self._art_bulk_progress = None
            self._refresh_art_counts()
            self._dirty = True
            log.info("bulk artwork (%s): done — %d albums", library, total)

        threading.Thread(target=_run, daemon=True).start()

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

    def _lrc_disk_path(self, uri: str, path: str | None) -> str | None:
        """Return the .lrc file path to use for disk caching.

        Local tracks: sidecar next to the audio file.
        Spotify / other remote tracks: ~/.cache/album2/lyrics/{track_id}.lrc
        """
        if path and not uri.startswith("spotify:") and not uri.startswith("http"):
            return os.path.splitext(path)[0] + ".lrc"
        if uri.startswith("spotify:track:"):
            track_id = uri.split(":")[-1]
            return os.path.join(_LYRICS_CACHE_DIR, f"spotify_track_{track_id}.lrc")
        return None

    def _save_lrc(self, lrc_path: str, text: str):
        """Write *text* to *lrc_path*, creating parent dirs as needed."""
        try:
            os.makedirs(os.path.dirname(lrc_path), exist_ok=True)
            with open(lrc_path, "w", encoding="utf-8") as fh:
                fh.write(text)
            log.info("lyrics: saved to %s", lrc_path)
        except Exception as e:
            log.warning("lyrics: could not save lrc: %s", e)

    @staticmethod
    def _embedded_lyrics(path: str | None) -> str | None:
        """Non-empty embedded lyrics tag from a local FLAC file, or None.

        A tag that exists but is blank does not count — callers should fall
        back to external lookups in that case rather than showing nothing.
        """
        if not path or not path.lower().endswith(".flac"):
            return None
        try:
            from mutagen.flac import FLAC
            f = FLAC(path)
            for tag in ("LYRICS", "UNSYNCEDLYRICS", "lyrics", "unsyncedlyrics"):
                if tag in f:
                    text = "\n".join(f[tag]).strip()
                    if text:
                        log.info("lyrics: found embedded tag %r", tag)
                        return text
                    log.info("lyrics: embedded tag %r present but empty", tag)
        except ImportError:
            log.info("lyrics: mutagen not available, skipping embedded")
        except Exception as e:
            log.warning("lyrics: embedded load error: %s", e)
        return None

    def _load_lyrics_for_uri(self, uri: str,
                             song: dict | None = None,
                             status: dict | None = None,
                             force: bool = False) -> str | None:
        """Return lyrics text for *uri*, preferring embedded then cache then
        online sources — unless *force*, which skips straight to a fresh
        online lookup (used by the explicit "Download lyrics" menu action)."""
        if song is None:
            song = self._song
        if status is None:
            status = self._status
        path     = self._resolve_music_path(uri)
        lrc_path = self._lrc_disk_path(uri, path)
        log.info("lyrics: loading for uri=%s lrc_path=%s force=%s", uri, lrc_path, force)

        if not force:
            # 1. embedded tags (FLAC via mutagen)
            embedded = self._embedded_lyrics(path)
            if embedded:
                return embedded

            # 2. disk-cached .lrc file (sidecar for local, cache dir for Spotify)
            #    An empty file is a sentinel meaning "instrumental / no lyrics" —
            #    written below after all sources are exhausted.
            if lrc_path and os.path.exists(lrc_path):
                try:
                    with open(lrc_path, encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                    if not content:
                        log.info("lyrics: instrumental sentinel at %s — skipping lookups", lrc_path)
                        return ""
                    log.info("lyrics: found cached .lrc at %s", lrc_path)
                    return content
                except Exception as e:
                    log.warning("lyrics: lrc read error: %s", e)
        else:
            log.info("lyrics: force download — skipping embedded/cache, querying online sources")

        # 3. lrclib.net (synced LRC with timestamps — preferred for auto-scroll)
        artist = (song.get("artist") or song.get("albumartist") or "").strip()
        title  = (song.get("title") or "").strip()
        album  = (song.get("album") or "").strip()
        log.info("lyrics: song metadata — artist=%r title=%r album=%r", artist, title, album)
        if artist and title:
            try:
                import urllib.parse
                req = __import__("requests")
                time_str = status.get("time", "")
                tparts   = time_str.split(":")
                duration = int(float(tparts[1])) if len(tparts) >= 2 else 0
                params: dict = {"artist_name": artist, "track_name": title}
                if album:
                    params["album_name"] = album
                if duration:
                    params["duration"] = duration
                url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(params)
                log.info("lyrics: trying lrclib.net get — %s", url)
                r = req.get(url, timeout=10)
                log.info("lyrics: lrclib.net get status %s", r.status_code)
                lrclib_data = None
                if r.status_code == 200:
                    lrclib_data = r.json()
                elif r.status_code == 404:
                    s_url = ("https://lrclib.net/api/search?"
                             + urllib.parse.urlencode({"artist_name": artist,
                                                       "track_name":  title}))
                    log.info("lyrics: lrclib.net 404, trying search — %s", s_url)
                    sr = req.get(s_url, timeout=10)
                    log.info("lyrics: lrclib.net search status %s", sr.status_code)
                    if sr.status_code == 200:
                        results = sr.json()
                        log.info("lyrics: lrclib.net search returned %d results", len(results))
                        for res in results:
                            if (res.get("syncedLyrics") or "").strip():
                                lrclib_data = res
                                log.info("lyrics: using search result id=%s", res.get("id"))
                                break
                        if lrclib_data is None:
                            for res in results:
                                if (res.get("plainLyrics") or "").strip():
                                    lrclib_data = res
                                    log.info("lyrics: using search result (plain) id=%s", res.get("id"))
                                    break
                if lrclib_data is not None:
                    if lrclib_data.get("instrumental"):
                        log.info("lyrics: lrclib.net says instrumental — writing sentinel")
                        if lrc_path:
                            self._save_lrc(lrc_path, "")
                        return ""
                    else:
                        synced = (lrclib_data.get("syncedLyrics") or "").strip()
                        if synced:
                            log.info("lyrics: lrclib.net synced LRC (%d chars)", len(synced))
                            if lrc_path:
                                self._save_lrc(lrc_path, synced)
                            return synced
                        plain = (lrclib_data.get("plainLyrics") or "").strip()
                        if plain:
                            log.info("lyrics: lrclib.net plain text (%d chars)", len(plain))
                            if lrc_path:
                                self._save_lrc(lrc_path, plain)
                            return plain
                        log.info("lyrics: lrclib.net both fields empty")
                else:
                    log.info("lyrics: lrclib.net no result found")
            except Exception as e:
                log.warning("lyrics: lrclib.net fetch failed: %s", e)
        else:
            log.info("lyrics: missing artist or title, skipping API lookups")

        # 4. lyrics.ovh (plain text fallback)
        if artist and title:
            try:
                import urllib.parse
                url = ("https://api.lyrics.ovh/v1/"
                       + urllib.parse.quote(artist) + "/"
                       + urllib.parse.quote(title))
                log.info("lyrics: trying lyrics.ovh — %s", url)
                r = __import__("requests").get(url, timeout=10)
                log.info("lyrics: lyrics.ovh status %s", r.status_code)
                if r.status_code == 200:
                    text = r.json().get("lyrics", "").strip()
                    if text:
                        log.info("lyrics: lyrics.ovh plain text (%d chars)", len(text))
                        if lrc_path:
                            self._save_lrc(lrc_path, text)
                        return text
                    log.info("lyrics: lyrics.ovh 200 but lyrics field empty")
                else:
                    log.info("lyrics: lyrics.ovh returned %s", r.status_code)
            except Exception as e:
                log.warning("lyrics: lyrics.ovh fetch failed: %s", e)

        log.info("lyrics: all sources exhausted, no lyrics found — writing instrumental sentinel")
        if lrc_path:
            self._save_lrc(lrc_path, "")
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

        # current logical line — integer only (no fractional interpolation)
        if times:
            idx = bisect.bisect_right(times, el) - 1
            idx = max(0, min(total - 1, idx))

            # Target scroll: put the active line ~30% from the top
            target = float(first_visual.get(idx, 0)) - n_vis_v * 0.3
            target = max(0.0, min(float(max(0, total_vis - n_vis_v)), target))

            now_t = time.monotonic()
            if idx != self._lyrics_prev_idx:
                self._lyrics_prev_idx   = idx
                self._lyrics_target_vis = target
                if self._lyrics_anim_t == 0.0:
                    # First draw after lyrics loaded: snap to position, no animation.
                    self._lyrics_anim_vis = target
                self._lyrics_anim_t = now_t

            # Ease-out toward target (only animate when target changes).
            diff = self._lyrics_target_vis - self._lyrics_anim_vis
            if abs(diff) > 0.02:
                dt_a = min(0.1, now_t - self._lyrics_anim_t)
                self._lyrics_anim_t   = now_t
                self._lyrics_anim_vis += diff * min(1.0, dt_a * 18.0)
                self._dirty = True
            else:
                self._lyrics_anim_vis = self._lyrics_target_vis

            float_start = self._lyrics_anim_vis
            focus_li    = idx
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
        a_dim   = alpha * 0.60 / 255
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
            or self._settings_clear_instrumental_btn_at(pos)
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
        rect(0, bar_y - SCRUB_LEEWAY // 2, W, SCRUB_LEEWAY // 2 + PROGRESS_H + SCRUB_LEEWAY // 4)

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
                ctrl_cy = H // 2
                circle(W // 2,     ctrl_cy, CTRL_ICON_LG + 10)   # play
                circle(W // 6,     ctrl_cy, CTRL_ICON_SM + 10)   # prev
                circle(5 * W // 6, ctrl_cy, CTRL_ICON_SM + 10)   # next
                # lyrics drag zone
                if (self._lyrics_parsed and self._lyrics_parsed[1] is None):
                    y_lyr = H // 2 + CTRL_BAR_H // 2 + CTRL_TEXT_GAP * 2
                    rect(0, y_lyr, W, H - y_lyr - PROGRESS_H)

        elif self._view == View.SETTINGS:
            circle(bx, by, BTN_RADIUS + 10)
            bt_rows   = self._bt_row_count()
            wifi_rows = self._wifi_row_count()
            total_rows = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 10
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
            _dbg_cell_h = _CELL_W + (GRID_TEXT_H if settings.get("grid_labels") else 0)
            _dbg_row_h  = _dbg_cell_h + GRID_PAD
            for i in range(len(self._albums)):
                row = i // GRID_COLS
                c   = i % GRID_COLS
                x   = GRID_PAD + c * (_CELL_W + GRID_PAD)
                y   = GRID_PAD + row * _dbg_row_h - int(self._grid_scroll)
                if y + _dbg_cell_h > 0 and y < H:
                    rect(x, y, _CELL_W, _dbg_cell_h)
            if self._peeking:
                rect(0, H - TRACKLIST_ART_H, W, TRACKLIST_ART_H)   # peek strip → unpeek

        # current touch position
        if self._t_start_pos is not None and self._t_prev_pos is not None:
            tx, ty = self._t_prev_pos
            pygame.gfxdraw.filled_circle(self.screen, tx, ty, 28, (255, 0, 0, 80))
            pygame.gfxdraw.aacircle(self.screen, tx, ty, 28, (255, 0, 0, 200))

    def _draw_speaker_icon(self, cx, cy, r, col=None):
        if col is None:
            col = _BTN_ICON_COL

        # Total icon spans ~1.5r wide; shift origin left so it's centred on (cx,cy)
        ox = cx - r // 5          # visual centre correction

        # Speaker body: trapezoid — small end left, cone flares right
        bh_sm = max(2, r * 3 // 10)   # half-height of small (left) end
        bw    = max(2, r * 11 // 20)  # horizontal width of body
        body_pts = [
            (ox - bw,  cy - bh_sm),
            (ox,       cy - r),
            (ox,       cy + r),
            (ox - bw,  cy + bh_sm),
        ]
        pygame.gfxdraw.filled_polygon(self.screen, body_pts, col)
        pygame.gfxdraw.aapolygon(self.screen, body_pts, col)

        # Sound waves — polygon rings so no moiré/aliasing
        wave_x = ox   # arcs centred at the cone tip
        thick  = max(1, r // 7)
        for scale in (0.52, 0.88):
            ro = int(r * scale)
            ri = max(1, ro - thick)
            a1, a2, n = -math.pi / 3, math.pi / 3, 16
            angles = [a1 + (a2 - a1) * i / (n - 1) for i in range(n)]
            outer = [(wave_x + int(ro * math.cos(a)), cy + int(ro * math.sin(a))) for a in angles]
            inner = [(wave_x + int(ri * math.cos(a)), cy + int(ri * math.sin(a))) for a in reversed(angles)]
            pygame.gfxdraw.filled_polygon(self.screen, outer + inner, col)
            pygame.gfxdraw.aapolygon(self.screen, outer + inner, col)

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
            ry           = py + i * row_h
            bt_conn      = sink.get("bt_connected", True)
            busy_key     = sink["id"] or sink.get("bt_addr")
            busy         = self._audio_busy_id == busy_key
            if busy:
                col = COL_TRACK_NUM
            elif not bt_conn:
                col = COL_TEXT_ALBUM   # dimmed — not connected
            else:
                col = COL_TRACK_NORMAL
            sl = _render_text(self._f_track, sink["name"], col, pw - BTN_MARGIN * 2 - dot_r * 3)
            self.screen.blit(sl, (px + BTN_MARGIN, ry + (row_h - sl.get_height()) // 2))
            dot_cx = px + pw - BTN_MARGIN - dot_r
            dot_cy = ry + row_h // 2
            if busy:
                pass  # no dot while connecting
            elif sink["active"]:
                accent_col = tuple(int(c) for c in self._accent_cur)
                pygame.gfxdraw.filled_circle(self.screen, dot_cx, dot_cy, dot_r, accent_col)
                pygame.gfxdraw.aacircle(self.screen, dot_cx, dot_cy, dot_r, accent_col)
            elif not bt_conn:
                pygame.gfxdraw.aacircle(self.screen, dot_cx, dot_cy, dot_r, COL_SEP)
            if i < len(self._audio_sinks) - 1:
                pygame.draw.line(self.screen, COL_SEP,
                                   (px, ry + row_h - 1), (px + pw, ry + row_h - 1))

    def _draw_gear_icon(self, cx, cy, r, col=None, hole_col=COL_HIGHLIGHT):
        if col is None:
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
        pygame.gfxdraw.filled_circle(self.screen, cx, cy, hole, hole_col)
        pygame.gfxdraw.aacircle(self.screen, cx, cy, hole, hole_col)

    def _draw_menu_icon(self, cx, cy, r, col=None):
        """Hamburger icon (three lines) for the artwork/lyrics action menu."""
        if col is None:
            col = _BTN_ICON_COL
        w     = int(r * 1.5)
        thick = max(2, r // 4)
        gap   = max(3, r * 2 // 3)
        for dy in (-gap, 0, gap):
            y0 = cy + dy - thick // 2
            pygame.draw.rect(self.screen, col, (cx - w // 2, y0, w, thick),
                             border_radius=thick // 2)

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
        pygame.draw.line(self.screen, COL_SEP, (0, sep_y), (W, sep_y))

        # compute total content height from row count (independent of scroll)
        bt_rows   = self._bt_row_count()
        wifi_rows = self._wifi_row_count()
        total_rows = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 10  # SYSTEM(hdr+cache+instrumental+debug) + TOUCH(hdr+cal+reset) + POWER(hdr+restart+shutdown)
        content_h  = total_rows * TRACK_ROW_H + BTN_MARGIN * 2
        max_scroll  = max(0.0, float(content_h - clip_h))
        self._settings_scroll = max(0.0, min(self._settings_scroll, max_scroll))
        self._settings_vel    = 0.0 if self._settings_scroll >= max_scroll else self._settings_vel

        scroll   = int(self._settings_scroll)
        old_clip = self.screen.get_clip()
        self.screen.set_clip(0, clip_y, W, H - clip_y)   # clip top only, open bottom

        y = clip_y - scroll   # content origin, offset by scroll
        deferred_dropdown = None   # drawn after all rows so it's on top
        for key, label in _SETTINGS_ITEMS:
            if key is None:
                # section header
                sh = _render_text(self._f_track_sm, label, COL_TEXT_ALBUM)
                self.screen.blit(sh, (BTN_MARGIN, y + (TRACK_ROW_H - sh.get_height()) // 2))
            elif key in _SETTINGS_SELECTORS:
                sl = _render_text(self._f_track, label, COL_TRACK_NORMAL)
                self.screen.blit(sl, (BTN_MARGIN, y + (TRACK_ROW_H - sl.get_height()) // 2))
                cur  = str(settings.get(key)).capitalize()
                open_ = self._settings_dropdown == key
                pt   = _render_text(self._f_track_sm, cur + ("  ▲" if open_ else "  ▼"), COL_BG)
                pw   = pt.get_width() + 24
                ph   = TOGGLE_H
                px   = W - BTN_MARGIN - pw
                py2  = y + (TRACK_ROW_H - ph) // 2
                pygame.draw.rect(self.screen, COL_HIGHLIGHT, (px, py2, pw, ph),
                                 border_radius=ph // 2)
                self.screen.blit(pt, (px + (pw - pt.get_width()) // 2,
                                      py2 + (ph - pt.get_height()) // 2))
                if open_:
                    opts    = _SETTINGS_SELECTORS[key]
                    cur_low = str(settings.get(key)).lower()
                    dp_w    = max(pw + 40, 160)
                    dp_x    = W - BTN_MARGIN - dp_w
                    dp_y    = y + TRACK_ROW_H
                    deferred_dropdown = (dp_x, dp_y, dp_w, opts, cur_low)
            elif key == "lyrics_cache_all":
                bulk      = self._lyrics_bulk_progress
                cached_n  = sum(1 for a in self._albums
                                if a.get("track_uri", "") in self._lyrics_album_index)
                total_n   = len(self._albums)
                all_done  = total_n > 0 and cached_n >= total_n
                if bulk is not None:
                    done, total = bulk
                    lbl = f"Caching lyrics… {done}/{total}"
                    lbl_col = COL_TRACK_NUM
                elif all_done:
                    lbl = "All lyrics cached"
                    lbl_col = COL_TEXT_ALBUM
                else:
                    lbl = "Cache all lyrics"
                    lbl_col = COL_HIGHLIGHT
                sl = _render_text(self._f_track, lbl, lbl_col)
                self.screen.blit(sl, (BTN_MARGIN, y + (TRACK_ROW_H - sl.get_height()) // 2))
                if bulk is not None:
                    done, total = bulk
                    bar_w = int((W - BTN_MARGIN * 2) * done / max(1, total))
                    pygame.draw.rect(self.screen, COL_HIGHLIGHT,
                                     (BTN_MARGIN, y + TRACK_ROW_H - 3, bar_w, 3))
                    stop_s = _render_text(self._f_track_sm, "Stop", COL_HIGHLIGHT)
                    self.screen.blit(stop_s, (W - BTN_MARGIN - stop_s.get_width(),
                                              y + (TRACK_ROW_H - stop_s.get_height()) // 2))
                elif not all_done:
                    sub   = f"{cached_n} / {total_n} albums" if total_n > 0 else "may be slow"
                    sub_s = _render_text(self._f_track_sm, sub, COL_TEXT_ALBUM)
                    self.screen.blit(sub_s, (W - BTN_MARGIN - sub_s.get_width(),
                                             y + (TRACK_ROW_H - sub_s.get_height()) // 2))
            elif key in ("art_cache_local", "art_cache_spotify"):
                library  = "local" if key == "art_cache_local" else "spotify"
                bulk     = self._art_bulk_progress
                running  = bulk is not None and bulk[2] == library
                other    = bulk is not None and bulk[2] != library
                counts   = self._art_lib_counts.get(library)
                done_n, total_n = counts if counts else (0, 0)
                all_done = counts is not None and total_n > 0 and done_n >= total_n
                if running:
                    lbl, lbl_col = f"Fetching artwork… {bulk[0]}/{bulk[1]}", COL_TRACK_NUM
                elif all_done:
                    lbl, lbl_col = f"Artwork cached ({library})", COL_TEXT_ALBUM
                elif other:
                    lbl, lbl_col = label, COL_TRACK_NUM   # dimmed while other runs
                else:
                    lbl, lbl_col = label, COL_HIGHLIGHT
                sl = _render_text(self._f_track, lbl, lbl_col)
                self.screen.blit(sl, (BTN_MARGIN, y + (TRACK_ROW_H - sl.get_height()) // 2))
                if running:
                    bar_w = int((W - BTN_MARGIN * 2) * bulk[0] / max(1, bulk[1]))
                    pygame.draw.rect(self.screen, COL_HIGHLIGHT,
                                     (BTN_MARGIN, y + TRACK_ROW_H - 3, bar_w, 3))
                    stop_s = _render_text(self._f_track_sm, "Stop", COL_HIGHLIGHT)
                    self.screen.blit(stop_s, (W - BTN_MARGIN - stop_s.get_width(),
                                              y + (TRACK_ROW_H - stop_s.get_height()) // 2))
                elif not all_done and counts is not None:
                    sub_s = _render_text(self._f_track_sm, f"{done_n} / {total_n} albums",
                                         COL_TEXT_ALBUM)
                    self.screen.blit(sub_s, (W - BTN_MARGIN - sub_s.get_width(),
                                             y + (TRACK_ROW_H - sub_s.get_height()) // 2))
            else:
                sl = _render_text(self._f_track, label, COL_TRACK_NORMAL)
                self.screen.blit(sl, (BTN_MARGIN, y + (TRACK_ROW_H - sl.get_height()) // 2))
                self._draw_toggle(W - BTN_MARGIN - TOGGLE_W, y + (TRACK_ROW_H - TOGGLE_H) // 2, settings.get(key))
            pygame.draw.line(self.screen, COL_SEP,
                               (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
            y += TRACK_ROW_H

        if self.bt and self.bt.available:
            sh = _render_text(self._f_track_sm, "BLUETOOTH", COL_TEXT_ALBUM)
            self.screen.blit(sh, (BTN_MARGIN, y + (TRACK_ROW_H - sh.get_height()) // 2))
            self._draw_toggle(W - BTN_MARGIN - TOGGLE_W, y + (TRACK_ROW_H - TOGGLE_H) // 2, self._bt_powered)
            pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
            y += TRACK_ROW_H

            if self._bt_powered:
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
                    pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
                    y += TRACK_ROW_H

                sc = _render_text(self._f_track, "Search for new devices", COL_HIGHLIGHT)
                self.screen.blit(sc, (BTN_MARGIN, y + (TRACK_ROW_H - sc.get_height()) // 2))
                pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
                y += TRACK_ROW_H

        if self.wifi and self.wifi.available:
            sh = _render_text(self._f_track_sm, "WI-FI", COL_TEXT_ALBUM)
            self.screen.blit(sh, (BTN_MARGIN, y + (TRACK_ROW_H - sh.get_height()) // 2))
            self._draw_toggle(W - BTN_MARGIN - TOGGLE_W, y + (TRACK_ROW_H - TOGGLE_H) // 2, self._wifi_powered)
            pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
            y += TRACK_ROW_H

            # signal bar geometry
            bar_w  = max(3, TRACK_ROW_H // 8)
            bar_gap = max(1, bar_w // 3)
            bar_max = TRACK_ROW_H * 2 // 5
            n_bars  = 4
            sig_w   = n_bars * bar_w + (n_bars - 1) * bar_gap
            lock_w  = max(10, TRACK_ROW_H // 4)

            for net in (self._wifi_networks if self._wifi_powered else []):
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
                pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
                y += TRACK_ROW_H

        # ── SYSTEM ────────────────────────────────────────────────────────────
        sh = _render_text(self._f_track_sm, "SYSTEM", COL_TEXT_ALBUM)
        self.screen.blit(sh, (BTN_MARGIN, y + (TRACK_ROW_H - sh.get_height()) // 2))
        pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        now_ms = pygame.time.get_ticks()
        recently_cleared = self._cache_cleared_ms and now_ms - self._cache_cleared_ms < 2000
        cc_label = "Cache cleared" if recently_cleared else "Clear cache"
        cc_col   = COL_TEXT_ALBUM if recently_cleared else COL_HIGHLIGHT
        cc = _render_text(self._f_track, cc_label, cc_col)
        self.screen.blit(cc, (BTN_MARGIN, y + (TRACK_ROW_H - cc.get_height()) // 2))
        pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        recently_instr = self._instrumental_cleared_ms and now_ms - self._instrumental_cleared_ms < 2000
        instr_label = "Instrumental tags cleared" if recently_instr else "Clear instrumental tags"
        instr_col   = COL_TEXT_ALBUM if recently_instr else COL_HIGHLIGHT
        instr_s = _render_text(self._f_track, instr_label, instr_col)
        self.screen.blit(instr_s, (BTN_MARGIN, y + (TRACK_ROW_H - instr_s.get_height()) // 2))
        pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        dbg_s = _render_text(self._f_track, "Debug mode", COL_TRACK_NORMAL)
        self.screen.blit(dbg_s, (BTN_MARGIN, y + (TRACK_ROW_H - dbg_s.get_height()) // 2))
        self._draw_toggle(W - BTN_MARGIN - TOGGLE_W, y + (TRACK_ROW_H - TOGGLE_H) // 2, settings.get("debug"))
        pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        # ── TOUCH ─────────────────────────────────────────────────────────────
        sh = _render_text(self._f_track_sm, "TOUCH", COL_TEXT_ALBUM)
        self.screen.blit(sh, (BTN_MARGIN, y + (TRACK_ROW_H - sh.get_height()) // 2))
        pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        cal_s = _render_text(self._f_track, "Calibrate touch", COL_HIGHLIGHT)
        self.screen.blit(cal_s, (BTN_MARGIN, y + (TRACK_ROW_H - cal_s.get_height()) // 2))
        pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        is_default = (settings.get("cal_sx") == 1.0 and settings.get("cal_ox") == 0.0
                      and settings.get("cal_sy") == 1.0 and settings.get("cal_oy") == 0.0)
        rst_col = COL_TEXT_ALBUM if is_default else COL_HIGHLIGHT
        rst_s   = _render_text(self._f_track, "Reset calibration", rst_col)
        self.screen.blit(rst_s, (BTN_MARGIN, y + (TRACK_ROW_H - rst_s.get_height()) // 2))
        pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        # ── POWER ─────────────────────────────────────────────────────────────
        sh = _render_text(self._f_track_sm, "POWER", COL_TEXT_ALBUM)
        self.screen.blit(sh, (BTN_MARGIN, y + (TRACK_ROW_H - sh.get_height()) // 2))
        pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        rb_s = _render_text(self._f_track, "Restart", (220, 160, 60))
        self.screen.blit(rb_s, (BTN_MARGIN, y + (TRACK_ROW_H - rb_s.get_height()) // 2))
        pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))
        y += TRACK_ROW_H

        sd_s = _render_text(self._f_track, "Shut down", (220, 80, 80))
        self.screen.blit(sd_s, (BTN_MARGIN, y + (TRACK_ROW_H - sd_s.get_height()) // 2))
        pygame.draw.line(self.screen, COL_SEP, (0, y + TRACK_ROW_H - 1), (W, y + TRACK_ROW_H - 1))

        if deferred_dropdown:
            dp_x, dp_y, dp_w, opts, cur_low = deferred_dropdown
            dp_h = len(opts) * TRACK_ROW_H
            pygame.draw.rect(self.screen, COL_CELL_BG,
                             (dp_x, dp_y, dp_w, dp_h), border_radius=6)
            pygame.draw.rect(self.screen, COL_SEP,
                             (dp_x, dp_y, dp_w, dp_h), 1, border_radius=6)
            for oi, opt in enumerate(opts):
                oy  = dp_y + oi * TRACK_ROW_H
                sel = opt.lower() == cur_low
                oc  = COL_HIGHLIGHT if sel else COL_TRACK_NORMAL
                ot  = _render_text(self._f_track, opt, oc)
                self.screen.blit(ot, (dp_x + 16, oy + (TRACK_ROW_H - ot.get_height()) // 2))
                if oi:
                    pygame.draw.line(self.screen, COL_SEP, (dp_x, oy), (dp_x + dp_w, oy))

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
        pygame.draw.line(self.screen, COL_SEP, (0, sep_y), (W, sep_y))

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
                pygame.draw.line(self.screen, COL_SEP,
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

    def _bt_row_count(self) -> int:
        if not (self.bt and self.bt.available):
            return 0
        return (2 + len(self._bt_devices)) if self._bt_powered else 1

    def _wifi_row_count(self) -> int:
        if not (self.wifi and self.wifi.available):
            return 0
        return (1 + len(self._wifi_networks)) if self._wifi_powered else 1

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
        if not (self.bt and self.bt.available and self._bt_devices and self._bt_powered):
            return None
        base = len(_SETTINGS_ITEMS) + 1   # +1 for BT section header
        for i, dev in enumerate(self._bt_devices):
            if self._settings_row_hit(pos, base + i):
                return dev
        return None

    def _settings_scan_btn_at(self, pos) -> bool:
        if not (self.bt and self.bt.available and self._bt_powered):
            return False
        row = len(_SETTINGS_ITEMS) + 1 + len(self._bt_devices)
        return self._settings_row_hit(pos, row)

    def _settings_bt_power_toggle_at(self, pos) -> bool:
        if not (self.bt and self.bt.available):
            return False
        row = len(_SETTINGS_ITEMS)   # BT header row index
        ry = self._settings_row_y(row)
        tx = W - BTN_MARGIN - TOGGLE_W
        ty = ry + (TRACK_ROW_H - TOGGLE_H) // 2
        return (tx <= pos[0] <= tx + TOGGLE_W
                and ty <= pos[1] <= ty + TOGGLE_H)

    def _settings_wifi_power_toggle_at(self, pos) -> bool:
        if not (self.wifi and self.wifi.available):
            return False
        row = len(_SETTINGS_ITEMS) + self._bt_row_count()   # WiFi header row index
        ry = self._settings_row_y(row)
        tx = W - BTN_MARGIN - TOGGLE_W
        ty = ry + (TRACK_ROW_H - TOGGLE_H) // 2
        return (tx <= pos[0] <= tx + TOGGLE_W
                and ty <= pos[1] <= ty + TOGGLE_H)

    def _wifi_network_at(self, pos) -> dict | None:
        if not (self.wifi and self.wifi.available and self._wifi_networks and self._wifi_powered):
            return None
        base = len(_SETTINGS_ITEMS) + self._bt_row_count() + 1   # +1 for wifi header
        for i, net in enumerate(self._wifi_networks):
            if self._settings_row_hit(pos, base + i):
                return net
        return None

    def _settings_clear_cache_btn_at(self, pos) -> bool:
        bt_rows   = self._bt_row_count()
        wifi_rows = self._wifi_row_count()
        row = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 1   # +0 = SYSTEM header
        return self._settings_row_hit(pos, row)

    def _settings_clear_instrumental_btn_at(self, pos) -> bool:
        bt_rows   = self._bt_row_count()
        wifi_rows = self._wifi_row_count()
        row = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 2
        return self._settings_row_hit(pos, row)

    def _settings_debug_btn_at(self, pos) -> bool:
        bt_rows   = self._bt_row_count()
        wifi_rows = self._wifi_row_count()
        row = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 3
        return self._settings_row_hit(pos, row)

    def _settings_calibrate_btn_at(self, pos) -> bool:
        bt_rows   = self._bt_row_count()
        wifi_rows = self._wifi_row_count()
        row = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 5   # +4 = TOUCH header
        return self._settings_row_hit(pos, row)

    def _settings_reset_cal_btn_at(self, pos) -> bool:
        bt_rows   = self._bt_row_count()
        wifi_rows = self._wifi_row_count()
        row = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 6
        return self._settings_row_hit(pos, row)

    def _settings_restart_btn_at(self, pos) -> bool:
        bt_rows   = self._bt_row_count()
        wifi_rows = self._wifi_row_count()
        row = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 8   # +7 = POWER header
        return self._settings_row_hit(pos, row)

    def _settings_shutdown_btn_at(self, pos) -> bool:
        bt_rows   = self._bt_row_count()
        wifi_rows = self._wifi_row_count()
        row = len(_SETTINGS_ITEMS) + bt_rows + wifi_rows + 9
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
        pygame.draw.line(self.screen, COL_SEP,
                           (px, sy + TRACK_ROW_H - 1), (px + pw, sy + TRACK_ROW_H - 1))
        sy += TRACK_ROW_H

        # row 1: Connect / Disconnect
        action1 = "Disconnect" if dev["connected"] else "Connect"
        s1 = _render_text(self._f_track, action1, COL_TRACK_NORMAL)
        self.screen.blit(s1, (px + (pw - s1.get_width()) // 2,
                               sy + (TRACK_ROW_H - s1.get_height()) // 2))
        pygame.draw.line(self.screen, COL_SEP,
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
        pygame.draw.line(self.screen, COL_SEP,
                           (px, sy + TRACK_ROW_H - 1), (px + pw, sy + TRACK_ROW_H - 1))
        sy += TRACK_ROW_H

        action1 = "Disconnect" if net["connected"] else "Connect"
        s1 = _render_text(self._f_track, action1, COL_TRACK_NORMAL)
        self.screen.blit(s1, (px + (pw - s1.get_width()) // 2,
                               sy + (TRACK_ROW_H - s1.get_height()) // 2))
        pygame.draw.line(self.screen, COL_SEP,
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
            pygame.draw.line(self.screen, COL_SEP,
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
                self.wifi.forget(n)
                self._wifi_networks = self.wifi.get_networks()
                self._dirty = True
            threading.Thread(target=_do, daemon=True).start()

    # ── album action menu (download/clear artwork & lyrics) ────────────────────

    def _album_menu_rect(self):
        row_h = TRACK_ROW_H
        pw    = min(W - BTN_MARGIN * 2, max(280, W * 3 // 5))
        ph    = len(_ALBUM_MENU_ITEMS) * row_h
        px    = W - BTN_MARGIN - pw
        py    = BTN_MARGIN + 2 * BTN_RADIUS + 8
        return px, py, pw, ph

    def _draw_album_menu(self):
        px, py, pw, ph = self._album_menu_rect()
        pygame.draw.rect(self.screen, COL_CELL_BG, (px, py, pw, ph), border_radius=8)
        pygame.draw.rect(self.screen, COL_SEP,     (px, py, pw, ph), width=1, border_radius=8)

        row_h = TRACK_ROW_H
        for i, (key, label) in enumerate(_ALBUM_MENU_ITEMS):
            ry  = py + i * row_h
            col = (200, 80, 80) if key.startswith("clear") else COL_TRACK_NORMAL
            sl  = _render_text(self._f_track, label, col, pw - BTN_MARGIN * 2)
            self.screen.blit(sl, (px + BTN_MARGIN, ry + (row_h - sl.get_height()) // 2))
            if i < len(_ALBUM_MENU_ITEMS) - 1:
                pygame.draw.line(self.screen, COL_SEP, (px, ry + row_h - 1), (px + pw, ry + row_h - 1))

    def _album_menu_item_at(self, pos) -> str | None:
        px, py, pw, ph = self._album_menu_rect()
        if not (px <= pos[0] < px + pw and py <= pos[1] < py + ph):
            return None
        row = (pos[1] - py) // TRACK_ROW_H
        return _ALBUM_MENU_ITEMS[row][0] if 0 <= row < len(_ALBUM_MENU_ITEMS) else None

    def _exec_album_menu_tap(self, action: str):
        if action == "dl_art":
            self._open_release_picker()
        elif action == "clear_art":
            self._clear_album_art_current()
        elif action == "dl_lyrics":
            self._download_lyrics_current()
        elif action == "clear_lyrics":
            self._clear_lyrics_current()

    # ── release picker (manual "download album art" release choice) ────────────

    def _release_picker_rect(self):
        pw = min(W - BTN_MARGIN * 2, max(340, W * 9 // 10))
        ph = min(H - BTN_MARGIN * 4, H * 4 // 5)
        px = (W - pw) // 2
        py = (H - ph) // 2
        return px, py, pw, ph

    def _draw_release_picker(self):
        cands = self._art_release_picker
        if cands is None:
            return
        ov = self._overlay_surf
        ov.fill((0, 0, 0, 170))
        self.screen.blit(ov, (0, 0))

        px, py, pw, ph = self._release_picker_rect()
        pygame.draw.rect(self.screen, COL_CELL_BG, (px, py, pw, ph), border_radius=10)
        pygame.draw.rect(self.screen, COL_SEP,     (px, py, pw, ph), width=1, border_radius=10)

        header_h  = TRACK_ROW_H
        clip_y    = py + header_h
        clip_h    = ph - header_h
        max_rows  = max(1, clip_h // _REL_ROW_H)
        shown     = cands[:max_rows]
        overflow  = len(cands) - len(shown)

        title = f"Choose a release ({len(cands)})" if not overflow else \
                f"Choose a release ({len(shown)} of {len(cands)}, best shown)"
        ts = _render_text(self._f_track_sm, title, COL_TEXT_ALBUM, pw - BTN_MARGIN * 2)
        self.screen.blit(ts, (px + (pw - ts.get_width()) // 2,
                              py + (header_h - ts.get_height()) // 2))
        pygame.draw.line(self.screen, COL_SEP, (px, py + header_h - 1), (px + pw, py + header_h - 1))

        old_clip = self.screen.get_clip()
        self.screen.set_clip(px, clip_y, pw, clip_h)
        for i, cand in enumerate(shown):
            ry = clip_y + i * _REL_ROW_H
            title_txt = cand.get("title") or "—"
            disamb    = cand.get("disambiguation") or ""
            if disamb:
                title_txt = f"{title_txt} ({disamb})"
            n       = len(cand.get("refs", []))
            country = cand.get("country") or "??"
            sub_txt = f"{country} · {n} picture{'s' if n != 1 else ''}"

            t1 = _render_text(self._f_track, title_txt, COL_TRACK_NORMAL, pw - BTN_MARGIN * 2)
            self.screen.blit(t1, (px + BTN_MARGIN, ry + 6))
            t2 = _render_text(self._f_track_sm, sub_txt, COL_TEXT_ALBUM, pw - BTN_MARGIN * 2)
            self.screen.blit(t2, (px + BTN_MARGIN, ry + _REL_ROW_H - t2.get_height() - 6))
            if i < len(shown) - 1:
                pygame.draw.line(self.screen, COL_SEP,
                                 (px, ry + _REL_ROW_H - 1), (px + pw, ry + _REL_ROW_H - 1))
        self.screen.set_clip(old_clip)

    def _release_picker_hit(self, pos) -> bool:
        px, py, pw, ph = self._release_picker_rect()
        return px <= pos[0] < px + pw and py <= pos[1] < py + ph

    def _release_picker_item_at(self, pos) -> dict | None:
        cands = self._art_release_picker
        if not cands:
            return None
        px, py, pw, ph = self._release_picker_rect()
        header_h = TRACK_ROW_H
        clip_y, clip_h = py + header_h, ph - header_h
        if not (px <= pos[0] < px + pw and clip_y <= pos[1] < clip_y + clip_h):
            return None
        max_rows = max(1, clip_h // _REL_ROW_H)
        row = (pos[1] - clip_y) // _REL_ROW_H
        return cands[row] if 0 <= row < min(max_rows, len(cands)) else None

    def _open_release_picker(self):
        if self._cur_idx is None:
            return
        album = self._albums[self._cur_idx]
        artist = album.get("artist", "")
        name   = album.get("name", "")
        tracks = album.get("tracks") or self._tracks
        track_count = len(tracks) if tracks else 0
        self._show_menu_toast("Loading releases…")

        def _bg(artist=artist, name=name, track_count=track_count):
            try:
                cands = self._artwork.list_candidates(artist, name, track_count)
            except Exception as e:
                log.warning("release picker: list failed for %s - %s: %s", artist, name, e)
                cands = []
            if cands:
                self._art_release_picker = cands
            else:
                self._show_menu_toast("No releases found")
            self._dirty = True
        threading.Thread(target=_bg, daemon=True).start()

    def _pick_release_from_picker(self, candidate: dict):
        self._art_release_picker = None
        album_uri = self._art_album_uri
        if not album_uri or album_uri in self._art_fetching:
            return
        self._art_fetching.add(album_uri)
        self._art_paths = []
        self._art_count = 1
        self._art_pos = self._art_pos_t = 0.0
        self._art_idx = 0
        self._art_page_surf.clear()

        def _on_image(path):
            if album_uri != self._art_album_uri:
                return
            if path not in self._art_paths:
                self._art_paths.append(path)
                self._art_count = 1 + len(self._art_paths)
                self._ensure_art_window()
                self._dirty = True

        def _bg():
            try:
                self._artwork.fetch_release(album_uri, candidate, on_image=_on_image)
                self._show_menu_toast("Album art downloaded")
            except Exception as e:
                log.warning("release picker: fetch failed for %s: %s", album_uri, e)
                self._show_menu_toast("Download failed")
            finally:
                self._art_fetching.discard(album_uri)
        threading.Thread(target=_bg, daemon=True).start()

    def _clear_album_art_current(self):
        album_uri = self._art_album_uri
        if not album_uri:
            return
        self._artwork.clear(album_uri)
        self._reset_art_carousel()
        self._show_menu_toast("Album art cleared")

    # ── lyrics download / clear (playback-screen menu) ──────────────────────────

    def _download_lyrics_current(self):
        uri = self._song.get("file", "")
        if not uri:
            return
        song_snap   = dict(self._song)
        status_snap = dict(self._status)
        self._lyrics_loading = True
        self._show_menu_toast("Downloading lyrics…")

        def _bg(u=uri, ss=song_snap, st=status_snap):
            try:
                text   = self._load_lyrics_for_uri(u, song=ss, status=st, force=True)
                parsed = self._parse_lyrics(text) if text else None
            except Exception:
                log.exception("download lyrics failed for %s", u)
                parsed = None
            self._lyrics_cache[u] = parsed
            if self._lyrics_uri == u:
                self._lyrics_parsed   = parsed
                self._lyrics_scroll   = 0.0
                self._lyrics_prev_idx = -1
                self._lyrics_anim_t   = 0.0
            self._lyrics_loading = False
            self._show_menu_toast("Lyrics downloaded" if parsed else "No lyrics found")
            self._dirty = True
        threading.Thread(target=_bg, daemon=True).start()

    def _clear_lyrics_current(self):
        uri = self._song.get("file", "")
        if not uri:
            return
        path     = self._resolve_music_path(uri)
        lrc_path = self._lrc_disk_path(uri, path)
        if lrc_path and os.path.exists(lrc_path):
            try:
                os.remove(lrc_path)
            except Exception as e:
                log.warning("clear lyrics: %s", e)
        self._lyrics_cache.pop(uri, None)

        # The per-album "fully cached" marker is no longer accurate once a
        # track's lyrics are cleared — drop it and persist the index.
        album_uri = self._art_album_uri
        if album_uri and album_uri in self._lyrics_album_index:
            self._lyrics_album_index.discard(album_uri)
            self._save_lyrics_index()

        embedded = self._embedded_lyrics(path) if path else None
        parsed   = self._parse_lyrics(embedded) if embedded else None
        if self._lyrics_uri == uri:
            self._lyrics_parsed   = parsed
            self._lyrics_scroll   = 0.0
            self._lyrics_prev_idx = -1
            self._lyrics_anim_t   = 0.0
        self._show_menu_toast("Lyrics cleared")
        self._dirty = True

    # ── menu toast (small transient confirmation) ───────────────────────────────

    def _show_menu_toast(self, text: str):
        self._menu_toast    = text
        self._menu_toast_ms = pygame.time.get_ticks()
        self._dirty = True

    def _draw_menu_toast(self):
        if not self._menu_toast:
            return
        elapsed    = pygame.time.get_ticks() - self._menu_toast_ms
        hold, fade = 1400, 400
        if elapsed >= hold + fade:
            self._menu_toast = None
            return
        alpha = 255 if elapsed < hold else max(0, int(255 * (1 - (elapsed - hold) / fade)))

        text_s = _render_text(self._f_track, self._menu_toast, (235, 235, 235))
        pad_x, pad_y = 22, 12
        pw, ph = text_s.get_width() + pad_x * 2, text_s.get_height() + pad_y * 2
        surf = pygame.Surface((pw, ph), pygame.SRCALPHA)
        pygame.draw.rect(surf, (20, 20, 26, int(210 * alpha / 255)), (0, 0, pw, ph),
                         border_radius=ph // 2)
        if alpha < 255:
            text_s = text_s.copy()
            text_s.set_alpha(alpha)
        surf.blit(text_s, (pad_x, pad_y))
        self.screen.blit(surf, ((W - pw) // 2, H - PROGRESS_H - ph - 28))

    # ── hit testing ───────────────────────────────────────────────────────────

    def _cell_at(self, pos) -> int | None:
        x, y  = pos
        gt    = 0
        if y < gt:
            return None
        show_labels = settings.get("grid_labels")
        cell_h = _CELL_W + (GRID_TEXT_H if show_labels else 0)
        row_h  = cell_h + GRID_PAD
        gy    = y - gt + int(self._grid_scroll)
        row   = (gy - GRID_PAD) // row_h
        if row < 0 or (gy - GRID_PAD) % row_h > cell_h:
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

    def _menu_btn_pos(self):
        gx = W - BTN_RADIUS - BTN_MARGIN - 2 * BTN_RADIUS - BTN_GAP   # gear position
        return gx - 2 * BTN_RADIUS - BTN_GAP, BTN_MARGIN + BTN_RADIUS

    def _menu_btn_hit(self, pos) -> bool:
        mx, my = self._menu_btn_pos()
        dx, dy = pos[0] - mx, pos[1] - my
        return dx * dx + dy * dy <= (BTN_RADIUS + 10) ** 2

    def _build_audio_items(self) -> list[dict]:
        # Use pactl sink names as IDs (works on both PipeWire and PulseAudio)
        # so BT address matching is reliable — wpctl uses numeric IDs that can't
        # be matched against the "bluez_output.XX_XX.1" PA name format.
        sinks = self.audio.get_sinks_pa() if (self.audio and self.audio.available) else []
        # Start with all PA sinks; we'll annotate BT ones and replace entries.
        # Build a map: pa_name → item index for O(1) BT matching.
        items: list[dict] = []
        pa_name_to_idx: dict[str, int] = {}
        for s in sinks:
            pa_name_to_idx[s["id"]] = len(items)
            items.append(dict(s, bt_addr=None, bt_connected=True))

        if self.bt and self.bt.available:
            for dev in self.bt.get_devices():
                pa_name = (self.audio.bt_sink_pa_name(dev["address"])
                           if self.audio else "")
                if pa_name:
                    idx = pa_name_to_idx.get(pa_name)
                    if idx is not None:
                        # Annotate the existing sink entry as BT.
                        items[idx]["bt_addr"] = dev["address"]
                        items[idx]["name"]    = dev["name"]
                    else:
                        items.append({
                            "id":           pa_name,
                            "name":         dev["name"],
                            "active":       False,
                            "bt_addr":      dev["address"],
                            "bt_connected": True,
                        })
                else:
                    items.append({
                        "id":           None,
                        "name":         dev["name"],
                        "active":       False,
                        "bt_addr":      dev["address"],
                        "bt_connected": False,
                    })

        # Re-derive active flag from the actual PA default so it's correct
        # regardless of how _sinks_pactl() computed it.
        if self.audio and self.audio.available:
            default_pa = self.audio.get_default_sink_pa()
            if default_pa:
                for item in items:
                    item["active"] = (item.get("id") or "") == default_pa

        return items

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

    def _pressed_ctrl(self) -> str | None:
        """Return which control button the current touch is pressing, or None."""
        if self._t_start_pos is None or self._view != View.ALBUM or int(self._ctrl_a) <= 10:
            return None
        pos = self._t_start_pos
        if self._close_btn_hit(pos):   return "close"
        if self._stop_btn_hit(pos):    return "stop"
        if self._speaker_btn_hit(pos): return "speaker"
        if self._gear_btn_hit(pos):    return "gear"
        if self._menu_btn_hit(pos):    return "menu"
        return self._ctrl_zone(pos)    # "play" | "prev" | "next" | None

    def _ctrl_zone(self, pos) -> str | None:
        x, y = pos
        ctrl_cy = H // 2
        # play/pause — circle matching the visual CTRL_ICON_LG circle
        dx, dy = x - W // 2, y - ctrl_cy
        if dx * dx + dy * dy <= (CTRL_ICON_LG + 10) ** 2:
            return "play"
        # prev — circle around the two left triangles
        dx, dy = x - W // 6, y - ctrl_cy
        if dx * dx + dy * dy <= (CTRL_ICON_SM + 10) ** 2:
            return "prev"
        # next — circle around the two right triangles
        dx, dy = x - 5 * W // 6, y - ctrl_cy
        if dx * dx + dy * dy <= (CTRL_ICON_SM + 10) ** 2:
            return "next"
        return None

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

        if view == View.CAROUSEL:
            if self._peeking and pos[1] >= H - TRACKLIST_ART_H:
                self._unpeek()
                return
            if self._gear_btn_hit(pos):
                self._open_settings()
                return
            n = len(self._albums)
            if n > 0:
                idx = max(0, min(n - 1, int(round(self._carousel_pos))))
                self._go_album(idx)
            return

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
            # If a dropdown is open, check for option tap or dismiss
            if self._settings_dropdown:
                dk = self._settings_dropdown
                opts = _SETTINGS_SELECTORS[dk]
                # Find the row index of the open dropdown key
                for ri, (rk, _) in enumerate(_SETTINGS_ITEMS):
                    if rk == dk:
                        row_y = self._settings_row_y(ri)
                        dp_w  = 160
                        dp_x  = W - BTN_MARGIN - dp_w
                        dp_y  = row_y + TRACK_ROW_H
                        for oi, opt in enumerate(opts):
                            oy = dp_y + oi * TRACK_ROW_H
                            if dp_x <= pos[0] <= W and oy <= pos[1] <= oy + TRACK_ROW_H:
                                settings.set(dk, opt.lower())
                                if dk == "library":
                                    self._reload_library()
                                elif dk == "album_sort":
                                    self._albums = self._sort_albums(self._albums)
                                elif dk == "spotify_bitrate":
                                    threading.Thread(
                                        target=self._apply_spotify_bitrate,
                                        args=(opt.lower(),), daemon=True).start()
                                self._settings_dropdown = None
                                self._dirty = True
                                return
                        break
                self._settings_dropdown = None
                self._dirty = True
                return
            key = self._settings_item_at(pos)
            if key:
                if key in _SETTINGS_SELECTORS:
                    self._settings_dropdown = key if self._settings_dropdown != key else None
                    self._dirty = True
                elif key == "lyrics_cache_all":
                    if self._lyrics_bulk_progress is not None:
                        self._lyrics_bulk_progress = None  # signals thread to stop
                    else:
                        self._start_lyrics_bulk_cache()
                    self._dirty = True
                elif key in ("art_cache_local", "art_cache_spotify"):
                    library = "local" if key == "art_cache_local" else "spotify"
                    prog = self._art_bulk_progress
                    if prog is not None and prog[2] == library:
                        self._art_bulk_progress = None       # cancel this library's run
                    elif prog is None:
                        self._start_art_bulk_cache(library)  # ignore taps while other runs
                    self._dirty = True
                else:
                    settings.toggle(key)
                    if key == "carousel":
                        self._settings_return = self._browse_view()
                return
            if self._settings_bt_power_toggle_at(pos):
                new_val = not self._bt_powered
                self._bt_powered = new_val
                self._dirty = True
                def _do_bt_power(v=new_val):
                    self.bt.set_powered(v)
                    self._bt_powered  = self.bt.is_powered()
                    self._bt_devices  = self.bt.get_devices() if self._bt_powered else []
                    self._dirty = True
                threading.Thread(target=_do_bt_power, daemon=True).start()
                return
            if self._settings_wifi_power_toggle_at(pos):
                new_val = not self._wifi_powered
                self._wifi_powered = new_val
                self._dirty = True
                def _do_wifi_power(v=new_val):
                    self.wifi.set_enabled(v)
                    self._wifi_powered  = self.wifi.is_enabled()
                    self._wifi_networks = self.wifi.get_networks() if self._wifi_powered else []
                    self._dirty = True
                threading.Thread(target=_do_wifi_power, daemon=True).start()
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
            if self._settings_clear_instrumental_btn_at(pos):
                self._clear_instrumental_sentinels()
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

                # release-picker row selection (topmost overlay when open)
                if self._art_release_picker is not None:
                    if self._release_picker_hit(pos):
                        cand = self._release_picker_item_at(pos)
                        if cand is not None:
                            self._pick_release_from_picker(cand)
                    else:
                        self._art_release_picker = None
                    self._dirty = True
                    return

                # album action-menu item selection
                if self._album_menu_open:
                    action = self._album_menu_item_at(pos)
                    if action:
                        self._exec_album_menu_tap(action)
                    self._album_menu_open = False
                    self._dirty = True
                    return

                # audio popup sink selection (check before other buttons)
                sink = self._audio_popup_sink_at(pos)
                if sink is not None:
                    busy_key = sink["id"] or sink.get("bt_addr")
                    self._audio_busy_id = busy_key
                    self._dirty = True
                    def _switch(snk=sink):
                        bt_addr = snk.get("bt_addr")
                        pa_name = snk.get("id") if snk.get("bt_connected") else None
                        if bt_addr and not snk.get("bt_connected"):
                            self.bt.connect(bt_addr)
                            # Poll for the PA sink to appear (up to 10 s).
                            for _ in range(20):
                                time.sleep(0.5)
                                pa_name = (self.audio.bt_sink_pa_name(bt_addr)
                                           if self.audio else "")
                                if pa_name:
                                    break
                        if pa_name and self.audio and self.audio.available:
                            self.audio.set_sink_pa(pa_name)
                        self._audio_sinks   = self._build_audio_items()
                        self._audio_busy_id = None
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
                        self._audio_sinks = self._build_audio_items()
                    self._dirty = True
                    return
                if self._gear_btn_hit(pos):
                    self._open_settings()
                    return
                if self._menu_btn_hit(pos):
                    self._album_menu_open = not self._album_menu_open
                    self._dirty = True
                    return
                zone = self._ctrl_zone(pos)
                if zone == "prev":
                    self.player.previous(); self._reset_elapsed()
                elif zone == "next":
                    self.player.next(); self._reset_elapsed()
                elif zone == "play":
                    playing = self._status.get("state") == "play"
                    self._status["state"] = "pause" if playing else "play"
                    self._dirty = True
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
                    self._song_prev_file     = self._song.get("file", "")
                    self._song_block_until   = 0.0
                    self._song_guard_file    = track.get("file", "")
                    self._song_guard_timeout = time.monotonic() + 5.0
                    self._song = {
                        "title":  track.get("title", ""),
                        "artist": track.get("artist", ""),
                        "album":  track.get("album", ""),
                        "file":   track.get("file", ""),
                    }

    def _exec_double_tap(self):
        if self._view in (View.ALBUM, View.TRACKLIST):
            playing = self._status.get("state") == "play"
            self._status["state"] = "pause" if playing else "play"
            self._dirty = True
            self.player.toggle()
            self._show_flash("pause" if playing else "play")

    def _show_controls(self):
        self._ctrl_a = 255.0; self._ctrl_a_t = 255.0
        self._ctrl_shown_ms = pygame.time.get_ticks()
    def _hide_controls(self):
        self._ctrl_a = 0.0; self._ctrl_a_t = 0.0
        self._audio_popup_open  = False
        self._album_menu_open   = False
        self._art_release_picker = None

    def _reset_elapsed(self):
        self._elapsed_base   = 0.0
        self._elapsed_base_t = time.monotonic()

    def _in_scrub_zone(self, pos) -> bool:
        if self._song.get("file", "").startswith("spotify:"):
            return False
        bar_y = self._progress_bar_y()
        return bar_y - SCRUB_LEEWAY // 2 <= pos[1] <= bar_y + PROGRESS_H + SCRUB_LEEWAY // 4

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
        if self._view == View.CAROUSEL and not self._peeking:
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
            (float(_TL_ALBUM_Y),          View.TRACKLIST,       False),
            (0.0,                          View.ALBUM,           False),
            (float(H - TRACKLIST_ART_H),   self._browse_view(),  True),
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
                    self._art_drag_base     = self._art_pos   # for horizontal art swipe
                if self._view == View.CAROUSEL:
                    self._carousel_drag_start = self._carousel_pos
            self._dirty = True   # immediate press highlight

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
                elif (self._panel_touch and self._view == View.ALBUM
                      and self._art_count > 1 and total_dx > total_dy):
                    # Horizontal drag on the album panel scrolls the art
                    # carousel (front → back → booklet).  Hard-clamp at both
                    # ends — no overscroll, so the panel is always fully covered.
                    self._art_drag = True
                    delta = (self._t_start_pos[0] - pos[0]) / float(W)
                    raw   = self._art_drag_base + delta
                    self._art_pos = max(0.0, min(float(self._art_count - 1), raw))
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
                        self._view    = self._browse_view()
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
                elif self._view == View.CAROUSEL:
                    n = len(self._albums)
                    if n > 1:
                        dx_total = pos[0] - self._t_start_pos[0]
                        raw = self._carousel_drag_start - dx_total / _CAR_PX_PER_ALBUM
                        self._carousel_pos   = max(0.0, min(float(n - 1), raw))
                        self._carousel_pos_t = self._carousel_pos  # track live; snap on release

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
                if dur <= 0:
                    dur = float(self._song.get("time", 0) or 0)
                self._elapsed_base   = frac * dur
                self._elapsed_base_t = time.monotonic()
                self._seek_guard_until = time.monotonic() + 3.0
                self._t_start_pos    = None
                self._t_prev_pos     = None
                self._t_dragging     = False
                self._t_long_pressed = False
                self._panel_touch    = False
                self._lyrics_drag    = False
                return

            swipe_h = abs(total_x) >= SWIPE_H_MIN and abs(total_x) > abs(total_y)

            # Art carousel drag release: snap to the nearest image, with a
            # decisive flick guaranteeing at least one step in its direction.
            if self._art_drag:
                self._art_drag = False
                base_idx = int(round(self._art_drag_base))
                target   = int(round(self._art_pos))
                if swipe_h and target == base_idx:
                    target = base_idx + (1 if total_x < 0 else -1)
                target = max(0, min(self._art_count - 1, target))
                self._art_pos_t = float(target)
                self._art_idx   = target
                self._ensure_art_window()
                self._t_start_pos    = None
                self._t_prev_pos     = None
                self._t_dragging     = False
                self._t_long_pressed = False
                self._panel_touch    = False
                self._lyrics_drag    = False
                self._dirty          = True
                return

            # horizontal swipe on album art bypasses panel snap
            if self._panel_touch and self._t_dragging and not (swipe_h and self._view == View.ALBUM):
                self._snap_panel(total_y)
                # Also snap carousel position if the drag may have scrolled it
                if self._view == View.CAROUSEL:
                    n = len(self._albums)
                    if n > 0:
                        self._carousel_pos_t = float(
                            max(0, min(n - 1, round(self._carousel_pos))))
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
                    # Horizontal swipe now scrolls the art carousel (handled by
                    # the _art_drag release above).  With no extra art there is
                    # nothing to scroll; prev/next track lives on the control
                    # overlay's arrows.
                    pass

                elif self._view == View.CAROUSEL:
                    # Snap to nearest album after drag/flick; tap handled below.
                    n = len(self._albums)
                    if n > 0:
                        self._carousel_pos_t = float(
                            max(0, min(n - 1, round(self._carousel_pos))))
                    if is_tap:
                        self._exec_single_tap(self._t_start_pos)

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
            self._dirty          = True   # clear press highlight
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
