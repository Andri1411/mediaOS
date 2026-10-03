"""tvbox-inputd: reads controllers and remotes via evdev, turns buttons into
actions (bindings.toml) and emits them through one virtual input device, or
to the hub over input.sock.

    devices --evdev--> profile (devices.py) --buttons--> engine --> actions
        key:*, mouse   -> virtual keyboard/mouse (uinput)
        everything else, and navigation while the overlay is open -> input.sock

input.sock speaks JSON lines. Every client gets all events:
    {"event": "hello"|"status", "mode", "app", "devices", "config_errors"}
    {"event": "action", "action": "ui:system_menu", "button": "home"}
    {"event": "nav", "button": "up"}            (mode "ui")
    {"event": "mode", "mode": "app"|"ui"|"mouse"}
    {"event": "focus", "app": "youtube"}
    {"event": "devices", "devices": [...]}
    {"event": "config", "errors": [...]}         (empty = loaded fine)
and may send commands:
    {"cmd": "mode", "mode": "ui"}
    {"cmd": "button", "button": "up", "state": "down"|"up"|"tap"}   (phone remote)
    {"cmd": "action", "action": "volume:+2"}
    {"cmd": "key", "combo": "ctrl+l"}
    {"cmd": "move", "dx": 5, "dy": -3} / {"cmd": "scroll", "dy": 1} / {"cmd": "click", "button": "left"}
    {"cmd": "status"} / {"cmd": "reload"}
A command with an "id" gets a {"reply": id, "ok": bool, "error"?} line back.
"""
from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import math
import os
import signal
from pathlib import Path

import evdev
from evdev import ecodes as e

from . import bindings, sway
from .bindings import BUTTONS, Action, Config, ConfigError
from .devices import VIRTUAL_NAME, Gamepad, Remote, classify
from .engine import MODES, Engine
from .keys import ALL_KEYS, parse_combo
from .util import (IN_ATTRIB, IN_CLOSE_WRITE, IN_CREATE, IN_DELETE, IN_MOVED_FROM,
                   IN_MOVED_TO, Inotify, input_socket, sd_notify, setup_logging,
                   watchdog_interval)

log = setup_logging("inputd")

# Mouse mode (left stick = pointer, right stick = scroll, A = left click,
# X = right click; speeds come from [mouse] in bindings.toml).
MOUSE_HZ = 60
MOUSE_DEADZONE = 0.15
MOUSE_RAMP_S = 0.6         # pointer speed doubles over this long while the stick is held
CURSOR_SHOWN, CURSOR_HIDDEN = "seat * hide_cursor 0", "seat * hide_cursor 100"


class VirtualInput:
    """The one uinput device all output goes through. It exists for the whole
    life of the daemon, so apps never see a device come and go."""

    def __init__(self):
        self.ui = evdev.UInput(
            {e.EV_KEY: ALL_KEYS + [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
             e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL]},
            name=VIRTUAL_NAME, vendor=0x7462, product=0x0001, version=1)

    def tap(self, codes: tuple[int, ...]) -> None:
        """Hold the modifiers, tap the last key, release."""
        for code in codes:
            self.ui.write(e.EV_KEY, code, 1)
        self.ui.syn()
        for code in reversed(codes):
            self.ui.write(e.EV_KEY, code, 0)
        self.ui.syn()

    def click(self, down: bool, button: str = "left") -> None:
        self.ui.write(e.EV_KEY, e.BTN_RIGHT if button == "right" else e.BTN_LEFT, int(down))
        self.ui.syn()

    def move(self, dx: int, dy: int) -> None:
        if dx:
            self.ui.write(e.EV_REL, e.REL_X, dx)
        if dy:
            self.ui.write(e.EV_REL, e.REL_Y, dy)
        self.ui.syn()

    def scroll(self, vertical: int, horizontal: int) -> None:
        if vertical:
            self.ui.write(e.EV_REL, e.REL_WHEEL, vertical)
        if horizontal:
            self.ui.write(e.EV_REL, e.REL_HWHEEL, horizontal)
        self.ui.syn()

    def close(self) -> None:
        self.ui.close()


def load_config(paths: list[Path]) -> tuple[Config, list[str]]:
    """Best usable config and the problems found. A broken user file must not
    take the controller away: fall back to the files below it."""
    errors: list[str] = []
    for count in range(len(paths), 0, -1):
        try:
            return bindings.load(paths[:count]), errors
        except ConfigError as err:
            errors = errors or err.errors
    return Config(), errors


