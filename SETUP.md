# album2 — Setup Guide

Target: Raspberry Pi Zero 2W + HyperPixel Square 4.0, headless (no desktop).

---

## 1. Flash the OS

Use **Raspberry Pi OS Lite (64-bit, Buster legacy)**. Find the image at:
`https://downloads.raspberrypi.com/raspios_lite_arm64/images/` — pick the last build from 2021.

In Raspberry Pi Imager, click the **gear icon** (or `Ctrl+Shift+X`) before writing and configure:

- Hostname: `album` (you'll SSH in as `album.local`)
- Enable SSH, password authentication
- Username `pi`, set a password
- Wi-Fi SSID, password, and country code
- Timezone and keyboard layout

Write the card, insert into the Pi, power on.

---

## 2. First boot

```bash
ssh pi@album.local
sudo apt-get update && sudo apt-get upgrade -y
sudo raspi-config
```

In raspi-config: **Boot Options → Desktop / CLI → Console Autologin**

This disables the desktop entirely — the Pi boots straight to a TTY as `pi`, which is what the systemd service and framebuffer display need. Reboot when done.

---

## 3. Install HyperPixel

```bash
curl https://get.pimoroni.com/hyperpixel4 | bash
```

When prompted:
1. **Which Pi?** → `Raspberry Pi 3`
2. **Which HyperPixel 4?** → `Square (Pre-2020)`

Reboot when the installer finishes. The display should show a console after reboot.

---

## 4. Run the install script

Copy the project to the Pi (e.g. via `scp -r album2/ pi@album.local:~/album2`), then:

```bash
cd ~/album2
bash install.sh
```

This installs all system packages, Python dependencies, the Mopidy extensions, and registers the systemd service. It will print a sample `mopidy.conf` if you don't have one yet.

The script also writes a sudoers rule so the app can shut down or reboot the Pi from its Settings screen without a password prompt:

```
/etc/sudoers.d/album2
pi ALL=(ALL) NOPASSWD: /sbin/reboot, /sbin/shutdown
```

If you skipped the install script, add this rule manually with `sudo visudo -f /etc/sudoers.d/album2`.

---

## 5. Configure Mopidy

```bash
nano ~/.config/mopidy/mopidy.conf
```

Minimum config — adjust the audio output line to match your DAC (`aplay -l` lists cards):

```ini
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
output = alsasink device=hw:CARD=KA3,DEV=0
```

Copy music to `~/Music`, then scan:

```bash
mopidy local scan
```

---

## 6. Configure album2

Edit `config.py` for your hardware:

```python
FULLSCREEN       = True
MUSIC_DIR        = "/home/pi/Music"
VOLUME_I2C_BUS   = 11    # HyperPixel soft-I2C bus
VOLUME_I2C_ADDR  = 0x48  # ADS1x15 with ADDR pin tied to GND
```

Verify the volume knob is visible on the soft-I2C bus set up by the HyperPixel overlay
(do **not** enable hardware I2C via raspi-config — it conflicts with HyperPixel GPIO):

```bash
i2cdetect -y 11
```

You should see `48`. If bus 11 doesn't exist, check `dmesg | grep i2c`.

---

## 7. Start

```bash
sudo systemctl start mopidy
sudo systemctl start album2
```

Both services are already enabled at boot by the install script. Check logs if something's wrong:

```bash
journalctl -u album2 -f
journalctl -u mopidy -f
```

---

## Fast boot (optional)

Add to `/boot/config.txt`:

```
gpu_mem=64
```

Add `quiet` to the end of the single line in `/boot/cmdline.txt`.

Disable unused services:

```bash
sudo systemctl disable triggerhappy
```

---

## Quick reference

| Task | Command |
|---|---|
| Start app | `sudo systemctl start album2` |
| Stop app | `sudo systemctl stop album2` |
| View logs | `journalctl -u album2 -f` |
| Rescan music | `mopidy local scan` |
| Run manually | `cd ~/album2 && source venv/bin/activate && python main.py` |
| Check I2C | `i2cdetect -y 11` |
| List audio devices | `aplay -l` |
