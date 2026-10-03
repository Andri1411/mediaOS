"""tvbox-hub: the control centre between input, the on-screen UI, the phone
and the system. It listens to tvbox-inputd (input.sock) for actions and
overlay navigation, serves the web UI to tvbox-shell and to paired phones, and
keeps them up to date over a WebSocket.

HTTP on port 8080; who may do what is decided in auth.classify:
    GET  /home, /overlay   the TV's pages (loopback only)
    GET  /phone            the phone remote (paired devices)
    GET  /pair?t=<token>   pairing link from the QR code on the TV
    GET  /ws               WebSocket: {"type": "state"|"nav"|"osd"|"open", ...}
                           to the pages, {"cmd": ...} from them
    GET  /api/state        current state as JSON
    POST /api/cmd          {"cmd": ...}, same commands as over the WebSocket
    POST /api/pair/start   new pairing link (TV only); GET /api/pair/qr.svg?url=
    GET/POST /api/bindings bindings editor (validates before saving)
    GET  /api/health       services, restarts, temperature, disk, memory
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
from pathlib import Path

from aiohttp import WSMsgType, web

from . import NAME, audio, bindings, health, services, sway
from .apps import HOME, AppManager
from .auth import COOKIE, DeviceStore, classify, device_name
from .util import (IN_CLOSE_WRITE, IN_CREATE, IN_DELETE, IN_MOVED_FROM, IN_MOVED_TO, Inotify,
                   input_socket, runtime_dir, sd_notify, setup_logging, watchdog_interval)

log = setup_logging("hub")

PORT = 8080
SCALES = ("auto", "1", "1.25", "1.5", "2")
# Our navigation extension (src/extensions/tvnav; the id follows from the key
# in its manifest). It may tell the hub that a text field got focus.
EXTENSION_ORIGIN = "chrome-extension://ecgejpihnmlnjmgnejffnelbhbiehbpm"
# Keys of the on-screen keyboard that are not text.
OSK_KEYS = {"backspace": "BackSpace", "enter": "Return", "left": "Left", "right": "Right", "tab": "Tab"}
MAX_TYPE = 500
SWAY_UI_MODE = "tvbox-ui"                # binding mode defined in the sway config
PORT_KEY = web.AppKey("port", int)


def data_dir() -> Path:
    return Path(os.environ.get("TVBOX_DATA_DIR", f"/usr/share/{NAME}"))


def release_version() -> str:
    try:
        for line in (data_dir() / "release").read_text().splitlines():
            if line.startswith("VERSION="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return "dev"


def display_conf() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / NAME / "display.conf"


def display_scale() -> str:
    """The scale setting tvbox-display applies (see pkgs/tvbox-session)."""
    try:
        values = [line[6:].strip() for line in display_conf().read_text().splitlines()
                  if line.startswith("scale=")]
    except OSError:
        values = []
    return values[-1] if values and values[-1] in SCALES else "auto"


def lan_address() -> str:
    """The address other devices on the LAN reach the box at (no traffic is sent)."""
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    return ""


class Hub:
    def __init__(self):
        self.overlay: str | None = None       # None | "menu" | "keyboard"
        self.keyboard_auto = False            # keyboard opened because a text field got focus
        self.view = "main"                    # view the menu opens with
        self.base_mode = "app"                # input mode outside the overlay: app | mouse
        self.app: str | None = None           # focused app (workspace)
        self.apps = AppManager(self)
        self.volume: int | None = None
        self.muted = False
        self.sinks: list[dict] = []
        self.config_errors: list[str] = []
        self.input_connected = False
        self._input: asyncio.StreamWriter | None = None
        self._clients: dict[web.WebSocketResponse, str] = {}    # -> role: overlay | home | phone
        self.devices = DeviceStore()
        self._volume_pending = 0
        self._volume_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    # -- state --------------------------------------------------------------
    def state(self) -> dict:
        return {"type": "state", "overlay": self.overlay, "view": self.view,
                "mouse": self.base_mode == "mouse", "app": self.app,
                "services": self.apps.state(), "service_errors": self.apps.errors,
                "display_scale": display_scale(), "kernel": os.uname().release,
                "volume": self.volume, "muted": self.muted, "sinks": self.sinks,
                "config_errors": self.config_errors, "input_connected": self.input_connected,
                "devices": self.devices.listing(),
                "version": release_version(), "hostname": socket.gethostname(),
                "address": lan_address()}

    def send(self, message: dict) -> None:
        for ws in list(self._clients):
            self.spawn(self._send_one(ws, message))

    async def _send_one(self, ws: web.WebSocketResponse, message: dict) -> None:
        with contextlib.suppress(ConnectionError, RuntimeError):
            await ws.send_json(message)

    def has_overlay(self) -> bool:
        return "overlay" in self._clients.values()

    def push_state(self) -> None:
        self.send(self.state())

    def osd(self, **fields) -> None:
        self.send({"type": "osd", **fields})

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- link to inputd -----------------------------------------------------
    def input_send(self, **cmd) -> None:
        if self._input:
            self._input.write(json.dumps(cmd).encode() + b"\n")

    async def input_link(self) -> None:
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(str(input_socket()))
                self._input, self.input_connected = writer, True
                while line := await reader.readline():
                    await self.on_input(json.loads(line))
            except (OSError, ValueError) as err:
                log.debug("inputd link: %s", err)
            if self.input_connected:
                log.warning("lost tvbox-inputd, reconnecting")
            self._input, self.input_connected = None, False
            await asyncio.sleep(1)

    async def on_input(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "hello":
            self.config_errors = msg.get("config_errors", [])
            self.app = msg.get("app")
            await self.apps.refresh()
            if msg.get("mode") == "mouse":
                self.base_mode = "mouse"
            # Either side may have restarted: make inputd's mode match ours.
            self.input_send(cmd="mode", mode="ui" if self.overlay else self.base_mode)
            self.push_state()
        elif event == "nav":
            self.send({"type": "nav", "button": msg["button"]})
        elif event == "action":
            await self.on_action(msg["action"])
        elif event == "mode":
            if msg["mode"] in ("app", "mouse") and msg["mode"] != self.base_mode:
                self.base_mode = msg["mode"]
                self.osd(kind="message", text=f"Mouse mode {'on' if self.base_mode == 'mouse' else 'off'}")
                self.push_state()
        elif event == "focus":
            old, self.app = self.app, msg.get("app")
            self.push_state()
            await self.apps.focus_changed(old, self.app)
            self.push_state()
        elif event == "config":
            self.config_errors = msg.get("errors", [])
            if self.config_errors:
                self.osd(kind="message", text="Bindings file has errors, keeping the previous bindings")
            self.push_state()

    async def on_action(self, action: str) -> None:
        kind, _, arg = action.partition(":")
        if action == "ui:system_menu":
            await (self.close_overlay() if self.overlay else self.open_overlay("main"))
        elif action == "ui:app_switcher":
            if self.overlay and self.view == "apps":
                await self.close_overlay()
            else:
                await self.open_overlay("apps")
        elif action == "ui:home":
            await self.command({"cmd": "home"})
        elif action == "ui:keyboard":
            if self.overlay == "keyboard":
                await self.close_overlay()
            else:
                await self.open_overlay("main", "keyboard")
        elif kind == "volume":
            await self.command({"cmd": "mute"} if arg == "mute" else {"cmd": "volume", "delta": int(arg)})
        elif action == "audio:next_output":
            await self.next_output()
        elif action == "app:restart":
            await self.command({"cmd": "restart_app"})
        elif kind == "app" and arg.startswith("launch:"):
            await self.command({"cmd": "switch_app", "id": arg.removeprefix("launch:")})
        else:
            log.info("action %s is not handled yet", action)

    # -- overlay ------------------------------------------------------------
    async def open_overlay(self, view: str, kind: str = "menu", auto: bool = False) -> None:
        """Show the system menu (kind "menu") or the on-screen keyboard."""
        if not self.has_overlay():
            # Without the overlay page nothing would be drawn, and ui mode
            # would swallow the controller: stay in app mode.
            log.warning("overlay requested but the shell's overlay page is not connected")
            return
        self.overlay, self.view, self.keyboard_auto = kind, view, auto
        self.input_send(cmd="mode", mode="ui")
        self.push_state()
        if kind == "menu":
            await self.sway_command(f"mode {SWAY_UI_MODE}")     # a real keyboard drives the menu
            await asyncio.gather(self.refresh_audio(), self.apps.refresh())
            self.push_state()
        else:
            # With the on-screen keyboard a real keyboard keeps typing into the app.
            await self.sway_command("mode default")

    async def close_overlay(self) -> None:
        if self.overlay:
            self.overlay = None
            self.input_send(cmd="mode", mode=self.base_mode)
            self.push_state()
            await self.sway_command("mode default")

    # -- system -------------------------------------------------------------
    async def refresh_audio(self) -> None:
        volume, self.sinks = await asyncio.gather(audio.get_volume(), audio.list_sinks())
        if volume:
            self.volume, self.muted = volume
        else:
            self.volume = None

    async def _apply_volume(self) -> None:
        # Held triggers arrive faster than wpctl runs: apply what piled up.
        while self._volume_pending:
            delta, self._volume_pending = self._volume_pending, 0
            await audio.change_volume(delta)
            volume = await audio.get_volume()
            if volume:
                self.volume, self.muted = volume
            self.volume_changed()

    def volume_changed(self) -> None:
        if self.volume is None:
            self.osd(kind="message", text="No audio output")
        elif not self.overlay:
            self.osd(kind="volume", volume=self.volume, muted=self.muted)
        self.push_state()

    async def next_output(self) -> None:
        await self.refresh_audio()
        if not self.sinks:
            self.osd(kind="message", text="No audio output")
            return
        current = next((i for i, s in enumerate(self.sinks) if s["default"]), -1)
        await self.command({"cmd": "audio_output", "id": self.sinks[(current + 1) % len(self.sinks)]["id"]})

    async def sway_command(self, cmd: str) -> bool:
        try:
            return await sway.command(cmd)
        except (ConnectionError, OSError) as err:
            log.warning("sway: %s", err)
            return False

    async def command(self, msg: dict) -> dict:
        """Commands from the UI (and from actions). Raises ValueError/KeyError
        on bad input."""
        cmd = msg.get("cmd")
        if cmd == "close":
            await self.close_overlay()
        elif cmd == "action":                   # as if a button bound to it was pressed
            await self.on_action(str(msg["action"]))
        elif cmd == "button":                   # phone remote: a logical button
            if msg["button"] not in bindings.BUTTONS or msg.get("state", "tap") not in ("down", "up", "tap"):
                raise ValueError("unknown button or state")
            self.input_send(cmd="button", button=msg["button"], state=msg.get("state", "tap"))
        elif cmd == "pointer":                  # phone touchpad
            dx, dy = int(msg.get("dx", 0)), int(msg.get("dy", 0))
            if max(abs(dx), abs(dy)) > 2000:
                raise ValueError("pointer step too large")
            self.input_send(cmd="move", dx=dx, dy=dy)
        elif cmd == "scroll":
            dx, dy = int(msg.get("dx", 0)), int(msg.get("dy", 0))
            if max(abs(dx), abs(dy)) > 50:
                raise ValueError("scroll step too large")
            self.input_send(cmd="scroll", dx=dx, dy=dy)
        elif cmd == "click":
            if msg.get("button", "left") not in ("left", "right"):
                raise ValueError("button must be left or right")
            self.input_send(cmd="click", button=msg.get("button", "left"))
        elif cmd == "volume_set":
            percent = int(msg["percent"])
            if not 0 <= percent <= 100:
                raise ValueError("percent must be 0 to 100")
            await audio.set_volume(percent)
            volume = await audio.get_volume()
            self.volume, self.muted = volume if volume else (None, False)
            self.volume_changed()
        elif cmd == "revoke_device":
            if not self.devices.revoke(str(msg["id"])):
                raise ValueError("no such device")
            self.push_state()
        elif cmd == "type":                     # on-screen keyboard (later: the phone)
            text = msg["text"]
            if not isinstance(text, str) or not 0 < len(text) <= MAX_TYPE or "\0" in text:
                raise ValueError(f"text must be 1 to {MAX_TYPE} characters")
            # Browsers get the text through DevTools; everything else through
            # wtype, which types any Unicode text with sway's virtual-keyboard
            # protocol, independent of the keyboard layout. (wtype decodes its
            # arguments with the locale; services have none set.)
            if not await self.apps.insert_text(self.apps.get(self.app), text):
                code, _ = await audio.run("wtype", "--", text, env={"LC_ALL": "C.UTF-8"})
                if code != 0:
                    raise ValueError("typing failed (is wtype installed?)")
        elif cmd == "key":
            if msg["key"] not in OSK_KEYS:
                raise ValueError(f"key must be one of {', '.join(OSK_KEYS)}")
            self.input_send(cmd="key", combo=OSK_KEYS[msg["key"]])
        elif cmd == "keyboard":
            await self.open_overlay("main", "keyboard")
        elif cmd == "text_focus":               # from the navigation extension
            service = self.apps.get(self.app)
            if msg.get("focused") and not self.overlay and service and service.nav:
                await self.open_overlay("main", "keyboard", auto=True)
            elif not msg.get("focused") and self.overlay == "keyboard" and self.keyboard_auto:
                await self.close_overlay()
        elif cmd == "refresh":                  # a settings screen was opened
            await asyncio.gather(self.refresh_audio(), self.apps.refresh())
            self.push_state()
        elif cmd in ("home", "settings"):
            await self.close_overlay()
            await self.sway_command(f"workspace {HOME}")
            self.send({"type": "open", "view": "settings" if cmd == "settings" else "home"})
        elif cmd in ("launch", "switch_app"):
            await self.close_overlay()
            await self.apps.launch(str(msg["id"]))
        elif cmd == "stop_app":
            await self.apps.stop(str(msg["id"]))
        elif cmd == "display_scale":
            scale = str(msg["scale"])
            if scale not in SCALES:
                raise ValueError(f"scale must be one of {', '.join(SCALES)}")
            display_conf().parent.mkdir(parents=True, exist_ok=True)
            display_conf().write_text(f"scale={scale}\n")
            # Our own $SWAYSOCK is stale once sway has been restarted.
            await audio.run("tvbox-display", "apply", env={"SWAYSOCK": sway.socket_path() or ""})
            self.push_state()
        elif cmd == "volume":
            delta = int(msg["delta"])
            if not -100 <= delta <= 100:
                raise ValueError("delta out of range")
            self._volume_pending += delta
            if not (self._volume_task and not self._volume_task.done()):
                self._volume_task = self.spawn(self._apply_volume())
        elif cmd == "mute":
            await audio.toggle_mute()
            volume = await audio.get_volume()
            self.volume, self.muted = volume if volume else (None, False)
            self.volume_changed()
        elif cmd == "audio_output":
            await audio.set_default_sink(int(msg["id"]))
            await self.refresh_audio()
            name = next((s["name"] for s in self.sinks if s["default"]), None)
            self.osd(kind="message", text=f"Audio: {name}" if name else "No audio output")
            self.push_state()
        elif cmd == "mouse_toggle":
            self.base_mode = "app" if self.base_mode == "mouse" else "mouse"
            self.osd(kind="message", text=f"Mouse mode {'on' if self.base_mode == 'mouse' else 'off'}")
            if self.overlay:
                await self.close_overlay()
            else:
                self.input_send(cmd="mode", mode=self.base_mode)
                self.push_state()
        elif cmd == "restart_app":
            service = self.apps.get(self.app)
            await self.close_overlay()
            if service and await self.apps.restart(service.id):
                self.osd(kind="message", text=f"Restarting {service.name}")
            else:
                self.osd(kind="message", text="No app to restart here")
        elif cmd == "restart_session":
            # greetd logs in automatically only once per boot, so sway must be
            # restarted by tvbox-session's loop: the flag tells it this exit
            # is not a logout.
            # Apps go first: a browser whose compositor vanishes "crashes" and
            # systemd would bring it straight back in the new session.
            await self.apps.stop_all()
            (runtime_dir() / "restart-session").touch()
            await self.sway_command("exit")
        elif cmd == "reboot":
            await audio.run("systemctl", "reboot")
        else:
            raise ValueError(f"unknown command {cmd!r}")
        return {"ok": True}

    def watch_services(self) -> None:
        """Reload services.toml when one of its files is saved."""
        paths = services.default_paths()
        inotify = Inotify()
        for directory in {p.parent for p in paths}:
            inotify.watch(directory, IN_CLOSE_WRITE | IN_MOVED_TO | IN_MOVED_FROM | IN_CREATE | IN_DELETE)

        def changed() -> None:
            if any(name == paths[0].name for _dir, _mask, name in inotify.read()):
                self.apps.reload()
                self.push_state()
        asyncio.get_running_loop().add_reader(inotify.fd, changed)

    # -- HTTP ---------------------------------------------------------------
    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        role = request.query.get("role", "")
        if request[ACCESS] != "tv" and role != "phone":
            raise web.HTTPForbidden(text="only the TV's own pages may take that role\n")
        # Pings find phones that vanished. Not for the TV's own pages: WebKit
        # suspends the hidden overlay page, which then misses the pong and the
        # connection was dropped about every 30 s (a menu opened in that gap
        # closed again at once).
        ws = web.WebSocketResponse(heartbeat=20 if role == "phone" else None)
        await ws.prepare(request)
        self._clients[ws] = role
        held: set[str] = set()              # phone buttons currently down
        await ws.send_json(self.state())
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(message.data)
                    await self.command(msg)
                    if msg.get("cmd") == "button":
                        (held.add if msg.get("state") == "down" else held.discard)(msg["button"])
                except (ValueError, KeyError, TypeError, AttributeError) as err:
                    await ws.send_json({"type": "error", "error": str(err)})
        finally:
            # A phone that drops off mid-press must not leave a button held.
            for button in held:
                self.input_send(cmd="button", button=button, state="up")
            self._clients.pop(ws, None)
            if not self.has_overlay():
                await self.close_overlay()      # nobody left to draw the menu
        return ws

    async def api_state(self, _request: web.Request) -> web.Response:
        return web.json_response(self.state() | {"ui_clients": sorted(self._clients.values())})

    async def api_cmd(self, request: web.Request) -> web.Response:
        try:
            msg = await request.json()
            if not isinstance(msg, dict):
                raise ValueError("expected a JSON object")
            if request[ACCESS] == "extension" and msg.get("cmd") != "text_focus":
                raise ValueError("this origin may only send text_focus")
            return web.json_response(await self.command(msg))
        except (ValueError, KeyError, TypeError) as err:
            return web.json_response({"ok": False, "error": str(err)}, status=400)

    # -- phone pairing ------------------------------------------------------
    async def api_pair_start(self, request: web.Request) -> web.Response:
        tv_only(request)
        address = lan_address()
        if not address:
            return web.json_response({"ok": False, "error": "not connected to a network"}, status=409)
        token, expires = self.devices.start_pairing()
        port = request.app[PORT_KEY]
        return web.json_response({"ok": True, "url": f"http://{address}:{port}/pair?t={token}",
                                  "expires": expires})

    async def api_pair_qr(self, request: web.Request) -> web.Response:
        tv_only(request)
        import qrcode
        import qrcode.image.svg
        url = request.query.get("url", "")
        if not url.startswith("http://") or len(url) > 200:
            raise web.HTTPBadRequest(text="url wanted\n")
        image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=2)
        return web.Response(body=image.to_string(), content_type="image/svg+xml")

    async def pair(self, request: web.Request) -> web.Response:
        found = self.devices.pair(request.query.get("t", ""), device_name(request.headers.get("User-Agent", "")))
        if not found:
            return web.FileResponse(data_dir() / "web" / "pair-failed.html", status=403)
        device, token = found
        log.info("paired %s (%s) from %s", device.name, device.id, request.remote)
        self.osd(kind="message", text=f"Paired: {device.name}")
        self.push_state()
        response = web.HTTPFound("/phone")
        response.set_cookie(COOKIE, token, max_age=10 * 365 * 86400, httponly=True, samesite="Strict", path="/")
        return response

    # -- bindings editor, health --------------------------------------------
    async def api_bindings(self, request: web.Request) -> web.Response:
        paths = bindings.default_paths()          # defaults, /etc, user
        if request.method == "GET":
            def text(path):
                try:
                    return path.read_text()
                except OSError:
                    return None
            return web.json_response({"user": text(paths[2]) or "", "etc": text(paths[1]),
                                      "defaults": text(paths[0]), "errors": self.config_errors})
        try:
            body = await request.json()
        except ValueError:
            body = None
        content = body.get("text") if isinstance(body, dict) else None
        if not isinstance(content, str) or len(content) > 100_000:
            raise web.HTTPBadRequest(text="text wanted\n")
        errors = bindings.validate_text(content, "your bindings", paths[:2])
        if errors or body.get("check_only"):
            return web.json_response({"ok": not errors, "errors": errors})
        paths[2].parent.mkdir(parents=True, exist_ok=True)
        tmp = paths[2].with_suffix(".new")
        tmp.write_text(content)
        os.replace(tmp, paths[2])                 # inputd reloads it on its own
        return web.json_response({"ok": True, "errors": []})

    async def api_health(self, _request: web.Request) -> web.Response:
        return web.json_response(await health.collect() | {"version": release_version()})

    async def index(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/home" if request[ACCESS] == "tv" else "/phone")

    def app_factory(self, port: int = PORT) -> web.Application:
        webroot = data_dir() / "web"
        app = web.Application(middlewares=[access])
        app[PORT_KEY] = port
        app[HUB_KEY] = self

        def page(name, tv=False):
            async def handler(request):
                if tv:
                    tv_only(request)
                return web.FileResponse(webroot / name)
            return handler

        async def no_cache(_request, response):
            # The shell must pick up new pages and scripts after an update;
            # WebKit otherwise keeps using cached /static files.
            response.headers.setdefault("Cache-Control", "no-cache")
        app.on_response_prepare.append(no_cache)
        app.add_routes([web.get("/", self.index),
                        web.get("/overlay", page("overlay.html", tv=True)),
                        web.get("/home", page("home.html", tv=True)),
                        web.get("/phone", page("phone.html")), web.get("/pair", self.pair),
                        web.get("/ws", self.ws_handler),
                        web.get("/api/state", self.api_state), web.post("/api/cmd", self.api_cmd),
                        web.post("/api/pair/start", self.api_pair_start),
                        web.get("/api/pair/qr.svg", self.api_pair_qr),
                        web.get("/api/bindings", self.api_bindings), web.post("/api/bindings", self.api_bindings),
                        web.get("/api/health", self.api_health),
                        web.static("/static", webroot)])
        return app

    async def run(self, host: str = "", port: int = PORT) -> None:
        # All addresses: the phone remote reaches the box over the LAN; the
        # access middleware keeps unpaired clients out.
        runner = web.AppRunner(self.app_factory(port), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        self.spawn(self.input_link())
        self.spawn(self.refresh_audio())
        self.spawn(self.sway_command("mode default"))   # in case a previous hub died mid-menu
        self.spawn(self.apps.window_watch())
        self.spawn(self.apps.memory_watch())
        self.watch_services()
        sd_notify("READY=1")
        log.info("listening on port %d", port)
        interval = watchdog_interval()
        try:
            while True:
                sd_notify("WATCHDOG=1")
                await asyncio.sleep(interval or 3600)
        finally:
            await runner.cleanup()


HUB_KEY = web.AppKey("hub", object)
# (RequestKey arrived in aiohttp 3.13; plain keys work everywhere.)
ACCESS = web.RequestKey("access", str) if hasattr(web, "RequestKey") else "access"
DEVICE = web.RequestKey("device", object) if hasattr(web, "RequestKey") else "device"


@web.middleware
async def access(request: web.Request, handler):
    """Every request is classified (auth.classify) before it reaches a
    handler: the TV's own pages, our browser extension, an unpaired phone on
    the pairing page, or a phone that must show a paired device's cookie.
    Everything else is refused, including web pages running in the box's own
    browsers (they can reach 127.0.0.1 too)."""
    kind = classify(request.remote, request.host, request.headers.get("Origin"), request.path,
                    request.app[PORT_KEY], EXTENSION_ORIGIN, request.method)
    if kind == "deny":
        raise web.HTTPForbidden(text="tvbox-hub: request from a foreign origin refused\n")
    if kind == "lan":
        device = request.app[HUB_KEY].devices.check(request.cookies.get(COOKIE))
        if not device:
            if request.path in ("/", "/phone"):
                return web.FileResponse(data_dir() / "web" / "pair-failed.html", status=401)
            raise web.HTTPUnauthorized(text="tvbox-hub: pair this device first (TV: Settings, Pair phone)\n")
        request[DEVICE] = device
    request[ACCESS] = kind
    return await handler(request)


def tv_only(request: web.Request) -> None:
    if request[ACCESS] != "tv":
        raise web.HTTPForbidden(text="tvbox-hub: only the TV can do that\n")


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(Hub().run())


if __name__ == "__main__":
    main()
