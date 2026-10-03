#!/usr/bin/python
"""Runs inside the test VM as root (see tests/qemu/session.sh), after
menu_test.py: home screen, launching and switching services, window
placement, restarts, services.toml overrides and a browser service.

The services under test are local (terminals, and Chromium on a page served
inside the VM), so only the Widevine and ad-blocker checks need the internet;
they are skipped when it was not available.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
from evdev import ecodes as e
from input_test import RUNTIME, TV, Daemon, Pad, check, drain, failed, output_device, tapped, tv
from menu_test import (SERVICES, TEST_SERVICES, api, install_test_services, long_press, service, state, step,
                       swaymsg, ui_ready, wait_state)

USER_SERVICES = Path(TV.pw_dir) / ".config/tvbox/services.toml"
WWW = Path("/tmp/tvbox-www")
WEB_PORT = 8091
TEST_UA = "Mozilla/5.0 (TestTV) tvbox-test/1.0"
BROWSER_SERVICE = f'''
[user_agents]
testtv = "{TEST_UA}"
[[service]]
id = "webtest"
name = "Web test"
kind = "browser"
url = "http://127.0.0.1:{WEB_PORT}/index.html"
user_agent = "testtv"
'''
PAGE = '''<!doctype html><title>tvbox test page</title>
<body style="margin:0;background:#024"><video src="test.webm" autoplay loop muted style="width:100%"></video>
<script>window.keys = []; addEventListener("keydown", (ev) => keys.push(ev.key));</script>'''


def unit_pid(service_id):
    return tv("systemctl", "--user", "show", "-p", "MainPID", "--value", f"tvbox-app@{service_id}").stdout.strip()


def unit_active(service_id):
    return tv("systemctl", "--user", "is-active", f"tvbox-app@{service_id}").stdout.strip()


def windows():
    """{workspace: [app_id, ...]} from sway's tree."""
    tree = json.loads(swaymsg("-t", "get_tree", "-r").stdout or "{}")
    found = {}

    def walk(node, workspace):
        if node.get("type") == "workspace":
            workspace = node["name"]
            found.setdefault(workspace, [])
        elif workspace and node.get("pid"):
            found[workspace].append(node.get("app_id"))
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            walk(child, workspace)
    walk(tree, None)
    return found


def wait_for(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.3)
    return predicate()


def cdp(service_id, expression):
    """Evaluate JavaScript in a browser service through its DevTools port."""
    port_file = Path(TV.pw_dir) / f".local/share/tvbox/profiles/{service_id}/DevToolsActivePort"

    async def run():
        port = port_file.read_text().split()[0]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(f"http://127.0.0.1:{port}/json") as reply:
                page = next(p for p in await reply.json() if p["type"] == "page")
            async with session.ws_connect(page["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                await ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {
                    "expression": expression, "awaitPromise": True, "returnByValue": True}})
                async for message in ws:
                    data = json.loads(message.data)
                    if data.get("id") == 1:
                        return data["result"].get("result", {}).get("value")
    try:
        return asyncio.run(run())
    except (OSError, aiohttp.ClientError, asyncio.TimeoutError, StopIteration, KeyError, IndexError) as err:
        return f"cdp failed: {err!r}"


