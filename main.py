#!/usr/bin/env python3
"""
Album2 — Mopidy frontend
for Raspberry Pi Zero 2W + HyperPixel Square 4.0
"""
import os
import sys
import logging
import pygame

from config    import SCREEN_WIDTH, SCREEN_HEIGHT, DISPLAY_WIDTH, DISPLAY_HEIGHT, FULLSCREEN, ROTATE_DISPLAY, VOLUME_SIMULATE
from player    import MopidyPlayer
from volume    import VolumeController, VolumeSimulator
from display   import AlbumDisplay
from bluetooth import BluetoothManager
from wifi      import WiFiManager
from audio     import AudioOutputManager
from spotify   import SpotifyBrowser
import settings; settings.load()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("album2")

_BARE_METAL  = not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
_READY_FLAG  = "/tmp/album2.ready"


def _signal_ready():
    """Tell bootscreen.py to exit by creating the ready flag."""
    try:
        open(_READY_FLAG, "w").close()
    except Exception:
        pass


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
    volume  = VolumeSimulator() if VOLUME_SIMULATE else VolumeController()
    bt      = BluetoothManager()
    wifi    = WiFiManager()
    audio   = AudioOutputManager(player._vol_backend)
    spotify = SpotifyBrowser()
    spotify.configure(
        settings.get("spotify_client_id") or "",
        settings.get("spotify_client_secret") or "",
    )
    display = AlbumDisplay(screen, player, volume, bt, wifi, audio, spotify=spotify)

    # Everything is ready — tell bootscreen to exit and draw the first frame
    # before bootscreen clears the display.
    display.draw()
    if fb:
        fb.flip(screen)
    _signal_ready()

    clock   = pygame.time.Clock()
    running = True

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False
            else:
                display.handle_event(event)

        display.update()
        fps = display.target_fps()
        drew = display.draw()
        if drew:
            if fb:
                fb.flip(screen)
            else:
                pygame.display.flip()
        # When nothing changed, cap at 10 fps to avoid burning a core on busy-wait.
        clock.tick(fps if drew else min(fps, 10))

    log.info("Shutting down")
    try:
        os.remove(_READY_FLAG)
    except FileNotFoundError:
        pass
    player.disconnect()
    if fb:
        fb.close()
    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
