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


def _init_display() -> pygame.Surface:
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        # Bare framebuffer — let SDL_VIDEODRIVER be overridden from env/service,
        # otherwise probe kmsdrm (modern Pi OS) then fbcon (legacy).
        os.environ.setdefault("SDL_FBDEV",    "/dev/fb0")
        os.environ.setdefault("SDL_MOUSEDRV", "TSLIB")
        os.environ.setdefault("SDL_MOUSEDEV", "/dev/input/touchscreen")

        if not os.environ.get("SDL_VIDEODRIVER"):
            for driver in ("kmsdrm", "fbcon"):
                os.environ["SDL_VIDEODRIVER"] = driver
                try:
                    pygame.display.init()
                    log.info("SDL video driver: %s", driver)
                    break
                except pygame.error:
                    pygame.display.quit()
            else:
                raise RuntimeError(
                    "No working SDL video driver found (tried kmsdrm, fbcon). "
                    "Check that your user is in the 'video' group: "
                    "sudo usermod -aG video $USER"
                )
    else:
        if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("SDL_VIDEODRIVER"):
            # Prefer native Wayland over XWayland — gives pixel-accurate scroll
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
    player  = MopidyPlayer()
    volume  = VolumeSimulator() if VOLUME_SIMULATE else VolumeController()
    bt      = BluetoothManager()
    wifi    = WiFiManager()
    audio   = AudioOutputManager(player._vol_backend)
    display = AlbumDisplay(screen, player, volume, bt, wifi, audio)

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
        display.draw()
        pygame.display.flip()
        clock.tick(fps)

    log.info("Shutting down")
    player.disconnect()
    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
