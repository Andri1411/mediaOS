"""The daemon end to end, minus hardware: a fake output device, no device
scan, no sway; commands and events go over the real control socket."""
import asyncio
import json

import pytest
from conftest import DEFAULTS
from evdev import ecodes as e

from tvbox.inputd import InputDaemon, load_config, stick_curve


class FakeOutput:
    def __init__(self):
        self.events = []

    def tap(self, codes):
        self.events.append(("tap", codes))

    def click(self, down, button):
        self.events.append(("click", down) if button == "left" else ("click", down, button))

    def move(self, dx, dy):
        self.events.append(("move", dx, dy))

    def scroll(self, vertical, horizontal):
        self.events.append(("scroll", vertical, horizontal))

    def close(self):
        pass


class Client:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.next_id = 0

    async def recv(self):
        return json.loads(await asyncio.wait_for(self.reader.readline(), 2))

    async def until(self, **match):
        while True:
            msg = await self.recv()
            if all(msg.get(k) == v for k, v in match.items()):
                return msg

    async def call(self, **cmd):
        self.next_id += 1
        self.writer.write(json.dumps({"id": self.next_id, **cmd}).encode() + b"\n")
        return await self.until(reply=self.next_id)


def run(tmp_path, scenario, user_text=None):
    user = tmp_path / "bindings.toml"
    if user_text is not None:
        user.write_text(user_text)

    async def main():
        out = FakeOutput()
        daemon = InputDaemon(output=out, paths=[DEFAULTS, user], device_dir=None,
                             socket_path=tmp_path / "input.sock", use_sway=False)
        task = asyncio.create_task(daemon.run())
        for _ in range(100):
            if (tmp_path / "input.sock").exists():
                break
            await asyncio.sleep(0.01)
        client = Client(*await asyncio.open_unix_connection(str(tmp_path / "input.sock")))
        try:
            await scenario(daemon, client, out, user)
        finally:
            daemon.stop()
            await task
    asyncio.run(main())


def test_hello_and_key_output(tmp_path):
    async def scenario(daemon, client, out, user):
        hello = await client.recv()
        assert hello == {"event": "hello", "mode": "app", "app": None,
                         "devices": [], "config_errors": []}
        assert (await client.call(cmd="button", button="ok"))["ok"]
        assert (await client.call(cmd="key", combo="ctrl+l"))["ok"]
        assert out.events == [("tap", (e.KEY_ENTER,)), ("tap", (e.KEY_LEFTCTRL, e.KEY_L))]
    run(tmp_path, scenario)


def test_actions_for_the_hub_are_broadcast(tmp_path):
    async def scenario(daemon, client, out, user):
        client.writer.write(b'{"cmd": "button", "button": "home", "state": "down"}\n')
        assert (await client.until(event="action"))["action"] == "ui:system_menu"   # long press
        await client.call(cmd="button", button="home", state="up")
        client.writer.write(b'{"cmd": "button", "button": "rt"}\n')
        assert (await client.until(event="action")) == {
            "event": "action", "action": "volume:+2", "button": "rt"}
        assert out.events == []
    run(tmp_path, scenario, '[timing]\nlong_press_ms = 150\n')


def test_ui_mode_sends_navigation_instead_of_keys(tmp_path):
    async def scenario(daemon, client, out, user):
        await client.call(cmd="mode", mode="ui")
        client.writer.write(b'{"cmd": "button", "button": "down"}\n')
        assert (await client.until(event="nav"))["button"] == "down"
        await client.call(cmd="button", button="start")
        assert out.events == []
        await client.call(cmd="mode", mode="app")
        await client.call(cmd="button", button="down")
        assert out.events == [("tap", (e.KEY_DOWN,))]
        assert (await client.call(cmd="status"))["mode"] == "app"
    run(tmp_path, scenario)


def test_touchpad_commands(tmp_path):
    async def scenario(daemon, client, out, user):
        await client.call(cmd="move", dx=5, dy=-3)
        await client.call(cmd="scroll", dy=-2)
        await client.call(cmd="click", button="right")
        assert (await client.call(cmd="click", button="middle"))["ok"] is False
        assert out.events == [("move", 5, -3), ("scroll", -2, 0),
                              ("click", True, "right"), ("click", False, "right")]
    run(tmp_path, scenario)


def test_mouse_toggle_and_click(tmp_path):
    async def scenario(daemon, client, out, user):
        client.writer.write(b'{"cmd": "action", "action": "mouse:toggle"}\n')
        assert (await client.until(event="mode"))["mode"] == "mouse"
        await client.call(cmd="button", button="ok")
        assert out.events == [("click", True), ("click", False)]
        await client.call(cmd="action", action="mouse:toggle")
        assert daemon.engine.mode == "app"
    run(tmp_path, scenario)


def test_bad_commands_get_an_error_and_do_not_kill_the_connection(tmp_path):
    async def scenario(daemon, client, out, user):
        for cmd in ({"cmd": "nope"}, {"cmd": "button", "button": "fire"},
                    {"cmd": "mode", "mode": "x"}, {"cmd": "key", "combo": "NoKey"},
                    {"cmd": "action", "action": "shell:reboot"}, {"cmd": "mode"}):
            reply = await client.call(**cmd)
            assert reply["ok"] is False and reply["error"]
        client.writer.write(b"not json\n[1]\n")
        assert (await client.call(cmd="status"))["ok"]
    run(tmp_path, scenario)


def test_reload_on_file_change_keeps_last_good_config(tmp_path):
    async def scenario(daemon, client, out, user):
        user.write_text('[global]\nok = "key:x"\n')
        assert (await client.until(event="config"))["errors"] == []
        await client.call(cmd="button", button="ok")
        user.write_text('[global]\nok = "key:NoSuchKey"\n')
        errors = (await client.until(event="config"))["errors"]
        assert "NoSuchKey" in errors[0]
        await client.call(cmd="button", button="ok")
        assert out.events == [("tap", (e.KEY_X,)), ("tap", (e.KEY_X,))]
        assert (await client.call(cmd="status"))["config_errors"] == errors
        user.unlink()
        assert (await client.until(event="config"))["errors"] == []
        await client.call(cmd="button", button="ok")
        assert out.events[-1] == ("tap", (e.KEY_ENTER,))
    run(tmp_path, scenario, "")


def test_broken_user_file_at_startup_falls_back_to_defaults(tmp_path):
    user = tmp_path / "bindings.toml"
    user.write_text("[global\n")
    config, errors = load_config([DEFAULTS, user])
    assert str(config.global_["ok"].press) == "key:Return"
    assert str(user) in errors[0]
    config, errors = load_config([DEFAULTS])
    assert errors == []


def test_stick_curve():
    assert stick_curve(0.1) == 0 and stick_curve(-0.15) == 0
    assert stick_curve(1.0) == pytest.approx(1.0)
    assert stick_curve(-1.0) == pytest.approx(-1.0)
    assert 0 < stick_curve(0.5) < 0.25          # gentle near the centre