def main():
    st = wait_state(ui_ready, 30)
    check("shell and inputd are connected to the hub", ui_ready(st), str(st)[:300])
    if failed:
        return
    tv("systemctl", "--user", "stop", "tvbox-app@*")
    USER_SERVICES.unlink(missing_ok=True)
    st = install_test_services()
    ids = [s["id"] for s in st["services"]]
    check("services.toml in /etc adds tiles, defaults stay",
          ids == ["alpha", "beta", "youtube", "netflix", "disney", "floatplane", "jellyfin"], str(ids))

    api(cmd="home")
    daemon = Daemon()
    status = daemon.wait_status(lambda s: s["app"] == "home")
    home = windows().get("home", [])
    check("home screen window is on the home workspace", status["app"] == "home" and home == ["org.tvbox.Shell"],
          f"{status['app']} {home}")
    out = output_device()
    pad = Pad()
    daemon.wait_status(lambda s: s["devices"])
    drain(out, 0.3)

    # --- launch from the home screen with the controller ---
    step(pad, "left", "left", "left", "up", "up", e.BTN_SOUTH)       # first tile = Alpha
    st = wait_state(lambda s: s["app"] == "alpha" and service(s, "alpha")["state"] == "running")
    placed = wait_for(lambda: windows().get("alpha") == ["foot"])
    check("A on the first tile launches that service on its own workspace",
          st["app"] == "alpha" and placed, f"{st['app']} {windows()}")
    alpha_pid = unit_pid("alpha")

    pad.press(e.BTN_MODE)
    daemon.wait_status(lambda s: s["app"] == "home")
    time.sleep(0.5)
    step(pad, "right", e.BTN_SOUTH)                                  # second tile = Beta
    st = wait_state(lambda s: s["app"] == "beta" and service(s, "beta")["state"] == "running")
    check("home, right, A launches the second one; the first keeps running",
          st["app"] == "beta" and service(st, "alpha")["state"] == "running" and unit_pid("alpha") == alpha_pid,
          str(st["services"][:2]))

    pad.press(e.BTN_MODE)
    daemon.wait_status(lambda s: s["app"] == "home")
    time.sleep(0.5)
    step(pad, "left", e.BTN_SOUTH)
    status = daemon.wait_status(lambda s: s["app"] == "alpha")
    check("selecting a running service switches back without restarting it",
          status["app"] == "alpha" and unit_pid("alpha") == alpha_pid, f"{status['app']}")

    # --- window placement, exits, crashes ---
    api(cmd="stop_app", id="beta")
    api(cmd="home")
    daemon.wait_status(lambda s: s["app"] == "home")
    tv("systemctl", "--user", "start", "tvbox-app@beta")
    placed = wait_for(lambda: windows().get("beta") == ["foot"])
    status = daemon.call(cmd="status")
    check("a window that appears while another workspace is shown is moved to its own",
          placed and status["app"] == "home" and windows().get("home") == ["org.tvbox.Shell"], str(windows()))

    api(cmd="launch", id="beta")
    daemon.wait_status(lambda s: s["app"] == "beta")
    subprocess.run(["kill", "-SEGV", unit_pid("beta")])
    restarted = wait_for(lambda: unit_active("beta") == "active" and windows().get("beta") == ["foot"], 20)
    check("a crashed app is restarted in place", restarted and daemon.call(cmd="status")["app"] == "beta",
          f"{unit_active('beta')} {windows()}")

    before = unit_pid("beta")
    long_press(pad, e.BTN_MODE)
    wait_state(lambda s: s["overlay"] == "menu")
    step(pad, *["down"] * 5, e.BTN_SOUTH)                            # Restart app
    changed = wait_for(lambda: unit_pid("beta") not in ("", "0", before) and windows().get("beta") == ["foot"])
    check("menu: Restart app restarts the focused service", bool(changed), f"{before} -> {unit_pid('beta')}")

    swaymsg("[workspace=beta] kill")              # the app is closed (as if quit from its own menu)
    status = daemon.wait_status(lambda s: s["app"] == "home", 15)
    st = wait_state(lambda s: service(s, "beta")["state"] == "stopped")
    check("an app that exits leaves its workspace: back to home, not restarted",
          status["app"] == "home" and service(st, "beta")["state"] == "stopped",
          f"{status['app']} {service(st, 'beta')}")

    # --- overrides and broken files ---
    USER_SERVICES.parent.mkdir(parents=True, exist_ok=True)
    USER_SERVICES.write_text('[[service]]\nid = "alpha"\nname = "Renamed"\n[[service]]\nid = "netflix"\nenabled = false\n')
    st = wait_state(lambda s: service(s, "alpha").get("name") == "Renamed", 5)
    check("user services.toml changes a tile and removes one, without a restart",
          service(st, "alpha").get("name") == "Renamed" and not service(st, "netflix"), str(st["services"])[:200])
    USER_SERVICES.write_text("[[service]\n")
    st = wait_state(lambda s: s["service_errors"], 5)
    check("a broken services.toml is reported and the files below it stay in use",
          bool(st["service_errors"]) and service(st, "alpha").get("name") == "Alpha" and service(st, "netflix"),
          str(st["service_errors"]))
    USER_SERVICES.unlink()
    wait_state(lambda s: not s["service_errors"], 5)

    # --- settings ---
    api(cmd="display_scale", scale="2")
    scale = json.loads(swaymsg("-t", "get_outputs", "-r").stdout)[0]["scale"]
    check("settings: display scale applies", scale == 2.0 and state()["display_scale"] == "2", str(scale))
    api(cmd="display_scale", scale="auto")
    bad = api(cmd="display_scale", scale="7")
    check("settings: invalid values are refused", bad.get("ok") is False, str(bad))

    # --- a browser service ---
    WWW.mkdir(exist_ok=True)
    (WWW / "index.html").write_text(PAGE)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=20:size=320x180:rate=10",
                    "-c:v", "libvpx", "-b:v", "200k", str(WWW / "test.webm")], check=False)
    server = subprocess.Popen([sys.executable, "-m", "http.server", str(WEB_PORT), "--bind", "127.0.0.1"],
                              cwd=WWW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with open(SERVICES, "w") as f:
            f.write(TEST_SERVICES + BROWSER_SERVICE)
        wait_state(lambda s: service(s, "webtest"), 5)
        api(cmd="launch", id="webtest")
        # (an app window's id is chrome-<host>__<path>-Default; placement is by systemd unit)
        placed = wait_for(lambda: [w[:7] for w in windows().get("webtest", [])] == ["chrome-"], 60)
        check("browser service: Chromium app window on its workspace", bool(placed), str(windows()))
        playing = wait_for(lambda: cdp("webtest", "!document.querySelector('video').paused && "
                                       "document.querySelector('video').currentTime > 0") is True, 40)
        check("browser service: page loads and video plays", playing,
              str(cdp("webtest", "document.title + ' ' + document.querySelector('video')?.error?.message")))
        agent = cdp("webtest", "navigator.userAgent")
        check("browser service: user agent from services.toml", agent == TEST_UA, str(agent))
        profile = Path(TV.pw_dir) / ".local/share/tvbox/profiles/webtest"
        cache = Path(RUNTIME) / "tvbox/cache/webtest"
        check("browser service: own profile, cache on tmpfs", (profile / "Default").is_dir() and cache.is_dir(),
              f"{profile.exists()} {cache.exists()}")

        drain(out, 0.2)
        step(pad, "down", e.BTN_SOUTH)
        keys = cdp("webtest", "keys.join(',')")
        check("browser service: controller keys reach the page", keys == "ArrowDown,Enter", str(keys))

        if os.path.exists("/var/lib/tvbox/WidevineCdm/manifest.json"):
            widevine = cdp("webtest", '''navigator.requestMediaKeySystemAccess("com.widevine.alpha", [{
                initDataTypes: ["cenc"], videoCapabilities: [{contentType: 'video/mp4; codecs="avc1.42E01E"',
                robustness: "SW_SECURE_DECODE"}]}]).then((a) => a.keySystem, (err) => "refused: " + err.name)''')
            check("browser service: Widevine available", widevine == "com.widevine.alpha", str(widevine))
        else:
            print("  SKIP browser service: Widevine (module not fetched; needs internet at first boot)")
        extensions = profile / "Default/Extensions/ddkjiahejlhfcafbddmgiahcphecmpfh"
        if wait_for(extensions.is_dir, 45):
            check("browser service: uBlock Origin Lite installed by policy", True)
        else:
            print("  SKIP browser service: uBlock Origin Lite (not downloaded; needs internet)")

        pad.press(e.BTN_MODE)
        daemon.wait_status(lambda s: s["app"] == "home")
        paused = wait_for(lambda: cdp("webtest", "document.querySelector('video').paused") is True, 10)
        check("leaving a browser service pauses its video", paused, str(cdp("webtest", "document.querySelector('video').paused")))
        check("and it keeps running in the background", unit_active("webtest") == "active", unit_active("webtest"))
    finally:
        server.terminate()
        for sid in ("alpha", "beta", "webtest"):
            tv("systemctl", "--user", "stop", f"tvbox-app@{sid}")
        os.unlink(SERVICES)
        pad.close()
    st = wait_state(lambda s: not service(s, "alpha"), 5)
    check("removing the test services removes their tiles", [s["id"] for s in st["services"]][:1] == ["youtube"],
          str([s["id"] for s in st["services"]]))


if __name__ == "__main__":
    main()
    print(f"{'FAILED: ' + ', '.join(failed) if failed else 'all launcher checks passed'}")
    sys.exit(1 if failed else 0)
