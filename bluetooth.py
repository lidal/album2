from __future__ import annotations
import re
import subprocess
import threading
import logging

log = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")


def _run(*args, timeout=5) -> str:
    try:
        r = subprocess.run(
            ["bluetoothctl", *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout
    except Exception as e:
        raise RuntimeError(e) from e


def _dbus_get_devices() -> list[dict]:
    """Query paired/connected state via D-Bus (works on BlueZ 5.50)."""
    import dbus
    bus = dbus.SystemBus()
    obj = bus.get_object("org.bluez", "/")
    iface = dbus.Interface(obj, "org.freedesktop.DBus.ObjectManager")
    objs = iface.GetManagedObjects()
    devices = []
    for path, ifaces in objs.items():
        if "org.bluez.Device1" not in ifaces:
            continue
        dev = ifaces["org.bluez.Device1"]
        if not dev.get("Paired", False):
            continue
        addr = str(dev.get("Address", ""))
        devices.append({
            "address":   addr,
            "name":      str(dev.get("Name", addr)),
            "connected": bool(dev.get("Connected", False)),
            "trusted":   bool(dev.get("Trusted", False)),
        })
    return devices


class BluetoothManager:
    def __init__(self):
        self.available   = False
        self._scan_proc  = None
        self._name_cache: dict[str, str] = {}
        try:
            _run("show")
            self.available = True
        except Exception as e:
            log.warning("Bluetooth unavailable: %s", e)

    # ── paired devices ────────────────────────────────────────────────────────

    def is_powered(self) -> bool:
        try:
            out = _run("show")
            return "Powered: yes" in out
        except Exception:
            return False

    def set_powered(self, on: bool):
        try:
            _run("power", "on" if on else "off", timeout=6)
        except Exception as e:
            log.warning("set_powered %s: %s", on, e)

    def get_devices(self) -> list[dict]:
        """Return paired devices using D-Bus (bluetoothctl filter args not in BlueZ 5.50)."""
        if not self.available:
            return []
        try:
            devices = _dbus_get_devices()
            return sorted(devices, key=lambda d: d["name"].casefold())
        except Exception as e:
            log.warning("get_devices: %s", e)
            return []

    # ── scan / discovery ──────────────────────────────────────────────────────

    def start_scan(self):
        if self._scan_proc is not None:
            return
        self._name_cache = {}
        try:
            self._scan_proc = subprocess.Popen(
                ["bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self._scan_proc.stdin.write(b"scan on\n")
            self._scan_proc.stdin.flush()
            threading.Thread(target=self._read_scan_output, daemon=True).start()
        except Exception as e:
            log.warning("start_scan: %s", e)
            self._scan_proc = None

    def _read_scan_output(self):
        proc = self._scan_proc
        if proc is None:
            return
        try:
            for raw in proc.stdout:
                line = _ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).strip()
                # "[NEW] Device AA:BB:CC:DD:EE:FF Some Name"
                # "[CHG] Device AA:BB:CC:DD:EE:FF Name: Some Name"
                parts = line.split()
                if len(parts) >= 4 and parts[1] == "Device":
                    addr = parts[2]
                    if parts[0] == "[NEW]":
                        name = " ".join(parts[3:])
                        if name and name != addr:
                            self._name_cache[addr] = name
                    elif parts[0] == "[CHG]" and len(parts) >= 5 and parts[3] == "Name:":
                        name = " ".join(parts[4:])
                        if name:
                            self._name_cache[addr] = name
        except Exception:
            pass

    def stop_scan(self):
        if self._scan_proc is not None:
            try:
                self._scan_proc.stdin.write(b"scan off\nexit\n")
                self._scan_proc.stdin.flush()
            except Exception:
                pass
            try:
                self._scan_proc.terminate()
                self._scan_proc.wait(timeout=2)
            except Exception:
                pass
            self._scan_proc = None
        try:
            subprocess.run(["bluetoothctl", "scan", "off"],
                           capture_output=True, timeout=3)
        except Exception:
            pass

    def get_discovered_devices(self) -> list[dict]:
        """Devices seen during the current scan session only (from _name_cache)."""
        return [{"address": addr, "name": name}
                for addr, name in self._name_cache.items()]

    # ── pairing ───────────────────────────────────────────────────────────────

    def pair(self, address: str) -> bool:
        try:
            out = _run("pair", address, timeout=15)
            return "successful" in out.lower() or "already" in out.lower()
        except Exception as e:
            log.warning("pair %s: %s", address, e)
            return False

    def trust(self, address: str):
        try:
            _run("trust", address)
        except Exception as e:
            log.warning("trust %s: %s", address, e)

    def untrust(self, address: str):
        try:
            _run("untrust", address)
        except Exception as e:
            log.warning("untrust %s: %s", address, e)

    def forget(self, address: str):
        try:
            _run("remove", address)
        except Exception as e:
            log.warning("forget %s: %s", address, e)

    # ── connect / disconnect ──────────────────────────────────────────────────

    def connect(self, address: str):
        if not self.available:
            return
        try:
            _run("connect", address, timeout=10)
        except Exception as e:
            log.warning("connect %s: %s", address, e)

    def disconnect(self, address: str):
        if not self.available:
            return
        try:
            _run("disconnect", address)
        except Exception as e:
            log.warning("disconnect %s: %s", address, e)
