# ── Scale ─────────────────────────────────────────────────────────────────────
# Physical display pixels (hardware resolution — never scaled).
DISPLAY_WIDTH  = 720
DISPLAY_HEIGHT = 720

# Render at a fraction of the display resolution; framebuffer.py scales up.
# 1.0 = native 720p; 0.667 ≈ 480p (saves ~56% pixels drawn); 0.5 = 360p
SCALE         = 3/3

def _s(x):              # scale an integer constant
    return max(1, int(x * SCALE))

# ── Windowed / fullscreen ─────────────────────────────────────────────────────
FULLSCREEN    = False   # True on the Pi, False for desktop debugging
SCREEN_WIDTH  = _s(DISPLAY_WIDTH)
SCREEN_HEIGHT = _s(DISPLAY_HEIGHT)
ROTATE_DISPLAY = 0      # degrees 0/90/180/270 (for /boot/config.txt approach prefer dtoverlay)

# ── Music library ─────────────────────────────────────────────────────────────
MUSIC_DIR       = ""     # absolute path to music root; empty = auto-detect ~/Music
THUMB_CACHE_DIR = ""     # disk cache for resized thumbnails; empty = ~/.cache/album2/thumbs

# ── Mopidy ────────────────────────────────────────────────────────────────────
MOPIDY_HOST      = "localhost"
MOPIDY_MPD_PORT  = 6600
MOPIDY_HTTP_PORT = 6680
MOPIDY_PASSWORD  = None

# ── I2C Volume ────────────────────────────────────────────────────────────────
VOLUME_SIMULATE    = False    # open a desktop slider window instead of reading I2C
VOLUME_I2C_ENABLED = False

# System audio backend for volume control.
# "mpd"    — send setvol via Mopidy/MPD (default for Pi)
# "wpctl"  — PipeWire  (wpctl set-volume @DEFAULT_AUDIO_SINK@ N%)
# "pactl"  — PulseAudio (pactl set-sink-volume @DEFAULT_SINK@ N%)
# "amixer" — ALSA      (amixer set Master N%)
# "auto"   — try wpctl → pactl → amixer → mpd
VOLUME_BACKEND     = "auto"
VOLUME_I2C_BUS     = 11       # try 11 on HyperPixel (uses software I2C)
VOLUME_I2C_ADDR    = 0x48    # ADS1x15 ADDR→GND
VOLUME_I2C_CHANNEL = 0       # AIN0
VOLUME_POLL_HZ     = 20
VOLUME_DEADBAND    = 1
VOLUME_INVERT      = False

# ── Performance ───────────────────────────────────────────────────────────────
FPS            = 60          # Smooth control
THUMB_WORKERS  = 1           # parallel thumbnail fetch threads (keep low on Pi Zero)
SCROLL_FRICTION = 0.95       # per-frame velocity multiplier (higher = less friction)

# ── Grid ──────────────────────────────────────────────────────────────────────
GRID_COLS      = 2
GRID_PAD       = _s(35)      # outer margin + gap between cells
GRID_TEXT_H    = _s(54)      # height reserved below thumbnail for name/artist

# ── Layout ────────────────────────────────────────────────────────────────────
MINI_H          = _s(176)    # mini-player bar height at top of GRID view
TRACKLIST_ART_H = _s(144)    # album art strip at top when tracklist open (~1/5 of 720)
TRACK_ROW_H     = _s(54)     # height of one track row
CTRL_BAR_H      = _s(88)     # height of prev/play/next strip in controls overlay
PROGRESS_H      = _s(5)      # gold bar at very bottom
SCRUB_LEEWAY    = _s(40)     # px above/below progress bar that still registers as scrub

# ── Animation ─────────────────────────────────────────────────────────────────
ANIM_SPEED     = 12.0        # panels/sec (higher = snappier)
FADE_SPEED     = 8.0         # album-art crossfade
CTRL_FADE_SPEED = 10.0

