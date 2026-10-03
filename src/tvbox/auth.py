"""Phone pairing and access rules for the hub.

The TV (loopback) is trusted, apart from the origin checks that keep web
pages in the box's own browsers out. A phone on the LAN pairs once: the TV
shows a QR code with a one-time token (valid a few minutes); visiting it
trades the token for a long-lived device token in a cookie. Only a hash of
each device token is stored, so the file on disk can't be replayed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from . import NAME

COOKIE = f"{NAME}_device"
PAIRING_TTL_S = 300
LOOPBACK = ("127.0.0.1", "::1")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def default_store_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / NAME / "devices.json"


@dataclass
class Device:
    id: str             # short public id, used to revoke
    name: str
    token_hash: str
    created: float
    last_seen: float

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "created": self.created, "last_seen": self.last_seen}


class DeviceStore:
    """Paired devices, persisted as JSON (mode 600)."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_store_path()
        self.devices: dict[str, Device] = {}
        self._pairing: dict[str, float] = {}    # one-time token hash -> expiry
        self._dirty_seen = 0.0
        try:
            for raw in json.loads(self.path.read_text()):
                device = Device(**raw)
                self.devices[device.id] = device
        except (OSError, ValueError, TypeError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".new")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump([vars(d) for d in self.devices.values()], f, indent=1)
        os.replace(tmp, self.path)

    # -- pairing ------------------------------------------------------------
    def start_pairing(self, now: float | None = None) -> tuple[str, float]:
        """New one-time token and its expiry. Older unused ones stay valid
        until they expire (the QR code on screen may be redrawn)."""
        now = time.time() if now is None else now
        self._pairing = {h: t for h, t in self._pairing.items() if t > now}
        token = secrets.token_urlsafe(16)
        expires = now + PAIRING_TTL_S
        self._pairing[_hash(token)] = expires
        return token, expires

    def pair(self, token: str, name: str, now: float | None = None) -> tuple[Device, str] | None:
        """Trade a one-time token for a device token; None if invalid."""
        now = time.time() if now is None else now
        expires = self._pairing.pop(_hash(token), None) if token else None
        if expires is None or expires < now:
            return None
        device_token = secrets.token_urlsafe(32)
        device = Device(secrets.token_hex(4), name[:60] or "phone", _hash(device_token), now, now)
        self.devices[device.id] = device
        self.save()
        return device, device_token

    # -- checking -----------------------------------------------------------
    def check(self, device_token: str | None, now: float | None = None) -> Device | None:
        if not device_token:
            return None
        wanted = _hash(device_token)
        for device in self.devices.values():
            if hmac.compare_digest(device.token_hash, wanted):
                now = time.time() if now is None else now
                device.last_seen = now
                if now - self._dirty_seen > 3600:   # don't write on every request
                    self._dirty_seen = now
                    self.save()
                return device
        return None

    def revoke(self, device_id: str) -> bool:
        if self.devices.pop(device_id, None) is None:
            return False
        self.save()
        return True

    def listing(self) -> list[dict]:
        return sorted((d.public() for d in self.devices.values()), key=lambda d: d["created"])


def device_name(user_agent: str) -> str:
    """A readable name for a phone from its user agent."""
    for marker, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android phone"),
                         ("Macintosh", "Mac"), ("Windows", "Windows PC"), ("Linux", "Linux PC")):
        if marker in user_agent:
            return name
    return "phone"


def classify(remote: str | None, host: str, origin: str | None, path: str, port: int,
             extension_origin: str = "", method: str = "GET") -> str:
    """Who is asking: "tv" (loopback, trusted), "extension" (our browser
    extension, may only report text focus), "lan" (needs a paired device),
    "public" (an unpaired phone loading the pairing page), or "deny"."""
    local_hosts = {f"{h}:{port}" for h in ("127.0.0.1", "localhost")}
    if remote in LOOPBACK:
        if host not in local_hosts:
            return "deny"                       # DNS rebinding
        if not origin or origin.split("://", 1)[-1] in local_hosts:
            return "tv"
        if origin == extension_origin and method == "POST" and path == "/api/cmd":
            return "extension"
        return "deny"                           # a web page in one of our browsers
    # LAN: the only acceptable origin is the box itself, as the phone sees it.
    if origin and origin != f"http://{host}":
        return "deny"
    if path == "/pair" or path.startswith("/static/"):
        return "public"         # the pairing link, and the files its pages need
    return "lan"
