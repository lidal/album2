"""
Direct framebuffer + evdev touch for SDL2 offscreen mode.
Used on Raspberry Pi when no SDL video driver supports /dev/fb0 natively.
"""
from __future__ import annotations
import fcntl
import mmap
import struct
import threading
import logging

import pygame

log = logging.getLogger(__name__)

FBIOGET_VSCREENINFO = 0x4600

# input_event on 64-bit Linux: two int64 (timeval) + uint16 type + uint16 code + int32 value
_EV_FMT = "qqHHi"
_EV_SZ  = struct.calcsize(_EV_FMT)

EV_KEY    = 1
EV_ABS    = 3
ABS_X     = 0
ABS_Y     = 1
BTN_TOUCH = 330


def _eviocgabs(axis: int) -> int:
    # _IOR('E', 0x40+axis, 24)  — IOC_READ=2, DIRSHIFT=30, TYPESHIFT=8, SIZESHIFT=16
    return (2 << 30) | (ord("E") << 8) | (0x40 + axis) | (24 << 16)


class Framebuffer:
    """Blits a pygame surface directly to /dev/fb0 each frame."""

    def __init__(self, device: str = "/dev/fb0") -> None:
        self._f = open(device, "rb+")
        vinfo   = fcntl.ioctl(self._f, FBIOGET_VSCREENINFO, bytes(160))
        xres, yres          = struct.unpack_from("II", vinfo, 0)
        bpp                 = struct.unpack_from("I",  vinfo, 24)[0]
        r_off               = struct.unpack_from("I",  vinfo, 32)[0]
        b_off               = struct.unpack_from("I",  vinfo, 56)[0]
        self.width          = xres
        self.height         = yres
        self._fmt           = "BGRA" if r_off > b_off else "RGBA"
        self._map           = mmap.mmap(self._f.fileno(), xres * yres * (bpp // 8))
        log.info("Framebuffer %s: %dx%d %dbpp %s", device, xres, yres, bpp, self._fmt)

    def flip(self, surface: pygame.Surface) -> None:
        if surface.get_width() != self.width or surface.get_height() != self.height:
            surface = pygame.transform.scale(surface, (self.width, self.height))
        if self.bpp != 32:
            raise RuntimeError(
                "Framebuffer is {}bpp — add 'framebuffer_depth=32' to "
                "/boot/config.txt and reboot.".format(self.bpp)
            )
        data = pygame.image.tostring(surface, self._fmt)
        self._map.seek(0)
        self._map.write(data)

    def close(self) -> None:
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
            self._max_x     = max_x if max_x > 0 else 4095
            self._max_y     = max_y if max_y > 0 else 4095
            log.info("EvdevTouch %s: range %dx%d", device, self._max_x, self._max_y)
            threading.Thread(target=self._run, args=(f,), daemon=True).start()
        except Exception as exc:
            log.warning("EvdevTouch init failed: %s", exc)

    def _run(self, f) -> None:
        try:
            while True:
                data = f.read(_EV_SZ)
                if not data or len(data) < _EV_SZ:
                    break
                _, _, etype, code, value = struct.unpack(_EV_FMT, data)
                if etype == EV_ABS:
                    if code == ABS_X:
                        self._x = int(value * self._sw / self._max_x)
                    elif code == ABS_Y:
                        self._y = int(value * self._sh / self._max_y)
                elif etype == EV_KEY and code == BTN_TOUCH:
                    pos = (self._x, self._y)
                    if value:
                        pygame.event.post(pygame.event.Event(
                            pygame.MOUSEBUTTONDOWN, button=1, pos=pos))
                        pygame.event.post(pygame.event.Event(
                            pygame.MOUSEMOTION, pos=pos, rel=(0, 0), buttons=(1, 0, 0)))
                    else:
                        pygame.event.post(pygame.event.Event(
                            pygame.MOUSEBUTTONUP, button=1, pos=pos))
        except Exception as exc:
            log.warning("EvdevTouch read error: %s", exc)
