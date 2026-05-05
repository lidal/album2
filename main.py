#!/usr/bin/env python3
"""
Album2 — Mopidy frontend
for Raspberry Pi Zero 2W + HyperPixel Square 4.0
"""
import os
import sys
import logging
import pygame

from config    import SCREEN_WIDTH, SCREEN_HEIGHT, FULLSCREEN, ROTATE_DISPLAY, VOLUME_SIMULATE
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
            fb = Framebuffer("/dev/fb0", target_w=SCREEN_WIDTH, target_h=SCREEN_HEIGHT)
        except Exception as exc:
            log.error("Cannot open /dev/fb0: %s — display will be blank", exc)
        try:
            touch = EvdevTouch("/dev/input/event0", SCREEN_WIDTH, SCREEN_HEIGHT)
        except Exception as exc:
            log.warning("Cannot open touch device: %s", exc)

    player  = MopidyPlayer()
    volume  = VolumeSimulator() if VOLUME_SIMULATE else VolumeController()
    bt      = BluetoothManager()
    wifi    = WiFiManager()
    audio   = AudioOutputManager(player._vol_backend)
    display = AlbumDisplay(screen, player, volume, bt, wifi, audio)

    clock   = pygame.time.Clock()
    running = True
    _t_prof = [0.0] * 4   # [update, draw, flip, tick]
    _n_prof = 0

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False
            else:
                display.handle_event(event)

        import time as _time
        _t0 = _time.perf_counter()
        display.update()
        _t1 = _time.perf_counter()
        fps = display.target_fps()
        drew = display.draw()
        _t2 = _time.perf_counter()
        if drew:
            if fb:
                fb.flip(screen)
            else:
                pygame.display.flip()
        _t3 = _time.perf_counter()
        clock.tick(fps)
        _t4 = _time.perf_counter()

        _t_prof[0] += _t1 - _t0
        _t_prof[1] += _t2 - _t1
        _t_prof[2] += _t3 - _t2
        _t_prof[3] += _t4 - _t3
        _n_prof += 1
        if _n_prof == 120:
            log.info("PERF 120f: update=%.1fms draw=%.1fms flip=%.1fms tick=%.1fms",
                     _t_prof[0]/_n_prof*1000, _t_prof[1]/_n_prof*1000,
                     _t_prof[2]/_n_prof*1000, _t_prof[3]/_n_prof*1000)
            _t_prof = [0.0] * 4
            _n_prof = 0

    log.info("Shutting down")
    player.disconnect()
    if fb:
        fb.close()
    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
