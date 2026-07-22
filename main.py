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

    player  = MopidyPlayer()
    player.stop()   # clear any leftover queue from the previous session
    volume  = VolumeSimulator() if VOLUME_SIMULATE else VolumeController()
    bt      = BluetoothManager()
    wifi    = WiFiManager()
    audio   = AudioOutputManager(player._vol_backend)
    display = AlbumDisplay(screen, player, volume, bt, wifi, audio)

    clock   = pygame.time.Clock()
    running = True
    _booted = False

    _pt_update = _pt_draw = _pt_flip = _pt_rgb565 = 0.0
    _pn = 0
    _pt_wall = time.perf_counter()
    _t_last_flip = None   # for slow-frame diagnosis (see below)
    _prev_tick_ms = 0.0    # how long the *previous* iteration's clock.tick() blocked

    while running:
        t_loop_top = time.perf_counter()
        n_events = 0
        for event in pygame.event.get():
            n_events += 1
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False
            else:
                display.handle_event(event)
        t_ev = time.perf_counter()

        t0 = t_ev
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
            # Flag individual slow frames (missed the 60fps budget by 2x+) —
            # the periodic summary below averages over 200 frames and can
            # hide an occasional stutter entirely. Splits out event-draining
            # time and the gap since the *previous loop iteration started*
            # (not just since the last flip) to localize where the time goes:
            # scheduling delay before we even reach event processing, a flood
            # of queued touch events, or the update/draw/flip stages proper.
            if _t_last_flip is not None:
                gap_ms = (t3 - _t_last_flip) * 1000
                if gap_ms > 33.0:
                    log.warning(
                        "slow frame: gap=%.1fms  since_last_iter=%.1fms "
                        "(prev_tick=%.1fms)  events=%.1fms(n=%d)  update=%.1fms  "
                        "draw=%.1fms  flip=%.1fms  (rgb565=%.1fms  mmap-wait=%.1fms)  "
                        "fps=%d paces_loop=%s  view=%s peeking=%s dragging=%s",
                        gap_ms, (t_loop_top - _t_last_flip) * 1000, _prev_tick_ms,
                        (t_ev - t_loop_top) * 1000, n_events,
                        (t1 - t0) * 1000, (t2 - t1) * 1000, (t3 - t2) * 1000,
                        (fb.t_rgb565 * 1000) if fb else 0.0,
                        ((t3 - t2) - (fb.t_rgb565 if fb else 0.0)) * 1000,
                        fps, fb.paces_loop if fb else None,
                        display._view.name, display._peeking, display._panel_touch,
                    )
            _t_last_flip = t3

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

        # DRM page-flip blocks on vsync — normally rate-limited by hardware.
        # When target fps drops below 60 (idle mode), also call clock.tick so the
        # loop sleeps after the vsync to honour the lower rate.
        # fbdev path: target 58fps (1000/58=17ms → 58.8fps, just below 60Hz display
        # rate so the write never catches the scan — no rolling tear line).
        _ticked = not (drew and fb and fb.paces_loop) or (drew and fps < 60)
        if _ticked:
            _t_tick0 = time.perf_counter()
            clock.tick(min(fps, 58) if drew else min(fps, 15))
            _prev_tick_ms = (time.perf_counter() - _t_tick0) * 1000
        else:
            _prev_tick_ms = 0.0

    log.info("Shutting down")
    player.disconnect()
    if fb:
        fb.close()
    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
