#!/usr/bin/env python3
"""
Frame-pipeline benchmark — run on the Pi to find the bottleneck.
Usage: venv/bin/python bench.py
"""
import os, time, fcntl, mmap, struct
os.environ["SDL_VIDEODRIVER"] = "offscreen"

import pygame
import pygame.surfarray
import numpy as np

W, H = 720, 720
N = 200   # iterations per test

pygame.display.init()
screen = pygame.display.set_mode((W, H), pygame.NOFRAME)

art = pygame.Surface((W, H))
art.fill((60, 100, 200))
art.convert()

def bench(label, fn):
    fn()  # warm up
    t0 = time.perf_counter()
    for _ in range(N):
        fn()
    ms = (time.perf_counter() - t0) / N * 1000
    print(f"  {label:<38} {ms:6.2f} ms   ({1000/ms:5.0f} fps cap)")

print(f"\n=== SDL Surface operations ===")

def _fill():
    screen.fill((10, 10, 10))

def _blit_no_alpha():
    screen.blit(art, (0, 0))

art_a254 = art.copy()
art_a254.set_alpha(254)

def _blit_alpha254():
    screen.blit(art_a254, (0, 0))

def _fill_blit():
    screen.fill((10, 10, 10))
    screen.blit(art, (0, 0))

def _fill_blit_a254():
    screen.fill((10, 10, 10))
    screen.blit(art_a254, (0, 0))

bench("fill only",               _fill)
bench("blit (no alpha)",         _blit_no_alpha)
bench("blit (set_alpha=254)",    _blit_alpha254)
bench("fill + blit (no alpha)",  _fill_blit)
bench("fill + blit (alpha=254)", _fill_blit_a254)

print(f"\n=== Pixel extraction (screen → bytes) ===")

def _tostring_bgra():
    return pygame.image.tostring(screen, "BGRA")

def _tostring_rgba():
    return pygame.image.tostring(screen, "RGBA")

def _pixels2d_tobytes():
    arr = pygame.surfarray.pixels2d(screen)
    d = arr.T.tobytes()
    del arr
    return d

def _pixels3d_tobytes():
    arr = pygame.surfarray.pixels3d(screen)  # (W,H,3) RGB
    d = arr.transpose(1, 0, 2).tobytes()
    del arr
    return d

bench("image.tostring BGRA",     _tostring_bgra)
bench("image.tostring RGBA",     _tostring_rgba)
bench("surfarray.pixels2d .T",   _pixels2d_tobytes)
bench("surfarray.pixels3d .T",   _pixels3d_tobytes)

print(f"\n=== Full frame (fill+blit → bytes, no fb write) ===")

def _full_no_alpha():
    screen.fill((10, 10, 10))
    screen.blit(art, (0, 0))
    return pygame.image.tostring(screen, "BGRA")

def _full_alpha254():
    screen.fill((10, 10, 10))
    screen.blit(art_a254, (0, 0))
    return pygame.image.tostring(screen, "BGRA")

def _full_pixels2d():
    screen.fill((10, 10, 10))
    screen.blit(art, (0, 0))
    arr = pygame.surfarray.pixels2d(screen)
    d = arr.T.tobytes()
    del arr
    return d

bench("fill+blit+tostring (no α)", _full_no_alpha)
bench("fill+blit+tostring (α=254)",_full_alpha254)
bench("fill+blit+pixels2d",        _full_pixels2d)

print(f"\n=== RGB565 conversion (16bpp path) ===")

# Current: LUT gather (3 × fancy-index, 256-entry LUT fits in L1)
lut_r = (np.arange(256, dtype=np.uint16) >> 3) << 11
lut_g = (np.arange(256, dtype=np.uint16) >> 2) << 5
lut_b =  np.arange(256, dtype=np.uint16) >> 3

def _rgb565_lut():
    arr = pygame.surfarray.pixels2d(screen)
    arr8 = arr.T.view(np.uint8).reshape(H, W, 4)
    r565  = lut_r[arr8[:, :, 2]]
    r565 |= lut_g[arr8[:, :, 1]]
    r565 |= lut_b[arr8[:, :, 0]]
    del arr, arr8
    return r565.tobytes()

# Alternative: single uint32 formula on C-contiguous array (no strided reads, more data)
def _rgb565_u32():
    arr32 = np.asarray(pygame.surfarray.pixels2d(screen).T, dtype=np.uint32)  # C-contiguous copy
    result  = arr32 >> 8;  result &= np.uint32(0xF800)
    tmp     = arr32 >> 5;  tmp    &= np.uint32(0x07E0); result |= tmp; del tmp
    tmp     = arr32 >> 3;  tmp    &= np.uint32(0x001F); result |= tmp; del tmp
    del arr32
    return result.astype(np.uint16).tobytes()

# Baseline: old shift arithmetic (what we had before LUT)
def _rgb565_shifts():
    arr = pygame.surfarray.pixels2d(screen)
    arr8 = arr.T.view(np.uint8).reshape(H, W, 4)
    r = arr8[:, :, 2].astype(np.uint16); r >>= 3; r <<= 11
    g = arr8[:, :, 1].astype(np.uint16); g >>= 2; g <<= 5;  r |= g
    b = arr8[:, :, 0].astype(np.uint16); b >>= 3;            r |= b
    del arr, arr8, g, b
    return r.tobytes()

bench("rgb565 LUT (new)",          _rgb565_lut)
bench("rgb565 uint32 C-contig",    _rgb565_u32)
bench("rgb565 shifts (baseline)",  _rgb565_shifts)

print(f"\n=== Framebuffer write ===")
FBIOGET_VSCREENINFO = 0x4600
try:
    fb_f = open("/dev/fb0", "rb+")
    vinfo = bytearray(fcntl.ioctl(fb_f, FBIOGET_VSCREENINFO, bytes(160)))
    xres, yres = struct.unpack_from("II", vinfo, 0)
    bpp        = struct.unpack_from("I",  vinfo, 24)[0]
    size       = xres * yres * (bpp // 8)
    fb_map     = mmap.mmap(fb_f.fileno(), size)
    dummy      = b'\x00' * size

    def _mmap_write():
        fb_map.seek(0)
        fb_map.write(dummy)

    bench(f"mmap.write {xres}×{yres} {bpp}bpp ({size//1024}KB)", _mmap_write)
    fb_map.close()
    fb_f.close()
except Exception as e:
    print(f"  fb0 unavailable: {e}")

print()
