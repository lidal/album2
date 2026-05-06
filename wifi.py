from __future__ import annotations
import subprocess
import logging

log = logging.getLogger(__name__)

_WPA = "/usr/sbin/wpa_cli"
_IFACE = "wlan0"


def _cli(*args, timeout=8) -> str:
    try:
        r = subprocess.run(
            ["sudo", _WPA, "-i", _IFACE, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout
    except Exception as e:
        raise RuntimeError(e) from e


def _dbm_to_pct(dbm: int) -> int:
    """Convert dBm signal level to 0-100%."""
    return min(100, max(0, 2 * (dbm + 100)))


class WiFiManager:
    def __init__(self):
        self.available = False
        try:
            out = _cli("status")
            if "wpa_state" in out:
                self.available = True
        except Exception as e:
            log.warning("WiFiManager unavailable: %s", e)

    def _saved_networks(self) -> dict[str, int]:
        """Returns {ssid: network_id} for all saved networks."""
        out = _cli("list_networks")
        saved = {}
        for line in out.splitlines()[1:]:   # skip header
            parts = line.split("\t")
            if len(parts) >= 2:
                try:
                    saved[parts[1]] = int(parts[0])
                except ValueError:
                    pass
        return saved

    def _connected_ssid(self) -> str:
        """Returns the currently connected SSID or empty string."""
        out = _cli("status")
        for line in out.splitlines():
            if line.startswith("ssid="):
                state_line = [l for l in out.splitlines() if l.startswith("wpa_state=")]
                if state_line and "COMPLETED" in state_line[0]:
                    return line[5:]
        return ""

    def get_networks(self) -> list[dict]:
        if not self.available:
            return []
        try:
            saved = self._saved_networks()
            connected = self._connected_ssid()

            _cli("scan")   # trigger scan; results from previous scan are available immediately
            out = _cli("scan_results")

            seen: dict[str, dict] = {}
            for line in out.splitlines()[1:]:   # skip header
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                ssid = parts[4].strip()
                if not ssid:
                    continue
                try:
                    signal = _dbm_to_pct(int(parts[2]))
                except ValueError:
                    signal = 0
                is_connected = ssid == connected
                if ssid not in seen or is_connected:
                    seen[ssid] = {
                        "name":      ssid,
                        "signal":    signal,
                        "connected": is_connected,
                        "saved":     ssid in saved,
                    }

            result = list(seen.values())
            result.sort(key=lambda n: (not n["connected"], -n["signal"]))
            return result
        except Exception as e:
            log.warning("get_networks failed: %s", e)
            return []

    def connect(self, name: str) -> bool:
        """Connect to a saved network by SSID. Blocking."""
        try:
            saved = self._saved_networks()
            if name not in saved:
                return False
            net_id = saved[name]
            _cli("select_network", str(net_id))
            _cli("save_config")
            # Ask dhcpcd to renew
            subprocess.run(["sudo", "dhcpcd", "-n", _IFACE],
                           capture_output=True, timeout=15)
            return True
        except Exception as e:
            log.warning("WiFi connect %r failed: %s", name, e)
            return False

    def connect_new(self, ssid: str, password: str) -> bool:
        """Connect to a new network with a password. Blocking. Returns True on success."""
        try:
            net_id_line = _cli("add_network").strip()
            net_id = int(net_id_line)
            _cli("set_network", str(net_id), "ssid", f'"{ssid}"')
            _cli("set_network", str(net_id), "psk",  f'"{password}"')
            _cli("enable_network", str(net_id))
            import time; time.sleep(5)   # give supplicant time to associate
            connected = self._connected_ssid() == ssid
            if connected:
                _cli("save_config")
                subprocess.run(["sudo", "dhcpcd", "-n", _IFACE],
                               capture_output=True, timeout=15)
            else:
                _cli("remove_network", str(net_id))
                _cli("save_config")
            return connected
        except Exception as e:
            log.warning("WiFi connect_new %r failed: %s", ssid, e)
            return False

    def is_enabled(self) -> bool:
        """Return True if WiFi radio is not soft-blocked."""
        try:
            r = subprocess.run(["rfkill", "list", "wifi"],
                               capture_output=True, text=True, timeout=3)
            return "Soft blocked: yes" not in r.stdout
        except Exception:
            return True

    def set_enabled(self, on: bool):
        try:
            cmd = "unblock" if on else "block"
            subprocess.run(["sudo", "rfkill", cmd, "wifi"],
                           capture_output=True, timeout=5)
        except Exception as e:
            log.warning("WiFi set_enabled %s: %s", on, e)

    def disconnect(self, name: str):
        """Disconnect current network."""
        try:
            _cli("disconnect")
        except Exception as e:
            log.warning("WiFi disconnect %r failed: %s", name, e)

    def forget(self, name: str):
        """Remove a saved network by SSID."""
        try:
            saved = self._saved_networks()
            if name in saved:
                _cli("remove_network", str(saved[name]))
                _cli("save_config")
        except Exception as e:
            log.warning("WiFi forget %r failed: %s", name, e)
