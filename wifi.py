from __future__ import annotations
import shutil
import subprocess
import logging

log = logging.getLogger(__name__)

_WPA   = "/usr/sbin/wpa_cli"
_IFACE = "wlan0"

_HAS_NMCLI = bool(shutil.which("nmcli"))


def _dbm_to_pct(dbm: int) -> int:
    return min(100, max(0, 2 * (dbm + 100)))


# ── nmcli backend ─────────────────────────────────────────────────────────────

def _nm(*args, timeout=10) -> str:
    try:
        r = subprocess.run(
            ["sudo", "nmcli", *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout
    except Exception as e:
        raise RuntimeError(e) from e


class _NMWiFi:
    """NetworkManager backend (Raspberry Pi OS Bookworm+)."""

    @property
    def available(self) -> bool:
        try:
            _nm("radio", "wifi")
            return True
        except Exception:
            return False

    def is_enabled(self) -> bool:
        try:
            return "enabled" in _nm("radio", "wifi")
        except Exception:
            return True

    def set_enabled(self, on: bool):
        _nm("radio", "wifi", "on" if on else "off")

    def _saved_ssids(self) -> set[str]:
        out = _nm("-t", "-f", "NAME,TYPE", "con", "show")
        ssids = set()
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and "wireless" in parts[1]:
                ssids.add(parts[0])
        return ssids

    def _active_ssid(self) -> str:
        out = _nm("-t", "-f", "NAME,TYPE,STATE,DEVICE", "con", "show", "--active")
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 4 and "wireless" in parts[1] and _IFACE in parts[3]:
                return parts[0]
        return ""

    def get_networks(self) -> list[dict]:
        try:
            _nm("dev", "wifi", "rescan", timeout=5)
        except Exception:
            pass
        try:
            out   = _nm("-t", "-f", "IN-USE,SSID,SIGNAL", "dev", "wifi", "list")
            saved = self._saved_ssids()
            connected_ssid = self._active_ssid()
            seen: dict[str, dict] = {}
            for line in out.splitlines():
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                in_use, ssid, sig_s = parts[0], parts[1], parts[2]
                if not ssid:
                    continue
                try:
                    signal = int(sig_s)
                except ValueError:
                    signal = 0
                is_conn = bool(in_use.strip() == "*") or ssid == connected_ssid
                if ssid not in seen or is_conn:
                    seen[ssid] = {
                        "name":      ssid,
                        "signal":    signal,
                        "connected": is_conn,
                        "saved":     ssid in saved,
                    }
            result = list(seen.values())
            result.sort(key=lambda n: (not n["connected"], -n["signal"]))
            return result
        except Exception as e:
            log.warning("NM get_networks failed: %s", e)
            return []

    def connect(self, name: str) -> bool:
        try:
            _nm("con", "up", "id", name, timeout=15)
            return True
        except Exception as e:
            log.warning("NM connect %r failed: %s", name, e)
            return False

    def connect_new(self, ssid: str, password: str) -> bool:
        try:
            out = _nm("dev", "wifi", "connect", ssid,
                      "password", password, "ifname", _IFACE, timeout=20)
            return "successfully activated" in out
        except Exception as e:
            log.warning("NM connect_new %r failed: %s", ssid, e)
            return False

    def disconnect(self, name: str):
        try:
            _nm("dev", "disconnect", _IFACE)
        except Exception as e:
            log.warning("NM disconnect failed: %s", e)

    def forget(self, name: str):
        try:
            _nm("con", "delete", "id", name)
        except Exception as e:
            log.warning("NM forget %r failed: %s", name, e)


# ── wpa_cli backend ────────────────────────────────────────────────────────────

def _cli(*args, timeout=8) -> str:
    try:
        r = subprocess.run(
            ["sudo", _WPA, "-i", _IFACE, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout
    except Exception as e:
        raise RuntimeError(e) from e


class _WPAWiFi:
    """wpa_supplicant/dhcpcd backend (older Raspberry Pi OS)."""

    @property
    def available(self) -> bool:
        try:
            return "wpa_state" in _cli("status")
        except Exception:
            return False

    def is_enabled(self) -> bool:
        try:
            r = subprocess.run(["rfkill", "list", "wifi"],
                               capture_output=True, text=True, timeout=3)
            return "Soft blocked: yes" not in r.stdout
        except Exception:
            return True

    def set_enabled(self, on: bool):
        cmd = "unblock" if on else "block"
        subprocess.run(["sudo", "rfkill", cmd, "wifi"],
                       capture_output=True, timeout=5)

    def _saved_networks(self) -> dict[str, int]:
        out = _cli("list_networks")
        saved = {}
        for line in out.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 2:
                try:
                    saved[parts[1]] = int(parts[0])
                except ValueError:
                    pass
        return saved

    def _connected_ssid(self) -> str:
        out = _cli("status")
        for line in out.splitlines():
            if line.startswith("ssid="):
                state = [l for l in out.splitlines() if l.startswith("wpa_state=")]
                if state and "COMPLETED" in state[0]:
                    return line[5:]
        return ""

    def get_networks(self) -> list[dict]:
        try:
            saved     = self._saved_networks()
            connected = self._connected_ssid()
            _cli("scan")
            out = _cli("scan_results")
            seen: dict[str, dict] = {}
            for line in out.splitlines()[1:]:
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
                is_conn = ssid == connected
                if ssid not in seen or is_conn:
                    seen[ssid] = {
                        "name":      ssid,
                        "signal":    signal,
                        "connected": is_conn,
                        "saved":     ssid in saved,
                    }
            result = list(seen.values())
            result.sort(key=lambda n: (not n["connected"], -n["signal"]))
            return result
        except Exception as e:
            log.warning("get_networks failed: %s", e)
            return []

    def connect(self, name: str) -> bool:
        try:
            saved  = self._saved_networks()
            if name not in saved:
                return False
            net_id = saved[name]
            _cli("select_network", str(net_id))
            _cli("save_config")
            subprocess.run(["sudo", "dhcpcd", "-n", _IFACE],
                           capture_output=True, timeout=15)
            return True
        except Exception as e:
            log.warning("WiFi connect %r failed: %s", name, e)
            return False

    def connect_new(self, ssid: str, password: str) -> bool:
        try:
            net_id = int(_cli("add_network").strip())
            _cli("set_network", str(net_id), "ssid", f'"{ssid}"')
            _cli("set_network", str(net_id), "psk",  f'"{password}"')
            _cli("enable_network", str(net_id))
            import time; time.sleep(5)
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

    def disconnect(self, name: str):
        try:
            _cli("disconnect")
        except Exception as e:
            log.warning("WiFi disconnect %r failed: %s", name, e)

    def forget(self, name: str):
        try:
            saved = self._saved_networks()
            if name in saved:
                _cli("remove_network", str(saved[name]))
                _cli("save_config")
        except Exception as e:
            log.warning("WiFi forget %r failed: %s", name, e)


# ── public class ──────────────────────────────────────────────────────────────

class WiFiManager:
    def __init__(self):
        self._impl = _NMWiFi() if _HAS_NMCLI else _WPAWiFi()
        self.available = self._impl.available
        log.info("WiFi backend: %s", "nmcli" if _HAS_NMCLI else "wpa_cli")

    def is_enabled(self) -> bool:
        return self._impl.is_enabled()

    def set_enabled(self, on: bool):
        self._impl.set_enabled(on)
        self.available = self._impl.available

    def get_networks(self) -> list[dict]:
        return self._impl.get_networks()

    def connect(self, name: str) -> bool:
        return self._impl.connect(name)

    def connect_new(self, ssid: str, password: str) -> bool:
        return self._impl.connect_new(ssid, password)

    def disconnect(self, name: str):
        self._impl.disconnect(name)

    def forget(self, name: str):
        self._impl.forget(name)
