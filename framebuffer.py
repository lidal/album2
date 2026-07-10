"""
Direct framebuffer + evdev touch for SDL2 offscreen mode.
Used on Raspberry Pi when no SDL video driver supports /dev/fb0 natively.
"""
from __future__ import annotations
import fcntl
import mmap
import queue
import struct
import threading
import time
import logging

import pygame

log = logging.getLogger(__name__)

FBIOGET_VSCREENINFO = 0x4600
FBIOPUT_VSCREENINFO = 0x4601
FBIOPAN_DISPLAY     = 0x4606

# input_event on 64-bit Linux: two int64 (timeval) + uint16 type + uint16 code + int32 value
_EV_FMT = "qqHHi"
_EV_SZ  = struct.calcsize(_EV_FMT)

EV_SYN    = 0
EV_KEY    = 1
EV_ABS    = 3
ABS_X     = 0
ABS_Y     = 1
BTN_TOUCH = 330
SYN_REPORT = 0


def _eviocgabs(axis: int) -> int:
    # _IOR('E', 0x40+axis, 24)  — IOC_READ=2, DIRSHIFT=30, TYPESHIFT=8, SIZESHIFT=16
    return (2 << 30) | (ord("E") << 8) | (0x40 + axis) | (24 << 16)


class Framebuffer:
    """Blits a pygame surface directly to /dev/fb0 each frame."""

    def __init__(self, device: str = "/dev/fb0",
                 target_w: int = 0, target_h: int = 0) -> None:
        self._f = open(device, "rb+")
        vinfo   = bytearray(fcntl.ioctl(self._f, FBIOGET_VSCREENINFO, bytes(160)))
        xres, yres = struct.unpack_from("II", vinfo, 0)
        bpp        = struct.unpack_from("I",  vinfo, 24)[0]

        tw = target_w or xres
        th = target_h or yres
        if xres != tw or yres != th or bpp != 32:
            try:
                struct.pack_into("II", vinfo,  0, tw, th)   # xres, yres
                struct.pack_into("II", vinfo,  8, tw, th)   # xres_virtual, yres_virtual
                struct.pack_into("I",  vinfo, 24, 32)        # bits_per_pixel
                fcntl.ioctl(self._f, FBIOPUT_VSCREENINFO, bytes(vinfo))
                vinfo = bytearray(fcntl.ioctl(self._f, FBIOGET_VSCREENINFO, bytes(160)))
                xres, yres = struct.unpack_from("II", vinfo, 0)
                bpp        = struct.unpack_from("I",  vinfo, 24)[0]
                log.info("Framebuffer reconfigured to %dx%d %dbpp", xres, yres, bpp)
            except Exception as exc:
                log.warning("Could not reconfigure framebuffer: %s", exc)

        r_off               = struct.unpack_from("I", vinfo, 32)[0]
        b_off               = struct.unpack_from("I", vinfo, 56)[0]
        self.width          = xres
        self.height         = yres
        self.bpp            = bpp
        self._fmt           = "BGRA" if r_off > b_off else "RGBA"
        self._vinfo         = vinfo   # kept for FBIOPAN_DISPLAY calls
        self._rgb565_argb: bool | None = None  # cached surface format for _to_rgb565
        self.t_rgb565: float = 0.0   # seconds spent in last _to_rgb565 call
        self.t_flip:   float = 0.0   # seconds spent in last flip() call (excl. mmap write)
        import numpy as _np
        self._lut_r = (_np.arange(256, dtype=_np.uint16) >> 3) << 11   # 512 B — fits in L1
        self._lut_g = (_np.arange(256, dtype=_np.uint16) >> 2) << 5
        self._lut_b =  _np.arange(256, dtype=_np.uint16) >> 3

        # Try to enable double buffering: allocate yres_virtual = 2*yres so we
        # can write to the hidden buffer and flip atomically, eliminating tearing.
        self._dbl  = False
        self._back = 0   # which half (0 or 1) we write into next
        try:
            v2 = bytearray(vinfo)
            struct.pack_into("I", v2, 12, yres * 2)   # yres_virtual = 2*yres
            struct.pack_into("I", v2, 20, 0)           # yoffset = 0
            fcntl.ioctl(self._f, FBIOPUT_VSCREENINFO, bytes(v2))
            v2 = bytearray(fcntl.ioctl(self._f, FBIOGET_VSCREENINFO, bytes(160)))
            if struct.unpack_from("I", v2, 12)[0] >= yres * 2:
                self._dbl   = True
                self._vinfo = v2
                self._map   = mmap.mmap(self._f.fileno(), xres * yres * 2 * (bpp // 8))
                log.info("Framebuffer %s: %dx%d %dbpp %s (double-buffered)", device, xres, yres, bpp, self._fmt)
            else:
                raise RuntimeError("driver did not accept yres_virtual=2*yres")
        except Exception as exc:
            log.info("Framebuffer double-buffer unavailable (%s), using single buffer", exc)
            self._map = mmap.mmap(self._f.fileno(), xres * yres * (bpp // 8))
            log.info("Framebuffer %s: %dx%d %dbpp %s", device, xres, yres, bpp, self._fmt)

        # Worker thread owns the mmap write; GIL is released during I/O so this
        # runs on a second core while the main thread renders the next frame.
        self._q: queue.Queue[bytes | None] = queue.Queue(maxsize=1)
        threading.Thread(target=self._writer, daemon=True).start()

    def _writer(self) -> None:
        frame_bytes = self.width * self.height * (self.bpp // 8)
        while True:
            data = self._q.get()
            if data is None:
                return
            if self._dbl:
                # Write into the hidden back buffer, then flip to show it.
                # The display never reads what we're currently writing.
                back = self._back
                self._map.seek(back * frame_bytes)
                self._map.write(data)
                vinfo = bytearray(self._vinfo)
                struct.pack_into("I", vinfo, 20, back * self.height)  # yoffset
                try:
                    fcntl.ioctl(self._f, FBIOPAN_DISPLAY, bytes(vinfo))
                except Exception as e:
                    log.warning("FBIOPAN_DISPLAY failed: %s", e)
                self._back = 1 - back
            else:
                self._map.seek(0)
                self._map.write(data)

    def flip(self, surface: pygame.Surface, rotate: int = 0) -> None:
        t0 = time.perf_counter()
        if rotate in (90, 270):
            surface = pygame.transform.rotate(surface, rotate)
        if surface.get_width() != self.width or surface.get_height() != self.height:
            surface = pygame.transform.scale(surface, (self.width, self.height))
        if self.bpp == 32:
            arr = pygame.surfarray.pixels2d(surface)   # (W,H) uint32, zero-copy view
            if rotate == 180:
                data = arr[::-1, ::-1].T.tobytes()
            else:
                data = arr.T.tobytes()
            del arr
            self.t_rgb565 = 0.0
        elif self.bpp == 16:
            t1 = time.perf_counter()
            data = self._to_rgb565(surface, rotate == 180)
            self.t_rgb565 = time.perf_counter() - t1
        else:
            raise RuntimeError("Unsupported framebuffer depth: {}bpp".format(self.bpp))
        # Drop the oldest pending frame if the writer is still busy (maxsize=1).
        try:
            self._q.get_nowait()
        except queue.Empty:
            pass
        self._q.put(data)
        self.t_flip = time.perf_counter() - t0

    def _to_rgb565(self, surface: pygame.Surface, rotate_180: bool = False) -> bytes:
        import numpy as np
        # Reinterpret the (W,H) uint32 view as (W,H,4) uint8 bytes so channel
        # slices are 518 KB uint8 arrays — 4× smaller than uint32 ops, and avoids
        # the 22ms overhead of pixels3d which does internal format conversion.
        arr = pygame.surfarray.pixels2d(surface)
        # pixels2d returns (W,H) F-contiguous (surface memory is row-major (H,W)).
        # .T gives (H,W) C-contiguous, so .view(uint8) works without copying.
        arr8 = arr.T.view(np.uint8).reshape(self.height, self.width, 4)
        if self._rgb565_argb is None:
            # ARGB/XRGB surface: rmask=0x00FF0000 → memory bytes are [B,G,R,A]
            # RGBA/RGBX surface: rmask=0xFF000000 → memory bytes are [R,G,B,A]
            self._rgb565_argb = bool(surface.get_masks()[0] & 0x00FF0000)
        r_i, g_i, b_i = (2, 1, 0) if self._rgb565_argb else (0, 1, 2)
        # LUT gather: each 256-entry table fits in L1 cache, replacing astype+2 shifts per channel.
        rgb565  = self._lut_r[arr8[:, :, r_i]]
        rgb565 |= self._lut_g[arr8[:, :, g_i]]
        rgb565 |= self._lut_b[arr8[:, :, b_i]]
        del arr, arr8
        if rotate_180:
            return rgb565[::-1, ::-1].tobytes()
        return rgb565.tobytes()

    def close(self) -> None:
        self._q.put(None)  # signal writer to exit
        self._map.close()
        self._f.close()


class EvdevTouch:
    """
    Reads single-touch events from an evdev device and injects them as
    pygame MOUSEBUTTONDOWN / MOUSEBUTTONUP / MOUSEMOTION events so the
    existing display event handling works unchanged.
    """

    def __init__(self, device: str, screen_w: int, screen_h: int) -> None:
        self._sw = screen_w
        self._sh = screen_h
        self._x  = screen_w // 2
        self._y  = screen_h // 2
        try:
            f = open(device, "rb")
            info_x          = fcntl.ioctl(f, _eviocgabs(ABS_X), bytes(24))
            _, max_x        = struct.unpack_from("ii", info_x, 0)
            info_y          = fcntl.ioctl(f, _eviocgabs(ABS_Y), bytes(24))
            _, max_y        = struct.unpack_from("ii", info_y, 0)
            self._max_x     = max_x if max_x > 0 else 719
            self._max_y     = max_y if max_y > 0 else 719
            log.info("EvdevTouch %s: range %dx%d", device, self._max_x, self._max_y)
            threading.Thread(target=self._run, args=(f,), daemon=True).start()
        except Exception as exc:
            log.warning("EvdevTouch init failed: %s", exc)

    def _run(self, f) -> None:
        pressed        = False
        pending_press  = False
        pending_release = False
        try:
            while True:
                data = f.read(_EV_SZ)
                if not data or len(data) < _EV_SZ:
                    break
                _, _, etype, code, value = struct.unpack(_EV_FMT, data)
                if etype == EV_ABS:
                    if code == ABS_X:
                        self._x = self._sw - 1 - int(value * self._sw / self._max_x)
                    elif code == ABS_Y:
                        self._y = self._sh - 1 - int(value * self._sh / self._max_y)
                elif etype == EV_KEY and code == BTN_TOUCH:
                    if value:
                        pending_press = True
                    else:
                        pending_release = True
                elif etype == EV_SYN and code == SYN_REPORT:
                    # All events for this frame are in — fire with correct position
                    pos = (self._x, self._y)
                    if pending_press:
                        pending_press = False
                        pressed = True
                        pygame.event.post(pygame.event.Event(
                            pygame.MOUSEBUTTONDOWN, button=1, pos=pos))
                    elif pending_release:
                        pending_release = False
                        pressed = False
                        pygame.event.post(pygame.event.Event(
                            pygame.MOUSEBUTTONUP, button=1, pos=pos))
                    elif pressed:
                        pygame.event.post(pygame.event.Event(
                            pygame.MOUSEMOTION, pos=pos, rel=(0, 0), buttons=(1, 0, 0)))
        except Exception as exc:
            log.warning("EvdevTouch read error: %s", exc)
