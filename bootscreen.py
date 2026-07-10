#!/usr/bin/env python3
"""
Boot screen — pure stdlib, no pygame.
Writes a spinning wheel directly to /dev/fb0 via mmap.
Starts at basic.target (early boot); exits when /tmp/album2.ready appears.

Dot pixel indices are cached to disk so trig/range computation is skipped
on subsequent boots.
"""
import fcntl
import math
import mmap
import os
import signal
import socket
import struct
import time

FBIOGET_VSCREENINFO = 0x4600
FBIOPUT_VSCREENINFO = 0x4601

READY_FLAG   = "/tmp/album2.ready"


def _sd_ready():
    """Tell systemd the service is ready (Type=notify) after first frame is drawn."""
    sock = os.environ.get("NOTIFY_SOCKET", "")
    if not sock:
        return
    try:
        addr = "\0" + sock[1:] if sock.startswith("@") else sock
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(b"READY=1\n", addr)
    except Exception:
        pass
CACHE_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bootscreen_cache")
CACHE_MAGIC  = 0xAB2C0001

BG      = (15, 15, 15)
N_DOTS  = 12
FPS     = 12


# ── framebuffer ───────────────────────────────────────────────────────────────

def _open_fb(device="/dev/fb0", target_w=720, target_h=720):
    f     = open(device, "rb+")
    vinfo = bytearray(fcntl.ioctl(f, FBIOGET_VSCREENINFO, bytes(160)))
    w, h  = struct.unpack_from("II", vinfo, 0)
    bpp   = struct.unpack_from("I",  vinfo, 24)[0]
    if bpp != 32 or w != target_w or h != target_h:
        struct.pack_into("II", vinfo,  0, target_w, target_h)
        struct.pack_into("II", vinfo,  8, target_w, target_h)
        struct.pack_into("I",  vinfo, 24, 32)
        try:
            fcntl.ioctl(f, FBIOPUT_VSCREENINFO, bytes(vinfo))
        except OSError:
            pass  # DRM-backed fbdev (e.g. vc4drmfb) may not support resolution changes
        vinfo = bytearray(fcntl.ioctl(f, FBIOGET_VSCREENINFO, bytes(160)))
        w, h  = struct.unpack_from("II", vinfo, 0)
        bpp   = struct.unpack_from("I",  vinfo, 24)[0]
    r_off = struct.unpack_from("I", vinfo, 32)[0]
    b_off = struct.unpack_from("I", vinfo, 56)[0]
    bgra  = r_off > b_off
    buf   = mmap.mmap(f.fileno(), w * h * (bpp // 8))
    return f, buf, w, h, bpp, bgra


def _px(r, g, b, bpp, bgra):
    if bpp == 16:
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        return struct.pack("<H", v)
    return bytes([b, g, r, 255]) if bgra else bytes([r, g, b, 255])


# ── dot pixel index cache ─────────────────────────────────────────────────────

def _cache_load(w, h, n_dots, spoke_r, dot_r):
    try:
        with open(CACHE_PATH, "rb") as f:
            raw = f.read()
        pos = 0
        magic, cw, ch, cn, csr, cdr = struct.unpack_from("6I", raw, pos); pos += 24
        if (magic, cw, ch, cn, csr, cdr) != (CACHE_MAGIC, w, h, n_dots, spoke_r, dot_r):
            return None
        dot_pixels = []
        for _ in range(n_dots):
            count, = struct.unpack_from("I", raw, pos); pos += 4
            idxs = list(struct.unpack_from(f"{count}I", raw, pos)); pos += count * 4
            dot_pixels.append(idxs)
        return dot_pixels
    except Exception:
        return None


def _cache_save(w, h, n_dots, spoke_r, dot_r, dot_pixels):
    try:
        parts = [struct.pack("6I", CACHE_MAGIC, w, h, n_dots, spoke_r, dot_r)]
        for idxs in dot_pixels:
            parts.append(struct.pack(f"I{len(idxs)}I", len(idxs), *idxs))
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "wb") as f:
            f.write(b"".join(parts))
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def _compute_dot_pixels(w, h, n_dots, spoke_r, dot_r):
    cx, cy     = w // 2, h // 2
    dot_pixels = []
    for i in range(n_dots):
        angle = 2 * math.pi * i / n_dots - math.pi / 2
        dx    = int(cx + spoke_r * math.cos(angle))
        dy    = int(cy + spoke_r * math.sin(angle))
        idxs  = []
        for py in range(max(0, dy - dot_r), min(h, dy + dot_r + 1)):
            for px in range(max(0, dx - dot_r), min(w, dx + dot_r + 1)):
                if (px - dx) ** 2 + (py - dy) ** 2 <= dot_r ** 2:
                    idxs.append(py * w + px)
        dot_pixels.append(idxs)
    return dot_pixels


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    try:
        os.remove(READY_FLAG)
    except FileNotFoundError:
        pass

    try:
        with open("/dev/tty1", "wb") as t:
            t.write(b"\033[?25l\033[2J\033[H")
    except Exception:
        pass
    try:
        with open("/sys/class/graphics/fbcon/cursor_blink", "w") as f:
            f.write("0")
    except Exception:
        pass

    # Poll until /dev/fb0 is openable — it may exist before udev sets video group ownership.
    deadline = time.monotonic() + 30
    while True:
        try:
            open("/dev/fb0", "rb+").close()
            break
        except (FileNotFoundError, PermissionError, OSError):
            pass
        if time.monotonic() > deadline:
            print("bootscreen: timed out waiting for /dev/fb0", flush=True)
            return
        time.sleep(0.05)

    try:
        f, buf, w, h, bpp, bgra = _open_fb()
    except Exception as exc:
        print(f"bootscreen: {exc}", flush=True)
        return

    stride = bpp // 8
    spoke_r = min(w, h) // 10
    dot_r   = max(4, spoke_r // 5)

    dot_pixels = _cache_load(w, h, N_DOTS, spoke_r, dot_r)
    if dot_pixels is None:
        dot_pixels = _compute_dot_pixels(w, h, N_DOTS, spoke_r, dot_r)
        _cache_save(w, h, N_DOTS, spoke_r, dot_r, dot_pixels)

    # Build frame in a local bytearray — one mmap write per tick is faster and
    # smoother than many small mmap writes, and overwrites any console text.
    bg_px = _px(*BG, bpp, bgra)
    frame = bytearray(bg_px * (w * h))

    running = True

    def _stop(sig, _):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    head      = 0
    interval  = 1.0 / FPS
    next_tick = time.monotonic()
    first     = True

    while running and not os.path.exists(READY_FLAG):
        # Restore previous dot pixels to background in the local frame buffer.
        for idx in dot_pixels[head]:
            frame[idx * stride: idx * stride + stride] = bg_px
        head = (head + 1) % N_DOTS
        # Paint new dot positions into the local frame buffer.
        for i in range(N_DOTS):
            dist = (head - i) % N_DOTS
            bri  = max(35, 255 - dist * 18)
            px   = _px(bri, bri, bri, bpp, bgra)
            for idx in dot_pixels[i]:
                frame[idx * stride: idx * stride + stride] = px
        # Single write covers any console text drawn since last tick.
        buf.seek(0)
        buf.write(frame)
        if first:
            # Notify systemd (Type=notify) — getty won't start until after this.
            _sd_ready()
            first = False
        next_tick += interval
        delay = next_tick - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    buf.close()
    f.close()


if __name__ == "__main__":
    main()
