"""The family drive's key (B7): made on a family device, sealed objects, device-to-device wrap.

Pins: a sealed object opens only with the same family key; a tampered byte or a wrong key
fails loudly; a wrapped key opens only on the device it was wrapped for; the key file is
written once and a different key is refused; nothing here reads AITHER_MASTER_KEY; and the
CLI shares the key only as wrapped blobs (the plaintext key never goes over the wire).
"""
from __future__ import annotations

import base64
import json

import pytest

from adk import family_drive_cli as cli
from adk import family_vault as fv


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / "me"))
    monkeypatch.setenv("AITHER_MASTER_KEY", "must-never-matter")
    return tmp_path


def test_seal_round_trip_and_header():
    key = fv.new_family_key()
    blob = fv.seal(key, b"family photo bytes")
    assert blob[:4] == b"AFD1" and blob[4:12] == fv.key_id(key)
    assert b"family photo" not in blob
    assert fv.open_sealed(key, blob) == b"family photo bytes"
    assert fv.seal(key, b"x") != fv.seal(key, b"x")  # a fresh nonce every time


def test_wrong_key_and_tampering_fail_loudly():
    key, other = fv.new_family_key(), fv.new_family_key()
    blob = fv.seal(key, b"secret")
    with pytest.raises(fv.VaultError, match="different family key"):
        fv.open_sealed(other, blob)
    flipped = bytearray(blob)
    flipped[-1] ^= 1
    with pytest.raises(fv.VaultError, match="does not open"):
        fv.open_sealed(key, bytes(flipped))
    header = bytearray(blob)
    header[14] ^= 1  # the nonce is associated data too
    with pytest.raises(fv.VaultError):
        fv.open_sealed(key, bytes(header))
    with pytest.raises(fv.VaultError):
        fv.open_sealed(key, b"AFD1short")


def test_a_wrapped_key_opens_only_on_its_device(tmp_path):
    alice, bob = tmp_path / "alice", tmp_path / "bob"
    key = fv.new_family_key()
    _priv, bob_pub = fv.device_keypair(root=bob)
    blob = fv.wrap_key(key, bob_pub)
    assert len(blob) == 96 and key not in blob
    assert fv.unwrap_key(blob, root=bob) == key
    with pytest.raises(fv.VaultError, match="another device"):
        fv.unwrap_key(blob, root=alice)
    # the device key is made once and kept
    assert fv.device_keypair(root=bob)[1] == bob_pub


def test_key_file_is_kept_and_a_different_key_refused(tmp_path):
    key = fv.new_family_key()
    path = fv.save_family_key("family", key, root=tmp_path)
    assert fv.load_family_key("family", root=tmp_path) == key
    assert fv.save_family_key("family", key, root=tmp_path) == path  # same key: fine
    with pytest.raises(fv.VaultError, match="different key"):
        fv.save_family_key("family", fv.new_family_key(), root=tmp_path)
    # a family id is a name, never a path
    with pytest.raises(fv.VaultError):
        fv.save_family_key("../", key, root=tmp_path)
    assert fv._safe_name("../fam ily") == "family"


def test_the_vault_never_reads_the_master_key():
    import inspect
    src = inspect.getsource(fv)
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith(("#", "*", "``")))
    assert "environ.get(\"AITHER_MASTER_KEY\"" not in code and "getenv(\"AITHER_MASTER_KEY\"" not in code


# --- the CLI: the key travels only wrapped ---------------------------------------------

class FakeHousehold:
    def __init__(self):
        self.devices = {}
        self.wraps = {}
        self.wire = []

    def __call__(self, method, path, *, json_body=None, content=None, params=None):
        self.wire.append(json.dumps(json_body or {}) + (content or b"").hex() + json.dumps(params or {}))
        if path == "/family/storage/keys/devices" and method == "POST":
            self.devices[json_body["pubkey"]] = json_body["label"]
            return 200, {}
        if path == "/family/storage/keys/devices":
            return 200, {"devices": [{"pubkey": p, "label": l,
                                      "has_key": [k for (to, k) in self.wraps if to == p]}
                                     for p, l in self.devices.items()]}
        if path == "/family/storage/keys/wraps" and method == "POST":
            self.wraps[(json_body["to_pubkey"], json_body["key_id"])] = json_body["blob_b64"]
            return 200, {}
        if path == "/family/storage/keys/wraps":
            return 200, {"wraps": [{"key_id": k, "blob_b64": b} for (to, k), b in self.wraps.items()
                                   if to == params["pubkey"]]}
        raise AssertionError(path)


def test_init_share_accept_moves_the_key_only_wrapped(home, monkeypatch, capsys):
    house = FakeHousehold()
    monkeypatch.setattr(cli, "request", house)
    monkeypatch.setenv("AITHER_HOME", str(home / "laptop"))
    assert cli.main(["key", "init", "--label", "laptop"]) == 0
    key = fv.load_family_key("family")
    # a second computer registers, the laptop shares, the desktop accepts
    monkeypatch.setenv("AITHER_HOME", str(home / "desktop"))
    assert cli.main(["key", "accept"]) == 2  # nothing waiting yet
    assert cli.main(["key", "register", "--label", "desktop"]) == 0
    monkeypatch.setenv("AITHER_HOME", str(home / "laptop"))
    assert cli.main(["key", "share"]) == 0
    monkeypatch.setenv("AITHER_HOME", str(home / "desktop"))
    assert cli.main(["key", "accept"]) == 0
    assert fv.load_family_key("family") == key
    wire = "".join(house.wire)
    assert key.hex() not in wire and base64.b64encode(key).decode() not in wire
    monkeypatch.setenv("AITHER_HOME", str(home / "laptop"))
    capsys.readouterr()
    assert cli.main(["key", "share"]) == 0  # already shared: nothing new wrapped
    assert "with 0 computer(s)" in capsys.readouterr().out


def test_no_key_says_how_to_get_one(home, capsys):
    assert cli.main(["key", "share"]) == 2
    assert "key init" in capsys.readouterr().err


def test_api_base(monkeypatch):
    monkeypatch.delenv("AITHER_FAMILY_API", raising=False)
    monkeypatch.delenv("AITHER_PORTAL_URL", raising=False)
    assert cli.api_base() == "https://api.aitherium.com/api/tutor"
    monkeypatch.setenv("AITHER_PORTAL_URL", "https://portal.example/")
    assert cli.api_base() == "https://portal.example/api/tutor"
    monkeypatch.setenv("AITHER_FAMILY_API", "http://127.0.0.1:8001/api/v1/tutor/")
    assert cli.api_base() == "http://127.0.0.1:8001/api/v1/tutor"
