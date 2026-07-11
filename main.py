#!/usr/bin/env python3
"""
Album2 — Mopidy frontend
for Raspberry Pi Zero 2W + HyperPixel Square 4.0
"""
import os
import sys
import time
import logging
import pygame

from config    import SCREEN_WIDTH, SCREEN_HEIGHT, DISPLAY_WIDTH, DISPLAY_HEIGHT, FULLSCREEN, ROTATE_DISPLAY, VOLUME_SIMULATE
from player    import MopidyPlayer
from volume    import VolumeController, VolumeSimulator
from display   import AlbumDisplay
from bluetooth import BluetoothManager
from wifi      import WiFiManager
from audio     import AudioOutputManager
import settings; settings.load()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("album2")

_BARE_METAL = not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")


def _init_display() -> pygame.Surface:
    if _BARE_METAL:
        # No X11/Wayland — render offscreen and blit to /dev/fb0 ourselves.
        # Neither the bundled pip SDL2 nor the system SDL2 on Pi OS Buster
        # has fbcon/kmsdrm compiled in, so this is the only reliable path.
        os.environ["SDL_VIDEODRIVER"] = "offscreen"
        pygame.display.init()
        log.info("SDL video driver: offscreen (frames pushed to /dev/fb0)")
        try:
            with open("/dev/tty1", "wb") as _tty:
                _tty.write(b"\033[?25l"    # hide cursor
                           b"\033[2J"      # clear screen
                           b"\033[H")      # cursor to home (keeps it off visible area)
        except Exception:
            pass
        try:
            with open("/sys/class/graphics/fbcon/cursor_blink", "w") as _f:
                _f.write("0")              # disable blink at kernel level
        except Exception:
            pass
    else:
        if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("SDL_VIDEODRIVER"):
            os.environ["SDL_VIDEODRIVER"] = "wayland"
        pygame.display.init()

    pygame.font.init()

    flags = pygame.NOFRAME
    if FULLSCREEN:
        flags |= pygame.FULLSCREEN

    screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT), flags)
    pygame.display.set_caption("Album2")
    pygame.mouse.set_visible(True)

    if ROTATE_DISPLAY not in (0, None):
        log.info("Software display rotation: %d°", ROTATE_DISPLAY)

    return screen


def main():
    log.info("Starting album2")
    screen  = _init_display()

    fb    = None
    touch = None
    if _BARE_METAL:
        from framebuffer import Framebuffer, EvdevTouch
        try:
            fb = Framebuffer("/dev/fb0", target_w=DISPLAY_WIDTH, target_h=DISPLAY_HEIGHT)
        except Exception as exc:
            log.error("Cannot open /dev/fb0: %s — display will be blank", exc)
        try:
            touch = EvdevTouch("/dev/input/event0", SCREEN_WIDTH, SCREEN_HEIGHT)
        except Exception as exc:
            log.warning("Cannot open touch device: %s", exc)
        try:
            import subprocess
            subprocess.run(["plymouth", "quit"])
        except Exception as exc:
            log.warning("Cannot close plymouth: %s", exc)

    player  = MopidyPlayer()
    volume  = VolumeSimulator() if VOLUME_SIMULATE else VolumeController()
    bt      = BluetoothManager()
    wifi    = WiFiManager()
    audio   = AudioOutputManager(player._vol_backend)
    display = AlbumDisplay(screen, player, volume, bt, wifi, audio)

    running = True
    _booted = False

    _pt_update = _pt_draw = _pt_flip = _pt_rgb565 = 0.0
    _pn = 0
    _pt_wall = time.perf_counter()
    _t_frame_start = time.perf_counter()

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False
            else:
                display.handle_event(event)

        t0 = time.perf_counter()
        display.update()
        t1 = time.perf_counter()
        fps = display.target_fps()
        drew = display.draw()
        t2 = time.perf_counter()
        if drew:
            if fb:
                fb.flip(screen)
            else:
                pygame.display.flip()
            if not _booted:
                # Signal bootscreen to exit now that the first frame is on screen.
                try:
                    open("/tmp/album2.ready", "w").close()
                except Exception:
                    pass
                _booted = True
        t3 = time.perf_counter()

        if drew:
            _pt_update  += t1 - t0
            _pt_draw    += t2 - t1
            _pt_flip    += t3 - t2
            _pt_rgb565  += fb.t_rgb565 if fb else 0.0
            _pn         += 1
            if _pn >= 200:
                wall = t3 - _pt_wall
                log.info(
                    "perf/%d frames  fps=%.0f  update=%.1fms  draw=%.1fms  "
                    "flip=%.1fms  (rgb565=%.1fms  mmap-wait=%.1fms)",
                    _pn, _pn / wall,
                    _pt_update / _pn * 1000,
                    _pt_draw   / _pn * 1000,
                    _pt_flip   / _pn * 1000,
                    _pt_rgb565 / _pn * 1000,
                    (_pt_flip - _pt_rgb565) / _pn * 1000,
                )
                _pt_update = _pt_draw = _pt_flip = _pt_rgb565 = 0.0
                _pn = 0
                _pt_wall = t3

        # Float-precision frame cap — clock.tick(60) rounds 1000/60→16ms giving 62fps;
        # that 2fps excess causes a rolling tear as writes drift past the vertical blank.
        _target_fps = fps if drew else min(fps, 15)
        _elapsed = time.perf_counter() - _t_frame_start
        _sleep_s = (1.0 / _target_fps) - _elapsed
        if _sleep_s > 0.0005:
            time.sleep(_sleep_s)
        _t_frame_start = time.perf_counter()

    log.info("Shutting down")
    player.disconnect()
    if fb:
        fb.close()
    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
