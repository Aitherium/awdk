"""`adk mesh join` installs Tailscale itself when it is missing.

2026-10-21: a signed-in laptop ran `adk mesh join` and was told to go install
Tailscale by hand, although the installers already install it. The join now does
the same install (winget / brew cask / install.sh), finds the binary even when a
fresh install is not on PATH yet, and only then judges the join.
"""
from __future__ import annotations

from types import SimpleNamespace

from adk import cli, mesh


class TestInstallTailscale:
    def test_windows_uses_winget_and_judges_by_the_binary(self, monkeypatch):
        calls = []
        present = {"v": False}
        monkeypatch.setattr(mesh.platform, "system", lambda: "Windows")
        monkeypatch.setattr(mesh.shutil, "which",
                            lambda n: "C:/winget.exe" if n == "winget" else None)
        monkeypatch.setattr(mesh, "_tailscale",
                            lambda: "C:/ts.exe" if present["v"] else None)

        def run(argv, **kw):
            calls.append(argv)
            present["v"] = True
            return SimpleNamespace(returncode=0)
        ok, detail = mesh.install_tailscale(run=run)
        assert ok and detail == "installed"
        assert calls[0][:4] == ["winget", "install", "--id", "Tailscale.Tailscale"]

    def test_exit_zero_without_a_binary_is_not_installed(self, monkeypatch):
        monkeypatch.setattr(mesh.platform, "system", lambda: "Windows")
        monkeypatch.setattr(mesh.shutil, "which", lambda n: "C:/winget.exe")
        monkeypatch.setattr(mesh, "_tailscale", lambda: None)
        ok, detail = mesh.install_tailscale(run=lambda a, **k: SimpleNamespace(returncode=0))
        assert not ok and "still missing" in detail

    def test_linux_without_root_says_so_instead_of_hanging(self, monkeypatch):
        monkeypatch.setattr(mesh.platform, "system", lambda: "Linux")
        monkeypatch.setattr(mesh, "_tailscale", lambda: None)
        monkeypatch.setattr(mesh.os, "geteuid", lambda: 1000, raising=False)
        monkeypatch.setattr(mesh.shutil, "which",
                            lambda n: "/usr/bin/sudo" if n == "sudo" else None)
        ok, detail = mesh.install_tailscale(
            run=lambda a, **k: SimpleNamespace(returncode=1))  # sudo -n true fails
        assert not ok and "needs root" in detail

    def test_known_install_path_is_found_off_path(self, monkeypatch):
        monkeypatch.setattr(mesh.shutil, "which", lambda n: None)
        monkeypatch.setattr(mesh.os.path, "isfile",
                            lambda p: p == r"C:\Program Files\Tailscale\tailscale.exe")
        assert mesh._tailscale() == r"C:\Program Files\Tailscale\tailscale.exe"


class TestJoinInstallsClient:
    def _args(self, **kw):
        base = {"headscale_url": "https://hs.aitherium.com", "node_class": "laptop",
                "conductor": "", "no_install_client": False}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_missing_client_is_installed_then_judged(self, monkeypatch, capsys):
        states = iter([
            {"joined": False, "code": "tailscale_missing", "detail": ""},
            {"joined": True, "code": "ok", "tailnet_ip": "100.64.0.9", "backend_state": "Running"},
        ])
        installs = []
        monkeypatch.setattr(mesh, "overlay_status", lambda *a, **k: next(states))
        monkeypatch.setattr(mesh, "install_tailscale",
                            lambda: installs.append(1) or (True, "installed"))
        assert cli._mesh_join_signed_in(self._args()) == 0
        assert installs == [1]
        assert "100.64.0.9" in capsys.readouterr().out

    def test_no_install_flag_keeps_the_old_refusal(self, monkeypatch):
        monkeypatch.setattr(mesh, "overlay_status",
                            lambda *a, **k: {"joined": False, "code": "tailscale_missing",
                                             "detail": ""})
        monkeypatch.setattr(mesh, "install_tailscale",
                            lambda: (_ for _ in ()).throw(AssertionError("must not install")))
        assert cli._mesh_join_signed_in(self._args(no_install_client=True)) == 3

    def test_failed_install_exits_3(self, monkeypatch):
        monkeypatch.setattr(mesh, "overlay_status",
                            lambda *a, **k: {"joined": False, "code": "tailscale_missing",
                                             "detail": ""})
        monkeypatch.setattr(mesh, "install_tailscale", lambda: (False, "winget missing"))
        assert cli._mesh_join_signed_in(self._args()) == 3


class TestKeypairWithoutWg:
    def test_keypair_without_wg_is_a_valid_x25519_pair(self, monkeypatch):
        import base64

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        monkeypatch.setattr(mesh, "_wg", lambda: None)
        priv, pub = mesh.generate_keypair()
        raw = base64.b64decode(priv)
        assert len(raw) == 32 and len(base64.b64decode(pub)) == 32
        derived = X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        assert base64.b64encode(derived).decode() == pub
