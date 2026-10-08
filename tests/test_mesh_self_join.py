"""`adk mesh join` + adk.mesh.overlay_status: "Joined your mesh" means on the tailnet.

The installers used to print "Joined your mesh" when `adk pair` exited 0 -- an
Identity registration, with no overlay at all. They now print it off `adk mesh
join`'s exit code, so that exit code must be 0 ONLY when tailscale itself says
this device is Running on the mesh's Headscale with a tailnet address. These tests
fake tailscale and the platform; nothing touches the network.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from adk import cli, enrollment, fleet_enroll, mesh


def _ts(monkeypatch, status: dict | None, prefs: dict | None = None, binary="/usr/bin/tailscale"):
    """Fake the tailscale binary: `status --json` and `debug prefs` answers."""
    monkeypatch.setattr(mesh, "_tailscale", lambda: binary)

    def _run(cmd, **kw):
        if cmd[1:] == ["status", "--json"]:
            if status is None:
                raise FileNotFoundError(cmd[0])
            return SimpleNamespace(returncode=0, stdout=json.dumps(status), stderr="")
        if cmd[1:] == ["debug", "prefs"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(prefs or {}), stderr="")
        raise AssertionError(f"unexpected tailscale call {cmd}")

    monkeypatch.setattr(mesh.subprocess, "run", _run)


ON_MESH = {"BackendState": "Running",
           "Self": {"HostName": "aither-n1", "TailscaleIPs": ["100.64.0.7", "fd7a::7"]}}
HS = {"ControlURL": "https://hs.aitherium.com"}


class TestOverlayStatus:
    def test_running_on_the_mesh_headscale_is_joined(self, monkeypatch):
        _ts(monkeypatch, ON_MESH, HS)
        st = mesh.overlay_status("https://hs.aitherium.com")
        assert st["joined"] is True and st["code"] == "ok"
        assert st["tailnet_ip"] == "100.64.0.7"

    def test_missing_binary_names_the_install(self, monkeypatch):
        monkeypatch.setattr(mesh, "_tailscale", lambda: None)
        st = mesh.overlay_status()
        assert st["joined"] is False and st["code"] == "tailscale_missing"
        assert st["install_url"].startswith("https://")

    def test_stopped_backend_is_not_joined(self, monkeypatch):
        _ts(monkeypatch, {"BackendState": "NeedsLogin", "Self": {}}, HS)
        st = mesh.overlay_status()
        assert st["joined"] is False and st["code"] == "not_running"

    def test_personal_tailnet_is_not_the_mesh(self, monkeypatch):
        # A laptop already on its owner's tailscale.com tailnet is Running with a
        # 100.x address -- and is NOT on AitherMesh.
        _ts(monkeypatch, ON_MESH, {"ControlURL": "https://controlplane.tailscale.com"})
        st = mesh.overlay_status("https://hs.aitherium.com")
        assert st["joined"] is False and st["code"] == "other_tailnet"
        assert "tailscale logout" in st["detail"]

    def test_no_tailnet_address_is_not_joined(self, monkeypatch):
        _ts(monkeypatch, {"BackendState": "Running", "Self": {"TailscaleIPs": ["fd7a::7"]}}, HS)
        assert mesh.overlay_status()["code"] == "no_tailnet_ip"

    def test_unanswering_tailscale_is_not_joined(self, monkeypatch):
        _ts(monkeypatch, None)
        assert mesh.overlay_status()["code"] == "tailscale_unreachable"


class TestJoinNoSilentWireguard:
    async def test_failed_headscale_raises_instead_of_raw_wireguard(self, monkeypatch):
        async def _onboard(*a, **kw):
            assert kw["node_class"] == "laptop"
            return {"overlay_ip": "10.77.0.9"}

        def _up(*a, **kw):
            raise RuntimeError("tailscale up timed out (30s)")

        async def _never(*a, **kw):
            raise AssertionError("fell back to raw WireGuard")

        monkeypatch.setattr(mesh, "onboard", _onboard)
        monkeypatch.setattr(mesh, "_tailscale_up", _up)
        monkeypatch.setattr(mesh, "fetch_server_pubkey", _never)
        monkeypatch.setattr(mesh, "generate_keypair", lambda: ("priv", "pub"))
        with pytest.raises(RuntimeError, match="timed out"):
            await mesh.join("https://conductor.test", "n1", headscale=True,
                            headscale_auth_key="k", node_class="laptop",
                            wireguard_fallback=False)


@pytest.fixture()
def signed_in(tmp_path, monkeypatch):
    """Signed in, node record under tmp, every platform call faked."""
    import adk.auth as auth

    class _Store:
        def get_active_profile(self):
            return {"access_token": "user-token", "endpoint": "https://idp.example.test"}

    monkeypatch.setattr(auth, "AuthStore", _Store)
    monkeypatch.setattr(fleet_enroll, "_AITHER_DIR", tmp_path)
    monkeypatch.setattr(fleet_enroll, "_NODE_AUTH_FILE", tmp_path / "node_auth.json")
    (tmp_path / "node_auth.json").write_text(json.dumps({"node_id": "paired-node"}))

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            return httpx.Response(200, json={"mesh_key": "hskey"},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def _rich_enroll(base, token, node_id, **kw):
        return {"enrolled": True, "tenant_id": "ten-1", "bearer_token": "node-cap",
                "registration": {"node_class": kw.get("node_class")}}

    monkeypatch.setattr(enrollment, "rich_enroll", _rich_enroll)
    h = SimpleNamespace(joins=[])

    async def _join(conductor, node_id, **kw):
        h.joins.append({"conductor": conductor, "node_id": node_id, **kw})
        return {"overlay_ip": "10.77.0.9", "transport": "headscale", "tailscale_output": ""}

    monkeypatch.setattr(mesh, "join", _join)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(cli, "_MESH_JOIN_SETTLE_S", 0)
    return h


def _args(**kw):
    return SimpleNamespace(mesh_command="join", node_class="laptop", conductor="",
                           headscale_url="https://hs.aitherium.com", **kw)


class TestAdkMeshJoin:
    def test_exit_0_only_when_the_tailnet_says_running(self, signed_in, monkeypatch, capsys):
        states = iter([{"BackendState": "Stopped", "Self": {}}, ON_MESH])
        monkeypatch.setattr(mesh, "_tailscale", lambda: "/usr/bin/tailscale")
        current = {"st": next(states)}

        def _run(cmd, **kw):
            if cmd[1:] == ["status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps(current["st"]), stderr="")
            return SimpleNamespace(returncode=0, stdout=json.dumps(HS), stderr="")

        monkeypatch.setattr(mesh.subprocess, "run", _run)

        async def _join(conductor, node_id, **kw):
            signed_in.joins.append({"node_id": node_id, **kw})
            current["st"] = next(states)
            return {"overlay_ip": "10.77.0.9", "transport": "headscale"}

        monkeypatch.setattr(mesh, "join", _join)
        assert cli.cmd_mesh(_args()) == 0
        assert "Joined your mesh: 100.64.0.7" in capsys.readouterr().out
        j = signed_in.joins[0]
        # The paired node stays the node, NAT-friendly and never raw WireGuard.
        assert j["node_id"] == "paired-node"
        assert j["headscale"] is True and j["wireguard_fallback"] is False
        assert j["headscale_auth_key"] == "hskey" and j["psk"] == "node-cap"
        assert j["tenant_id"] == "ten-1" and j["node_class"] == "laptop"

    def test_up_returned_but_tailnet_not_running_is_a_failure(self, signed_in, monkeypatch, capsys):
        _ts(monkeypatch, {"BackendState": "NeedsLogin", "Self": {}}, HS)
        rc = cli.cmd_mesh(_args())
        err = capsys.readouterr().err
        assert rc == 6
        assert "Not on the mesh" in err and "Retry: adk mesh join" in err

    def test_missing_tailscale_names_the_install(self, signed_in, monkeypatch, capsys):
        monkeypatch.setattr(mesh, "_tailscale", lambda: None)
        assert cli.cmd_mesh(_args()) == 3
        assert signed_in.joins == []
        assert "install Tailscale" in capsys.readouterr().err

    def test_already_joined_is_idempotent(self, signed_in, monkeypatch):
        _ts(monkeypatch, ON_MESH, HS)
        assert cli.cmd_mesh(_args()) == 0
        assert signed_in.joins == []

    def test_not_signed_in(self, signed_in, monkeypatch, capsys):
        import adk.auth as auth

        class _Local:
            def get_active_profile(self):
                return {"endpoint": "local", "token_type": "local"}

        monkeypatch.setattr(auth, "AuthStore", _Local)
        _ts(monkeypatch, {"BackendState": "Stopped", "Self": {}}, HS)
        assert cli.cmd_mesh(_args()) == 5
        assert "adk login" in capsys.readouterr().err
