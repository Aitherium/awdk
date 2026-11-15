import subprocess

import pytest

from adk.desktop.sysinternals import Sysinternals, SysinternalsError, parse_csv

SIG = (b'Path,Verified,Publisher\r\n'
       b'"c:\\windows\\notepad.exe","Signed","Microsoft Windows"\r\n')
TCP = b"TCP,wslrelay.exe,26304,LISTENING,127.0.0.1,0.0.0.0\r\n"


@pytest.fixture
def home(tmp_path):
    for t in ("sigcheck64", "tcpvcon64", "handle64", "du"):
        (tmp_path / f"{t}.exe").write_bytes(b"")
    return tmp_path


def fake(stdout=b"", code=0, raise_timeout=False, seen=None):
    def run(argv, timeout):
        if seen is not None:
            seen.append(list(argv))
        if raise_timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(argv, code, stdout, b"")
    return run


def test_sigcheck_parses_header_csv_and_passes_eula_flags(home):
    seen = []
    r = Sysinternals(home, fake(SIG, seen=seen)).sigcheck("c:/windows/notepad.exe")
    assert r.rows == [{"Path": "c:\\windows\\notepad.exe", "Verified": "Signed",
                       "Publisher": "Microsoft Windows"}]
    assert seen[0][1:3] == ["-accepteula", "-nobanner"]
    assert seen[0][0].endswith("sigcheck64.exe")


def test_ports_uses_fixed_columns_because_tcpvcon_has_no_header(home):
    r = Sysinternals(home, fake(TCP)).ports()
    assert r.rows[0]["process"] == "wslrelay.exe" and r.rows[0]["pid"] == "26304"


def test_timeout_is_reported_not_raised(home):
    r = Sysinternals(home, fake(raise_timeout=True)).who_holds("notepad")
    assert r.timed_out and r.exit_code is None and "elevation" in r.stderr


def test_falls_back_to_32bit_exe(home):
    seen = []
    Sysinternals(home, fake(b"", seen=seen)).disk_usage("c:/x")
    assert seen[0][0].endswith("du.exe")


def test_missing_tool_is_loud(home):
    with pytest.raises(SysinternalsError):
        Sysinternals(home, fake()).autoruns()


def test_parse_csv_empty():
    assert parse_csv("\r\n\r\n") == []
