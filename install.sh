#!/usr/bin/env bash
# album2 install script — run as the pi user, not root
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/venv"
VENV_PYTHON="$VENV/bin/python"
USER="$(whoami)"

echo "=== album2 install ==="
echo "User: $USER  Dir: $SCRIPT_DIR"

# ── System packages ────────────────────────────────────────────────────────────
sudo apt-get update -q
sudo apt-get install -y --no-install-recommends \
    python3-pip python3-venv \
    libsdl2-dev libsdl2-image-dev libsdl2-ttf-dev \
    libjpeg-dev zlib1g-dev libfreetype6-dev \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
    bluez git

# ── Python venv (mopidy + album2 deps together) ───────────────────────────────
cd "$SCRIPT_DIR"
if [ ! -f "$VENV/bin/activate" ]; then
    python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --upgrade pip --quiet
pip install --quiet \
    "mopidy>=4.0" \
    "mopidy-local>=4.0" \
    "mopidy-mpd>=4.0"
pip install --quiet -r requirements.txt

# ── Mopidy config ─────────────────────────────────────────────────────────────
MOPIDY_CONF="$HOME/.config/mopidy/mopidy.conf"
if [ ! -f "$MOPIDY_CONF" ]; then
    mkdir -p "$(dirname "$MOPIDY_CONF")"
    cat > "$MOPIDY_CONF" << 'CONF'
[core]
restore_state = true

[logging]
verbosity = 0
color = false

[audio]
output = autoaudiosink

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

[file]
enabled = false

[stream]
enabled = false
CONF
    echo "Wrote default mopidy.conf — edit audio.output for your DAC"
fi

# ── Sudoers rule for shutdown/reboot ──────────────────────────────────────────
SUDOERS_FILE="/etc/sudoers.d/album2"
sudo tee "$SUDOERS_FILE" > /dev/null << SUDOERS
$USER ALL=(ALL) NOPASSWD: /sbin/reboot, /sbin/shutdown
SUDOERS
sudo chmod 0440 "$SUDOERS_FILE"

# ── Group membership (framebuffer + input device access) ──────────────────────
sudo usermod -aG video,input "$USER"

# ── Enable linger so /run/user/$UID exists without a login session ────────────
loginctl enable-linger "$USER"

# ── Systemd service files ─────────────────────────────────────────────────────
UID_NUM="$(id -u)"

sudo tee /etc/systemd/system/mopidy.service > /dev/null << UNIT
[Unit]
Description=Mopidy music server
After=network.target sound.target

[Service]
User=$USER
ExecStart=$VENV_PYTHON -m mopidy
Restart=on-failure
RestartSec=5
Environment=HOME=$HOME
Environment=XDG_RUNTIME_DIR=/run/user/$UID_NUM
Environment=PULSE_SERVER=unix:/run/user/$UID_NUM/pulse/native

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/album2.service > /dev/null << UNIT
[Unit]
Description=Album2 music player
After=network.target mopidy.service
Wants=mopidy.service

[Service]
User=$USER
WorkingDirectory=$SCRIPT_DIR
Environment=XDG_RUNTIME_DIR=/run/user/$UID_NUM
Environment=PULSE_SERVER=unix:/run/user/$UID_NUM/pulse/native
ExecStart=$VENV_PYTHON $SCRIPT_DIR/main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/bootscreen.service > /dev/null << UNIT
[Unit]
Description=Album2 boot screen
DefaultDependencies=no
After=local-fs.target
Before=album2.service

[Service]
User=$USER
ExecStart=$VENV_PYTHON $SCRIPT_DIR/bootscreen.py
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=basic.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable mopidy.service album2.service bootscreen.service

echo ""
echo "=== Done ==="
echo "Add music to ~/Music, then run:  $VENV_PYTHON -m mopidy local scan"
echo "Start services:  sudo systemctl start mopidy album2"
echo "Boot screen:     sudo systemctl start bootscreen"
echo "View logs:       journalctl -u album2 -f"
echo "Reboot to test full boot sequence."
