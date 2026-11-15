"""``get_bridge_url`` resolves the desk's PICKED port, not only the constant.

awdesk's launcher persists the port it bound to ``%APPDATA%\\Desk\\bridge-port.txt``
(a pinned 47931 can fall inside a Hyper-V/WSL reserved range after a reboot). The
``adk desk`` client is a second consumer of that door; on 2026-11-07 the bridge was
on 49031 while this module still handed out 47931.
"""

from __future__ import annotations

import pytest

from adk import desk_bridge as db

PERSISTED_PORT = 49031


@pytest.fixture
def appdata(tmp_path, monkeypatch):
    (tmp_path / "Desk").mkdir()
    (tmp_path / "Desk" / "bridge-port.txt").write_text(f"{PERSISTED_PORT}\n", encoding="ascii")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("AWDESK_URL", raising=False)
    monkeypatch.delenv("AITHERSHELL_AWDESK_URL", raising=False)
    return tmp_path


def test_url_uses_the_persisted_port(appdata):
    assert str(appdata) in str(db.bridge_port_file())
    assert db.bridge_port() == PERSISTED_PORT
    assert db.get_bridge_url(probe=False) == f"http://127.0.0.1:{PERSISTED_PORT}"


def test_env_url_wins_over_the_file(appdata, monkeypatch):
    monkeypatch.setenv("AWDESK_URL", "http://localhost:47931/")
    assert db.get_bridge_url(probe=False) == "http://localhost:47931"


def test_unreadable_or_absent_file_uses_the_constant(appdata):
    (appdata / "Desk" / "bridge-port.txt").write_text("0", encoding="ascii")
    assert db.get_bridge_url(probe=False) == f"http://127.0.0.1:{db.DEFAULT_BRIDGE_PORT}"


def test_awsh_events_url_is_stripped_to_the_base(appdata, monkeypatch):
    """awsh hands AITHERSHELL_AWDESK_URL over as the EVENTS url (".../events").

    Measured in awdesk-bridge.ts (strips `/events`) and mcp_awdesk.py
    (`.removesuffix("/events")`). A twin that kept the suffix called
    `/events/fleet/status`.
    """
    monkeypatch.setenv("AITHERSHELL_AWDESK_URL", "http://127.0.0.1:49031/events")
    assert db.get_bridge_url(probe=False) == "http://127.0.0.1:49031"


def _only(*alive_ports):
    """A /health answer only for these ports."""
    def answers(url, timeout=0.3):
        return any(url == f"http://127.0.0.1:{p}" for p in alive_ports)
    return answers


def test_liveness_beats_a_stale_file(appdata, monkeypatch):
    """Measured 2026-11-07 10:45 PT: the file said 49031, the desk answered on 47931.

    A desk relaunched without the Startup launcher never rewrites the file, so the
    file alone is a RECORD. The first candidate that answers /health wins.
    """
    db._bridge_cache = None
    monkeypatch.setattr(db, "_answers", _only(db.DEFAULT_BRIDGE_PORT))
    assert db.get_bridge_url() == f"http://127.0.0.1:{db.DEFAULT_BRIDGE_PORT}"


def test_the_file_port_wins_when_it_is_the_one_answering(appdata, monkeypatch):
    db._bridge_cache = None
    monkeypatch.setattr(db, "_answers", _only(PERSISTED_PORT))
    assert db.get_bridge_url() == f"http://127.0.0.1:{PERSISTED_PORT}"


def test_nothing_answering_still_answers_the_file_port(appdata, monkeypatch):
    db._bridge_cache = None
    monkeypatch.setattr(db, "_answers", lambda url, timeout=0.3: False)
    assert db.get_bridge_url() == f"http://127.0.0.1:{PERSISTED_PORT}"


def test_the_probe_is_cached_for_thirty_seconds(appdata, monkeypatch):
    db._bridge_cache = None
    calls = []

    def answers(url, timeout=0.3):
        calls.append(url)
        return url.endswith(f":{PERSISTED_PORT}")

    monkeypatch.setattr(db, "_answers", answers)
    assert db.get_bridge_url() == f"http://127.0.0.1:{PERSISTED_PORT}"
    assert db.get_bridge_url() == f"http://127.0.0.1:{PERSISTED_PORT}"
    assert len(calls) == 1, "a second read inside 30 s must not re-probe"
