"""
Direct framebuffer + evdev touch for SDL2 offscreen mode.
Used on Raspberry Pi when no SDL video driver supports /dev/fb0 natively.
Tries DRM/KMS page-flip first (tear-free), falls back to /dev/fb0 single-buffer.
"""
from __future__ import annotations
import ctypes
import fcntl
import mmap
import os
import queue
import select
import struct
import threading
import time
import logging

import pygame

log = logging.getLogger(__name__)

FBIOGET_VSCREENINFO = 0x4600
FBIOPUT_VSCREENINFO = 0x4601
FBIOPAN_DISPLAY     = 0x4606
FBIO_WAITFORVSYNC   = 0x40044620


# ── DRM/KMS page-flip ─────────────────────────────────────────────────────────

class _DrmModeModeInfo(ctypes.Structure):
    _fields_ = [
        ("clock",       ctypes.c_uint32),
        ("hdisplay",    ctypes.c_uint16), ("hsync_start", ctypes.c_uint16),
        ("hsync_end",   ctypes.c_uint16), ("htotal",      ctypes.c_uint16),
        ("hskew",       ctypes.c_uint16),
        ("vdisplay",    ctypes.c_uint16), ("vsync_start", ctypes.c_uint16),
        ("vsync_end",   ctypes.c_uint16), ("vtotal",      ctypes.c_uint16),
        ("vscan",       ctypes.c_uint16),
        ("vrefresh",    ctypes.c_uint32),
        ("flags",       ctypes.c_uint32),
        ("type",        ctypes.c_uint32),
        ("name",        ctypes.c_char * 32),
    ]

class _DrmModeRes(ctypes.Structure):
    _fields_ = [
        ("count_fbs",        ctypes.c_int),
        ("fbs",              ctypes.c_void_p),
        ("count_crtcs",      ctypes.c_int),
        ("crtcs",            ctypes.c_void_p),
        ("count_connectors", ctypes.c_int),
        ("connectors",       ctypes.c_void_p),
        ("count_encoders",   ctypes.c_int),
        ("encoders",         ctypes.c_void_p),
        ("min_width",        ctypes.c_uint32), ("max_width",  ctypes.c_uint32),
        ("min_height",       ctypes.c_uint32), ("max_height", ctypes.c_uint32),
    ]

class _DrmModeConnector(ctypes.Structure):
    _fields_ = [
        ("connector_id",      ctypes.c_uint32),
        ("encoder_id",        ctypes.c_uint32),
        ("connector_type",    ctypes.c_uint32),
        ("connector_type_id", ctypes.c_uint32),
        ("connection",        ctypes.c_uint32),
        ("mmWidth",           ctypes.c_uint32),
        ("mmHeight",          ctypes.c_uint32),
        ("subpixel",          ctypes.c_uint32),
        ("count_modes",       ctypes.c_int),
        ("modes",             ctypes.POINTER(_DrmModeModeInfo)),
        ("count_props",       ctypes.c_int),
        ("props",             ctypes.c_void_p),
        ("prop_values",       ctypes.c_void_p),
        ("count_encoders",    ctypes.c_int),
        ("encoders",          ctypes.c_void_p),
    ]

class _DrmModeEncoder(ctypes.Structure):
    _fields_ = [
        ("encoder_id",      ctypes.c_uint32),
        ("encoder_type",    ctypes.c_uint32),
        ("crtc_id",         ctypes.c_uint32),
        ("possible_crtcs",  ctypes.c_uint32),
        ("possible_clones", ctypes.c_uint32),
    ]

_DRM_MODE_PAGE_FLIP_EVENT = 0x01
_DRM_EVENT_FLIP_COMPLETE  = 0x02


