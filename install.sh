#!/usr/bin/env bash
# album2 install script — run as the pi user, not root
set -euo pipefail

echo "=== album2 install ==="

# ── System packages ────────────────────────────────────────────────────────────
sudo apt-get update -q
sudo apt-get install -y \
    python3-pip python3-venv \
    libsdl2-dev libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev \
    libjpeg-dev zlib1g-dev libfreetype6-dev \
    python3-smbus i2c-tools \
    bluez \
    mopidy mopidy-local

# ── Mopidy extensions ─────────────────────────────────────────────────────────
sudo pip3 install \
    Mopidy-MPD \
    Mopidy-HTTP \
    Mopidy-Local

# ── Python venv for album2 ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

python3 -m venv --system-site-packages venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# ── Mopidy config hint ────────────────────────────────────────────────────────
if [ ! -f "$HOME/.config/mopidy/mopidy.conf" ]; then
    echo ""
    echo "=== Mopidy configuration ==="
    echo "No mopidy.conf found. Create one at ~/.config/mopidy/mopidy.conf:"
    echo ""
    cat << 'CONF'
[mpd]
enabled = true
hostname = 127.0.0.1
port = 6600

[http]
enabled = true
hostname = 127.0.0.1
port = 6680

[local]
enabled = true
media_dir = ~/Music

[audio]
# find your DAC card name with:  aplay -l
output = alsasink device=hw:CARD=KA3,DEV=0
CONF
fi

# ── Sudoers rule for shutdown/reboot (used by the in-app buttons) ─────────────
SUDOERS_FILE="/etc/sudoers.d/album2"
sudo tee "$SUDOERS_FILE" > /dev/null << SUDOERS
$USERNAME ALL=(ALL) NOPASSWD: /sbin/reboot, /sbin/shutdown
SUDOERS
sudo chmod 0440 "$SUDOERS_FILE"
echo "Sudoers rule written to $SUDOERS_FILE"

# ── Group membership (framebuffer + input device access) ──────────────────────
sudo usermod -aG video,input "$USERNAME"
echo "Added $USERNAME to video and input groups (re-login required)"

# ── Enable Mopidy service ─────────────────────────────────────────────────────
sudo systemctl enable mopidy

# ── Autostart via systemd (framebuffer, no desktop) ───────────────────────────
UNIT_FILE="/etc/systemd/system/album2.service"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"

sudo tee "$UNIT_FILE" > /dev/null << UNIT
[Unit]
Description=Album2 music player
After=network.target mopidy.service
Wants=mopidy.service

[Service]
User=$USERNAME
WorkingDirectory=$SCRIPT_DIR
Environment=SDL_FBDEV=/dev/fb0
Environment=SDL_MOUSEDRV=TSLIB
Environment=SDL_MOUSEDEV=/dev/input/touchscreen
ExecStart=$VENV_PYTHON $SCRIPT_DIR/main.py
Restart=on-failure
RestartSec=5
TTYPath=/dev/tty1
StandardInput=tty-force
TTYVHangup=yes
TTYReset=yes
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable album2.service

echo ""
echo "=== Done ==="
echo "If mopidy.conf is new, edit it then run:  mopidy local scan"
echo "Start manually:  sudo systemctl start album2"
echo "View logs:       journalctl -u album2 -f"
echo "Check I2C:       i2cdetect -y 11"
echo "List audio:      aplay -l"