# ── Touch ─────────────────────────────────────────────────────────────────────
SWIPE_V_MIN    = _s(55)      # px to register a vertical swipe
SWIPE_H_MIN    = _s(55)      # px to register a horizontal swipe
TAP_MAX_MOVE   = _s(22)      # max movement (px) still counted as tap
TAP_MAX_MS     = 380         # max duration (ms) still counted as tap
DOUBLE_TAP_MS  = 300         # max gap between two taps for double-tap
LONG_PRESS_MS  = 600         # min duration (ms) to register a long press
DRAG_THRESH    = _s(12)      # px before a press becomes a live drag

# ── Volume badge ──────────────────────────────────────────────────────────────
VOLUME_BADGE_MS = 1600

# ── Controls overlay ──────────────────────────────────────────────────────────
CTRL_TIMEOUT_MS = 0        # auto-hide controls after this many ms (0 = never)
BTN_MARGIN      = _s(24)   # gap from screen edge to button circle edge
BTN_RADIUS      = _s(32)   # overlay button circle radius
BTN_GAP         = _s(16)   # gap between adjacent overlay buttons
CTRL_ICON_SM    = _s(36)   # prev/next skip triangle size
CTRL_ICON_LG    = _s(50)   # play/pause icon size
CTRL_TEXT_GAP   = _s(10)   # gap between text lines above controls

# ── Toggle ────────────────────────────────────────────────────────────────────
TOGGLE_W        = _s(56)   # settings toggle pill width
TOGGLE_H        = _s(27)   # settings toggle pill height (odd = flush semicircles)

# ── Track rows ────────────────────────────────────────────────────────────────
TRACK_PAD       = _s(16)   # left/right padding in track rows

# ── Mini player ───────────────────────────────────────────────────────────────
MINI_PAD        = _s(8)    # thumbnail margin and text gap
MINI_ICON_SIZE  = _s(20)   # play/pause icon size in peek mini-player

# ── Volume badge ──────────────────────────────────────────────────────────────
VOL_BADGE_W     = _s(22)   # vertical badge pill width
VOL_BADGE_PAD   = _s(48)   # gap from top and bottom of screen

# ── Colours ───────────────────────────────────────────────────────────────────
COL_BG            = (10,  10,  14)
COL_GRID_BG       = (10,  10,  14)
COL_CELL_BG       = (20,  20,  28)
COL_MINI_BG       = (14,  14,  20)
COL_TL_BG         = (11,  11,  16)
COL_SEP           = (38,  38,  52)

COL_HIGHLIGHT     = (210, 165,  60)   # accent / gold

COL_TEXT_TITLE    = (245, 245, 245)
COL_TEXT_ARTIST   = (165, 165, 165)
COL_TEXT_ALBUM    = (110, 110, 110)
COL_TRACK_PLAYING = COL_HIGHLIGHT
COL_TRACK_NORMAL  = (210, 210, 210)
COL_TRACK_NUM     = ( 90,  90,  90)
COL_TRACK_DUR     = ( 90,  90,  90)

COL_PROGRESS_BG   = ( 45,  45,  45)
COL_PROGRESS_FG   = COL_HIGHLIGHT

COL_VOLUME_BG     = ( 28,  28,  28, 210)
COL_VOLUME_FG     = COL_HIGHLIGHT

# ── Fonts ─────────────────────────────────────────────────────────────────────
# Set to a .ttf path for a nicer typeface, e.g.:
#   /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"#"/usr/share/fonts/TTF/DejaVuSans.ttf"

FONT_SZ_TITLE   = _s(34)
FONT_SZ_ARTIST  = _s(19)
FONT_SZ_ALBUM   = _s(16)
FONT_SZ_GRID    = _s(16)
FONT_SZ_GRID_SM = _s(13)
FONT_SZ_MINI    = _s(19)
FONT_SZ_TRACK    = _s(19)
FONT_SZ_TRACK_SM = _s(15)
FONT_SZ_LYRICS   = _s(26)
