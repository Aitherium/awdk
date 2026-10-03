"""Lend context routes: the daemon drives a REAL relay, and a real holder joins with its token.

The guard arms must refuse (a non-loopback peer, a stranger's Origin). The live arm starts
`adk kvholder phone --via local` through the route on free ports, mints a join token, runs
`adk kvholder serve --connect` with it as a separate process, and waits for the relay to
report the holder attached. No response may carry the relay's master token.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

GOOD_ORIGIN = "https://aitherium.com"
LOOPBACK = ("127.0.0.1", 41234)

np = pytest.importorskip("numpy")  # a holder needs numpy


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _app():
    from adk.config import Config
    from adk.server import create_app

    config = Config()
    config.gateway_url = ""
    config.aither_api_key = ""
    agent = MagicMock()
    agent.name = "test"
    agent.llm = MagicMock()
    agent.llm.provider_name = "test"
    agent._identity = MagicMock()
    agent._identity.name = "test"
    agent._identity.description = "Test"
    agent._identity.skills = []
    agent._tools = MagicMock()
    agent._tools.list_tools = MagicMock(return_value=[])
    agent._safety = None
    return create_app(agent=agent, identity="test", config=config)


@pytest.fixture()
def state(tmp_path, monkeypatch):
    path = tmp_path / "relay.json"
    monkeypatch.setenv("AITHER_KVHOLDER_STATE", str(path))
    monkeypatch.delenv("AITHER_BROWSER_HANDOFF", raising=False)
    return path


@pytest.fixture()
def client(state):
    return TestClient(_app(), client=LOOPBACK)


def test_guard_refuses_a_stranger_origin(client):
    r = client.get("/kvholder/status", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_guard_refuses_a_remote_peer(state):
    c = TestClient(_app(), client=("10.0.0.7", 5555))
    r = c.post("/kvholder/join", json={}, headers={"Origin": GOOD_ORIGIN})
    assert r.status_code == 403


def test_status_without_a_relay(client):
    r = client.get("/kvholder/status", headers={"Origin": GOOD_ORIGIN})
    assert r.status_code == 200
    assert r.json()["running"] is False


def test_join_without_a_relay_is_409(client):
    r = client.post("/kvholder/join", json={"max_mb": 256}, headers={"Origin": GOOD_ORIGIN})
    assert r.status_code == 409


def test_holder_plan_container_contract():
    from adk.lend_routes import holder_plan

    p = holder_plan("wss://relay.example.com/holder", "j-abc", 2048, "tq4")
    c = p["container"]
    assert c["memory_mb"] == 2048 + 384
    assert c["egress"] == ["relay.example.com:443"]
    assert c["env"] == {
        "RELAY": "wss://relay.example.com/holder",
        "JOIN": "j-abc",
        "MAX_MB": "2048",
    }
    assert "--store tq4" in p["command"] and "j-abc" in p["command"]


def test_live_relay_join_and_attach(client, state):
    h = {"Origin": GOOD_ORIGIN}
    wp, ep = _free_port(), _free_port()
    r = client.post(
        "/kvholder/relay",
        json={"action": "start", "via": "local", "web_port": wp, "port": ep},
        headers=h,
    )
    assert r.status_code == 200, r.text
    holder = None
    try:
        master = json.loads(state.read_text(encoding="utf-8"))["token"]
        st = r.json()
        assert st["running"] and st["managed"] and st["attached"] is False
        assert st["relay"] == f"ws://127.0.0.1:{wp}/holder"

        j = client.post("/kvholder/join", json={"max_mb": 128, "ttl_s": 120}, headers=h)
        assert j.status_code == 200, j.text
        body = j.json()
        assert body["token"].startswith("j-") and body["token"] != master
        assert body["page"].endswith(f"/#t={body['token']}")
        assert body["qr"].startswith("data:image/svg+xml;base64,")
        for resp in (r, j):
            assert master not in resp.text

        holder = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "adk",
                "kvholder",
                "serve",
                "--connect",
                body["container"]["env"]["RELAY"],
                "--token",
                body["token"],
                "--max-mb",
                "128",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        end = time.time() + 30
        s = {}
        while time.time() < end:
            s = client.get("/kvholder/status", headers=h).json()
            if s.get("attached"):
                break
            time.sleep(0.3)
        assert s.get("attached"), s
        assert s["holders"][0]["max_bytes"] == 128 << 20

        # The join token is single-use: a second holder with it is refused.
        again = subprocess.run(
            [
                sys.executable,
                "-m",
                "adk",
                "kvholder",
                "serve",
                "--connect",
                body["container"]["env"]["RELAY"],
                "--token",
                body["token"],
                "--max-mb",
                "64",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        assert again.returncode != 0
    finally:
        if holder is not None:
            holder.terminate()
            holder.wait(timeout=10)
        stop = client.post("/kvholder/relay", json={"action": "stop"}, headers=h)
    assert stop.status_code == 200 and stop.json()["running"] is False
    assert not state.exists()
