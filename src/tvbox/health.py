"""System health for the phone's health page: services, restarts,
temperature, uptime, disk, memory."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

from . import NAME
from .audio import run

USER_UNITS = f"{NAME}-*"
SYSTEM_UNITS = ("greetd.service", "NetworkManager.service", "bluetooth.service", "sshd.service",
                f"{NAME}-widevine.service")
RECENT_RESTARTS = 20


def cpu_temperature(root: Path = Path("/sys/class")) -> float | None:
    """Hottest CPU sensor in °C: coretemp (Intel) if present, else the
    x86 package thermal zone, else any thermal zone."""
    temps = []
    for hwmon in (root / "hwmon").glob("hwmon*"):
        try:
            if (hwmon / "name").read_text().strip() in ("coretemp", "k10temp", "zenpower"):
                temps += [int(f.read_text()) / 1000 for f in hwmon.glob("temp*_input")]
        except (OSError, ValueError):
            continue
    if not temps:
        zones = sorted((root / "thermal").glob("thermal_zone*"))
        preferred = [z for z in zones if _read(z / "type") == "x86_pkg_temp"] or zones
        for zone in preferred:
            try:
                temps.append(int((zone / "temp").read_text()) / 1000)
            except (OSError, ValueError):
                continue
    return round(max(temps), 1) if temps else None


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def parse_show(text: str) -> list[dict]:
    """`systemctl show` output (blocks of key=value) -> list of dicts."""
    units, current = [], {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if current:
                units.append(current)
            current = {}
        elif "=" in line:
            key, value = line.split("=", 1)
            current[key] = value
    return units


def parse_restarts(text: str) -> list[dict]:
    """Restart events from `journalctl -o json` lines, newest first."""
    events = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        unit = entry.get("USER_UNIT") or entry.get("UNIT") or entry.get("_SYSTEMD_USER_UNIT") or "?"
        try:
            when = int(entry["__REALTIME_TIMESTAMP"]) / 1e6
        except (KeyError, ValueError):
            continue
        events.append({"unit": unit, "time": when, "message": entry.get("MESSAGE", "")})
    return sorted(events, key=lambda e: e["time"], reverse=True)[:RECENT_RESTARTS]


async def _units(user: bool) -> list[dict]:
    props = "-p", "Id,Description,LoadState,ActiveState,SubState,NRestarts,ActiveEnterTimestamp"
    args = ["systemctl", "--user", "show", *props, "--all", USER_UNITS] if user else \
        ["systemctl", "show", *props, *SYSTEM_UNITS]
    code, out = await run(*args)
    if code != 0:
        return []
    return [{"unit": u.get("Id", "?"), "description": u.get("Description", ""),
             "state": u.get("ActiveState", "?"), "sub": u.get("SubState", ""),
             "restarts": int(u.get("NRestarts") or 0), "since": u.get("ActiveEnterTimestamp", "")}
            for u in parse_show(out) if u.get("Id") and u.get("LoadState") == "loaded"]


async def collect() -> dict:
    user_units, system_units, (code, journal) = await asyncio.gather(
        _units(True), _units(False),
        run("journalctl", "--user", "-o", "json", "--no-pager", "-n", "300",
            "-g", "Scheduled restart job|Main process exited", "--since", "-7d"))
    disk = shutil.disk_usage("/")
    meminfo = {}
    for line in _read(Path("/proc/meminfo")).splitlines():
        key, _, rest = line.partition(":")
        meminfo[key] = int(rest.split()[0]) if rest.split() else 0
    try:
        uptime = float(_read(Path("/proc/uptime")).split()[0])
    except (IndexError, ValueError):
        uptime = None
    root_options = ""
    for line in _read(Path("/proc/self/mounts")).splitlines():
        fields = line.split()
        if len(fields) > 3 and fields[1] == "/":
            root_options = fields[3]
    return {
        "time": time.time(),
        "uptime_s": uptime,
        "load": list(os.getloadavg()),
        "cpu_temp_c": cpu_temperature(),
        "disk": {"total": disk.total, "free": disk.free},
        "memory": {"total": meminfo.get("MemTotal", 0) * 1024, "available": meminfo.get("MemAvailable", 0) * 1024},
        # booted into a snapshot from the boot menu (read-only root with an overlay)
        "snapshot_boot": "@snapshots" in root_options or _read(Path("/proc/cmdline")).find("@snapshots") >= 0,
        "services": user_units,
        "system": system_units,
        "restarts": parse_restarts(journal) if code == 0 else [],
    }
