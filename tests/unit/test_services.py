import tomllib
from pathlib import Path

import pytest
from conftest import ROOT

from tvbox import app, apps, services
from tvbox.bindings import ConfigError

DEFAULTS = ROOT / "src" / "config" / "services.toml"
BASE = ("defaults", tomllib.loads(DEFAULTS.read_text()))


def override(text):
    return services.parse([BASE, ("user", tomllib.loads(text))])


def errors(text):
    with pytest.raises(ConfigError) as info:
        override(text)
    return "\n".join(info.value.errors)


def test_default_services_from_the_brief():
    found = services.load([DEFAULTS])
    assert [s.id for s in found] == ["youtube", "netflix", "disney", "floatplane", "jellyfin"]
    youtube, jellyfin = found[0], found[-1]
    assert youtube.kind == "browser" and youtube.url == "https://www.youtube.com/tv"
    assert "SmartTV" in youtube.user_agent              # preset name resolved
    assert found[1].user_agent == ""                    # Netflix: the browser's own
    assert jellyfin.kind == "native" and jellyfin.exec[0] == "jellyfin-desktop"
    assert [s.id for s in found if s.nav] == ["netflix", "disney"]


def test_add_change_remove_reorder_without_touching_code():
    found = override('''
order = ["plex", "youtube"]
[[service]]
id = "plex"
name = "Plex"
kind = "browser"
url = "https://app.plex.tv/desktop"
[[service]]
id = "netflix"
enabled = false
[[service]]
id = "youtube"
url = "https://www.youtube.com/tv#/settings"
''')
    assert [s.id for s in found] == ["plex", "youtube", "disney", "floatplane", "jellyfin"]
    youtube = found[1]
    assert youtube.url.endswith("#/settings") and youtube.name == "YouTube"   # other keys kept
    assert "SmartTV" in youtube.user_agent


def test_problems_are_reported_together():
    text = errors('''
[[service]]
id = "Bad Id"
[[service]]
id = "home"
[[service]]
id = "a"
kind = "browser"
[[service]]
id = "b"
kind = "native"
[[service]]
id = "c"
kind = "browser"
url = "https://example.org"
user_agent = "nope"
color = "red"
flags = ["no-dashes"]
colour = "#000000"
''')
    for needle in ("needs an id", "browser service needs url", "native service needs exec",
                   "'nope' is not defined", "color must look like", "flags must be",
                   "unknown key(s) colour"):
        assert needle in text


def test_broken_override_keeps_the_defaults(tmp_path):
    user = tmp_path / "services.toml"
    user.write_text("[[service]\n")
    found, errs = services.load_best([DEFAULTS, tmp_path / "none.toml", user])
    assert len(found) == 5 and str(user) in errs[0]
    assert services.load_best([DEFAULTS]) == (found, [])


def test_browser_command_line(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    youtube, netflix = services.load([DEFAULTS])[:2]
    argv = app.browser_argv(youtube, Path("/run/user/1000/tvbox/cache"))
    assert argv[0] == "chromium" and argv[-1] == "--app=https://www.youtube.com/tv"
    assert f"--user-data-dir={tmp_path}/data/tvbox/profiles/youtube" in argv
    assert "--kiosk" not in argv
    assert "--disk-cache-dir=/run/user/1000/tvbox/cache/youtube" in argv
    assert any(a.startswith("--user-agent=") and "SmartTV" in a for a in argv)
    assert any(a.startswith("--enable-features=") and "AcceleratedVideoDecodeLinuxGL" in a for a in argv)
    assert not any(a.startswith("--user-agent") for a in app.browser_argv(netflix, tmp_path))
    assert not any(a.startswith("--load-extension") for a in argv)
    assert any(a.startswith("--load-extension=") and a.endswith("/extensions/tvnav")
               for a in app.browser_argv(netflix, tmp_path))
    custom = services.parse([BASE, ("u", tomllib.loads(
        '[[service]]\nid = "netflix"\nflags = ["--force-dark-mode"]'))])[1]
    assert app.browser_argv(custom, tmp_path)[-2:] == ["--force-dark-mode", "--app=https://www.netflix.com/browse"]


def test_profile_gets_widevine_hint_only_when_the_module_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    youtube = services.load([DEFAULTS])[0]
    hint = app.profile_dir("youtube") / "WidevineCdm" / "latest-component-updated-widevine-cdm"
    monkeypatch.setattr(app, "WIDEVINE_DIR", tmp_path / "missing")
    app.prepare_profile(youtube)
    assert app.profile_dir("youtube").is_dir() and not hint.exists()
    cdm = tmp_path / "WidevineCdm"
    cdm.mkdir()
    (cdm / "manifest.json").write_text("{}")
    monkeypatch.setattr(app, "WIDEVINE_DIR", cdm)
    (app.profile_dir("youtube") / "DevToolsActivePort").write_text("1234\n/devtools/browser/x\n")
    assert app.devtools_port("youtube") == 1234
    app.prepare_profile(youtube)
    assert str(cdm) in hint.read_text()
    assert app.devtools_port("youtube") is None          # stale port file removed


def test_windows_by_workspace():
    tree = {"type": "root", "nodes": [{"type": "output", "nodes": [
        {"type": "workspace", "name": "home", "nodes": [{"type": "con", "pid": 5, "nodes": []}]},
        {"type": "workspace", "name": "youtube", "nodes": [
            {"type": "con", "nodes": [{"type": "con", "pid": 7, "nodes": []}]}],
         "floating_nodes": [{"type": "floating_con", "pid": 8, "nodes": []}]},
        {"type": "workspace", "name": "netflix", "nodes": []}]}]}
    assert apps.windows_by_workspace(tree) == {"home": 1, "youtube": 2, "netflix": 0}
    assert [(w["pid"], w["workspace"]) for w in apps.app_windows(tree)] == [
        (5, "home"), (7, "youtube"), (8, "youtube")]


def test_unit_of_pid(tmp_path, monkeypatch):
    assert apps.unit_of_pid(0) is None
    assert apps._UNIT_RE.search(
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-tvbox\\x2dapp.slice/"
        "tvbox-app@youtube.service").group(1) == "youtube"


def test_low_memory_stops_least_recently_used_background_apps(monkeypatch):
    import asyncio

    class FakeHub:
        app = "netflix"                     # focused: never stopped

    stopped = []
    memory = iter([500_000, 900_000, 2_000_000])        # kB available before each stop

    async def fake_run(*argv, **_kwargs):
        if argv[2] == "stop":
            stopped.append(argv[3])
        return 0, ""

    async def no_sleep(_seconds):
        pass

    manager = apps.AppManager.__new__(apps.AppManager)
    manager.hub, manager.services, manager.errors = FakeHub(), [], []
    manager.active = dict.fromkeys(["youtube", "netflix", "disney", "jellyfin"], "running")
    manager.last_used = {"youtube": 30.0, "netflix": 40.0, "disney": 10.0, "jellyfin": 20.0}

    async def keep_active():
        pass
    monkeypatch.setattr(manager, "refresh", keep_active)
    monkeypatch.setattr(apps, "run", fake_run)
    monkeypatch.setattr(apps, "available_kb", lambda: next(memory))
    monkeypatch.setattr(apps.asyncio, "sleep", no_sleep)
    asyncio.run(manager.free_memory(keep="youtube"))
    # oldest first, stops as soon as enough memory is free, never the focused or the new app
    assert stopped == ["tvbox-app@disney.service", "tvbox-app@jellyfin.service"]
