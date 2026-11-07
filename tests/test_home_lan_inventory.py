"""LAN inventory: neighbor-table parsers on captured output for Windows, Linux and
macOS, SSDP reply parsing, the new-device diff, and `adk home devices scan` end to end
with the OS and network reads stubbed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adk.home import cli
from adk.home import lan_inventory as li

WINDOWS_ARP = """
Interface: 192.168.1.10 --- 0xb
  Internet Address      Physical Address      Type
  192.168.1.1           a4-2b-b0-12-34-56     dynamic
  192.168.1.23          3c-22-fb-0a-0b-0c     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
  239.255.255.250       01-00-5e-7f-ff-fa     static

Interface: 172.20.16.1 --- 0x2c
  Internet Address      Physical Address      Type
  172.20.31.255         ff-ff-ff-ff-ff-ff     static
"""

# A German Windows: headers translated, rows identical.
WINDOWS_ARP_DE = """
Schnittstelle: 192.168.1.10 --- 0xb
  Internetadresse       Physische Adresse     Typ
  192.168.1.1           a4-2b-b0-12-34-56     dynamisch
"""

LINUX_IP_NEIGH = """192.168.1.1 dev eth0 lladdr a4:2b:b0:12:34:56 REACHABLE
192.168.1.23 dev eth0 lladdr 3c:22:fb:0a:0b:0c STALE
192.168.1.50 dev eth0  FAILED
192.168.1.51 dev eth0  INCOMPLETE
192.168.1.77 dev wlan0 lladdr b8:27:eb:11:22:33 router DELAY
fe80::1 dev eth0 lladdr a4:2b:b0:12:34:56 router STALE
"""

PROC_NET_ARP = """IP address       HW type     Flags       HW address            Mask     Device
192.168.1.1      0x1         0x2         a4:2b:b0:12:34:56     *        eth0
192.168.1.77     0x1         0x0         00:00:00:00:00:00     *        eth0
192.168.1.23     0x1         0x6         3c:22:fb:0a:0b:0c     *        eth0
"""

MACOS_ARP = """? (192.168.1.1) at a4:2b:b0:12:34:56 on en0 ifscope [ethernet]
? (192.168.1.9) at (incomplete) on en0 ifscope [ethernet]
? (192.168.1.23) at 3c:22:fb:a:b:c on en0 ifscope [ethernet]
? (192.168.1.255) at ff:ff:ff:ff:ff:ff on en0 ifscope [ethernet]
? (224.0.0.251) at 1:0:5e:0:0:fb on en0 ifscope permanent [ethernet]
"""

MACOS_ARP_NAMES = "router.lan (192.168.1.1) at a4:2b:b0:12:34:56 on en0 ifscope [ethernet]\n"

ROKU_REPLY = (
    b"HTTP/1.1 200 OK\r\n"
    b"Cache-Control: max-age=3600\r\n"
    b"ST: roku:ecp\r\n"
    b"USN: uuid:roku:ecp:YN00AB123456\r\n"
    b"Ext: \r\n"
    b"Server: Roku/12.0.0 UPnP/1.0 Roku/12.0.0\r\n"
    b"LOCATION: http://192.168.1.40:8060/\r\n"
    b"\r\n"
)

GW = {"ip": "192.168.1.1", "mac": "a4:2b:b0:12:34:56", "hostname": None}
MAC23 = {"ip": "192.168.1.23", "mac": "3c:22:fb:0a:0b:0c", "hostname": None}


# ── parsers ──────────────────────────────────────────────────────────────────

def test_windows_arp_keeps_unicast_only() -> None:
    assert li.parse_windows_arp(WINDOWS_ARP) == [GW, MAC23]


def test_windows_arp_any_language() -> None:
    assert li.parse_windows_arp(WINDOWS_ARP_DE) == [GW]


def test_linux_ip_neigh_skips_failed_incomplete_and_ipv6() -> None:
    assert li.parse_linux_ip_neigh(LINUX_IP_NEIGH) == [
        GW, MAC23, {"ip": "192.168.1.77", "mac": "b8:27:eb:11:22:33", "hostname": None}]


def test_proc_net_arp_skips_incomplete_flag() -> None:
    assert li.parse_proc_net_arp(PROC_NET_ARP) == [GW, MAC23]


def test_macos_arp_pads_octets_and_skips_incomplete() -> None:
    assert li.parse_macos_arp(MACOS_ARP) == [GW, MAC23]


def test_macos_arp_keeps_hostname_when_not_numeric() -> None:
    assert li.parse_macos_arp(MACOS_ARP_NAMES) == [{**GW, "hostname": "router.lan"}]


@pytest.mark.parametrize("raw,want", [
    ("A4-2B-B0-12-34-56", "a4:2b:b0:12:34:56"),
    ("0:11:32:a:b:c", "00:11:32:0a:0b:0c"),
    ("ff:ff:ff:ff:ff:ff", None),
    ("00:00:00:00:00:00", None),
    ("01:00:5e:00:00:16", None),
    ("33:33:00:00:00:01", None),
    ("(incomplete)", None),
])
def test_normalize_mac(raw: str, want: object) -> None:
    assert li.normalize_mac(raw) == want


def test_vendor_is_null_without_a_table() -> None:
    assert li.vendor_for_mac("a4:2b:b0:12:34:56") is None


# ── SSDP ─────────────────────────────────────────────────────────────────────

def test_ssdp_reply_parsed_case_insensitive() -> None:
    hdr = li.parse_ssdp_response(ROKU_REPLY)
    assert hdr is not None
    assert hdr["st"] == "roku:ecp"
    assert hdr["location"] == "http://192.168.1.40:8060/"
    assert hdr["server"].startswith("Roku/12.0.0")
    assert hdr["ext"] == ""


def test_ssdp_rejects_non_200() -> None:
    assert li.parse_ssdp_response(b"NOTIFY * HTTP/1.1\r\nNT: upnp:rootdevice\r\n\r\n") is None
    assert li.parse_ssdp_response("HTTP/1.1 404 Not Found\r\n\r\n") is None
    assert li.parse_ssdp_response(b"") is None


def test_msearch_is_a_valid_request() -> None:
    assert li.SSDP_MSEARCH.startswith("M-SEARCH * HTTP/1.1\r\n")
    assert "HOST: 239.255.255.250:1900" in li.SSDP_MSEARCH
    assert li.SSDP_MSEARCH.endswith("\r\n\r\n")


def test_combine_joins_ssdp_by_ip_and_keeps_ssdp_only() -> None:
    ssdp = [{"ip": "192.168.1.23", "server": "Linux UPnP/1.0", "location": "http://x/",
             "st": ["upnp:rootdevice"]},
            {"ip": "192.168.1.40", "server": "Roku", "location": None, "st": ["roku:ecp"]}]
    rows = li.combine([GW, MAC23], ssdp)
    assert [r["ip"] for r in rows] == ["192.168.1.1", "192.168.1.23", "192.168.1.40"]
    assert "ssdp" not in rows[0]
    assert rows[1]["ssdp"]["server"] == "Linux UPnP/1.0"
    assert rows[2]["mac"] is None and rows[2]["ssdp"]["st"] == ["roku:ecp"]
    assert li.device_key(rows[2]) == "ip:192.168.1.40"


# ── diff ─────────────────────────────────────────────────────────────────────

def test_merge_flags_only_unseen_devices() -> None:
    known, seen = li.merge_inventory({}, [GW], "t1")
    assert seen[0]["new"] is True and seen[0]["first_seen"] == "t1"
    known2, seen2 = li.merge_inventory(known, [{**GW, "ip": "192.168.1.2"}, MAC23], "t2")
    by_mac = {r["mac"]: r for r in seen2}
    assert by_mac[GW["mac"]]["new"] is False
    assert by_mac[GW["mac"]]["first_seen"] == "t1"
    assert by_mac[GW["mac"]]["last_seen"] == "t2"
    assert by_mac[GW["mac"]]["ip"] == "192.168.1.2"  # DHCP moved it: same device
    assert by_mac[MAC23["mac"]]["new"] is True


def test_merge_keeps_absent_devices_with_old_last_seen() -> None:
    known, _ = li.merge_inventory({}, [GW, MAC23], "t1")
    known2, seen2 = li.merge_inventory(known, [GW], "t2")
    assert len(seen2) == 1
    assert known2[MAC23["mac"]]["last_seen"] == "t1"
    assert known2[MAC23["mac"]]["new"] is False


def test_merge_keeps_hostname_learned_earlier() -> None:
    known, _ = li.merge_inventory({}, [{**GW, "hostname": "router.lan"}], "t1")
    _, seen = li.merge_inventory(known, [GW], "t2")
    assert seen[0]["hostname"] == "router.lan"


def test_state_roundtrip_and_corrupt_refused(tmp_path: Path) -> None:
    p = tmp_path / "inv.json"
    assert li.load_state(p) == {}
    known, _ = li.merge_inventory({}, [GW], "t1")
    li.save_state(p, known, "t1")
    assert li.load_state(p) == known
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        li.load_state(p)


def test_read_neighbor_table_raises_when_nothing_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(li, "_run", lambda cmd: None)
    with pytest.raises(RuntimeError):
        li.read_neighbor_table("win32")


def test_read_neighbor_table_dispatches_per_os(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    win_arp = li.windows_arp_path()
    outputs = {f"{win_arp} -a": WINDOWS_ARP, "/usr/sbin/arp -an": MACOS_ARP,
               "/usr/sbin/ip -4 neigh show": LINUX_IP_NEIGH}
    ran: list = []

    def run(cmd: list) -> object:
        ran.append(cmd[0])
        return outputs.get(" ".join(cmd))

    monkeypatch.setattr(li, "_run", run)
    monkeypatch.setattr(li, "_existing", lambda paths: list(paths))
    assert li.read_neighbor_table("win32") == [GW, MAC23]
    assert li.read_neighbor_table("darwin") == [GW, MAC23]
    assert len(li.read_neighbor_table("linux")) == 3
    # absolute paths only: never a PATH lookup for arp / ip
    assert win_arp.endswith("System32" + ("\\" if "\\" in win_arp else "/") + "ARP.EXE")
    assert all(c in (win_arp, "/usr/sbin/arp", "/usr/sbin/ip") for c in ran)


def test_linux_falls_back_to_sbin_ip_then_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list = []
    monkeypatch.setattr(li, "_run", lambda cmd: ran.append(cmd[0]) or (
        LINUX_IP_NEIGH if cmd[0] == "/sbin/ip" else None))
    monkeypatch.setattr(li, "_existing", lambda paths: list(paths))
    assert len(li.read_neighbor_table("linux")) == 3
    assert ran == ["/usr/sbin/ip", "/sbin/ip"]


# ── hardening: what the LAN can send is bounded ─────────────────────────────

def test_ssdp_merge_caps_ips_and_st_and_drops_public_addresses() -> None:
    found: dict = {}
    for i in range(li.SSDP_MAX_IPS + 50):
        li._ssdp_merge(found, f"10.0.{i // 250}.{i % 250 + 1}", {"st": "a"})
    assert len(found) == li.SSDP_MAX_IPS
    one: dict = {}
    for i in range(40):
        li._ssdp_merge(one, "192.168.1.40", {"st": f"st-{i}"})
    assert len(one["192.168.1.40"]["st"]) == li.SSDP_ST_MAX
    li._ssdp_merge(one, "8.8.8.8", {"server": "evil"})
    assert "8.8.8.8" not in one


def test_ssdp_headers_are_truncated_and_stripped_of_control_chars() -> None:
    reply = (b"HTTP/1.1 200 OK\r\nSERVER: TV\x1b[31m\xe2\x80\xaeevil\r\nST: " + b"x" * 5000
             + b"\r\n\r\n")
    hdr = li.parse_ssdp_response(reply)
    assert hdr is not None and hdr["server"] == "TV[31mevil"
    assert len(hdr["st"]) == li.SSDP_HEADER_MAX


def test_is_local_ip() -> None:
    for ip in ("10.1.2.3", "172.16.0.1", "172.31.255.1", "192.168.1.1", "169.254.1.1",
               "fe80::1", "fd00::1"):
        assert li.is_local_ip(ip), ip
    for ip in ("8.8.8.8", "172.32.0.1", "100.64.0.1", "2001:db8::1", "nope"):
        assert not li.is_local_ip(ip), ip


def test_combine_drops_ssdp_only_public_addresses() -> None:
    rows = li.combine([GW], [{"ip": "203.0.113.9", "server": "x", "location": None,
                              "st": []}])
    assert [r["ip"] for r in rows] == [GW["ip"]]


def test_resolve_hostnames_keeps_its_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _time

    def slow(ip: str) -> tuple:
        if ip.endswith(".2"):
            _time.sleep(3)
        return (f"host\x07-{ip.split('.')[-1]}", [], [ip])

    monkeypatch.setattr(li.socket, "gethostbyaddr", slow)
    t0 = _time.monotonic()
    got = li.resolve_hostnames(["192.168.1.1", "192.168.1.2"], timeout=0.5)
    assert _time.monotonic() - t0 < 1.5
    assert got == {"192.168.1.1": "host-1"}


def test_inventory_is_capped_evicting_ssdp_only_rows_first() -> None:
    prev = {f"ip:10.0.0.{i}": {"ip": f"10.0.0.{i}", "mac": None,
                               "last_seen": f"2026-01-01T00:00:{i:02d}"} for i in range(1, 6)}
    prev.update({f"aa:bb:cc:00:00:{i:02x}": {"ip": f"192.168.1.{i}",
                                             "mac": f"aa:bb:cc:00:00:{i:02x}",
                                             "last_seen": "2025-01-01"} for i in range(1, 6)})
    known, seen = li.merge_inventory(prev, [GW], "2026-11-06")
    assert len(known) == 11  # under the cap: nothing evicted
    known = {**prev}
    li._cap_inventory(known, {}, limit=7)
    assert len(known) == 7
    # the 4 evicted are SSDP-only rows, oldest first; every MAC row survives
    assert sum(1 for v in known.values() if v["mac"]) == 5
    assert "ip:10.0.0.5" in known and "ip:10.0.0.1" not in known


def test_merge_inventory_never_exceeds_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(li, "MAX_INVENTORY_ROWS", 20)
    rows = [{"ip": f"10.0.{i // 200}.{i % 200 + 1}", "mac": None, "hostname": None}
            for i in range(100)]
    known, _seen = li.merge_inventory({}, rows, "t")
    assert len(known) == 20


def test_state_file_is_owner_only_and_leaves_no_tmp(tmp_path: Path) -> None:
    import os
    import sys

    p = tmp_path / li.STATE_NAME
    li.save_state(p, {"a": {"ip": "192.168.1.1"}}, "t")
    li.save_state(p, {"a": {"ip": "192.168.1.2"}}, "t")
    assert li.load_state(p)["a"]["ip"] == "192.168.1.2"
    assert [x.name for x in tmp_path.iterdir()] == [li.STATE_NAME]
    if sys.platform != "win32":
        assert (os.stat(p).st_mode & 0o777) == 0o600


# ── CLI end to end ───────────────────────────────────────────────────────────

def test_cli_second_scan_flags_new_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AITHER_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(li, "_now", lambda: "2026-11-06T00:00:00+00:00")
    table = [[GW]]
    monkeypatch.setattr(li, "read_neighbor_table", lambda platform=None: list(table[0]))
    monkeypatch.setattr(li, "ssdp_discover", lambda timeout=2.0: [])
    monkeypatch.setattr(li, "state_path", lambda root=None: tmp_path / li.STATE_NAME)

    assert cli.main(["devices", "scan", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["first_scan"] is True and first["new"] == []

    table[0] = [GW, MAC23]
    assert cli.main(["devices", "scan", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert [d["mac"] for d in second["new"]] == [MAC23["mac"]]
    assert set(second["devices"][0]) >= {"ip", "mac", "hostname", "vendor",
                                          "first_seen", "last_seen", "new"}

    assert cli.main(["devices", "scan"]) == 0
    assert "0 new" in capsys.readouterr().out


def test_cli_exit_2_when_no_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(platform: object = None) -> list:
        raise RuntimeError("no table")

    monkeypatch.setattr(li, "read_neighbor_table", boom)
    monkeypatch.setattr(li, "state_path", lambda root=None: tmp_path / li.STATE_NAME)
    assert cli.main(["devices", "scan", "--no-ssdp"]) == 2
