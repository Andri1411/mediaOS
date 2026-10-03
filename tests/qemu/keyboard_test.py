#!/usr/bin/python
"""Runs inside the test VM as root (see tests/qemu/session.sh), after
launcher_test.py: the navigation extension on a desktop-style page, the
on-screen keyboard (opened by hand and by focusing a text field) and the rest
of mouse mode. The page is served inside the VM.
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from evdev import ecodes as e
from input_test import USER_CONF, Daemon, Pad, check, drain, failed, output_device, tv
from launcher_test import WEB_PORT, WWW, cdp, wait_for, windows
from menu_test import HUB, SERVICES, api, service, state, step, ui_ready, wait_state

EXTENSION = "chrome-extension://ecgejpihnmlnjmgnejffnelbhbiehbpm"
NAV_SERVICE = f'''
[[service]]
id = "navtest"
name = "Nav test"
kind = "browser"
url = "http://127.0.0.1:{WEB_PORT}/nav.html"
nav = true
'''
PAGE = '''<!doctype html><title>tvbox nav test</title>
<style>body{background:#123;margin:40px} button,a{display:inline-block;width:220px;height:90px;margin:20px}
div[role=button]{width:220px;height:90px;margin:20px;background:#456}</style>
<div><button id="b1">1</button><button id="b2">2</button><button id="b3" disabled>3</button></div>
<div style="display:flex"><button id="b4">4</button><button id="b5">5</button><div id="d6" role="button" tabindex="0">6</div></div>
<div id="dialog" role="dialog" tabindex="0" style="position:fixed;right:20px;top:20px;width:260px;background:#789">
  <button id="ok-dialog" onclick="this.parentNode.remove()">OK</button></div>
<form onsubmit="submitted = field.value; return false"><input id="field" style="width:400px;height:50px;margin:20px"></form>
<script>
// Like the cookie banners on Netflix and Disney+: a focusable box around the
// buttons, focused by the site when the page loads.
document.getElementById("dialog").focus();
var clicks = [], submitted = null;
document.querySelectorAll("button, [role=button]").forEach((b) => b.addEventListener("click", () => clicks.push(b.id)));
</script>'''


def focused():
    return cdp("navtest", "document.querySelector('[data-tvnav-focus]')?.id ?? null")


def post(origin, **cmd):
    request = urllib.request.Request(f"{HUB}/api/cmd", data=json.dumps(cmd).encode(), headers={"Origin": origin})
    try:
        with urllib.request.urlopen(request, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as err:
        return err.code


def main():
    st = wait_state(ui_ready, 30)
    check("shell and inputd are connected to the hub", ui_ready(st), str(st)[:300])
    if failed:
        return
    WWW.mkdir(exist_ok=True)
    (WWW / "nav.html").write_text(PAGE)
    server = subprocess.Popen([sys.executable, "-m", "http.server", str(WEB_PORT), "--bind", "127.0.0.1"],
                              cwd=WWW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    Path(SERVICES).write_text(NAV_SERVICE)
    wait_state(lambda s: service(s, "navtest"), 5)
    out = output_device()
    daemon = Daemon()
    pad = Pad()
    daemon.wait_status(lambda s: s["devices"])
    try:
        api(cmd="launch", id="navtest")
        ready = wait_for(lambda: [w[:7] for w in windows().get("navtest", [])] == ["chrome-"]
                         and cdp("navtest", "document.readyState") == "complete"
                         and cdp("navtest", "document.documentElement.dataset.tvnav") == "on", 60)
        check("nav service: page loaded with the navigation extension (reloaded by the hub if the "
              "extension missed the first load)", bool(ready), str(windows()))
        time.sleep(1)

        # --- d-pad navigation on a desktop page ---
        step(pad, "down")
        check("a dialog on top of the page gets the focus first", focused() == "ok-dialog", str(focused()))
        step(pad, "left", "up")
        check("and keeps it until it is closed", focused() == "ok-dialog", str(focused()))
        step(pad, e.BTN_SOUTH, "right")
        check("then the first arrow key puts the focus ring on the top-left element", focused() == "b1", str(focused()))
        step(pad, "right")
        check("right moves to the neighbour", focused() == "b2", str(focused()))
        step(pad, "down")
        check("down moves to the element below", focused() == "b5", str(focused()))
        step(pad, e.BTN_SOUTH)
        clicks = cdp("navtest", "clicks.join()")
        check("A activates the focused element (once)", clicks == "ok-dialog,b5", str(clicks))
        step(pad, "right", e.BTN_SOUTH)
        clicks = cdp("navtest", "clicks.join()")
        check("elements that are buttons only by role work too", clicks == "ok-dialog,b5,d6", f"{focused()} {clicks}")
        step(pad, "up")
        check("disabled elements are skipped", focused() in ("d6", "b2"), str(focused()))
        step(pad, "left", "left", "left")
        check("the focus stops at the edge", focused() in ("b4", "b1"), str(focused()))

        # --- on-screen keyboard opens when a text field gets focus ---
        step(pad, "down", "down")
        st = wait_state(lambda s: s["overlay"] == "keyboard", 5)
        check("focusing a text field opens the on-screen keyboard", focused() == "field" and st["overlay"] == "keyboard",
              f"{focused()} {st['overlay']}")
        mode = daemon.call(cmd="status")["mode"]
        check("the controller now drives the keyboard", mode == "ui", mode)
        value = lambda: cdp("navtest", "field.value")           # noqa: E731
        step(pad, e.BTN_SOUTH)                                   # q
        check("A types the focused key", wait_for(lambda: value() == "q", 5), str(value()))
        step(pad, "down", "down", "down", "right", "right", e.BTN_SOUTH)     # layer with accented letters
        step(pad, "up", "up", "up", "up", e.BTN_SOUTH)                         # í
        check("letters outside the keyboard layout can be typed", wait_for(lambda: value() == "qí", 5), str(value()))
        step(pad, e.BTN_X)
        check("X deletes", wait_for(lambda: value() == "q", 5), str(value()))
        step(pad, e.BTN_EAST)
        st = wait_state(lambda s: s["overlay"] is None, 5)
        check("B closes the keyboard", st["overlay"] is None and daemon.call(cmd="status")["mode"] == "app",
              str(st["overlay"]))

        step(pad, e.BTN_Y)
        st = wait_state(lambda s: s["overlay"] == "keyboard", 5)
        check("Y opens the keyboard by hand", st["overlay"] == "keyboard", str(st["overlay"]))
        step(pad, "down", "down", "down", e.BTN_SOUTH)           # shift
        step(pad, "up", "up", "up", e.BTN_SOUTH, e.BTN_SOUTH)    # Q then q
        check("shift applies to one letter", wait_for(lambda: value() == "qQq", 5), str(value()))
        step(pad, e.BTN_START)
        submitted = wait_for(lambda: cdp("navtest", "submitted") == "qQq", 5)
        st = wait_state(lambda s: s["overlay"] is None, 5)
        check("Start presses Enter and closes the keyboard", submitted and st["overlay"] is None,
              f"{cdp('navtest', 'submitted')} {st['overlay']}")
        drain(out, 0.2)

        # --- what the extension may ask of the hub ---
        check("the extension may report text focus", post(EXTENSION, cmd="text_focus", focused=False) == 200)
        check("but nothing else", post(EXTENSION, cmd="reboot") == 400 and post(EXTENSION, cmd="type", text="x") == 400)
        check("other extensions and sites are refused",
              post("chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", cmd="text_focus", focused=True) == 403
              and post("https://www.netflix.com", cmd="text_focus", focused=True) == 403)

        # --- mouse mode: right click, configurable speed ---
        api(cmd="mouse_toggle")
        daemon.wait_status(lambda s: s["mode"] == "mouse")
        drain(out, 0.2)
        pad.press(e.BTN_X)
        got = drain(out)
        check("mouse mode: X is the right button", got == ["BTN_RIGHT:1", "BTN_RIGHT:0"], str(got))
        pad.set(e.EV_KEY, e.BTN_SOUTH, 1)
        pad.axis(e.ABS_X, 32767, hold=0.3)
        pad.set(e.EV_KEY, e.BTN_SOUTH, 0)
        got = drain(out)
        moved = [ev for ev in got if ev.startswith("REL_X")]
        check("mouse mode: holding A while moving drags",
              got[0] == "BTN_LEFT:1" and got[-1] == "BTN_LEFT:0" and len(moved) > 3, str(got[:4]))

        def travel():
            drain(out, 0.1)
            pad.axis(e.ABS_X, 32767, hold=0.5)
            return sum(int(ev.split(":")[1]) for ev in drain(out) if ev.startswith("REL_X"))
        fast = travel()
        USER_CONF.parent.mkdir(parents=True, exist_ok=True)
        USER_CONF.write_text("[mouse]\nspeed = 200\n")
        daemon.read(1.0)
        slow = travel()
        USER_CONF.unlink()
        daemon.read(1.0)
        check("mouse speed follows [mouse] in bindings.toml", 0 < slow < fast / 2, f"default {fast}, speed=200 {slow}")
        api(cmd="mouse_toggle")
        daemon.wait_status(lambda s: s["mode"] == "app")
    finally:
        server.terminate()
        tv("systemctl", "--user", "stop", "tvbox-app@navtest")
        Path(SERVICES).unlink(missing_ok=True)
        USER_CONF.unlink(missing_ok=True)
        pad.close()
    api(cmd="home")


if __name__ == "__main__":
    main()
    print(f"{'FAILED: ' + ', '.join(failed) if failed else 'all keyboard and navigation checks passed'}")
    sys.exit(1 if failed else 0)
