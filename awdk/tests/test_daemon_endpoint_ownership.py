"""The daemon-discovery file is SHARED by every adk daemon on the box.

Both bugs these cover were real on 2026-07-29 and neither was visible: every consumer
falls back to the `127.0.0.1:9001` default, and that default happened to be correct, so
a completely broken discovery mechanism looked exactly like a working one. The point of
`daemon_endpoint.py` is that the port is NOT assumed — a silent fallback to the
assumption is the failure it exists to prevent.
"""
from __future__ import annotations

import json
import os
import socket


import pytest

from adk import daemon_endpoint


@pytest.fixture
def endpoint_file(tmp_path, monkeypatch):
    f = tmp_path / "daemon.json"
    monkeypatch.setattr(daemon_endpoint, "ENDPOINT_FILE", f)
    return f


def _listener() -> tuple[socket.socket, int]:
    """A real listening socket, so the liveness probe has something true to find.

    No accept() thread: `connect_ex` completes against the listen backlog, and an
    accept loop racing the socket's close only produces WinError 10038 noise. A test
    that prints scary-looking errors while passing teaches people to stop reading output.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    # Backlog must exceed the number of probes a test makes. With listen(1) each
    # unaccepted probe connection stays queued, so the SECOND probe fails and the test
    # reads as "the code hijacked discovery" when the code was right and the fixture was
    # under-built. A real daemon accepts its connections, so this is a test artefact only.
    s.listen(64)
    return s, s.getsockname()[1]


def test_publish_then_resolve_roundtrip(endpoint_file):
    url = daemon_endpoint.publish_daemon_url("127.0.0.1", 9001)
    assert url == "http://127.0.0.1:9001"
    assert daemon_endpoint.resolve_daemon_url() == "http://127.0.0.1:9001"
    assert json.loads(endpoint_file.read_text())["pid"] == os.getpid()


def test_wildcard_bind_is_advertised_as_loopback(endpoint_file):
    # 0.0.0.0 is not a usable client target.
    assert daemon_endpoint.publish_daemon_url("0.0.0.0", 9001) == "http://127.0.0.1:9001"


def test_clear_removes_our_own_entry(endpoint_file):
    daemon_endpoint.publish_daemon_url("127.0.0.1", 9001)
    daemon_endpoint.clear_daemon_url()
    assert not endpoint_file.exists()


def test_clear_does_NOT_remove_another_daemons_entry(endpoint_file):
    """The outage: a daemon that failed to bind deleted the LIVE daemon's entry on exit."""
    endpoint_file.write_text(
        json.dumps({"url": "http://127.0.0.1:9001", "host": "127.0.0.1",
                    "port": 9001, "pid": os.getpid() + 90000})
    )
    daemon_endpoint.clear_daemon_url()
    assert endpoint_file.exists(), "clear() deleted a foreign daemon's published endpoint"
    assert daemon_endpoint.resolve_daemon_url() == "http://127.0.0.1:9001"


def test_publish_does_NOT_hijack_a_LIVE_foreign_entry(endpoint_file):
    """A second daemon must not silently redirect every consumer at itself."""
    sock, live_port = _listener()
    try:
        endpoint_file.write_text(
            json.dumps({"url": f"http://127.0.0.1:{live_port}", "host": "127.0.0.1",
                        "port": live_port, "pid": os.getpid() + 90000})
        )
        daemon_endpoint.publish_daemon_url("127.0.0.1", 9999)
        assert daemon_endpoint.resolve_daemon_url() == f"http://127.0.0.1:{live_port}", (
            "a second daemon hijacked discovery from the running primary"
        )
    finally:
        sock.close()


def test_publish_DOES_take_over_a_DEAD_foreign_entry(endpoint_file):
    """The counter-case: a stale entry must never wedge discovery forever."""
    sock, dead_port = _listener()
    sock.close()  # nothing is listening there now
    endpoint_file.write_text(
        json.dumps({"url": f"http://127.0.0.1:{dead_port}", "host": "127.0.0.1",
                    "port": dead_port, "pid": os.getpid() + 90000})
    )
    daemon_endpoint.publish_daemon_url("127.0.0.1", 9001)
    assert daemon_endpoint.resolve_daemon_url() == "http://127.0.0.1:9001"


def test_env_override_beats_the_file(endpoint_file, monkeypatch):
    daemon_endpoint.publish_daemon_url("127.0.0.1", 9001)
    monkeypatch.setenv("ADK_DAEMON_URL", "http://127.0.0.1:9500/")
    assert daemon_endpoint.resolve_daemon_url() == "http://127.0.0.1:9500"


def test_search_tools_reports_nothing_when_the_catalogue_is_cleared():
    """`_mark_mcp_detached()` clears the catalogue; this is the contract that relies on.

    Before it did, a detached daemon kept serving 1,227 real-looking tool names from a
    stale catalogue — the model would pick one, `call_tool` would fail at invoke time,
    and it would burn its tool budget retrying names that could not possibly work.
    """
    from adk.tools_meta import search_tools

    stale = [{"name": "graph_code_search", "description": "Search CodeGraph"}]
    assert "graph_code_search" in search_tools("code search", stale, 3)
    assert json.loads(search_tools("code search", [], 3)) == {"results": [], "count": 0}


def test_resolve_ignores_a_STALE_entry_and_falls_back_to_the_default(endpoint_file):
    """A force-killed daemon never runs its shutdown hook, so its entry outlives it.

    Observed 2026-07-29: the file said :9101 (a Stop-Process'd test daemon) while the
    real daemon served on :9001, so every consumer would have been routed at a dead
    port — strictly worse than the default this file exists to replace.
    """
    sock, dead_port = _listener()
    sock.close()
    endpoint_file.write_text(
        json.dumps({"url": f"http://127.0.0.1:{dead_port}", "host": "127.0.0.1",
                    "port": dead_port, "pid": os.getpid() + 90000})
    )
    assert daemon_endpoint.resolve_daemon_url() == daemon_endpoint.DEFAULT_URL


def test_resolve_honours_a_LIVE_entry_on_a_non_default_port(endpoint_file):
    """The counter-case: liveness checking must not defeat discovery itself."""
    sock, live_port = _listener()
    try:
        endpoint_file.write_text(
            json.dumps({"url": f"http://127.0.0.1:{live_port}", "host": "127.0.0.1",
                        "port": live_port, "pid": os.getpid()})
        )
        assert daemon_endpoint.resolve_daemon_url() == f"http://127.0.0.1:{live_port}"
    finally:
        sock.close()
