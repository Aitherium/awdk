"""An employee's `adk up` runs as THEIR company's persona, not the generic 'aither'.

The chain, end to end on the device side:
  1. the portal lists the company pack on `license/mine` with source="tenant";
  2. the pack sync (both the `adk pack sync` plugin and the enrol-time sync)
     installs it AND records why it is here (`.aither-entitlement.json`);
  3. `adk up` with no --identity/--brain-pack finds the login's company pack and
     serves under its name with its system_prompt;
  4. the agent adopts that prompt (a pack named by `id:` matches the agent name).

A bought pack, another tenant's pack and a login with no company are all unchanged,
and `--no-tenant-persona` / an explicit --identity always win.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import sys
import tarfile
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk import agent_daemon, cli  # noqa: E402
from adk.shell.plugins.builtins import packs as packs_mod  # noqa: E402

GARG_LOGIN = {"username": "cy", "tenant_id": "tnt_garg", "api_key": "saved-key"}
PERSONA = "You are GargBot, an AI assistant for Garg Consulting Group."


def _garg_tarball() -> bytes:
    """Shaped like Genesis pack_delivery._create_tarball for the `garg` pack."""
    body = f"id: garg\napp_name: GargBot Professional\nsystem_prompt: |\n  {PERSONA}\n".encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("garg-1.0.0/packs/garg/app_pack.yaml")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _portal(rows):
    tarball = _garg_tarball()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/marketplace/license/mine"):
            return httpx.Response(200, json={"licenses": rows})
        if request.url.path.endswith("/v1/packs/garg/download"):
            return httpx.Response(200, content=tarball)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


TENANT_ROW = {"listing_id": "garg", "status": "active", "license_key": None,
              "source": "tenant", "tenant_id": "tnt_garg"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway HOME, unsigned packs allowed, no credential plane."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("AITHER_PACK_REQUIRE_SIGNING", "false")
    monkeypatch.setattr(packs_mod, "_credential_hooks", lambda: (None, None))
    return tmp_path


def _patch_clients(monkeypatch, transport):
    real_client, real_async = httpx.Client, httpx.AsyncClient
    monkeypatch.setattr(packs_mod.httpx, "Client",
                        lambda *a, **k: real_client(transport=transport))
    monkeypatch.setattr(packs_mod.httpx, "AsyncClient",
                        lambda *a, **k: real_async(transport=transport))


def _plugin_sync():
    plugin = packs_mod.PacksPlugin()
    plugin._base_url = "https://portal.test"
    plugin.auth.set_auth("tok", "tnt_garg")
    return plugin._sync([])


# ── 2. the sync records provenance ───────────────────────────────────────────

def test_plugin_sync_installs_the_company_pack_and_records_why(home, monkeypatch):
    _patch_clients(monkeypatch, _portal([TENANT_ROW]))
    out = _plugin_sync()
    assert "installed: 1" in out
    rec = json.loads((home / ".aitheros" / "packs" / "garg" / packs_mod.ENTITLEMENT_FILE)
                     .read_text(encoding="utf-8"))
    assert rec == {"pack_id": "garg", "source": "tenant", "tenant_id": "tnt_garg"}


def test_enrol_sync_records_provenance_too(home, monkeypatch):
    _patch_clients(monkeypatch, _portal([TENANT_ROW]))
    installed, failed = asyncio.run(packs_mod.sync_entitled_packs(
        "tok", "tnt_garg", base_url="https://portal.test"))
    assert (installed, failed) == (1, 0)
    assert (home / ".aitheros" / "packs" / "garg" / packs_mod.ENTITLEMENT_FILE).is_file()


def test_a_pack_synced_before_provenance_existed_gets_it_on_the_next_sync(home, monkeypatch):
    pre = home / ".aitheros" / "packs" / "garg"
    pre.mkdir(parents=True)
    (pre / "old.yaml").write_text("x: 1\n", encoding="utf-8")
    _patch_clients(monkeypatch, _portal([TENANT_ROW]))
    assert "already present: 1" in _plugin_sync()
    assert json.loads((pre / packs_mod.ENTITLEMENT_FILE).read_text())["source"] == "tenant"


# ── 3. adk up picks the login's company pack, and nothing else ───────────────

def _synced(home, monkeypatch, row=TENANT_ROW):
    _patch_clients(monkeypatch, _portal([row]))
    _plugin_sync()


def test_company_login_resolves_its_persona(home, monkeypatch):
    _synced(home, monkeypatch)
    path, ident, pack = cli._tenant_persona_pack(GARG_LOGIN)
    assert ident == "garg" and pack == "garg"
    assert path is not None and path.name == "app_pack.yaml"


@pytest.mark.parametrize("login,row", [
    ({"username": "sam"}, TENANT_ROW),                                     # no company
    ({"username": "x", "tenant_id": "platform"}, TENANT_ROW),             # the platform
    ({"username": "eve", "tenant_id": "tnt_other"}, TENANT_ROW),          # another company
    (GARG_LOGIN, {**TENANT_ROW, "source": None, "tenant_id": None}),      # a BOUGHT pack
])
def test_no_company_pack_for_this_login_means_no_persona(home, monkeypatch, login, row):
    _synced(home, monkeypatch, row)
    assert cli._tenant_persona_pack(login) == (None, "", "")


def _up_args(**kw):
    base = dict(yes=True, offline=True, identity=None, name="t", port=8080,
                foreground=False, no_persist=True, force=False, dry_run=True,
                require_register=False, provider="", approve=None, reach="tunnel",
                no_register=True, brain_pack="", no_tenant_persona=False)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def up_env(home, monkeypatch):
    _synced(home, monkeypatch)
    monkeypatch.delenv("AGENT_BRAIN_PACK", raising=False)
    monkeypatch.chdir(home)
    monkeypatch.setattr(cli, "load_saved_config", lambda: dict(GARG_LOGIN))
    monkeypatch.setattr(agent_daemon, "read_status", lambda: None)
    monkeypatch.setattr(agent_daemon, "port_owner", lambda port: None)
    import adk.shell_launcher as sl
    monkeypatch.setattr(sl, "_preflight_check", lambda: (True, "local"))
    return home


def _plan(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_adk_up_runs_as_the_company_persona(up_env, capsys):
    assert cli.cmd_up(_up_args()) == 0
    plan = _plan(capsys)
    assert plan["identity"] == "garg"
    assert plan["tenant_pack"] == "garg"
    assert plan["brain_pack"].endswith("app_pack.yaml")


@pytest.mark.parametrize("kw", [{"no_tenant_persona": True}, {"identity": "aither"}])
def test_opt_out_and_explicit_identity_keep_the_generic_agent(up_env, capsys, kw):
    assert cli.cmd_up(_up_args(**kw)) == 0
    plan = _plan(capsys)
    assert plan["identity"] == "aither" and plan["brain_pack"] is None
    assert plan["tenant_pack"] is None


def test_up_parser_default_identity_is_unset_and_has_the_opt_out():
    seen = {}

    def _capture(args):
        seen.update(identity=args.identity, opt_out=args.no_tenant_persona)
        return 0

    orig, argv = cli.cmd_up, sys.argv
    cli.cmd_up = _capture
    sys.argv = ["adk", "up", "--no-tenant-persona", "--dry-run"]
    try:
        with contextlib.suppress(SystemExit):  # argparse may exit after dispatch
            cli.main()
    finally:
        cli.cmd_up, sys.argv = orig, argv
    assert seen == {"identity": None, "opt_out": True}


# ── 4. the agent adopts a pack that names itself by `id:` ────────────────────

def test_agent_adopts_an_id_named_pack_prompt_only_under_that_name(tmp_path, monkeypatch):
    from adk.agent import AitherAgent

    pack = tmp_path / "app_pack.yaml"
    pack.write_text(f"id: garg\nsystem_prompt: |\n  {PERSONA}\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_BRAIN_PACK", str(pack))

    def _probe(name):
        a = AitherAgent.__new__(AitherAgent)
        a.name = name
        a._identity = type("I", (), {"name": name})()
        return a._load_matching_brain_pack_prompt()

    assert _probe("garg").startswith("You are GargBot")
    assert _probe("hydra") is None
