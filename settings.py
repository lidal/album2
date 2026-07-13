import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "settings.json")

_DEFAULTS: dict = {
    "autoplay":     False,
    "lyrics":       True,
    "grid_labels":  True,
    "carousel":     False,
    "debug":      False,
    "idle_fps":   True,
    "skip_draw":  True,
    # touch calibration: affine coefficients raw→screen
    # screen_x = cal_sx * raw_x + cal_ox
    # screen_y = cal_sy * raw_y + cal_oy
    "cal_sx": 1.0, "cal_ox": 0.0,
    "cal_sy": 1.0, "cal_oy": 0.0,
    "car_reflections":  True,
    "library":          "local",
    "album_sort":       "artist a→z",
    "spotify_bitrate":  "160",
}

_data: dict = {}


def load():
    global _data
    try:
        with open(_PATH) as f:
            _data = {**_DEFAULTS, **json.load(f)}
    except Exception:
        _data = dict(_DEFAULTS)


def save():
    try:
        with open(_PATH, "w") as f:
            json.dump(_data, f, indent=2)
    except Exception:
        pass


def get(key):
    return _data.get(key, _DEFAULTS.get(key))


def toggle(key):
    _data[key] = not _data.get(key, _DEFAULTS.get(key, False))
    save()


def set(key, value):
    _data[key] = value
    save()


def cycle(key: str, options: tuple | list):
    """Advance *key* to the next value in *options* (case-insensitive match)."""
    opts_lower = [str(o).lower() for o in options]
    cur = str(_data.get(key, _DEFAULTS.get(key, options[0]))).lower()
    try:
        idx = opts_lower.index(cur)
    except ValueError:
        idx = -1
    _data[key] = str(options[(idx + 1) % len(options)]).lower()
    save()