class _DRMPageFlip:
    """
    DRM/KMS double-buffered page-flip renderer.
    Writes to the hidden back buffer while the front buffer is being scanned,
    then atomically swaps at the next vertical blank — completely tear-free.
    flip() blocks until the swap completes, naturally pacing the loop to 60 fps.
    """

    def __init__(self, device: str = "/dev/dri/card0") -> None:
        lib = ctypes.CDLL("libdrm.so.2")

        lib.drmSetMaster.restype         = ctypes.c_int
        lib.drmSetMaster.argtypes        = [ctypes.c_int]
        lib.drmModeGetResources.restype  = ctypes.c_void_p
        lib.drmModeGetResources.argtypes = [ctypes.c_int]
        lib.drmModeFreeResources.restype = None
        lib.drmModeFreeResources.argtypes= [ctypes.c_void_p]
        lib.drmModeGetConnector.restype  = ctypes.c_void_p
        lib.drmModeGetConnector.argtypes = [ctypes.c_int, ctypes.c_uint32]
        lib.drmModeFreeConnector.restype = None
        lib.drmModeFreeConnector.argtypes= [ctypes.c_void_p]
        lib.drmModeGetEncoder.restype    = ctypes.c_void_p
        lib.drmModeGetEncoder.argtypes   = [ctypes.c_int, ctypes.c_uint32]
        lib.drmModeFreeEncoder.restype   = None
        lib.drmModeFreeEncoder.argtypes  = [ctypes.c_void_p]
        lib.drmModeSetCrtc.restype       = ctypes.c_int
        lib.drmModeSetCrtc.argtypes      = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                            ctypes.c_uint32, ctypes.c_uint32,
                                            ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
                                            ctypes.POINTER(_DrmModeModeInfo)]
        lib.drmModePageFlip.restype      = ctypes.c_int
        lib.drmModePageFlip.argtypes     = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                            ctypes.c_uint32, ctypes.c_void_p]
        lib.drmModeCreateDumbBuffer.restype  = ctypes.c_int
        lib.drmModeCreateDumbBuffer.argtypes = [
            ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64)]
        lib.drmModeAddFB.restype  = ctypes.c_int
        lib.drmModeAddFB.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint32,
                                     ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        lib.drmModeMapDumbBuffer.restype  = ctypes.c_int
        lib.drmModeMapDumbBuffer.argtypes = [ctypes.c_int, ctypes.c_uint32,
                                             ctypes.POINTER(ctypes.c_uint64)]
        self._lib = lib

        self._fd = os.open(device, os.O_RDWR | os.O_CLOEXEC)
        lib.drmSetMaster(self._fd)

        crtc_id, connector_id, mode = self._discover()
        self._crtc_id      = crtc_id
        self._connector_id = connector_id
        self._mode         = mode
        self.width         = mode.hdisplay
        self.height        = mode.vdisplay
        self.bpp           = 16

        self._fb_ids: list[int]        = []
        self._maps:   list[mmap.mmap]  = []
        self._handles: list[int]       = []
        for _ in range(2):
            handle, fb_id, m = self._create_buffer()
            self._handles.append(handle)
            self._fb_ids.append(fb_id)
            self._maps.append(m)

        conn_arr = ctypes.c_uint32(connector_id)
        mode_ref = _DrmModeModeInfo()
        ctypes.memmove(ctypes.byref(mode_ref), ctypes.byref(mode), ctypes.sizeof(mode))
        r = lib.drmModeSetCrtc(self._fd, crtc_id, self._fb_ids[0],
                               0, 0, ctypes.byref(conn_arr), 1, ctypes.byref(mode_ref))
        if r != 0:
            raise RuntimeError(f"drmModeSetCrtc returned {r}")

        self._back = 1
        log.info("DRM page-flip: %dx%d @%dHz on crtc=%d connector=%d",
                 self.width, self.height, mode.vrefresh, crtc_id, connector_id)

    def _discover(self) -> tuple[int, int, _DrmModeModeInfo]:
        lib = self._lib
        rp  = lib.drmModeGetResources(self._fd)
        if not rp:
            raise RuntimeError("drmModeGetResources failed")
        res = ctypes.cast(rp, ctypes.POINTER(_DrmModeRes))[0]
        conn_ids = (ctypes.c_uint32 * res.count_connectors).from_address(res.connectors)
        try:
            for i in range(res.count_connectors):
                cp = lib.drmModeGetConnector(self._fd, conn_ids[i])
                if not cp:
                    continue
                conn = ctypes.cast(cp, ctypes.POINTER(_DrmModeConnector))[0]
                connected  = conn.connection == 1
                has_modes  = conn.count_modes > 0
                has_enc    = conn.encoder_id != 0
                if connected and has_modes and has_enc:
                    ep = lib.drmModeGetEncoder(self._fd, conn.encoder_id)
                    enc = ctypes.cast(ep, ctypes.POINTER(_DrmModeEncoder))[0]
                    crtc_id = enc.crtc_id
                    lib.drmModeFreeEncoder(ep)
                    if crtc_id:
                        mode = _DrmModeModeInfo()
                        ctypes.memmove(ctypes.byref(mode), conn.modes,
                                       ctypes.sizeof(_DrmModeModeInfo))
                        lib.drmModeFreeConnector(cp)
                        return crtc_id, conn.connector_id, mode
                lib.drmModeFreeConnector(cp)
        finally:
            lib.drmModeFreeResources(rp)
        raise RuntimeError("No connected DRM connector with active CRTC found")

    def _create_buffer(self) -> tuple[int, int, mmap.mmap]:
        lib = self._lib
        handle = ctypes.c_uint32(0)
        pitch  = ctypes.c_uint32(0)
        size   = ctypes.c_uint64(0)
        r = lib.drmModeCreateDumbBuffer(self._fd, self.width, self.height, self.bpp, 0,
                                        ctypes.byref(handle), ctypes.byref(pitch),
                                        ctypes.byref(size))
        if r != 0:
            raise RuntimeError(f"drmModeCreateDumbBuffer failed: {r}")
        fb_id = ctypes.c_uint32(0)
        r = lib.drmModeAddFB(self._fd, self.width, self.height, 16, self.bpp,
                              pitch.value, handle.value, ctypes.byref(fb_id))
        if r != 0:
            raise RuntimeError(f"drmModeAddFB failed: {r}")
        offset = ctypes.c_uint64(0)
        r = lib.drmModeMapDumbBuffer(self._fd, handle.value, ctypes.byref(offset))
        if r != 0:
            raise RuntimeError(f"drmModeMapDumbBuffer failed: {r}")
        m = mmap.mmap(self._fd, size.value, access=mmap.ACCESS_WRITE, offset=offset.value)
        return handle.value, fb_id.value, m

    def flip(self, data: bytes) -> None:
        """Write frame bytes to the back buffer and page-flip at the next vsync."""
        m = self._maps[self._back]
        m.seek(0)
        m.write(data)
        r = self._lib.drmModePageFlip(self._fd, self._crtc_id,
                                       self._fb_ids[self._back],
                                       _DRM_MODE_PAGE_FLIP_EVENT, None)
        if r == 0:
            rlist, _, _ = select.select([self._fd], [], [], 0.1)
            if rlist:
                try:
                    os.read(self._fd, 64)
                except OSError:
                    pass
        self._back ^= 1

    def close(self) -> None:
        for m in self._maps:
            m.close()
        try:
            self._lib.drmDropMaster(self._fd)
        except Exception:
            pass
        os.close(self._fd)

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
    """
    Renders pygame surfaces to the display.
    Tries DRM/KMS page-flip first (vsync-locked, tear-free).
    Falls back to /dev/fb0 mmap if DRM is unavailable.
    When `paces_loop` is True the caller must NOT sleep — flip() already blocks on vsync.
    """

    def __init__(self, device: str = "/dev/fb0",
                 target_w: int = 0, target_h: int = 0) -> None:
        self._drm: _DRMPageFlip | None = None
        self.t_rgb565: float = 0.0
        self.t_flip:   float = 0.0
        self.paces_loop: bool = False

        try:
            drm = _DRMPageFlip("/dev/dri/card0")
            self._drm        = drm
            self.width       = drm.width
            self.height      = drm.height
            self.bpp         = drm.bpp
            self._surf16     = pygame.Surface((drm.width, drm.height), 0, 16)
            self.paces_loop  = True
            return
        except Exception as exc:
            log.info("DRM unavailable (%s), falling back to %s", exc, device)

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
        self.t_rgb565: float = 0.0   # seconds spent in last _to_rgb565 call
        self.t_flip:   float = 0.0   # seconds spent in last flip() call (excl. mmap write)
        if bpp == 16:
            # Persistent 16bpp surface used by _to_rgb565; SDL's blitter converts
            # 32bpp→16bpp in C+NEON which is 4× faster than the numpy path.
            self._surf16 = pygame.Surface((xres, yres), 0, 16)

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

        # Try vsync on the single-buffer path; disabled on first failure.
        self._vsync = not self._dbl

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
                if self._vsync:
                    try:
                        fcntl.ioctl(self._f, FBIO_WAITFORVSYNC, struct.pack("I", 0))
                    except Exception as exc:
                        log.info("FBIO_WAITFORVSYNC not supported (%s), vsync disabled", exc)
                        self._vsync = False
                self._map.seek(0)
                self._map.write(data)

    def flip(self, surface: pygame.Surface) -> None:
        t0 = time.perf_counter()
        if surface.get_width() != self.width or surface.get_height() != self.height:
            surface = pygame.transform.scale(surface, (self.width, self.height))
        if self._drm is not None:
            t1 = time.perf_counter()
            data = self._to_rgb565(surface)
            self.t_rgb565 = time.perf_counter() - t1
            self._drm.flip(data)
            self.t_flip = time.perf_counter() - t0
            return
        if self.bpp == 32:
            arr = pygame.surfarray.pixels2d(surface)   # (W,H) uint32, zero-copy view
            data = arr.T.tobytes()
            del arr
            self.t_rgb565 = 0.0
        elif self.bpp == 16:
            t1 = time.perf_counter()
            data = self._to_rgb565(surface)
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

    def _to_rgb565(self, surface: pygame.Surface) -> bytes:
        # SDL's C+NEON blitter converts 32bpp→16bpp in one pass (~4ms vs ~18ms numpy).
        self._surf16.blit(surface, (0, 0))
        arr = pygame.surfarray.pixels2d(self._surf16)   # (W,H) uint16, zero-copy view
        data = arr.T.tobytes()
        del arr
        return data

    def close(self) -> None:
        if self._drm is not None:
            self._drm.close()
            return
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
