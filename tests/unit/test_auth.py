from tvbox import auth
from tvbox.auth import DeviceStore, classify

EXT = "chrome-extension://ecgejpihnmlnjmgnejffnelbhbiehbpm"


def test_pairing_is_one_time_and_expires(tmp_path):
    store = DeviceStore(tmp_path / "devices.json")
    token, expires = store.start_pairing(now=1000)
    assert expires == 1000 + auth.PAIRING_TTL_S
    assert store.pair("wrong", "x", now=1001) is None
    device, device_token = store.pair(token, "iPhone", now=1001)
    assert device.name == "iPhone"
    assert store.pair(token, "again", now=1002) is None              # used up
    late, _ = store.start_pairing(now=2000)
    assert store.pair(late, "late", now=2000 + auth.PAIRING_TTL_S + 1) is None
    assert store.check(device_token).id == device.id
    assert store.check("nope") is None and store.check(None) is None


def test_devices_persist_hashed_and_can_be_revoked(tmp_path):
    path = tmp_path / "devices.json"
    store = DeviceStore(path)
    device, device_token = store.pair(store.start_pairing()[0], "Android phone")
    assert device_token not in path.read_text()                    # only the hash is stored
    assert path.stat().st_mode & 0o777 == 0o600
    again = DeviceStore(path)
    assert again.check(device_token).name == "Android phone"
    assert [d["name"] for d in again.listing()] == ["Android phone"]
    assert "token_hash" not in again.listing()[0]
    assert again.revoke(device.id) and not again.revoke(device.id)
    assert DeviceStore(path).check(device_token) is None


def test_device_names():
    assert auth.device_name("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)") == "iPhone"
    assert auth.device_name("Mozilla/5.0 (Linux; Android 15; Pixel 9)") == "Android phone"
    assert auth.device_name("") == "phone"


def test_classify():
    def c(remote, host="127.0.0.1:8080", origin=None, path="/api/state", method="GET"):
        return classify(remote, host, origin, path, 8080, EXT, method)
    # the TV's own pages and tools
    assert c("127.0.0.1") == "tv"
    assert c("127.0.0.1", "localhost:8080", "http://localhost:8080") == "tv"
    # pages in the box's browsers, DNS rebinding, other extensions
    assert c("127.0.0.1", origin="https://www.netflix.com") == "deny"
    assert c("127.0.0.1", host="evil.example:8080") == "deny"
    assert c("127.0.0.1", origin="chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") == "deny"
    # our extension: one endpoint only
    assert c("127.0.0.1", origin=EXT, path="/api/cmd", method="POST") == "extension"
    assert c("127.0.0.1", origin=EXT, path="/api/state") == "deny"
    # phones on the LAN
    assert c("192.168.1.20", "192.168.1.5:8080") == "lan"
    assert c("192.168.1.20", "192.168.1.5:8080", "http://192.168.1.5:8080") == "lan"
    assert c("192.168.1.20", "192.168.1.5:8080", "http://evil.example") == "deny"
    assert c("192.168.1.20", "192.168.1.5:8080", path="/pair") == "public"
    assert c("192.168.1.20", "192.168.1.5:8080", path="/static/phone.css") == "public"
    assert c("192.168.1.20", "192.168.1.5:8080", path="/pairing") == "lan"
