"""Pack catalogue and install reach the host that serves them, never localhost:8001.

Measured 2026-11-12: ``adk pack install`` said "Cannot reach Genesis" because the
final fallback was ``http://localhost:8001`` (Genesis publishes no host port);
``adk pack list`` silently showed the bundled catalogue because the live read sent
no credential; and a user-level ``AITHER_API_URL`` naming the MCP gateway (which
serves no catalogue HTTP route) made every pack command 404.
"""
from __future__ import annotations

import io
import tarfile
import types
from pathlib import Path

import httpx
import pytest

from adk import cli, pack_remote

BRIDGE = "https://api.aitherium.com/api/bridge/genesis"
MCP = "https://mcp.aitherium.com"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AITHER_GENESIS_URL", "AITHER_API_URL", "AITHER_GATEWAY_URL", "AITHER_URL",
                "AITHER_PORTAL_URL", "AITHER_ELYSIUM_URL", "AITHER_API_KEY",
                "AITHER_TENANT_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "load_saved_config", lambda: {"api_key": "tok-1"})


class _Resp:
    def __init__(self, status: int, payload=None, content: bytes = b"", headers=None):
        self.status_code = status
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)


def test_no_candidate_is_localhost_8001_and_the_bridge_is_always_last():
    assert pack_remote.bases() == [BRIDGE]
    assert pack_remote.bases("http://localhost:8001") == [BRIDGE]
    assert pack_remote.bases("http://127.0.0.1:8001/") == [BRIDGE]
    assert pack_remote.bases("https://genesis.example") == ["https://genesis.example", BRIDGE]


def test_the_kit_resolvers_never_answer_localhost_8001(monkeypatch):
    monkeypatch.setattr(cli, "load_saved_config", lambda: {})  # an outside user, signed out
    assert cli._get_genesis_url() == BRIDGE
    import importlib
    shell_cli = importlib.import_module("adk.shell.cli")
    assert shell_cli._genesis_url() == BRIDGE
    monkeypatch.setenv("AITHER_GENESIS_URL", "https://genesis.example/")
    assert cli._get_genesis_url() == "https://genesis.example"
    assert shell_cli._genesis_url() == "https://genesis.example"


def test_a_misdirected_override_falls_through_to_the_bridge_with_the_bearer(monkeypatch):
    monkeypatch.setenv("AITHER_API_URL", MCP)  # the measured machine's user env
    seen = []

    def get(base, route, headers, timeout):
        seen.append((base, route, headers.get("Authorization")))
        if base == MCP:
            return _Resp(404)
        rows = {"/api/v1/catalog/packs": [{"id": "atlas-pack"}, {"id": "dup"}],
                "/v1/packs/catalog": [{"id": "git-github"}, {"id": "dup", "x": 1}]}[route]
        return _Resp(200, {"packs": rows})

    packs, source, tried = pack_remote.fetch_catalog(get=get)
    assert source == BRIDGE
    assert [p["id"] for p in packs] == ["atlas-pack", "dup", "git-github"]
    assert "x" not in packs[1], "first id wins"
    assert all(auth == "Bearer tok-1" for _b, _r, auth in seen)
    assert any(line.startswith(MCP) and "HTTP 404" in line for line in tried)


def test_a_401_is_reported_as_not_signed_in_not_swallowed(monkeypatch):
    monkeypatch.setattr(cli, "load_saved_config", lambda: {})
    packs, source, tried = pack_remote.fetch_catalog(
        get=lambda base, route, headers, timeout: _Resp(401))
    assert packs == [] and source == ""
    assert all("adk login" in line for line in tried)

    monkeypatch.setattr(pack_remote, "fetch_catalog",
                        lambda explicit="", **k: ([], "", ["x: HTTP 401 (not signed in: run `adk login`)"]))
    catalog = cli._load_pack_catalog("")
    assert catalog, "the bundled catalogue still answers"
    line = cli._pack_catalog_source_line()
    assert "bundled with this kit" in line and "NOT the live one" in line and "adk login" in line


def test_a_live_answer_is_labelled_live(monkeypatch):
    monkeypatch.setattr(pack_remote, "fetch_catalog",
                        lambda explicit="", **k: ([{"id": "p1"}], BRIDGE, [f"{BRIDGE}: 1 packs"]))
    assert cli._load_pack_catalog("") == [{"id": "p1"}]
    assert cli._pack_catalog_source_line() == f"source: live catalogue at {BRIDGE}"


