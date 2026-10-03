"""tvbox-app <id>: start one service from services.toml (the process behind
tvbox-app@<id>.service). Builds the command line and replaces itself with it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import NAME, services
from .services import Service
from .util import runtime_dir

BROWSER = os.environ.get("TVBOX_BROWSER", "chromium")
# Fetched from Google on the box by tvbox-widevine-update (not redistributable).
WIDEVINE_DIR = Path(f"/var/lib/{NAME}/WidevineCdm")
NAV_EXTENSION = Path(os.environ.get("TVBOX_DATA_DIR", f"/usr/share/{NAME}")) / "extensions" / "tvnav"
CACHE_BYTES = 256 * 1024 * 1024
# Hardware video decoding through VA-API (names change between Chromium
# versions; unknown ones are ignored).
VAAPI_FEATURES = ("AcceleratedVideoDecodeLinuxGL", "AcceleratedVideoDecodeLinuxZeroCopyGL",
                  "VaapiIgnoreDriverChecks")
DISABLED_FEATURES = ("Translate", "MediaRouter", "GlobalMediaControls")


def profile_dir(service_id: str) -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / NAME / "profiles" / service_id


def devtools_port(service_id: str) -> int | None:
    """Port of the running browser's DevTools endpoint (Chromium writes it
    into the profile when started with --remote-debugging-port=0)."""
    try:
        return int((profile_dir(service_id) / "DevToolsActivePort").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def browser_argv(service: Service, cache_root: Path) -> list[str]:
    argv = [
        BROWSER,
        f"--user-data-dir={profile_dir(service.id)}",
        "--ozone-platform=wayland",
        # An app window (no tabs or address bar); sway makes it fullscreen.
        # Not --kiosk: on cold starts Chromium then sometimes kept drawing
        # with the offsets of its first, smaller window (black bar on the
        # left, page cut off on the right), seen in about 1 of 4 boots.
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",                 # no desktop keyring on the box
        "--remote-debugging-port=0",              # loopback; port lands in the profile
        "--autoplay-policy=no-user-gesture-required",
        "--hide-crash-restore-bubble",
        "--disable-session-crashed-bubble",
        "--noerrdialogs",
        # Cache on tmpfs: fewer disk writes, and it may be lost.
        f"--disk-cache-dir={cache_root / service.id}",
        f"--disk-cache-size={CACHE_BYTES}",
        f"--enable-features={','.join(VAAPI_FEATURES)}",
        f"--disable-features={','.join(DISABLED_FEATURES)}",
    ]
    if service.user_agent:
        argv.append(f"--user-agent={service.user_agent}")
    if service.nav:
        argv.append(f"--load-extension={NAV_EXTENSION}")
    return argv + list(service.flags) + [f"--app={service.url}"]


def prepare_profile(service: Service) -> None:
    profile = profile_dir(service.id)
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "DevToolsActivePort").unlink(missing_ok=True)      # stale after a crash
    # Chromium has no Widevine of its own; this hint file is how it finds a
    # module outside its install directory.
    hint = profile / "WidevineCdm" / "latest-component-updated-widevine-cdm"
    if (WIDEVINE_DIR / "manifest.json").exists():
        hint.parent.mkdir(exist_ok=True)
        hint.write_text(json.dumps({"Path": str(WIDEVINE_DIR)}))
    else:
        hint.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: tvbox-app <service-id>", file=sys.stderr)
        return 2
    found, errors = services.load_best()
    for line in errors:
        print(f"services: {line}", file=sys.stderr)
    service = next((s for s in found if s.id == args[0]), None)
    if not service:
        print(f"tvbox-app: no service {args[0]!r} in services.toml", file=sys.stderr)
        return 2
    if service.kind == "browser":
        prepare_profile(service)
        command = browser_argv(service, runtime_dir() / "cache")
    else:
        command = list(service.exec)
    try:
        os.execvp(command[0], command)
    except OSError as err:
        print(f"tvbox-app: cannot start {command[0]}: {err}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
