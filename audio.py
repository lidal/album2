"""
Audio output sink enumeration and switching.
Supports wpctl (PipeWire) and pactl (PulseAudio).
"""
import re
import subprocess
import logging

log = logging.getLogger(__name__)


def _run(*args, timeout=5) -> str:
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception as e:
        log.debug("audio cmd %s failed: %s", args[0], e)
        return ""


class AudioOutputManager:
    def __init__(self, backend: str):
        self.backend = backend   # "wpctl" | "pactl" | other → unavailable
        self.available = backend in ("wpctl", "pactl")

    def get_sinks(self) -> list[dict]:
        """Return list of {id, name, active} for available audio sinks."""
        if self.backend == "wpctl":
            return self._sinks_wpctl()
        if self.backend == "pactl":
            return self._sinks_pactl()
        return []

    def set_sink(self, sink_id: str):
        """Make sink_id the default output. Blocking."""
        if self.backend == "wpctl":
            _run("wpctl", "set-default", sink_id)
        elif self.backend == "pactl":
            _run("pactl", "set-default-sink", sink_id)

    # ── backends ──────────────────────────────────────────────────────────────

    def _sinks_wpctl(self) -> list[dict]:
        out = _run("wpctl", "status")
        sinks = []
        in_sinks = False
        for line in out.splitlines():
            if "Sinks:" in line:
                in_sinks = True
                continue
            if in_sinks:
                if line.strip() == "" or (line[0] != " " and ":" in line):
                    break
                # e.g. "  *   53. Built-in Audio Analog Stereo [vol: 0.40]"
                m = re.match(r"\s+(\*?)\s*(\d+)\.\s+(.+?)\s*(?:\[|$)", line)
                if m:
                    active = bool(m.group(1))
                    sid    = m.group(2)
                    name   = m.group(3).strip()
                    sinks.append({"id": sid, "name": name, "active": active})
        return sinks

    def _sinks_pactl(self) -> list[dict]:
        out     = _run("pactl", "list", "sinks")
        default = _run("pactl", "get-default-sink").strip()
        sinks   = []
        sid, name = None, None
        for line in out.splitlines():
            m = re.match(r"Sink #(\d+)", line)
            if m:
                sid = m.group(1)
                name = None
            if sid and re.match(r"\s+Name:\s+(.+)", line):
                name = re.match(r"\s+Name:\s+(.+)", line).group(1).strip()
            if sid and name and re.match(r"\s+Description:\s+(.+)", line):
                desc   = re.match(r"\s+Description:\s+(.+)", line).group(1).strip()
                active = (name == default)
                sinks.append({"id": name, "name": desc, "active": active})
                sid, name = None, None
        return sinks
