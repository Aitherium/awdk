"""The relay's owner view: /swarm (this machine only), /swarm/join (owner key), richer /status."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from adk import kvholder as kv
from adk import kvholder_net as net
from adk.kvholder_page import PAGE_HTML, SWARM_HTML

np = pytest.importorskip("numpy")  # the relay merges several holders with numpy


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_KVHOLDER_STATE", str(tmp_path / "relay.json"))
    token = "t0k-" + str(time.time_ns())
    ep, wp = _free_port(), _free_port()
    r, servers = net.start_relay(token, ("127.0.0.1", ep), ("127.0.0.1", wp))
    yield r, token, ep, wp
    net.stop_relay(r, servers)


def _get(url: str, headers: dict | None = None) -> tuple[int, bytes]:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _raw_get(port: int, path: str, host: str) -> int:
    """A GET with an arbitrary Host header (a DNS-rebinding page sends its own name)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    head = s.recv(64).decode("latin-1")
    s.close()
    return int(head.split(" ")[1])


def test_pages_ship_no_external_assets():
    for page in (PAGE_HTML, SWARM_HTML):
        assert "http://" not in page and "https://" not in page
    assert "KV Holder" in PAGE_HTML and "/swarm/join" in SWARM_HTML


def test_swarm_page_is_for_this_machine_only(relay):
    _, _, _, wp = relay
    code, body = _get(f"http://127.0.0.1:{wp}/swarm")
    assert code == 200 and b"KV Swarm" in body
    assert _raw_get(wp, "/swarm", f"evil.example:{wp}") == 403  # rebinding: wrong Host
    assert _raw_get(wp, "/swarm", f"localhost:{wp}") == 200


def test_swarm_join_needs_the_owner_key_and_mints_a_single_use_link(relay):
    r, token, _, wp = relay
    url = f"http://127.0.0.1:{wp}/swarm/join?ttl=120"
    assert _get(url)[0] == 403
    assert _get(url, {"X-KV-Token": "wrong"})[0] == 403
    tok, _ = r.mint_join(60)
    assert _get(url, {"X-KV-Token": tok})[0] == 403  # a join token is not the owner key
    code, body = _get(url, {"X-KV-Token": token})
    assert code == 200
    j = json.loads(body)
    assert j["token"].startswith("j-") and j["expires"] > time.time()
    vias = {x["via"]: x for x in j["links"]}
    assert vias["usb"]["url"] == f"http://localhost:{wp}/#t={j['token']}"
    assert vias["usb"]["svg"].startswith("<svg") and "<script" not in vias["usb"]["svg"]
    assert r.admit(j["token"]) == "join" and r.admit(j["token"]) is None  # works once


def test_swarm_join_offers_the_recorded_tunnel(relay):
    _, token, _, wp = relay
    net.write_state({"web_port": wp, "public": "https://kv.example.test", "token": token})
    _, body = _get(f"http://127.0.0.1:{wp}/swarm/join", {"X-KV-Token": token})
    links = json.loads(body)["links"]
    assert links[0]["via"] == "tunnel" and links[0]["url"].startswith("https://kv.example.test/#t=")


def test_status_tells_each_holders_share_and_never_a_token(relay):
    r, token, ep, wp = relay
    for name, mb in (("first", 4), ("second", 64)):
        h = kv.KVHolder(mb << 20, device=name)
        t = threading.Thread(
            target=net.dial_holder,
            args=(f"ws://127.0.0.1:{wp}/holder", token, h),
            daemon=True,
        )
        t.start()
        end = time.time() + 10
        while time.time() < end and not any(x.device == name for x in r.holders):
            time.sleep(0.05)
    st0 = json.loads(_get(f"http://127.0.0.1:{wp}/status")[1])
    assert st0["configured"] is False and st0["used_bytes"] == 0 and len(st0["holders"]) == 2
    c = kv.KVHolderClient("127.0.0.1", ep, timeout=30)
    cfg = kv.Config.for_model(2, 1, "q8_0")
    c.configure(cfg)
    rng = np.random.default_rng(0)
    first_cap = r.holders[0].cap
    n = first_cap + 100  # spills into the second holder's range
    for layer in range(cfg.n_layer):
        k = rng.standard_normal((n, 1, kv.HD)).astype(np.float32)
        c.append(layer, 0, k, k)
    q = rng.standard_normal((1, kv.NR, kv.HD)).astype(np.float32)
    for _ in range(3):
        c.attn(0, q, kv.HD**-0.5, n_tok=1)
    c.close()
    raw = _get(f"http://127.0.0.1:{wp}/status")[1]
    for secret in [token, *r.sessions, *r.joins]:
        assert secret.encode() not in raw
    st = json.loads(raw)
    a, b = st["holders"]
    assert (a["off"], a["held"]) == (0, first_cap) and (b["off"], b["held"]) == (first_cap, 100)
    assert a["calls"] == b["calls"] == 3 and a["mean_ms"] > 0 and b["rtt_ms"] > 0
    assert a["used_bytes"] > b["used_bytes"] > 0
    assert st["used_bytes"] == a["used_bytes"] + b["used_bytes"]
    assert st["lent_bytes"] == (4 + 64) << 20 and st["attn_calls"] == 3
    assert st["shape"]["n_layer"] == 2 and st["held_per_layer"] == [n, n]
    assert st["held"] == n and st["attached"] is True and st["device"] == "first"  # old keys
