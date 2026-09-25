"""`adk grid activate <license-key>` and the /grid shell plugin.

AitherGrid's billing model says a
purchase returns a license key and `adk grid activate <key>` unlocks Pro /
Enterprise. The subcommand did not exist. These tests pin the contract:
a VERIFIED key is written to ~/.aither/license.json and lifts the grid plan;
an unsigned/tampered key is refused and never overwrites the current license.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from adk import cli, licensing  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,  # noqa: E402
)
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    PublicFormat,
)


@pytest.fixture
def signer(monkeypatch, tmp_path):
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    monkeypatch.setenv("AITHER_LICENSE_PUBLIC_KEY", pub_hex)
    monkeypatch.setenv("AITHER_LICENSE_ENFORCE", "1")
    for var in ("AITHER_LICENSE_KEY", "AITHER_LICENSE_FILE", "AITHER_TENANT_SLUG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    licensing.reset_license_manager()
    yield priv
    licensing.reset_license_manager()


def _key(priv, packs, tier="professional", sign_with=None):
    payload = json.dumps({
        "tier": tier, "tenant_id": "t-1", "packs": packs,
        "issued_at": time.time(), "expires_at": time.time() + 3600,
    }).encode()
    sig = (sign_with or priv).sign(payload).hex()
    env = {"payload": base64.b64encode(payload).decode(), "signature": sig}
    return base64.b64encode(json.dumps(env).encode()).decode()


def _parse(*argv):
    from adk.shell.plugins.builtins.grid import grid_parser

    return grid_parser().parse_args(["grid", *argv])


def _activate(key):
    return cli.cmd_grid(_parse("activate", key))


def test_parser_has_activate():
    ns = _parse("activate", "abc")
    assert ns.grid_command == "activate" and ns.license_key == "abc"


def test_valid_key_is_written_and_unlocks_grid_pro(signer, tmp_path, capsys):
    assert licensing.get_license_manager().grid_plan().sku == licensing.GRID_STARTER_SKU
    assert _activate(_key(signer, [licensing.GRID_PRO_SKU])) == 0
    lic_file = tmp_path / ".aither" / "license.json"
    assert lic_file.is_file()
    assert licensing.get_license_manager().grid_plan().sku == licensing.GRID_PRO_SKU
    assert "Grid Pro" in capsys.readouterr().out


def test_forged_key_refused_and_existing_license_untouched(signer, tmp_path):
    lic_file = tmp_path / ".aither" / "license.json"
    lic_file.parent.mkdir(parents=True)
    lic_file.write_text("ORIGINAL", encoding="utf-8")
    forged = _key(signer, [licensing.GRID_ENTERPRISE_SKU], sign_with=Ed25519PrivateKey.generate())
    assert _activate(forged) == 1
    assert lic_file.read_text(encoding="utf-8") == "ORIGINAL"


def test_garbage_key_refused(signer, tmp_path):
    assert _activate("not-a-license") == 1
    assert not (tmp_path / ".aither" / "license.json").exists()


def test_grid_shell_plugin_registers_and_delegates(monkeypatch):
    import asyncio

    from adk.shell.plugins import PluginRegistry

    reg = PluginRegistry([])
    reg.load_all()
    cmd = reg.get("grid")
    assert cmd is not None, "/grid is not registered"
    assert reg.get("aithergrid") is cmd

    seen = {}

    def fake_cmd_grid(ns):
        seen["sub"] = ns.grid_command
        seen["key"] = getattr(ns, "license_key", None)
        return 0

    monkeypatch.setattr(cli, "cmd_grid", fake_cmd_grid)
    out = asyncio.run(cmd.run(["activate", "KEY"], {}))
    assert out == ""
    assert seen == {"sub": "activate", "key": "KEY"}