def stick_curve(value: float) -> float:
    """Deadzone, then quadratic response: fine control near the centre."""
    mag = abs(value)
    if mag <= MOUSE_DEADZONE:
        return 0.0
    return math.copysign(((mag - MOUSE_DEADZONE) / (1 - MOUSE_DEADZONE)) ** 2, value)


class InputDaemon:
    def __init__(self, output=None, paths: list[Path] | None = None,
                 socket_path: Path | None = None, device_dir: str | None = "/dev/input",
                 use_sway: bool = True):
        self.paths = paths or bindings.default_paths()
        self.socket_path = socket_path or input_socket()
        self.device_dir = device_dir
        self.use_sway = use_sway
        self.output = output
        self.config_errors: list[str] = []
        self.engine: Engine
        self._devices: dict[str, dict] = {}       # path -> {dev, task, info, translator}
        self._clients: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._mouse_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # -- lifecycle ----------------------------------------------------------
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        config, self.config_errors = load_config(self.paths)
        for err in self.config_errors:
            log.error("bindings: %s", err)
        self.engine = Engine(config, loop, self)
        if self.output is None:
            self.output = VirtualInput()

        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        server = await asyncio.start_unix_server(self._client, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)

        self._inotify = Inotify()
        loop.add_reader(self._inotify.fd, self._on_inotify)
        # inotify can only watch directories that exist; make sure the user's does.
        with contextlib.suppress(OSError):
            self.paths[-1].parent.mkdir(parents=True, exist_ok=True)
        for directory in {p.parent for p in self.paths}:
            self._inotify.watch(directory, IN_CLOSE_WRITE | IN_MOVED_TO | IN_MOVED_FROM
                                | IN_CREATE | IN_DELETE)
        if self.device_dir:
            self._inotify.watch(Path(self.device_dir), IN_CREATE | IN_ATTRIB | IN_DELETE)
            self.scan_devices()
        if self.use_sway:
            self._spawn(self._focus_loop())
        self._spawn(self._watchdog())
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)
        loop.add_signal_handler(signal.SIGHUP, self.reload)

        sd_notify("READY=1")
        log.info("ready: %d device(s), socket %s", len(self._devices), self.socket_path)
        try:
            await self._stop.wait()
        finally:
            sd_notify("STOPPING=1")
            server.close()
            loop.remove_reader(self._inotify.fd)
            for task in list(self._tasks) + [d["task"] for d in self._devices.values()]:
                task.cancel()
            self.engine.cancel_held()
            for writer in self._clients:
                writer.close()
            self._inotify.close()
            self.output.close()
            with contextlib.suppress(FileNotFoundError):
                self.socket_path.unlink()

    def stop(self) -> None:
        self._stop.set()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _debounce(self, key: str, delay: float, callback) -> None:
        if key in self._pending:
            self._pending[key].cancel()
        self._pending[key] = asyncio.get_running_loop().call_later(delay, callback)

    async def _watchdog(self) -> None:
        interval = watchdog_interval()
        while interval:
            sd_notify("WATCHDOG=1")
            await asyncio.sleep(interval)

    def _on_inotify(self) -> None:
        for directory, _mask, name in self._inotify.read():
            if self.device_dir and str(directory) == self.device_dir:
                if name.startswith("event"):
                    self._debounce("devices", 0.3, self.scan_devices)
            elif any(p.parent == directory and p.name == name for p in self.paths):
                self._debounce("config", 0.2, self.reload)

    # -- config -------------------------------------------------------------
    def reload(self) -> None:
        """Re-read the bindings files; keep the current bindings if they are broken."""
        try:
            config = bindings.load(self.paths)
        except ConfigError as err:
            self.config_errors = err.errors
            for line in err.errors:
                log.error("bindings not reloaded: %s", line)
        else:
            self.config_errors = []
            rules_changed = config.devices != self.engine.config.devices
            self.engine.set_config(config)
            log.info("bindings reloaded")
            if rules_changed and self.device_dir:
                self._close_devices()
                self.scan_devices()
        self.broadcast({"event": "config", "errors": self.config_errors})

    # -- devices ------------------------------------------------------------
    def scan_devices(self) -> None:
        for path in sorted(glob.glob(f"{self.device_dir}/event*")):
            if path in self._devices:
                continue
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue        # gone again, or udev has not set permissions yet
            try:
                caps = dev.capabilities(absinfo=True)
                found = classify(dev.name, dev.info.vendor, dev.info.product,
                                 set(caps.get(e.EV_KEY, [])), e.EV_ABS in caps,
                                 self.engine.config.devices)
                if not found:
                    dev.close()
                    continue
                profile, grab, extra = found
                if profile == "gamepad":
                    absinfo = {code: (info.min, info.max) for code, info in caps.get(e.EV_ABS, [])}
                    translator = Gamepad(absinfo, extra)
                else:
                    translator = Remote(extra)
                grabbed = False
                if grab:
                    try:
                        dev.grab()
                        grabbed = True
                    except OSError as err:
                        log.warning("cannot grab %s: %s", dev.name, err)
            except OSError:
                dev.close()
                continue
            info = {"name": dev.name, "path": path, "profile": profile, "grabbed": grabbed,
                    "id": f"{dev.info.vendor:04x}:{dev.info.product:04x}"}
            task = asyncio.get_running_loop().create_task(self._read_device(path, dev, translator))
            self._devices[path] = {"dev": dev, "task": task, "info": info, "translator": translator}
            log.info("device added: %s (%s, %s%s)", dev.name, info["id"], profile,
                     ", grabbed" if grabbed else "")
            self._devices_changed()

    async def _read_device(self, path: str, dev: evdev.InputDevice, translator) -> None:
        held: set[str] = set()
        try:
            async for event in dev.async_read_loop():
                for button, down in translator.feed(event.type, event.code, event.value):
                    (held.add if down else held.discard)(button)
                    self.engine.button(button, down)
        except OSError:
            log.info("device removed: %s", dev.name)
        finally:
            for button in held:
                self.engine.button(button, False)
            with contextlib.suppress(OSError):
                dev.close()
            if self._devices.get(path, {}).get("dev") is dev:
                del self._devices[path]
                self._devices_changed()

    def _close_devices(self) -> None:
        for entry in list(self._devices.values()):
            # Close right away, not when the cancelled task gets to run: the
            # grab has to be gone before the rescan opens the device again.
            with contextlib.suppress(OSError):
                entry["dev"].close()
            entry["task"].cancel()
        self._devices.clear()

    def _devices_changed(self) -> None:
        self._debounce("devices-event", 0.1, lambda: self.broadcast(
            {"event": "devices", "devices": self.device_list()}))

    def device_list(self) -> list[dict]:
        return [d["info"] for d in self._devices.values()]

    # -- engine output (engine.Output) --------------------------------------
    def action(self, action: Action, button: str) -> None:
        if action.kind == "key":
            self.output.tap(action.keys)
        elif str(action) == "mouse:toggle":
            self.set_mode("app" if self.engine.mode == "mouse" else "mouse")
        else:
            if not self._clients:
                log.warning("no hub connected, dropped %s", action)
            self.broadcast({"event": "action", "action": str(action), "button": button})

    def nav(self, button: str) -> None:
        self.broadcast({"event": "nav", "button": button})

    def click(self, down: bool, button: str) -> None:
        self.output.click(down, button)

    # -- modes --------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode == self.engine.mode:
            return
        self.engine.set_mode(mode)
        log.info("mode: %s", mode)
        if mode == "mouse" and not (self._mouse_task and not self._mouse_task.done()):
            self._mouse_task = self._spawn(self._mouse_loop())
        if self.use_sway:
            self._spawn(self._sway_command(CURSOR_SHOWN if mode == "mouse" else CURSOR_HIDDEN))
        self.broadcast({"event": "mode", "mode": mode})

    async def _mouse_loop(self) -> None:
        dt = 1 / MOUSE_HZ
        rem = [0.0, 0.0, 0.0, 0.0]
        held = 0.0
        while self.engine.mode == "mouse":
            await asyncio.sleep(dt)
            pads = [d["translator"].axes for d in self._devices.values()
                    if isinstance(d["translator"], Gamepad)]
            lx, ly, rx, ry = (stick_curve(max((p[a] for p in pads), key=abs, default=0.0))
                              for a in ("lx", "ly", "rx", "ry"))
            held = min(MOUSE_RAMP_S, held + dt) if (lx or ly) else 0.0
            mouse = self.engine.config.mouse
            speed = mouse.speed * (1 + held / MOUSE_RAMP_S) * dt
            rem[0] += lx * speed
            rem[1] += ly * speed
            rem[2] += -ry * mouse.scroll_speed * dt        # stick up = scroll up
            rem[3] += rx * mouse.scroll_speed * dt
            steps = [int(v) for v in rem]
            rem = [v - s for v, s in zip(rem, steps)]
            if steps[0] or steps[1]:
                self.output.move(steps[0], steps[1])
            if steps[2] or steps[3]:
                self.output.scroll(steps[2], steps[3])

    # -- sway ---------------------------------------------------------------
    async def _sway_command(self, cmd: str) -> None:
        try:
            await sway.command(cmd)
        except (ConnectionError, OSError) as err:
            log.debug("sway command failed: %s", err)

    def _set_app(self, workspace: str | None) -> None:
        # One workspace per app, named after the service id.
        app = workspace if workspace and bindings.APP_ID.match(workspace) else None
        if app != self.engine.app:
            self.engine.set_app(app)
            self.broadcast({"event": "focus", "app": app})

    async def _focus_loop(self) -> None:
        while True:
            try:
                self._set_app(await sway.focused_workspace())
                await sway.command(CURSOR_SHOWN if self.engine.mode == "mouse" else CURSOR_HIDDEN)
                async for event in sway.events("workspace"):
                    if event.get("change") == "focus":
                        self._set_app((event.get("current") or {}).get("name"))
            except (ConnectionError, OSError) as err:
                log.debug("sway: %s", err)
            await asyncio.sleep(2)

    # -- control socket -----------------------------------------------------
    def status(self, event: str = "status") -> dict:
        return {"event": event, "mode": self.engine.mode, "app": self.engine.app,
                "devices": self.device_list(), "config_errors": self.config_errors}

    def broadcast(self, message: dict) -> None:
        data = json.dumps(message).encode() + b"\n"
        for writer in list(self._clients):
            if writer.transport.get_write_buffer_size() > 1 << 20:
                self._clients.discard(writer)       # stuck client
                writer.close()
            else:
                writer.write(data)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        writer.write(json.dumps(self.status("hello")).encode() + b"\n")
        try:
            while line := await reader.readline():
                msg, reply = {}, {"ok": True}
                try:
                    parsed = json.loads(line)
                    if not isinstance(parsed, dict):
                        raise ValueError("expected a JSON object")
                    msg = parsed
                    reply.update(self.handle(msg) or {})
                except (ValueError, KeyError, TypeError) as err:
                    reply = {"ok": False, "error": str(err)}
                if "id" in msg:
                    writer.write(json.dumps({"reply": msg["id"], **reply}).encode() + b"\n")
        except (ConnectionError, asyncio.LimitOverrunError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()

    def handle(self, msg: dict) -> dict | None:
        """Run one client command. Raises ValueError/KeyError on bad input."""
        cmd = msg.get("cmd")
        if cmd == "mode":
            if msg["mode"] not in MODES:
                raise ValueError(f"unknown mode {msg['mode']!r}")
            self.set_mode(msg["mode"])
        elif cmd == "button":
            button, state = msg["button"], msg.get("state", "tap")
            if button not in BUTTONS:
                raise ValueError(f"unknown button {button!r}")
            if state not in ("down", "up", "tap"):
                raise ValueError(f"unknown state {state!r}")
            if state in ("down", "tap"):
                self.engine.button(button, True)
            if state in ("up", "tap"):
                self.engine.button(button, False)
        elif cmd == "action":
            action = bindings.parse_action(msg["action"])
            if action:
                self.action(action, "")
        elif cmd == "key":
            self.output.tap(parse_combo(msg["combo"]))
        elif cmd == "move":                     # phone touchpad
            self.output.move(int(msg.get("dx", 0)), int(msg.get("dy", 0)))
        elif cmd == "scroll":
            self.output.scroll(int(msg.get("dy", 0)), int(msg.get("dx", 0)))
        elif cmd == "click":
            button = msg.get("button", "left")
            if button not in ("left", "right"):
                raise ValueError(f"unknown mouse button {button!r}")
            self.output.click(True, button)
            self.output.click(False, button)
        elif cmd == "status":
            return self.status()
        elif cmd == "reload":
            self.reload()
            return {"errors": self.config_errors}
        else:
            raise ValueError(f"unknown command {cmd!r}")
        return None


def main() -> None:
    try:
        asyncio.run(InputDaemon().run())
    except PermissionError as err:
        raise SystemExit(f"tvbox-inputd: {err} (is the user in the input group "
                         f"and /dev/uinput writable?)") from err


if __name__ == "__main__":
    main()
