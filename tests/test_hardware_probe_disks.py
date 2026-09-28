"""adk.hardware_probe disk detection: the storage plane's volume list."""

from __future__ import annotations

import builtins
import io
import platform

from adk import hardware_probe as hp

_MOUNTS = """\
/dev/sda1 / ext4 rw,relatime 0 0
proc /proc proc rw 0 0
tmpfs /run tmpfs rw 0 0
/dev/sdb1 /mnt/data\\040disk xfs rw 0 0
overlay /var/lib/containers/x overlay rw 0 0
//nas/share /mnt/nas cifs rw 0 0
/dev/sdc1 /mnt/empty ext4 rw 0 0
"""


def test_posix_disks_parse_and_filter(monkeypatch):
    real_open = builtins.open

    def fake_open(path, *a, **k):
        if path == "/proc/mounts":
            return io.StringIO(_MOUNTS)
        return real_open(path, *a, **k)

    sizes = {"/": (100, 40), "/mnt/data disk": (500, 10), "/mnt/nas": (900, 800),
             "/mnt/empty": (0, 0)}
    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(hp, "_disk_usage", lambda m: sizes.get(m))
    disks = hp._posix_disks()
    by = {d["mount"]: d for d in disks}
    assert set(by) == {"/", "/mnt/data disk", "/mnt/nas"}, "pseudo fs, overlay, 0-byte dropped"
    assert by["/mnt/data disk"]["fs"] == "xfs" and by["/mnt/data disk"]["free_bytes"] == 10
    assert by["/mnt/nas"]["kind"] == "network" and by["/"]["kind"] == "fixed"


def test_detect_disks_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(hp, "_windows_disks", boom)
    monkeypatch.setattr(hp, "_posix_disks", boom)
    assert hp.detect_disks() == []


def test_detect_disks_on_this_host_has_the_expected_shape():
    disks = hp.detect_disks()
    assert disks, f"no disks detected on {platform.system()}"
    for d in disks:
        assert {"mount", "device", "fs", "total_bytes", "free_bytes", "kind"} <= set(d)
        assert d["total_bytes"] > 0 and 0 <= d["free_bytes"] <= d["total_bytes"]


def test_system_info_carries_disks(monkeypatch):
    monkeypatch.setattr(hp, "detect_disks", lambda: [{"mount": "X:/"}])
    monkeypatch.setattr(hp, "_detect_gpu", lambda: ("none", "", 0))
    assert hp.detect_system().disks == [{"mount": "X:/"}]
