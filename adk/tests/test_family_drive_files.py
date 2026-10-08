"""The family drive (B8): put / get / ls / rm against a household that only sees ciphertext.

Pins: a file round-trips through sealed chunks; names and contents never reach the
household; a large file is chunked; a reader waits while a device sends a chunk back; two
computers writing at once both land (compare-and-set index); rm drops the chunks and the
old index; a path cannot climb out of the drive.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from adk import family_drive_cli as cli
from adk import family_drive_files as files
from adk import family_vault as fv


class Household:
    """The real routes' contract, in memory: content-addressed objects + CAS index."""

    def __init__(self):
        self.objects = {}
        self.index = ""
        self.withheld = {}   # object_id -> polls left before a device "sends it back"
        self.race = None     # a function run once just before the next index move
        self.seen = []
        self.params = []     # query params of each object put
        self.ec_devices = True   # False: the family has too few devices for the shards
        self.modes = {}

    def __call__(self, method, path, *, json_body=None, content=None, params=None):
        self.seen.append((method, path, content or b"", json.dumps(json_body or {})))
        if path == "/family/storage/objects" and method == "POST":
            self.params.append(dict(params or {}))
            ec = (params or {}).get("ec", "")
            mode = "copies"
            if ec and len(content) >= 64 * 1024:
                if not self.ec_devices:
                    raise cli.ApiError(409, "not enough lending devices to spread shards")
                mode = "ec"
            oid = hashlib.sha256(content).hexdigest()
            self.objects[oid] = content
            self.modes[oid] = mode
            return 201, {"object_id": oid, "mode": mode}
        if path == "/family/storage/objects" and method == "GET":
            return 200, {"objects": [
                {"object_id": o, "stored": 6, "replicas": 6, "mode": "ec", "k": 4, "m": 2,
                 "recoverable": True} if self.modes.get(o) == "ec" else
                {"object_id": o, "stored": 2, "mode": "copies"} for o in self.objects]}
        if path.startswith("/family/storage/objects/"):
            oid = path.rsplit("/", 1)[1]
            if method == "DELETE":
                self.objects.pop(oid, None)
                return 200, {"deleted": True}
            if self.withheld.get(oid, 0) > 0:
                self.withheld[oid] -= 1
                return 202, b""
            return 200, self.objects[oid]
        if path == "/family/storage/drive/index" and method == "GET":
            return 200, {"object_id": self.index, "version": 0}
        if path == "/family/storage/drive/index" and method == "POST":
            if self.race:
                race, self.race = self.race, None
                race()
            if json_body["previous"] != self.index:
                raise cli.ApiError(409, "index_moved")
            self.index = json_body["object_id"]
            return 200, {}
        if path == "/family/storage/drive/rules" and method == "GET":
            return 200, {"rules": [], "devices": []}
        raise AssertionError(path)


@pytest.fixture
def house(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / "home"))
    fv.save_family_key("family", fv.new_family_key())
    h = Household()
    monkeypatch.setattr(cli, "request", h)
    monkeypatch.setattr(files, "_sleep", lambda s: None)
    return h


