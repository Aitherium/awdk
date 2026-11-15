"""Add-on discovery parity: one resolver (adk.addons) fronts awpack, Library packs, awnode
extensions and awskills; every surface's verb exists; every install is recorded with
source + sha256; an agent can install mid-task behind an approval gate for third-party."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import types
from pathlib import Path

import httpx
import pytest

from adk import addons
from adk.addon_lock import lookup, record_install

# ── fakes ────────────────────────────────────────────────────────────────────────────


class FakeBricks:
    """Stands in for the awpack and awnode CLIs (the real ones are subprocesses)."""

    def __init__(self, awpack=None, awnode=None, verify_ok=True, ext_install_rc=0):
        self.awpack = awpack or {}
        self.awnode = awnode or {}
        self.verify_ok = verify_ok
        self.ext_install_rc = ext_install_rc
        self.calls: list = []

    def __call__(self, name, argv, timeout=0):
        self.calls.append((name, *argv))
        if name == "awpack":
            verb = argv[0]
            if verb == "list":
                return 0, json.dumps({"ok": True, "packs": [
                    {"id": k, "version": "0.1.0", "summary": v, "provenance": "first-party"}
                    for k, v in self.awpack.items()]}), ""
            if verb == "install":
                return 0, json.dumps({"ok": True, "op": "install", "id": argv[1],
                                      "provenance": "first-party", "version": "0.1.0",
                                      "dest": f"/agents/{argv[1]}", "sha256": "a" * 64,
                                      "next": "pip install awdk"}), ""
            if verb == "verify":
                if not self.verify_ok:
                    return 1, json.dumps({"ok": False, "detail": "changed since install"}), ""
                return 0, json.dumps({"ok": True, "sha256": "b" * 64,
                                      "dest": f"/agents/{argv[1]}"}), ""
            if verb == "remove":
                return 0, json.dumps({"ok": True, "op": "remove"}), ""
        if name == "awnode":
            if argv[:2] == ["ext", "list"]:
                return 0, json.dumps({"ok": True, "extensions": [
                    {"id": k, "name": k, "description": v} for k, v in self.awnode.items()]}), ""
            if argv[:2] == ["ext", "install"]:
                if self.ext_install_rc:
                    return 1, json.dumps({"ok": False, "id": argv[2], "error": "no image"}), ""
                return 0, json.dumps({"ok": True, "id": argv[2]}), ""
            return 0, "", ""
        return 127, "", f"{name} not installed"


def _library(rows):
    def lister():
        return [addons.Addon(kind="library", id=k, summary=v, trust="official",
                             source="catalog") for k, v in rows.items()], ""
    return lister


@pytest.fixture
def bricks(monkeypatch, tmp_path):
    fake = FakeBricks(awpack={"persona": "avatar bridge", "awconnect": "browser ext"},
                      awnode={"canvas": "image generation", "awconnect": "ext copy"})
    monkeypatch.setattr(addons, "_brick", fake)
    monkeypatch.setitem(addons._LISTERS, "library",
                        _library({"aitherium": "Aitherium HQ", "deep_research": "research"}))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    addons.clear_cache()
    return fake


# ── 1. the verbs exist; tools list; info falls back ──────────────────────────────────


def test_pack_enable_parses_with_the_real_parser():
    from adk.cli import get_parser
    args = get_parser().parse_args(["pack", "enable", "deep_research"])
    assert (args.command, args.pack_command, args.pack_id) == ("pack", "enable", "deep_research")


def test_every_command_discover_suggests_parses():
    import re

    from adk.cli import get_parser
    src = (Path(__file__).resolve().parents[1] / "adk" / "discovery" / "sources.py").read_text(
        encoding="utf-8")
    cmds = set(re.findall(r'f"adk ([a-z]+ [a-z]+) \{', src))
    assert cmds, "no suggested adk commands found -- the scan is broken"
    for cmd in cmds:
        get_parser().parse_args([*cmd.split(), "some-id"])  # SystemExit(2) = bad verb


def test_skills_verbs_parse():
    from adk.cli import get_parser
    for verb in ("install", "update", "remove"):
        a = get_parser().parse_args(["skills", verb, "acp-drive"])
        assert a.skills_command == verb and a.name == "acp-drive"


def test_tools_list_does_not_raise(capsys, monkeypatch):
    from adk import cli
    monkeypatch.delenv("AITHER_API_KEY", raising=False)
    monkeypatch.setattr(cli, "load_saved_config", lambda *a, **k: {})
    rc = cli.cmd_tools(types.SimpleNamespace(tools_command="list", upgrade=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "file_read" in out and "local tools" in out


def _client_returning(status: int):
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return httpx.Response(status, json={"detail": "unauthorized"},
                                  request=httpx.Request("GET", "http://x" + url))
    return _Client


def test_pack_info_falls_back_to_the_catalog_on_401(monkeypatch, capsys, tmp_path):
    from adk import cli
    monkeypatch.setattr(httpx, "Client", _client_returning(401))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli, "_get_genesis_url", lambda: "http://genesis.invalid")
    rc = cli._cmd_pack(types.SimpleNamespace(pack_command="info", pack_id="aitherium"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Aitherium HQ" in out


def test_pack_info_finds_an_awpack_pack(monkeypatch, capsys, bricks):
    from adk import cli
    monkeypatch.setattr(httpx, "Client", _client_returning(401))
    monkeypatch.setattr(cli, "_get_genesis_url", lambda: "http://genesis.invalid")
    monkeypatch.setattr(cli, "_local_pack_manifests", lambda: {})
    rc = cli._cmd_pack(types.SimpleNamespace(pack_command="info", pack_id="persona"))
    assert rc == 0 and "avatar bridge" in capsys.readouterr().out


# ── the resolver: order, brick calls, records ────────────────────────────────────────


def test_resolution_order_awpack_then_library_then_awnode(bricks):
    assert addons.resolve("awconnect")[0].kind == "awpack"      # in awpack AND awnode
    assert addons.resolve("deep_research")[0].kind == "library"
    assert addons.resolve("canvas")[0].kind == "awnode-ext"
    assert addons.resolve("ext:awconnect")[0].kind == "awnode-ext"  # prefix pins
    assert addons.resolve("nope-not-here")[0] is None


def test_awpack_install_runs_install_then_verify_and_records_verify_hash(bricks):
    res = addons.install("persona")
    assert res.ok, res.detail
    verbs = [c[1] for c in bricks.calls if c[0] == "awpack"]
    assert verbs[-2:] == ["install", "verify"]
    row = lookup("persona", "awpack")
    assert row["sha256"] == "b" * 64 and row["trust"] == "official"
    assert row["source"].startswith("awpack:") and row["dest"] == "/agents/persona"


def test_awpack_install_that_fails_verify_is_not_recorded(bricks):
    bricks.verify_ok = False
    res = addons.install("persona")
    assert not res.ok and "verify" in res.detail
    assert lookup("persona", "awpack") is None


def test_awnode_install_failure_is_a_failure(bricks):
    bricks.ext_install_rc = 1
    res = addons.install("canvas")
    assert not res.ok and "no image" in res.detail
    assert lookup("canvas", "awnode-ext") is None
    bricks.ext_install_rc = 0
    assert addons.install("canvas").ok
    assert lookup("canvas", "awnode-ext")["hash_of"] == "manifest"


def test_search_spans_every_source(bricks):
    hits, down = addons.search("a")
    assert {h.kind for h in hits} == {"awpack", "library", "awnode-ext"}
    assert down == {}


def test_a_source_that_cannot_answer_is_named(bricks, monkeypatch):
    monkeypatch.setattr(addons, "_brick", lambda *a, **k: (127, "", "awpack is not installed"))
    hits, down = addons.search("research")
    assert [h.id for h in hits] == ["deep_research"]
    assert "awpack" in down and "awnode-ext" in down


def test_lock_refuses_a_row_without_a_hash():
    with pytest.raises(ValueError):
        record_install("library", "x", version="1", source="s", sha256="", trust="official")
    with pytest.raises(ValueError):
        record_install("library", "x", version="1", source="s", sha256="ab", trust="meh")
    with pytest.raises(ValueError):
        record_install("plugin", "x", version="1", source="s", sha256="ab", trust="official")


# ── skills ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def shelf(monkeypatch, tmp_path):
    d = tmp_path / "awskills" / "skills"
    d.mkdir(parents=True)
    (d / "acp-drive.md").write_text("---\nname: acp-drive\ndescription: drive ACP agents\n"
                                    "---\n\n# acp-drive\n", encoding="utf-8")
    monkeypatch.setenv("AWSKILLS_DIR", str(tmp_path / "awskills"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    return d


def test_skill_install_copies_records_and_removes(shelf, tmp_path):
    res = addons.install("skill:acp-drive")
    assert res.ok, res.detail
    dest = tmp_path / "home" / ".claude" / "skills" / "acp-drive" / "SKILL.md"
    assert dest.read_bytes() == (shelf / "acp-drive.md").read_bytes()
    row = lookup("acp-drive", "skill")
    assert row["sha256"] == hashlib.sha256(dest.read_bytes()).hexdigest()
    assert row["hash_of"] == "file" and row["dest"] == str(dest)
    assert not addons.install_skill("acp-drive").ok            # already installed
    assert addons.install_skill("acp-drive", replace=True).ok  # update
    assert addons.remove("skill:acp-drive").ok and not dest.exists()
    assert lookup("acp-drive", "skill") is None


def test_skill_install_never_overwrites_a_hand_written_skill(shelf, tmp_path):
    mine = tmp_path / "home" / ".claude" / "skills" / "acp-drive" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("my own", encoding="utf-8")
    res = addons.install_skill("acp-drive", replace=True)
    assert not res.ok and mine.read_text(encoding="utf-8") == "my own"


# ── CLI wiring ───────────────────────────────────────────────────────────────────────


def test_pack_install_cli_goes_through_the_resolver(monkeypatch):
    from adk import cli
    seen = []
    monkeypatch.setattr(cli, "_addons_install", lambda ref, **kw: (
        seen.append((ref, kw)) or addons.Result(True, "awpack", ref, "ok")))
    rc = cli._cmd_pack(types.SimpleNamespace(pack_command="install", pack_id="persona",
                                             allow_community=False))
    assert rc == 0 and seen == [("persona", {"replace": False, "allow_community": False})]


def test_pack_enable_installs_a_missing_pack_then_enables_it(monkeypatch):
    from adk import cli
    saved: dict = {}
    monkeypatch.setattr(cli, "load_saved_config", lambda *a, **k: dict(saved))
    monkeypatch.setattr(cli, "save_saved_config", lambda d, *a, **k: saved.update(d))
    monkeypatch.setattr(cli, "_local_pack_manifests", lambda: {})
    monkeypatch.setattr(cli, "_addons_install",
                        lambda ref, **kw: addons.Result(True, "awpack", ref, "installed"))
    assert cli._cmd_pack_enable("persona", enable=True) == 0
    assert saved["required_packs"] == ["persona"]
    assert cli._cmd_pack_enable("persona", enable=False) == 0
    assert saved["required_packs"] == []


def _tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"id: dev.lock\nversion: 0.2.0\n"
        info = tarfile.TarInfo(".toolpack.yaml")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_library_download_writes_a_lock_row(monkeypatch, tmp_path):
    from adk import cli
    blob = _tarball()

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return httpx.Response(200, content=blob, headers={"X-Pack-Version": "0.2.0"},
                                  request=httpx.Request("GET", "http://x" + url))

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: None)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli, "_get_genesis_url", lambda: "http://genesis.invalid")
    monkeypatch.delenv("AITHER_PACK_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("AITHER_PACK_REQUIRE_SIGNING", raising=False)
    rc = cli._cmd_pack(types.SimpleNamespace(pack_command="install", pack_id="dev.lock",
                                             library_only=True))
    assert rc in (0, None)
    row = lookup("dev.lock", "library")
    assert row["sha256"] == hashlib.sha256(blob).hexdigest()
    assert (row["version"], row["trust"], row["hash_of"]) == ("0.2.0", "official", "tarball")
    rc = cli._cmd_pack(types.SimpleNamespace(pack_command="remove", pack_id="dev.lock",
                                             library_only=True))
    assert rc == 0 and lookup("dev.lock", "library") is None


# ── 2. the agent tool and its approval gate ──────────────────────────────────────────


class _FakeAgent:
    name = "tester"

    def __init__(self):
        from adk.tools import ToolRegistry
        self._tools = ToolRegistry()
        self.activated: list = []

    def activate_pack(self, pack_id):
        self.activated.append(pack_id)
        return {"pack": pack_id, "tools": ["new_tool"]}


@pytest.fixture
def gate_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_ADK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AITHER_TOOL_APPROVAL", raising=False)


def test_agent_gets_install_pack_and_search_packs(gate_env):
    from adk.builtin_tools import register_builtin_tools
    agent = _FakeAgent()
    register_builtin_tools(agent, categories=["packs"])
    names = {t.name for t in agent._tools.list_tools()}
    assert {"install_pack", "search_packs"} <= names


def test_install_pack_uses_the_resolver_and_mounts_a_library_pack(gate_env, monkeypatch):
    from adk import pack_tools
    seen = []
    monkeypatch.setattr(addons, "install", lambda ref, **kw: (
        seen.append(ref) or addons.Result(True, "library", ref, "installed")))
    monkeypatch.setattr(addons, "trust_of", lambda ref: "official")
    agent = _FakeAgent()
    pack_tools.register_pack_tools(agent)
    out = json.loads(agent._tools.get("install_pack").fn(pack_id="deep_research"))
    assert out["ok"] is True, out
    assert seen == ["deep_research"] and agent.activated == ["deep_research"]


def test_install_pack_does_not_mount_an_awpack_pack_live(gate_env, monkeypatch):
    from adk import pack_tools
    monkeypatch.setattr(addons, "install",
                        lambda ref, **kw: addons.Result(True, "awpack", ref, "installed"))
    monkeypatch.setattr(addons, "trust_of", lambda ref: "official")
    agent = _FakeAgent()
    pack_tools.register_pack_tools(agent)
    out = json.loads(agent._tools.get("install_pack").fn(pack_id="persona"))
    assert out["ok"] and out["kind"] == "awpack" and agent.activated == []


def test_install_pack_rejects_a_bad_id(gate_env, bricks):
    from adk import pack_tools
    agent = _FakeAgent()
    pack_tools.register_pack_tools(agent)
    out = json.loads(agent._tools.get("install_pack").fn(pack_id="../../etc"))
    assert out["ok"] is False and agent.activated == []


def test_third_party_install_needs_approval_first_party_does_not(gate_env, bricks):
    from adk import pack_tools
    from adk.approval import allow_tool, needs_approval
    pack_tools.register_pack_tools(_FakeAgent())
    assert needs_approval("tester", "install_pack", {"pack_id": "community:abc123"}) is True
    assert needs_approval("tester", "install_pack", {"pack_id": "someone.unknown"}) is True
    assert needs_approval("tester", "install_pack", {"pack_id": "persona"}) is False
    assert needs_approval("tester", "install_pack", {"pack_id": "aitherium"}) is False
    # A durable "always allow install_pack" cannot pre-approve third-party code.
    allow_tool("install_pack")
    assert needs_approval("tester", "install_pack", {"pack_id": "community:abc123"}) is True
    assert needs_approval("tester", "search_packs", {"query": "x"}) is False


# ── 5. Hermes taps and agentskills.io folders as sources ─────────────────────────────

_REMOTE = b"---\nname: k8s-deploy\ndescription: deploy to k8s\n---\n# k8s\n"


@pytest.fixture
def remote(monkeypatch, tmp_path):
    seen: list = []

    def fake_get(url):
        seen.append(url)
        return _REMOTE

    monkeypatch.setattr(addons, "_http_get", fake_get)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    return seen


def test_hermes_tap_ref_maps_to_the_tap_skills_path():
    url, owner = addons.remote_skill_url("hermes:openai/skills/k8s")
    assert url == "https://raw.githubusercontent.com/openai/skills/HEAD/skills/k8s/SKILL.md"
    assert owner == "openai"
    url, _ = addons.remote_skill_url("agentskills:acme/repo/tools/k8s")
    assert url.endswith("/acme/repo/HEAD/tools/k8s/SKILL.md")
    for bad in ("hermes:openai/skills", "hermes:a/b/../c", "agentskills:https://x/y.md"):
        with pytest.raises(ValueError):
            addons.remote_skill_url(bad)


def test_third_party_skill_needs_consent_then_records_source_and_hash(remote, tmp_path):
    ref = "hermes:openai/skills/k8s"
    res = addons.install(ref)
    assert not res.ok and "--allow-community" in res.detail and remote == []
    res = addons.install(ref, allow_community=True)
    assert res.ok, res.detail
    dest = tmp_path / "home" / ".claude" / "skills" / "k8s-deploy" / "SKILL.md"
    assert dest.read_bytes() == _REMOTE
    row = lookup("k8s-deploy", "skill")
    assert row["source"] == ref and row["trust"] == "community"
    assert row["sha256"] == hashlib.sha256(_REMOTE).hexdigest()


def test_aitherium_owned_remote_skill_is_official(remote):
    assert addons.trust_of("hermes:Aitherium/awskills/acp-drive") == "official"
    assert addons.trust_of("agentskills:someone/repo/x") == "community"
    assert addons.install("hermes:Aitherium/awskills/acp-drive").ok


def test_remote_skill_without_a_valid_name_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(addons, "_http_get", lambda url: b"# no frontmatter\n")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    res = addons.install("agentskills:acme/repo/x", allow_community=True)
    assert not res.ok and "frontmatter name" in res.detail


def test_agent_install_of_a_hermes_skill_asks_the_owner(gate_env, remote):
    from adk import pack_tools
    from adk.approval import needs_approval
    pack_tools.register_pack_tools(_FakeAgent())
    assert needs_approval("tester", "install_pack",
                          {"pack_id": "hermes:openai/skills/k8s"}) is True
    assert needs_approval("tester", "install_pack",
                          {"pack_id": "hermes:Aitherium/awskills/acp-drive"}) is False


def test_skills_cli_installs_a_hermes_skill_with_consent(remote, capsys):
    from adk import cli
    base = dict(skills_command="install", name="hermes:openai/skills/k8s",
                target="claude", json_output=False)
    assert cli._cmd_skills(types.SimpleNamespace(**base, allow_community=False)) == 1
    assert cli._cmd_skills(types.SimpleNamespace(**base, allow_community=True)) == 0
    assert lookup("k8s-deploy", "skill")["source"] == "hermes:openai/skills/k8s"
