"""
I2C volume controller — reads a slide potentiometer via ADS1015 / ADS1115 ADC.

Wiring (ADS1x15):
  VDD  → 3.3 V
  GND  → GND
  SCL  → HyperPixel SCL  (GPIO 3, pin 5)  — check your Pi's I2C bus
  SDA  → HyperPixel SDA  (GPIO 2, pin 3)
  ADDR → GND             (I2C address 0x48)
  AIN0 → pot wiper
  pot high leg  → VDD (3.3 V)
  pot low leg   → GND

NOTE: The HyperPixel Square uses ALL GPIO pins for its display, which blocks
the hardware I2C bus.  Pimoroni's newer drivers expose a software-I2C bus on
a different bus number (often bus 11).  Check with `i2cdetect -l` and set
VOLUME_I2C_BUS in config.py accordingly.
"""
from __future__ import annotations
import threading
import time
import logging
from typing import Callable

from config import (
    VOLUME_SIMULATE,
    VOLUME_I2C_ENABLED, VOLUME_I2C_BUS, VOLUME_I2C_ADDR,
    VOLUME_I2C_CHANNEL, VOLUME_POLL_HZ, VOLUME_DEADBAND, VOLUME_INVERT,
    SCREEN_WIDTH, SCREEN_HEIGHT,
)

log = logging.getLogger(__name__)


class VolumeController:
    """
    Reads the pot in a background thread and fires an on_change callback
    whenever the value shifts by more than VOLUME_DEADBAND.
    """

    def __init__(self, on_change: Callable[[int], None] | None = None):
        self._volume   = 50
        self._prev_vol = 50
        self._lock     = threading.Lock()
        self._on_change = on_change
        self.available = False

        if not VOLUME_I2C_ENABLED:
            log.info("I2C volume disabled in config")
            return

        try:
            import smbus2
            self._bus  = smbus2.SMBus(VOLUME_I2C_BUS)
            self._addr = VOLUME_I2C_ADDR
            self._ch   = VOLUME_I2C_CHANNEL
            self.available = True
            log.info("ADS1x15 on bus %d addr 0x%02X", VOLUME_I2C_BUS, VOLUME_I2C_ADDR)
            t = threading.Thread(target=self._poll_loop, daemon=True)
            t.start()
        except Exception as e:
            log.warning("I2C init failed (%s) — volume control disabled", e)

    # ── Reading ───────────────────────────────────────────────────────────────

    def _read_ads1x15(self) -> float:
        """
        Returns 0.0–1.0.  Works for both ADS1015 (12-bit) and ADS1115 (16-bit).
        Uses single-shot mode so we don't have to configure continuous mode.
        """
        ch_mux = [0x4000, 0x5000, 0x6000, 0x7000]  # AIN0..AIN3 vs GND
        config = (
            0x8000          |   # start single conversion
            ch_mux[self._ch]|   # input channel
            0x0200          |   # PGA ±4.096 V (covers 0–3.3 V)
            0x0100          |   # single-shot mode
            0x00E0          |   # data rate = max (3300 SPS / 860 SPS)
            0x0003              # disable comparator
        )
        hi = (config >> 8) & 0xFF
        lo = config & 0xFF
        self._bus.write_i2c_block_data(self._addr, 0x01, [hi, lo])
        time.sleep(0.003)       # wait for conversion (1/860 SPS ≈ 1.2 ms)
        raw_bytes = self._bus.read_i2c_block_data(self._addr, 0x00, 2)
        raw = (raw_bytes[0] << 8) | raw_bytes[1]
        # ADS1x15 returns a signed 16-bit value; negative means below GND
        if raw > 0x7FFF:
            raw = 0
        return min(1.0, raw / 32767.0)

    def _poll_loop(self):
        interval = 1.0 / VOLUME_POLL_HZ
        while True:
            try:
                val = self._read_ads1x15()
                if VOLUME_INVERT:
                    val = 1.0 - val
                vol = int(round(val * 100))
                with self._lock:
                    self._volume = vol
                    if abs(vol - self._prev_vol) >= VOLUME_DEADBAND:
                        self._prev_vol = vol
                        cb = self._on_change
                if abs(vol - self._prev_vol) >= VOLUME_DEADBAND and cb:
                    cb(vol)          # called outside the lock
            except Exception as e:
                log.debug("I2C read error: %s", e)
            time.sleep(interval)

    # ── Public ────────────────────────────────────────────────────────────────

    @property
    def volume(self) -> int:
        with self._lock:
            return self._volume

    def set_on_change(self, cb: Callable[[int], None]):
        self._on_change = cb


class VolumeSimulator:
    """
    Desktop stand-in for VolumeController.
    Opens a small tkinter window with a vertical slider (0–100).
    Fires the on_change callback whenever the slider moves.
    """

    def __init__(self):
        self._volume    = 50
        self._on_change: Callable[[int], None] | None = None
        self._lock      = threading.Lock()
        self.available  = True
        threading.Thread(target=self._run_ui, daemon=True).start()

    def _run_ui(self):
        try:
            import tkinter as tk
        except ImportError:
            log.warning("tkinter not available — VolumeSimulator disabled")
            self.available = False
            return

        root = tk.Tk()
        root.overrideredirect(True)   # frameless
        root.resizable(False, False)

        win_w = 80
        # Pygame centers its window; mirror that to find where to sit beside it
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        pygame_x = (screen_w - SCREEN_WIDTH) // 2
        pygame_y = (screen_h - SCREEN_HEIGHT) // 2
        x = pygame_x - win_w
        root.geometry(f"{win_w}x{SCREEN_HEIGHT}+{x}+{pygame_y}")

        slider = tk.Scale(
            root,
            from_=100, to=0,
            orient=tk.VERTICAL,
            length=SCREEN_HEIGHT - 32,
            width=win_w - 8,
            sliderlength=40,
            showvalue=True,
            command=self._on_slider,
        )
        slider.set(self._volume)
        slider.pack(padx=4, pady=16)

        root.mainloop()

    def _on_slider(self, val: str):
        vol = int(val)
        with self._lock:
            prev = self._volume
            self._volume = vol
            cb = self._on_change
        if abs(vol - prev) >= VOLUME_DEADBAND and cb:
            cb(vol)

    @property
    def volume(self) -> int:
        with self._lock:
            return self._volume

    def set_on_change(self, cb: Callable[[int], None]):
        self._on_change = cb
