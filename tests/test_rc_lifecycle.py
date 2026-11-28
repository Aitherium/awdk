"""Tests for M2.2 F10: NodeLink ``hello`` frame, path hardening, ``adk rc`` lifecycle, and autolink."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from adk import autolink
from adk import node_link as NL
from adk import rc as RC


# ══════════════════════════════════════════════════════════════════════════
# NodeLink hello frame & loopback WS connect/reconnect
# ══════════════════════════════════════════════════════════════════════════


def test_hello_frame_shape_and_never_includes_owner(tmp_path, monkeypatch):
    state_file = tmp_path / "rc-state.json"
    state_file.write_text(
        json.dumps({"instance_id": "calm-otter-12ab", "rc_mode": "daemon"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AITHER_RC_STATE_FILE", str(state_file))
    monkeypatch.delenv("AITHER_RC_INSTANCE_ID", raising=False)
    monkeypatch.delenv("AITHER_RC_MODE", raising=False)

    link = NL.NodeLink(
        tunnel_url="http://127.0.0.1:8310",
        node_id="node-1",
        token="tok",
        hostname="dev-box",
        harnesses=["claude", "awdk"],
    )
    frame = link.hello_frame()
    assert frame == {
        "t": "hello",
        "instance_id": "calm-otter-12ab",
        "hostname": "dev-box",
        "harnesses": ["claude", "awdk"],
        "rc_mode": "daemon",
        "runtime_tier": "standard",
        "zero_libc": False,
    }
    assert "owner" not in frame


def test_hello_frame_kwargs_and_env_override_state_file(tmp_path, monkeypatch):
    state_file = tmp_path / "rc-state.json"
    state_file.write_text(
        json.dumps({"instance_id": "from-state", "rc_mode": "cli"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AITHER_RC_STATE_FILE", str(state_file))
    monkeypatch.setenv("AITHER_RC_INSTANCE_ID", "from-env")
    monkeypatch.setenv("AITHER_RC_MODE", "antigravity")

    link_env = NL.NodeLink(tunnel_url="http://127.0.0.1:8310", node_id="n1", token="tok")
    assert link_env.hello_frame()["instance_id"] == "from-env"
    assert link_env.hello_frame()["rc_mode"] == "antigravity"

    link_kw = NL.NodeLink(
        tunnel_url="http://127.0.0.1:8310",
        node_id="n1",
        token="tok",
        instance_id="from-kw",
        rc_mode="invalid-mode",
        harnesses=["awdk"],
    )
    frame = link_kw.hello_frame()
    assert frame["instance_id"] == "from-kw"
    assert frame["rc_mode"] == "cli"
    assert "owner" not in frame


def test_nodelink_sends_hello_on_attach_and_reconnect_over_loopback():
    websockets = pytest.importorskip("websockets")

    async def _run():
        seen_paths = []
        seen_hellos = []
        conn_count = 0

        async def _handler(ws):
            nonlocal conn_count
            conn_count += 1
            req_path = getattr(getattr(ws, "request", None), "path", None) or getattr(
                ws, "path", ""
            )
            seen_paths.append(req_path)
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            seen_hellos.append(json.loads(raw))
            await ws.close()

        async with websockets.serve(_handler, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            link = NL.NodeLink(
                tunnel_url=f"http://127.0.0.1:{port}",
                node_id="node-loop",
                token="tok-secret",
                instance_id="swift-falcon-0001",
                hostname="loop-host",
                harnesses=["claude", "awdk"],
                rc_mode="cli",
            )
            # First connect
            await link._serve_once()
            # Reconnect
            await link._serve_once()

        assert conn_count == 2
        assert seen_paths == [
            "/tunnel/nodes/node-loop/attach",
            "/tunnel/nodes/node-loop/attach",
        ]
        assert len(seen_hellos) == 2
        for frame in seen_hellos:
            assert frame == {
                "t": "hello",
                "instance_id": "swift-falcon-0001",
                "hostname": "loop-host",
                "harnesses": ["claude", "awdk"],
                "rc_mode": "cli",
                "runtime_tier": "standard",
                "zero_libc": False,
            }
            assert "owner" not in frame

    asyncio.run(_run())


# ══════════════════════════════════════════════════════════════════════════
# NodeLink path hardening & credential stripping
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "path,method,want",
    [
        ("harness/sessions/s1/transcript", "GET", ("harness", "sessions/s1/transcript")),
        ("harness/sessions/s1/interrupt", "POST", ("harness", "sessions/s1/interrupt")),
        ("harness/rc/status", "GET", ("harness", "rc/status")),
        ("harness/rc/status", "HEAD", ("harness", "rc/status")),
        ("harness/rc/status?x=1", "GET", ("harness", "rc/status")),
        ("v1/models?x=1", "GET", ("inference", "v1/models")),
        ("harness/sessions", "", ("harness", "sessions")),
        ("harness/sessions", "GET", ("harness", "sessions")),
    ],
)
def test_path_target_allows_expected_rc_paths(path, method, want):
    assert NL.path_target(path, method=method) == want


@pytest.mark.parametrize(
    "path,method",
    [
        ("harness/sessions/s1/focus", "POST"),
        ("harness/sessions/s1/focus", "GET"),
        ("harness/sessions", "POST"),
        ("harness/rc/off", "POST"),
        ("harness/rc/status/extra", "GET"),
        ("harness/rc/status", "POST"),
        ("harness/sessions/%2e%2e/secrets", "GET"),
        ("harness/sessions/..%2fsecrets", "GET"),
        ("harness/sessions/%252e%252e/secrets", "GET"),
        ("harness/sessions/%25252e%25252e/secrets", "GET"),
        ("harness/sessions\\..\\secrets", "GET"),
        ("harness/sessions/%5c../secrets", "GET"),
        ("harness/sessions\x00/secrets", "GET"),
        ("harness/sessions%00/secrets", "GET"),
        # Single-dot (raw and multi-encoded) & empty segments (//, %2f) must be blocked
        ("harness/sessions/.", "POST"),
        ("harness/sessions/%2e", "POST"),
        ("harness/sessions/%252e", "POST"),
        ("harness/sessions/%25252e", "POST"),
        ("harness/sessions/./", "POST"),
        ("harness/sessions/%2e/", "POST"),
        ("harness/sessions/s1/./focus", "POST"),
        ("harness/sessions/s1/%2e/focus", "POST"),
        ("harness/sessions/s1/%252e/focus", "POST"),
        ("harness/sessions//s1/focus", "POST"),
        ("harness/sessions/s1//focus", "POST"),
        ("harness/sessions/%2f%2f", "POST"),
        ("harness/sessions/foo%2f", "GET"),
        ("harness/rc/status/.", "GET"),
        ("harness/rc/status//", "GET"),
        ("v1/chat/completions/.", "GET"),
        ("v1/chat/completions//", "GET"),
        # URI delimiter smuggling (#, %23, %3f, multi-encoded) must be blocked
        ("harness/sessions/#x", "POST"),
        ("harness/sessions/%23x", "POST"),
        ("harness/sessions/%3fx", "POST"),
        ("harness/sessions/s1/focus#x", "POST"),
        ("harness/sessions/s1/focus%23x", "POST"),
        ("harness/sessions/s1/focus%3fx", "POST"),
        ("harness/sessions/s1/focus%2523x", "POST"),
        ("harness/sessions/s1/focus%253fx", "POST"),
        ("harness/rc/status#bar", "GET"),
        ("harness/rc/status%3fa=1", "GET"),
        ("harness/rc/status?x=1#frag", "GET"),
        ("harness/rc/status#frag?x=1", "GET"),
        ("v1/models#bar", "GET"),
        ("v1/models%3fa=1", "GET"),
    ],
)
def test_path_target_blocks_focus_post_sessions_and_encoded_traversal(path, method):
    assert NL.path_target(path, method=method) == ("", "")


def test_handle_request_returns_403_for_refused_verbs_and_traversal():
    class _FakeWS:
        def __init__(self):
            self.sent = []

        async def send(self, text):
            self.sent.append(json.loads(text))

    link = NL.NodeLink(
        tunnel_url="http://127.0.0.1:8310",
        node_id="n1",
        token="tok",
        inference_url="http://127.0.0.1:8080",
        harness_url="http://127.0.0.1:8362",
        harness_token="local-tok",
    )
    ws = _FakeWS()
    bad_frames = [
        {"path": "harness/sessions/s1/focus", "method": "POST"},
        {"path": "harness/sessions", "method": "POST"},
        {"path": "harness/sessions/.", "method": "POST"},
        {"path": "harness/sessions/%2e", "method": "POST"},
        {"path": "harness/sessions/%252e", "method": "POST"},
        {"path": "harness/sessions/%25252e", "method": "POST"},
        {"path": "harness/sessions/./", "method": "POST"},
        {"path": "harness/sessions/%2e/", "method": "POST"},
        {"path": "harness/sessions/s1/./focus", "method": "POST"},
        {"path": "harness/sessions/s1/%2e/focus", "method": "POST"},
        {"path": "harness/sessions//s1/focus", "method": "POST"},
        {"path": "harness/sessions/s1//focus", "method": "POST"},
        {"path": "harness/rc/status/.", "method": "GET"},
        {"path": "harness/rc/status//", "method": "GET"},
        {"path": "v1/chat/completions/.", "method": "GET"},
        {"path": "harness/sessions/%2e%2e/secret", "method": "GET"},
        {"path": "harness/sessions/..\\secret", "method": "GET"},
        # URI delimiter smuggling (#, %23, %3f, multi-encoded, query fragment)
        {"path": "harness/sessions/#x", "method": "POST"},
        {"path": "harness/sessions/%23x", "method": "POST"},
        {"path": "harness/sessions/%3fx", "method": "POST"},
        {"path": "harness/sessions/s1/focus#x", "method": "POST"},
        {"path": "harness/sessions/s1/focus%23x", "method": "POST"},
        {"path": "harness/sessions/s1/focus%3fx", "method": "POST"},
        {"path": "harness/sessions/s1/focus%2523x", "method": "POST"},
        {"path": "harness/sessions/s1/focus%253fx", "method": "POST"},
        {"path": "harness/rc/status#bar", "method": "GET"},
        {"path": "harness/rc/status%3fa=1", "method": "GET"},
        {"path": "v1/models#bar", "method": "GET"},
        {"path": "v1/models%3fa=1", "method": "GET"},
        {"path": "harness/sessions/s1/transcript", "method": "GET", "query": "a=1#frag"},
    ]
    for idx, frame in enumerate(bad_frames):
        asyncio.run(link._handle_request(ws, f"r{idx}", frame))
        assert ws.sent[-1] == {
            "t": "err",
            "id": f"r{idx}",
            "status": 403,
            "detail": "path not permitted on this link",
        }


def test_handle_request_strips_caller_creds_and_stamps_owner_from_frame(monkeypatch):
    captured = {}

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_raw(self):
            yield b'{"ok":true}'

    class _FakeClient:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None, content=None):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["content"] = content
            return _FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    class _FakeWS:
        def __init__(self):
            self.sent = []

        async def send(self, text):
            self.sent.append(json.loads(text))

    link = NL.NodeLink(
        tunnel_url="http://127.0.0.1:8310",
        node_id="n1",
        token="tunnel-bearer",
        harness_url="http://127.0.0.1:8362",
        harness_token="local-harness-bearer",
    )
    ws = _FakeWS()
    asyncio.run(
        link._handle_request(
            ws,
            "req-1",
            {
                "path": "harness/sessions/s1/interrupt",
                "method": "POST",
                "actor": "owner",
                "headers": {
                    "Authorization": "Bearer attacker",
                    "Cookie": "sid=evil",
                    "X-Internal-Token": "forged",
                    "X-Aither-Link-Actor": "spoofed",
                    "Content-Type": "application/json",
                },
            },
        )
    )
    hdrs = {k.lower(): v for k, v in captured["headers"].items()}
    assert hdrs["authorization"] == "Bearer local-harness-bearer"
    assert hdrs[NL.LINK_ACTOR_HEADER] == "owner"
    assert "cookie" not in hdrs
    assert "x-internal-token" not in hdrs
    assert captured["url"] == "http://127.0.0.1:8362/sessions/s1/interrupt"
    assert ws.sent[0]["status"] == 200
    assert ws.sent[-1] == {"t": "end", "id": "req-1"}


# ══════════════════════════════════════════════════════════════════════════
# Slug generation, validation, rc-state.json & adk rc CLI lifecycle
# ══════════════════════════════════════════════════════════════════════════


def test_slug_generation_and_validation():
    slug = RC.generate_instance_slug()
    assert RC.validate_instance_slug(slug) == slug
    assert len(slug.split("-")) == 3

    assert RC.validate_instance_slug(" My-Box-1 ") == "my-box-1"
    for bad in ("", "   ", "../evil", "a/b", "a\\b", "-lead", "trail-", "bad name", "a" * 65):
        with pytest.raises(ValueError):
            RC.validate_instance_slug(bad)


def test_rc_state_persistence_uses_aither_home(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_RC_STATE_FILE", raising=False)
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))

    assert RC.rc_state_path() == tmp_path / "rc-state.json"
    assert RC.load_rc_state() == {}

    payload = {
        "instance_id": "swift-falcon-1234",
        "node_id": "node-abc",
        "rc_url": "https://tunnel.aitherium.com/r/swift-falcon-1234",
        "app_url": "https://app.aitherium.com/code/node-abc",
        "pid": os.getpid(),
        "started_at": "2026-11-27T11:40:00+00:00",
        "harness_url": "http://127.0.0.1:8362",
        "rc_mode": "cli",
    }
    saved = RC.save_rc_state(payload)
    assert saved == tmp_path / "rc-state.json"
    assert RC.load_rc_state() == payload
    assert RC.clear_rc_state() is True
    assert RC.clear_rc_state() is False
    assert RC.load_rc_state() == {}


def test_main_start_name_once_persists_state_and_prints_canonical_urls(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.delenv("AITHER_RC_STATE_FILE", raising=False)
    monkeypatch.delenv("AITHER_TUNNEL_URL", raising=False)
    monkeypatch.delenv("AITHER_GATEWAY_URL", raising=False)
    monkeypatch.delenv("AITHER_RC_BASE_URL", raising=False)

    monkeypatch.setattr(RC, "_signed_in", lambda: True)
    monkeypatch.setattr(RC, "probe_harness", lambda *_a, **_k: (True, "1 session(s)"))

    from adk import fleet_enroll
    from adk.harnesses import daemon as D

    monkeypatch.setattr(D, "mint_scoped_token", lambda *_a, **_k: "scoped-link-tok")

    fake_link = NL.NodeLink(
        tunnel_url="https://tunnel.aitherium.com",
        node_id="node-xyz",
        token="dev-tok",
    )

    async def _fake_enroll_on_boot(**_kw):
        # NodeLink created during enroll_on_boot sees pre-written rc-state.json
        assert fake_link.hello_frame()["instance_id"] == "dev-laptop"
        return {"enrolled": True, "node_id": "node-xyz"}

    monkeypatch.setattr(fleet_enroll, "enroll_on_boot", _fake_enroll_on_boot)
    monkeypatch.setattr(fleet_enroll, "active_node_link", lambda: fake_link)

    code = RC.main(["--name", "dev-laptop", "--once"])
    out = capsys.readouterr().out
    assert code == 0
    assert "https://a30.link/r/dev-laptop" in out
    assert "https://app.aitherium.com/code/node-xyz" in out
    assert "api.aitherium.com/code" not in out

    state = RC.load_rc_state()
    assert state["instance_id"] == "dev-laptop"
    assert state["node_id"] == "node-xyz"
    assert state["rc_url"] == "https://a30.link/r/dev-laptop"
    assert state["app_url"] == "https://app.aitherium.com/code/node-xyz"
    assert state["pid"] == os.getpid()
    assert state["rc_mode"] == "cli"
    assert fake_link.instance_id == "dev-laptop"
    assert fake_link.rc_mode == "cli"


def test_main_rejects_invalid_slug_with_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    code = RC.main(["--name", "../bad-slug", "--once"])
    assert code == 2
    assert "invalid instance slug" in capsys.readouterr().out


def test_main_status_json_and_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.delenv("AITHER_RC_STATE_FILE", raising=False)
    monkeypatch.setenv("AITHER_HARNESS_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("AITHER_HARNESS_TOKEN", raising=False)

    # Inactive when empty
    assert RC.main(["status", "--json"]) == 0
    st0 = json.loads(capsys.readouterr().out)
    assert st0["active"] is False
    assert st0["reach"] == "offline"

    # Active with live PID
    RC.save_rc_state({
        "instance_id": "keen-lynx-beef",
        "node_id": "node-9",
        "rc_url": "https://tunnel.aitherium.com/r/keen-lynx-beef",
        "app_url": "https://app.aitherium.com/code/node-9",
        "pid": 424242,
        "started_at": "2026-11-27T11:40:00+00:00",
        "harness_url": "http://127.0.0.1:1",
        "rc_mode": "cli",
    })
    monkeypatch.setattr(RC, "_pid_alive", lambda pid: pid == 424242)
    terminated = []
    monkeypatch.setattr(RC, "_terminate_pid", lambda pid: terminated.append(pid) or True)

    assert RC.main(["status", "--json"]) == 0
    st1 = json.loads(capsys.readouterr().out)
    assert st1["active"] is True
    assert st1["instance_id"] == "keen-lynx-beef"
    assert st1["rc_url"] == "https://tunnel.aitherium.com/r/keen-lynx-beef"
    assert st1["reach"] == "tunnel"

    assert RC.main(["off"]) == 0
    out_off = capsys.readouterr().out
    assert "stopped" in out_off.lower()
    assert terminated == [424242]
    assert RC.load_rc_state() == {}


def test_pid_alive_never_calls_os_kill_on_windows(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("os.kill must never be called on Windows")

    fake_os = SimpleNamespace(name="nt", getpid=os.getpid, kill=_boom)
    monkeypatch.setattr(RC, "os", fake_os)

    import adk.turn_journal as tj

    monkeypatch.setattr(tj, "pid_alive", lambda pid: pid == 9999)
    assert RC._pid_alive(9999) is True
    assert RC._pid_alive(1234) is False


def test_autolink_announce_and_hold_persist_daemon_rc_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.delenv("AITHER_RC_STATE_FILE", raising=False)
    monkeypatch.setattr(autolink, "PENDING_PATH", tmp_path / "pending-signin.json")
    monkeypatch.setattr(autolink.shutil, "which", lambda _n: "/bin/awask")
    cmds = []

    class _P:
        stdout, stderr = "", ""

    monkeypatch.setattr(autolink.subprocess, "run", lambda cmd, **_k: cmds.append(cmd) or _P())
    autolink.announce_code("ABCD-EFGH", "https://idp.example/link?code=ABCD-EFGH", 600)
    detail = cmds[0][cmds[0].index("--detail") + 1]
    assert "tunnel.aitherium.com" in detail
    assert "api.aitherium.com/code" not in detail

    from adk import fleet_enroll
    from adk.harnesses import daemon as D

    monkeypatch.setattr(D, "mint_scoped_token", lambda *_a, **_k: "scoped-tok")
    hellos = []

    class _FakeLink:
        def __init__(self):
            self.instance_id = ""
            self.rc_mode = ""
            self._ws = object()

        async def send_hello(self):
            hellos.append((self.instance_id, self.rc_mode))
            return True

        def reach_kind(self):
            return "ws"

    link = _FakeLink()

    async def _fake_enroll(**_kw):
        return {"enrolled": True, "node_id": "daemon-node-1"}

    monkeypatch.setattr(fleet_enroll, "enroll_on_boot", _fake_enroll)
    monkeypatch.setattr(fleet_enroll, "active_node_link", lambda: link)
    autolink._STOP_EVENT.clear()

    async def _stop_soon(_secs):
        autolink.stop_autolink()

    monkeypatch.setattr(autolink.asyncio, "sleep", _stop_soon)

    ok = asyncio.run(autolink._hold("http://127.0.0.1:8362"))
    assert ok is True
    state = RC.load_rc_state()
    assert state["rc_mode"] == "daemon"
    assert state["node_id"] == "daemon-node-1"
    assert state["rc_url"].startswith("https://a30.link/r/")
    assert state["app_url"] == "https://app.aitherium.com/code/daemon-node-1"
    assert hellos == [(state["instance_id"], "daemon")]


def test_rc_status_health_fallback_ignores_stale_dead_pid_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.delenv("AITHER_RC_STATE_FILE", raising=False)
    monkeypatch.delenv("AITHER_HARNESS_TOKEN", raising=False)
    monkeypatch.setattr(RC, "_read_harness_token", lambda: "")
    monkeypatch.setattr(RC, "_pid_alive", lambda _pid: False)
    RC.save_rc_state({
        "instance_id": "stale-dead-slug",
        "node_id": "stale-node",
        "rc_url": "https://tunnel.aitherium.com/r/stale-dead-slug",
        "app_url": "https://app.aitherium.com/code/stale-node",
        "pid": 99999999,
        "rc_mode": "cli",
    })
    monkeypatch.setattr(
        RC,
        "_http_json",
        lambda url, **_k: {"node_link": {"connected": True}} if url.endswith("/health") else None,
    )
    st = RC.rc_status("http://127.0.0.1:8362")
    assert st["active"] is True
    assert st["reach"] == "tunnel"
    assert st["instance_id"] is None
    assert st["node_id"] is None
    assert st["rc_url"] is None
    assert st["app_url"] is None
    assert st["rc_mode"] == "daemon"


def test_safe_remote_url_blocks_multi_encoded_traversal_and_fragments():
    from adk.harnesses import session_verbs

    assert (
        session_verbs.safe_remote_url(
            "https://tunnel.aitherium.com/r/%25252e%25252e/secret"
        )
        == ""
    )
    assert (
        session_verbs.safe_remote_url(
            "https://tunnel.aitherium.com/r/%25252525252e%25252525252e/secret"
        )
        == ""
    )
    assert (
        session_verbs.safe_remote_url("https://tunnel.aitherium.com/r/slug#frag")
        == ""
    )
    assert (
        session_verbs.safe_remote_url("https://tunnel.aitherium.com/r/slug%23frag")
        == ""
    )
    assert (
        session_verbs.safe_remote_url("https://tunnel.aitherium.com/r/slug-ok")
        == "https://tunnel.aitherium.com/r/slug-ok"
    )
