"""G3 — installing an external registry pack directory into ~/.aither/agents.

``adk install`` only accepts the packs bundled inside the adk wheel, so a pack
that came from the registry (``Library/packs/<company>/pack/<agent>``) had no
installer at all. These tests assert the properties that make the installer
usable for managed twins: a normalized TOP-LEVEL prompt, idempotence, a
destination name derived from the pack (never from a path), containment inside
the agents root, plain files only, and never silently adopting a stranger's
agent directory.
"""

from __future__ import annotations

import os

import pytest
import yaml

from adk.pack_install import (
    PackCollisionError,
    _contained_destination,
    install_from,
    install_pack_dir,
)


def _probe_symlinks() -> bool:
    """Windows needs Developer Mode / SeCreateSymbolicLinkPrivilege for os.symlink."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "target")
        link = os.path.join(d, "link")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("x")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            return False
        return os.path.islink(link)


_SYMLINKS_OK = _probe_symlinks()
needs_symlinks = pytest.mark.skipif(
    not _SYMLINKS_OK, reason="this platform does not permit creating symlinks")


def _registry_pack(root, agent, *, prompt=None, identity=None):
    pack = root / "acme" / "pack" / agent
    (pack / "skills").mkdir(parents=True)
    (pack / "skills" / "lookup.md").write_text("# lookup\n", encoding="utf-8")
    (pack / "brain_pack.yaml").write_text(
        yaml.safe_dump(
            {
                "system_prompt": prompt or f"You are {agent}.",
                "identity": identity or agent,
                "tools": ["web_search"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (pack / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "aitheros/v1",
                "kind": "AgentPack",
                "metadata": {"name": agent, "company": "acme"},
                "spec": {"runtime": {"model": "claude-haiku-4-5-20251001"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return pack


def test_install_writes_a_top_level_prompt(tmp_path):
    pack = _registry_pack(tmp_path / "registry", "weather")

    result = install_pack_dir(pack, agents_root=tmp_path / "agents")

    dest = tmp_path / "agents" / "weather"
    assert result["agent"] == "weather"
    assert result["installed_to"] == str(dest)
    manifest = yaml.safe_load((dest / "agent.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "weather"
    assert manifest["identity"] == "weather"
    assert manifest["system_prompt"] == "You are weather."
    assert (dest / "skills" / "lookup.md").is_file()


def test_install_twice_changes_nothing(tmp_path):
    pack = _registry_pack(tmp_path / "registry", "weather")

    first = install_pack_dir(pack, agents_root=tmp_path / "agents")
    snapshot = (tmp_path / "agents" / "weather" / "agent.yaml").read_bytes()
    second = install_pack_dir(pack, agents_root=tmp_path / "agents")

    assert first["changed"] is True
    assert second["changed"] is False
    assert (tmp_path / "agents" / "weather" / "agent.yaml").read_bytes() == snapshot
    assert [p.name for p in (tmp_path / "agents").iterdir()] == ["weather"]


def test_install_from_pack_parent_installs_every_agent(tmp_path):
    root = tmp_path / "registry"
    _registry_pack(root, "weather")
    _registry_pack(root, "flights")

    results = install_from(root / "acme" / "pack", agents_root=tmp_path / "agents")

    assert [r["agent"] for r in results] == ["flights", "weather"]
    assert sorted(p.name for p in (tmp_path / "agents").iterdir()) == ["flights", "weather"]


def test_install_refuses_a_dir_without_a_pack(tmp_path):
    empty = tmp_path / "not-a-pack"
    empty.mkdir()

    with pytest.raises(ValueError):
        install_pack_dir(empty, agents_root=tmp_path / "agents")


def test_install_sanitizes_the_agent_name(tmp_path):
    pack = _registry_pack(tmp_path / "registry", "weather", identity="../../escape")

    result = install_pack_dir(pack, agents_root=tmp_path / "agents")

    assert result["agent"] == "escape"
    assert (tmp_path / "agents" / "escape").is_dir()


# ── F1: never silently adopt (replace) a stranger's agent directory ─────────
def _seed_live_agent(agents_root, name, *, prompt="You are the REAL agent."):
    live = agents_root / name
    (live / "oplog").mkdir(parents=True)
    (live / "oplog" / "0001.jsonl").write_text('{"turn": 1}\n', encoding="utf-8")
    (live / "agent.yaml").write_text(
        yaml.safe_dump({"name": name, "identity": name, "system_prompt": prompt},
                       sort_keys=False),
        encoding="utf-8",
    )
    return live


def test_install_refuses_to_replace_an_agent_it_did_not_install(tmp_path):
    pack = _registry_pack(tmp_path / "registry", "weather",
                          prompt="You are an impostor.")
    agents = tmp_path / "agents"
    live = _seed_live_agent(agents, "weather")
    before = (live / "agent.yaml").read_bytes()

    with pytest.raises(PackCollisionError) as excinfo:
        install_pack_dir(pack, agents_root=agents)

    assert "weather" in str(excinfo.value)
    assert (live / "agent.yaml").read_bytes() == before, "the live persona was replaced"
    assert (live / "oplog" / "0001.jsonl").is_file()
    # The fake pack's skill never landed.
    assert not (live / "skills" / "lookup.md").exists()


def test_install_refuses_when_the_existing_identity_differs(tmp_path):
    pack = _registry_pack(tmp_path / "registry", "weather")
    agents = tmp_path / "agents"
    live = _seed_live_agent(agents, "weather")
    project = (live / "agent.yaml").read_text(encoding="utf-8")
    (live / "agent.yaml").write_text(
        project + "installed_from_pack: weather\n", encoding="utf-8")  # owns the dir...
    data = yaml.safe_load((live / "agent.yaml").read_text(encoding="utf-8"))
    data["identity"] = "someone-else"  # ...but declares a DIFFERENT identity
    (live / "agent.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(PackCollisionError):
        install_pack_dir(pack, agents_root=agents)


def test_overwrite_replaces_a_stranger_only_when_asked(tmp_path):
    pack = _registry_pack(tmp_path / "registry", "weather",
                          prompt="You are the new weather agent.")
    agents = tmp_path / "agents"
    live = _seed_live_agent(agents, "weather")

    result = install_pack_dir(pack, agents_root=agents, overwrite=True)

    assert result["created"] is False
    manifest = yaml.safe_load((live / "agent.yaml").read_text(encoding="utf-8"))
    assert manifest["system_prompt"] == "You are the new weather agent."
    assert (live / "oplog" / "0001.jsonl").is_file(), "history must survive a forced update"


def test_reinstall_of_the_same_pack_stays_idempotent(tmp_path):
    pack = _registry_pack(tmp_path / "registry", "weather")
    agents = tmp_path / "agents"

    install_pack_dir(pack, agents_root=agents)
    (agents / "weather" / "oplog").mkdir()
    (agents / "weather" / "oplog" / "0001.jsonl").write_text("{}\n", encoding="utf-8")

    second = install_pack_dir(pack, agents_root=agents)  # must NOT raise a collision

    assert second["changed"] is False
    assert (agents / "weather" / "oplog" / "0001.jsonl").is_file()


# ── F2/F3: plain files only, and contained inside the agents root ───────────
@needs_symlinks
def test_install_refuses_a_symlink_inside_the_pack(tmp_path):
    secret = tmp_path / "private.key"
    secret.write_text("TOP-SECRET", encoding="utf-8")
    pack = _registry_pack(tmp_path / "registry", "weather")
    os.symlink(secret, pack / "leaked.key")

    with pytest.raises(ValueError, match="symlink"):
        install_pack_dir(pack, agents_root=tmp_path / "agents")

    assert not (tmp_path / "agents" / "weather").exists(), "a refused pack wrote nothing"


@needs_symlinks
def test_install_refuses_a_symlinked_destination(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    agents = tmp_path / "agents"
    agents.mkdir()
    os.symlink(outside, agents / "weather")
    pack = _registry_pack(tmp_path / "registry", "weather")

    with pytest.raises(ValueError, match="symlink"):
        install_pack_dir(pack, agents_root=agents)

    assert list(outside.iterdir()) == [], "the install escaped through the destination link"


def test_containment_refuses_a_destination_outside_the_root(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    with pytest.raises(ValueError, match="outside the agents root"):
        _contained_destination(agents, agents / ".." / "escape")
    # The sane arm: a plain child of the root is allowed.
    _contained_destination(agents, agents / "weather")


def _admin_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from adk.admin_api import register_admin_routes

    async def _get_agent(name=None, **kwargs):  # pragma: no cover - never called
        raise AssertionError("the install route must not build an agent")

    app = FastAPI()
    register_admin_routes(app, get_agent=_get_agent, state={})
    return TestClient(app)


def test_admin_install_refuses_a_path_outside_the_registry(tmp_path, monkeypatch):
    (tmp_path / "registry").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr("adk.pack_install.pack_roots", lambda: [tmp_path / "registry"])

    response = _admin_client().post("/admin/packs/install", json={"path": str(outside)})

    assert response.status_code == 400
    assert response.json()["error"] == "pack_root_required"


def test_admin_install_calls_the_installer_inside_the_registry(tmp_path, monkeypatch):
    root = tmp_path / "registry"
    pack = _registry_pack(root, "weather")
    seen = {}

    def _fake_install_from(path, agents_root=None, *, overwrite=False):
        seen["path"] = path
        seen["overwrite"] = overwrite
        return [{"agent": "weather", "installed_to": "/tmp/weather",
                 "created": True, "changed": True}]

    monkeypatch.setattr("adk.pack_install.pack_roots", lambda: [root])
    monkeypatch.setattr("adk.pack_install.install_from", _fake_install_from)

    response = _admin_client().post("/admin/packs/install", json={"path": str(pack)})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert seen["path"] == pack.resolve()
    assert seen["overwrite"] is False, "overwrite must never be implied"


def test_admin_install_passes_overwrite_from_the_body(tmp_path, monkeypatch):
    root = tmp_path / "registry"
    pack = _registry_pack(root, "weather")
    seen = {}

    def _fake_install_from(path, agents_root=None, *, overwrite=False):
        seen["overwrite"] = overwrite
        return [{"agent": "weather", "installed_to": "/tmp/weather",
                 "created": False, "changed": True}]

    monkeypatch.setattr("adk.pack_install.pack_roots", lambda: [root])
    monkeypatch.setattr("adk.pack_install.install_from", _fake_install_from)

    response = _admin_client().post(
        "/admin/packs/install", json={"path": str(pack), "overwrite": True})

    assert response.status_code == 200
    assert seen["overwrite"] is True


def test_admin_install_reports_a_collision_distinctly(tmp_path, monkeypatch):
    root = tmp_path / "registry"
    pack = _registry_pack(root, "weather")

    def _colliding(path, agents_root=None, *, overwrite=False):
        raise PackCollisionError("weather", root / "agents" / "weather",
                                 "an agent directory already exists")

    monkeypatch.setattr("adk.pack_install.pack_roots", lambda: [root])
    monkeypatch.setattr("adk.pack_install.install_from", _colliding)

    response = _admin_client().post("/admin/packs/install", json={"path": str(pack)})

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "pack_collision"
    assert body["agent"] == "weather"
    assert body["overwrite"] is False