def test_put_get_round_trip_and_the_household_sees_only_ciphertext(house, tmp_path, capsys):
    src = tmp_path / "Grandma birthday.txt"
    src.write_bytes(b"secret family recipe")
    assert cli.main(["put", str(src)]) == 0
    out = tmp_path / "back.txt"
    assert cli.main(["get", "Grandma birthday.txt", "--out", str(out)]) == 0
    assert out.read_bytes() == b"secret family recipe"
    wire = b"".join(c for _m, _p, c, _j in house.seen) + "".join(j for *_x, j in house.seen).encode()
    assert b"secret family recipe" not in wire and b"Grandma" not in wire
    capsys.readouterr()
    assert cli.main(["ls", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["name"] == "Grandma birthday.txt" and rows[0]["copies"] == 2


def test_a_large_file_is_chunked(house, tmp_path, monkeypatch):
    monkeypatch.setattr(files, "CHUNK", 10)
    src = tmp_path / "big.bin"
    src.write_bytes(bytes(range(25)))
    cli.main(["put", str(src)])
    key = cli.family_key()
    _oid, index = files.read_index(key)
    assert len(index["files"]["big.bin"]["chunks"]) == 3
    out = tmp_path / "big.out"
    cli.main(["get", "big.bin", "--out", str(out)])
    assert out.read_bytes() == bytes(range(25))


def test_an_empty_file_round_trips(house, tmp_path):
    src = tmp_path / "empty"
    src.write_bytes(b"")
    cli.main(["put", str(src)])
    out = tmp_path / "empty.out"
    assert cli.main(["get", "empty", "--out", str(out)]) == 0 and out.read_bytes() == b""


def test_a_reader_waits_while_a_device_sends_a_chunk_back(house, tmp_path):
    src = tmp_path / "f"
    src.write_bytes(b"later")
    cli.main(["put", str(src)])
    key = cli.family_key()
    _oid, index = files.read_index(key)
    house.withheld[index["files"]["f"]["chunks"][0]] = 3
    out = tmp_path / "f.out"
    assert cli.main(["get", "f", "--out", str(out)]) == 0 and out.read_bytes() == b"later"
    house.withheld[index["files"]["f"]["chunks"][0]] = 10 ** 6
    assert cli.main(["get", "f", "--out", str(out), "--wait", "0"]) == 2


def test_two_computers_writing_at_once_both_land(house, tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"A")
    b.write_bytes(b"B")
    cli.main(["put", str(a)])
    # while this computer adds b.txt, another one adds c.txt first
    other_key = cli.family_key()

    def other_writer():
        _prev, index = files.read_index(other_key)
        index["files"]["c.txt"] = {"size": 1, "chunks": [], "replicas": 2}
        house.index = files.put_object(fv.seal(other_key, json.dumps(index).encode()), replicas=2)

    house.race = other_writer
    assert cli.main(["put", str(b)]) == 0
    _oid, index = files.read_index(cli.family_key())
    assert set(index["files"]) == {"a.txt", "b.txt", "c.txt"}


def test_rm_drops_chunks_and_the_old_index(house, tmp_path):
    src = tmp_path / "x"
    src.write_bytes(b"bye")
    cli.main(["put", str(src)])
    _oid, index = files.read_index(cli.family_key())
    chunk = index["files"]["x"]["chunks"][0]
    old_index = house.index
    assert cli.main(["rm", "x"]) == 0
    assert chunk not in house.objects and old_index not in house.objects
    assert cli.main(["rm", "x"]) == 2


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/../../b", "", "/"])
def test_paths_cannot_climb_out(bad):
    with pytest.raises(fv.VaultError):
        files._clean_name(bad)


# -- erasure coding -------------------------------------------------------------------

def _chunk_puts(house):
    return [p for p in house.params]


def test_large_chunks_ask_for_4_plus_2_shards_by_default(house, tmp_path, capsys):
    src = tmp_path / "video.bin"
    src.write_bytes(b"v" * (200 * 1024))
    assert cli.main(["put", str(src)]) == 0
    assert _chunk_puts(house)[0]["ec"] == "4+2"
    _oid, index = files.read_index(cli.family_key())
    assert index["files"]["video.bin"]["redundancy"] == "ec 4+2"
    assert "4+2 erasure-coded shards" in capsys.readouterr().out
    assert cli.main(["ls", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["shards"] == 6 and row["shards_wanted"] == 6 and row["recoverable"] is True
    out = tmp_path / "v.out"
    assert cli.main(["get", "video.bin", "--out", str(out)]) == 0
    assert out.read_bytes() == b"v" * (200 * 1024)


def test_too_few_devices_falls_back_to_copies_but_an_explicit_spec_fails(house, tmp_path, capsys):
    house.ec_devices = False
    src = tmp_path / "big.bin"
    src.write_bytes(b"b" * (100 * 1024))
    assert cli.main(["put", str(src)]) == 0
    assert "keeping 2 whole copies" in capsys.readouterr().err
    _oid, index = files.read_index(cli.family_key())
    assert index["files"]["big.bin"]["redundancy"] == "copies"
    assert cli.main(["put", str(src), "--name", "again.bin", "--ec", "3+3"]) != 0
    _oid, index = files.read_index(cli.family_key())
    assert "again.bin" not in index["files"]


def test_copies_flag_and_bad_specs(house, tmp_path):
    src = tmp_path / "big.bin"
    src.write_bytes(b"c" * (100 * 1024))
    assert cli.main(["put", str(src), "--copies", "--replicas", "3"]) == 0
    assert "ec" not in _chunk_puts(house)[0] and _chunk_puts(house)[0]["replicas"] == "3"
    for bad in ("4", "1+1", "4+9", "x+y"):
        with pytest.raises(SystemExit):
            cli.main(["put", str(src), "--ec", bad])
