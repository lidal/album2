import subprocess
import logging

log = logging.getLogger(__name__)


def _nmcli(*args, timeout=8) -> str:
    r = subprocess.run(
        ["nmcli", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout


def _nmcli_ok(*args, timeout=8) -> bool:
    r = subprocess.run(
        ["nmcli", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode == 0


class WiFiManager:
    def __init__(self):
        self.available = False
        try:
            _nmcli("-t", "-f", "VERSION", "general")
            self.available = True
        except Exception as e:
            log.warning("WiFiManager unavailable: %s", e)

    def get_networks(self) -> list[dict]:
        """
        Returns visible WiFi networks sorted by: connected first, then signal desc.
        Each dict: {name, connected, signal, saved}
        """
        if not self.available:
            return []
        try:
            saved_out = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
            saved = set()
            for line in saved_out.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[1] == "802-11-wireless":
                    saved.add(parts[0])

            wifi_out = _nmcli("-t", "-f", "SSID,SIGNAL,ACTIVE", "device", "wifi", "list")
            seen: dict[str, dict] = {}
            for line in wifi_out.splitlines():
                parts = line.split(":")
                if len(parts) < 3:
                    continue
                ssid, sig, active = parts[0], parts[1], parts[2]
                if not ssid:
                    continue
                try:
                    signal = int(sig)
                except ValueError:
                    signal = 0
                connected = active.strip() == "yes"
                if ssid not in seen or connected:
                    seen[ssid] = {
                        "name":      ssid,
                        "signal":    signal,
                        "connected": connected,
                        "saved":     ssid in saved,
                    }

            result = list(seen.values())
            result.sort(key=lambda n: (not n["connected"], -n["signal"]))
            return result
        except Exception as e:
            log.warning("get_networks failed: %s", e)
            return []

    def connect(self, name: str):
        """Connect to a saved network by connection name. Blocking."""
        try:
            _nmcli("connection", "up", name, timeout=20)
        except Exception as e:
            log.warning("WiFi connect %r failed: %s", name, e)

    def connect_new(self, ssid: str, password: str) -> bool:
        """Connect to a new (unsaved) network with a password. Blocking. Returns True on success."""
        try:
            ok = _nmcli_ok("device", "wifi", "connect", ssid, "password", password, timeout=30)
            if not ok:
                # nmcli creates a connection profile even on auth failure — delete it
                try:
                    _nmcli("connection", "delete", ssid, timeout=5)
                except Exception:
                    pass
            return ok
        except Exception as e:
            log.warning("WiFi connect_new %r failed: %s", ssid, e)
            return False

    def disconnect(self, name: str):
        """Disconnect a network by connection name. Blocking."""
        try:
            _nmcli("connection", "down", name, timeout=10)
        except Exception as e:
            log.warning("WiFi disconnect %r failed: %s", name, e)