def test_download_moves_past_a_404_host_and_stops_at_a_license_answer(monkeypatch):
    monkeypatch.setenv("AITHER_API_URL", MCP)
    calls = []

    def get(base, route, headers, timeout):
        calls.append(("GET", base))
        return _Resp(404) if base == MCP else _Resp(200, content=b"tar")

    def post(base, route, headers, timeout):
        calls.append(("POST", base))
        return _Resp(404)

    resp, base, _tried = pack_remote.download("git-github", get=get, post=post)
    assert resp.status_code == 200 and base == BRIDGE
    assert calls == [("GET", MCP), ("POST", MCP), ("GET", BRIDGE)]

    resp, base, _tried = pack_remote.download(
        "paid", "https://genesis.example",
        get=lambda b, r, h, t: _Resp(403), post=post)
    assert resp.status_code == 403 and base == "https://genesis.example"

    def boom(base, route, headers, timeout):
        raise OSError("refused")

    resp, base, tried = pack_remote.download("x", get=boom, post=boom)
    assert resp is None and base == "" and "refused" in tried[0]


def _genesis_tarball(pack_id: str, version: str) -> bytes:
    """The archive shape Genesis sends: every member under ``<pack>-<version>/``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in ((".toolpack.yaml", f"id: {pack_id}\nversion: {version}\n".encode()),
                           ("tools.py", b"X = 1\n")):
            info = tarfile.TarInfo(f"{pack_id}-{version}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _install(monkeypatch, tmp_path, resp, base=BRIDGE, tried=()):
    monkeypatch.setattr(pack_remote, "download",
                        lambda pack_id, explicit="", **k: (resp, base, list(tried)))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: None)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("AITHER_PACK_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("AITHER_PACK_REQUIRE_SIGNING", raising=False)
    args = types.SimpleNamespace(pack_command="install", pack_id="git-github",
                                 library_only=True)
    return cli._cmd_pack(args)


def test_a_user_without_the_monorepo_gets_a_pack_the_loader_can_see(monkeypatch, tmp_path):
    rc = _install(monkeypatch, tmp_path, _Resp(
        200, content=_genesis_tarball("git-github", "1.0.0"),
        headers={"X-Pack-Version": "1.0.0"}))
    assert rc in (0, None)
    target = tmp_path / ".aitheros" / "packs" / "git-github"
    assert (target / ".toolpack.yaml").is_file(), "nested under <pack>-<version>/ is invisible"
    assert (target / "tools.py").is_file()
    assert not (target / "git-github-1.0.0").exists()


@pytest.mark.parametrize("status,expect,rc_want", [
    (403, "needs a license", 2),
    (401, "adk login", 2),
    (404, f"not found at {BRIDGE}", 1),
])
def test_each_refusal_says_what_to_do(monkeypatch, tmp_path, capsys, status, expect, rc_want):
    rc = _install(monkeypatch, tmp_path, _Resp(status))
    assert rc == rc_want
    assert expect in capsys.readouterr().out
    assert not (tmp_path / ".aitheros" / "packs" / "git-github").exists()


def test_no_host_reachable_lists_what_was_tried(monkeypatch, tmp_path, capsys):
    rc = _install(monkeypatch, tmp_path, None, base="",
                  tried=[f"{BRIDGE}/v1/packs/git-github/download: ConnectError: x"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Cannot reach the pack catalogue" in out and BRIDGE in out
    assert "localhost:8001" not in out


def test_hoist_leaves_a_flat_archive_alone(tmp_path):
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / ".toolpack.yaml").write_text("id: flat\n", encoding="utf-8")
    assert pack_remote.hoist_single_root(flat) is False
    assert (flat / ".toolpack.yaml").is_file()


def _string_consts(code) -> list:
    out = [c for c in code.co_consts if isinstance(c, str)]
    for c in code.co_consts:
        if hasattr(c, "co_consts"):
            out += _string_consts(c)
    return out


def test_the_pack_paths_no_longer_name_the_dead_port():
    """Compiled string constants, docstring excluded: no code path can produce :8001."""
    import importlib
    shell_cli = importlib.import_module("adk.shell.cli")
    for fn in (cli._get_genesis_url, cli._load_pack_catalog, shell_cli._genesis_url,
               pack_remote.fetch_catalog, pack_remote.download):
        consts = [c for c in _string_consts(fn.__code__) if c != (fn.__doc__ or "")]
        assert not [c for c in consts if "8001" in c], fn.__name__
