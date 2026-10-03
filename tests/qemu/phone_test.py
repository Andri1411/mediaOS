#!/usr/bin/env python3
"""Phone remote test, run on the host (stdlib only) against the VM's hub
through QEMU's port forward: requests arrive in the guest from 10.0.2.2, i.e.
like a phone on the LAN. Setup steps run in the guest over SSH.

    tests/qemu/phone_test.py [port]      (see tests/qemu/session.sh)
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("QEMU_HUB_PORT", 8080))
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = BASE
TYPED = "/tmp/phone-typed"
SERVICES = f'''
[[service]]
id = "term"
name = "Term"
kind = "native"
exec = ["foot", "sh", "-c", "read line; echo \\"$line\\" > {TYPED}; sleep 60"]
'''
failed = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"\n       {detail}" if not ok else ""), flush=True)
    if not ok:
        failed.append(name)


def vm(command):
    return subprocess.run([str(ROOT / "tests/qemu/vm.sh"), "ssh", command],
                          capture_output=True, text=True).stdout


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def http(path, method="GET", body=None, cookie=None, origin=None):
    """(status, headers, body text)."""
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=15) as r:
            return r.status, r.headers, r.read().decode(errors="replace")
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read().decode(errors="replace")


def cmd(cookie, **command):
    return http("/api/cmd", "POST", command, cookie, ORIGIN)


def state(cookie):
    status, _, text = http("/api/state", cookie=cookie)
    return json.loads(text) if status == 200 else {}


def wait(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.3)
    return predicate()


class WebSocket:
    """Just enough of RFC 6455 for the test: text frames, masked."""

    def __init__(self, path, cookie):
        self.sock = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                           f"Origin: {ORIGIN}\r\nCookie: {cookie}\r\n\r\n").encode())
        self.status = int(self.sock.recv(4096).split(b" ", 2)[1])

    def send(self, message):
        payload = json.dumps(message).encode()
        mask = os.urandom(4)
        header = bytes([0x81]) + (bytes([0x80 | len(payload)]) if len(payload) < 126
                                  else bytes([0x80 | 126]) + struct.pack(">H", len(payload)))
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def drop(self):
        """Vanish without a close handshake, like a phone losing Wi-Fi."""
        self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()


def main():
    # --- unpaired ---
    status, _, _ = http("/api/state")
    check("unpaired LAN client: API refused", status == 401, str(status))
    status, _, body = http("/phone")
    check("unpaired LAN client: the remote page explains how to pair",
          status == 401 and "not paired" in body, f"{status} {body[:80]}")
    status, _, _ = http("/static/phone.css")
    check("static files are public", status == 200, str(status))
    status, _, _ = http("/pair?t=guessed")
    check("a wrong pairing token is refused", status == 403, str(status))

    # --- pairing ---
    reply = json.loads(vm("curl -s -X POST 127.0.0.1:8080/api/pair/start") or "{}")
    url = reply.get("url", "")
    check("the TV makes a pairing link with the box's LAN address",
          url.startswith("http://10.0.2.15:8080/pair?t="), url)
    token_path = url[url.index("/pair"):] if "/pair" in url else "/pair"
    qr = vm(f"curl -s '127.0.0.1:8080/api/pair/qr.svg?url={urllib.request.quote(url, safe='')}'")
    check("the TV can draw it as a QR code", qr.lstrip().startswith("<svg") and "<path" in qr, qr[:60])
    status, headers, _ = http(token_path)
    cookie_header = headers.get("Set-Cookie", "")
    cookie = cookie_header.split(";", 1)[0]
    check("the pairing link sets a device cookie and opens the remote",
          status == 302 and headers.get("Location") == "/phone" and cookie.startswith("tvbox_device="),
          f"{status} {headers.get('Location')} {cookie_header}")
    check("the cookie is HttpOnly and SameSite=Strict",
          "HttpOnly" in cookie_header and "SameSite=Strict" in cookie_header, cookie_header)
    status, _, _ = http(token_path)
    check("a pairing link works only once", status == 403, str(status))
    st = state(cookie)
    check("paired: state readable, device listed", len(st.get("devices", [])) >= 1, str(st)[:200])
    device_id = st.get("devices", [{}])[-1].get("id")
    status, _, body = http("/phone", cookie=cookie)
    check("paired: the remote page loads", status == 200 and "tvbox remote" in body, str(status))

    # --- what a phone may not do ---
    check("phone cannot open the TV's own pages", http("/home", cookie=cookie)[0] == 403)
    check("phone cannot start pairing", http("/api/pair/start", "POST", {}, cookie, ORIGIN)[0] == 403)
    check("requests from other web sites are refused even with the cookie",
          http("/api/cmd", "POST", {"cmd": "home"}, cookie, "http://evil.example")[0] == 403)

    # --- remote control ---
    vm(f"rm -f {TYPED}; cat > /etc/tvbox/services.toml <<'END'\n{SERVICES}\nEND")
    wait(lambda: any(s["id"] == "term" for s in state(cookie).get("services", [])), 5)
    cmd(cookie, cmd="launch", id="term")
    st = wait(lambda: (lambda s: s if s.get("app") == "term" else None)(state(cookie)), 15) or {}
    check("phone launches an app", st.get("app") == "term", str(st.get("app")))
    time.sleep(1.5)
    status, _, _ = cmd(cookie, cmd="type", text="hello from the phone")
    cmd(cookie, cmd="key", key="enter")
    typed = wait(lambda: vm(f"cat {TYPED} 2>/dev/null").strip(), 10)
    check("text typed on the phone arrives in the app", status == 200 and typed == "hello from the phone",
          f"{status} {typed!r}")
    for button in ("up", "down", "ok"):
        status, _, _ = cmd(cookie, cmd="button", button=button)
    check("d-pad buttons are accepted", status == 200)
    status, _, body = cmd(cookie, cmd="button", button="fire")
    check("unknown buttons are rejected", status == 400, body)
    status, _, _ = cmd(cookie, cmd="pointer", dx=40, dy=-20)
    status2, _, _ = cmd(cookie, cmd="click", button="left")
    check("touchpad moves and clicks are accepted", status == status2 == 200)

    # A phone that loses its connection mid-press must not leave a button
    # held: if "home" stayed down, its long press would open the menu.
    ws = WebSocket("/ws?role=phone", cookie)
    check("paired phone gets a WebSocket", ws.status == 101, str(ws.status))
    ws.send({"cmd": "button", "button": "home", "state": "down"})
    time.sleep(0.15)
    ws.drop()
    time.sleep(1.5)
    st = state(cookie)
    check("dropped connection releases held buttons (short press, no menu)",
          st.get("app") == "home" and st.get("overlay") is None, f"{st.get('app')} {st.get('overlay')}")
    status = WebSocket("/ws?role=overlay", cookie).status
    check("a phone cannot pose as the TV's overlay", status == 403, str(status))
    unpaired = WebSocket("/ws?role=phone", "tvbox_device=wrong").status
    check("unpaired WebSocket refused", unpaired == 401, str(unpaired))

    # --- bindings editor ---
    status, _, text = http("/api/bindings", cookie=cookie)
    b = json.loads(text) if status == 200 else {}
    check("bindings editor shows the defaults", "[global]" in (b.get("defaults") or ""), str(status))
    status, _, text = http("/api/bindings", "POST", {"text": '[global]\nstart = "key:NoSuchKey"\n'}, cookie, ORIGIN)
    result = json.loads(text)
    check("invalid bindings are refused with the reason", not result["ok"] and "NoSuchKey" in result["errors"][0],
          str(result))
    status, _, text = http("/api/bindings", "POST", {"text": '[global]\nx = "key:x"\n', "check_only": True},
                           cookie, ORIGIN)
    exists = vm("test -e /home/tv/.config/tvbox/bindings.toml && echo yes").strip()
    check("check only: valid, nothing saved", json.loads(text)["ok"] and exists != "yes", f"{text} {exists}")
    status, _, text = http("/api/bindings", "POST", {"text": '[global]\nx = "key:x"\n'}, cookie, ORIGIN)
    saved = vm("cat /home/tv/.config/tvbox/bindings.toml")
    st = wait(lambda: vm("sudo -u tv env XDG_RUNTIME_DIR=/run/user/$(id -u tv) tvbox-ctl status").count("bindings ok"), 5)
    check("valid bindings are saved and taken into use", json.loads(text)["ok"] and 'key:x' in saved and st,
          f"{text} {saved!r}")
    vm("rm -f /home/tv/.config/tvbox/bindings.toml")

    # --- health ---
    status, _, text = http("/api/health", cookie=cookie)
    h = json.loads(text) if status == 200 else {}
    names = {u["unit"]: u["state"] for u in h.get("services", [])}
    check("health: our services with their state",
          names.get("tvbox-inputd.service") == "active" and names.get("tvbox-hub.service") == "active", str(names))
    check("health: uptime, disk, memory, load",
          h.get("uptime_s", 0) > 0 and h["disk"]["total"] > h["disk"]["free"] > 0
          and h["memory"]["total"] > 0 and len(h["load"]) == 3, str(h)[:200])
    check("health: restarts from the journal (list)", isinstance(h.get("restarts"), list), str(h.get("restarts"))[:100])
    print(f"       (CPU temperature in the VM: {h.get('cpu_temp_c')})")

    # --- revoke ---
    vm("systemctl --user -M tv@ stop tvbox-app@term; rm -f /etc/tvbox/services.toml")
    status, _, _ = cmd(cookie, cmd="revoke_device", id=device_id)
    check("a phone can be removed", status == 200)
    check("and is locked out afterwards", http("/api/state", cookie=cookie)[0] == 401)


if __name__ == "__main__":
    main()
    print(f"{'FAILED: ' + ', '.join(failed) if failed else 'all phone checks passed'}")
    sys.exit(1 if failed else 0)
