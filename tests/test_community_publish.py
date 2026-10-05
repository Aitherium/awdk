"""adk pack publish / adk pack install community:<id> / aither publish (S15).

A fake Genesis community plane (httpx.MockTransport) stands in for the server:

* publish builds the pack, submits, uploads the exact built bytes, exits 0;
* ANY non-2xx (submit 403, upload 500) exits 1 and never claims success --
  ``aither publish`` used to print PUBLISHED after a 404;
* install of a bought listing verifies the sha256 (header AND listing record)
  and extracts the pack; a 403 (not bought) or a sha mismatch installs nothing.
"""
from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path
from typing import List

import httpx
import pytest

from adk import cli
from adk import pack_author as pa

_RealClient = httpx.Client


class FakeGenesis:
    def __init__(self) -> None:
        self.calls: List[httpx.Request] = []
        self.uploaded: bytes = b""
        self.submit_status = 201
        self.upload_status = 200
        self.download_status = 200
        self.bundle: bytes = b""
        self.header_sha: str = ""
        self.record_sha: str = ""

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path.endswith("/submit"):
            if self.submit_status >= 300:
                return httpx.Response(self.submit_status, json={"detail": "publisher not approved"})
            json.loads(request.content)
            return httpx.Response(
                201, json={"ok": True, "listing": {"id": "comm.abc", "status": "pending"}})
        if path.endswith("/upload"):
            if self.upload_status >= 300:
                return httpx.Response(self.upload_status, json={"detail": "scanner exploded"})
            body = request.content
            # pull the file part's bytes out of the multipart body
            start = body.index(b"\r\n\r\n") + 4
            end = body.rindex(b"\r\n--")
            self.uploaded = body[start:end]
            return httpx.Response(200, json={
                "ok": True, "sha256": hashlib.sha256(self.uploaded).hexdigest(), "signed": False,
                "listing": {"status": "pending"}, "scan": {"decision": "needs_review"}})
        if path.endswith("/download"):
            if self.download_status >= 300:
                return httpx.Response(self.download_status,
                                      json={"detail": "purchase this listing"})
            return httpx.Response(200, content=self.bundle, headers={
                "X-Pack-SHA256": self.header_sha, "X-Pack-Version": "0.1.0"})
        if "/listings/" in path:
            return httpx.Response(200, json={"id": "comm.abc", "version": "0.1.0",
                                             "artifact_sha256": self.record_sha})
        return httpx.Response(404, json={"detail": "no route"})


@pytest.fixture
def genesis(monkeypatch, tmp_path) -> FakeGenesis:
    fake = FakeGenesis()

    def _client(*a, **k) -> httpx.Client:
        k.pop("transport", None)
        return _RealClient(transport=httpx.MockTransport(fake.handler), **k)
    monkeypatch.setattr(httpx, "Client", _client)
    monkeypatch.setattr(cli, "_get_genesis_url", lambda: "https://genesis.test")
    monkeypatch.setattr(cli, "load_saved_config", lambda: {"api_key": "test-account-key"})
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.delenv("AITHER_PACK_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("AITHER_PACK_REQUIRE_SIGNING", raising=False)
    return fake


def _publish_args(pack: Path, **kw) -> types.SimpleNamespace:
    base = dict(pack_command="publish", pack_dir=str(pack), kind="tool", summary=None,
                one_time_cents=0, subscription_cents=0, output=None, dry_run=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_pack_publish_submits_and_uploads_the_built_bytes(genesis, tmp_path, capsys):
    pack = pa.scaffold("dev.pub", tmp_path)
    rc = cli._cmd_pack(_publish_args(pack))
    out = capsys.readouterr().out
    assert rc == 0, out
    built = (pack / "dist" / "dev.pub-0.1.0.tar.gz").read_bytes()
    assert genesis.uploaded == built
    paths = [r.url.path for r in genesis.calls]
    assert paths == ["/v1/marketplace/community/submit",
                     "/v1/marketplace/community/comm.abc/upload"]
    auth = {r.headers.get("authorization") for r in genesis.calls}
    assert auth == {"Bearer test-account-key"}
    assert all("x-internal-key" not in r.headers for r in genesis.calls)
    assert "community:comm.abc" in out


@pytest.mark.parametrize("which", ["submit", "upload"])
def test_pack_publish_non_2xx_exits_1(genesis, tmp_path, capsys, which):
    pack = pa.scaffold("dev.fail", tmp_path)
    setattr(genesis, f"{which}_status", 403 if which == "submit" else 500)
    rc = cli._cmd_pack(_publish_args(pack))
    out = capsys.readouterr().out
    assert rc == 1
    assert "NOT published" in out
    assert "submitted listing" not in out


def _agent_project(tmp_path: Path) -> Path:
    proj = tmp_path / "agentproj"
    proj.mkdir()
    (proj / "agent.py").write_text("print('hi')\n", encoding="utf-8")
    (proj / "config.yaml").write_text("identity: demo-agent\n", encoding="utf-8")
    (proj / "README.md").write_text("demo\n", encoding="utf-8")
    return proj


def _cmd_publish_args(proj: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        directory=str(proj), name=None, api_key="test-account-key", gateway=None,
        description="demo", capabilities="chat", version="0.1.0", pricing="free",
        tier="agent", category="general", dry_run=False,
        one_time_cents=0, subscription_cents=0)


def test_aither_publish_non_2xx_exits_1_and_never_says_published(genesis, tmp_path, capsys):
    genesis.submit_status = 404
    rc = cli.cmd_publish(_cmd_publish_args(_agent_project(tmp_path)))
    out = capsys.readouterr().out
    assert rc == 1
    assert "PUBLISHED" not in out and "SUBMITTED" not in out
    assert "NOT published" in out


def test_aither_publish_success_goes_through_the_community_plane(genesis, tmp_path, capsys):
    rc = cli.cmd_publish(_cmd_publish_args(_agent_project(tmp_path)))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert [r.url.path for r in genesis.calls][0] == "/v1/marketplace/community/submit"
    assert "SUBMITTED: demo-agent" in out


def test_aither_publish_paid_pricing_without_a_price_is_refused(genesis, tmp_path, capsys):
    args = _cmd_publish_args(_agent_project(tmp_path))
    args.pricing = "flat_monthly"
    assert cli.cmd_publish(args) == 1
    assert genesis.calls == []


def _install(pack_id: str) -> int:
    return cli._cmd_pack(types.SimpleNamespace(pack_command="install", pack_id=pack_id))


def _bundle(tmp_path: Path) -> bytes:
    pack = pa.scaffold("dev.bought", tmp_path / "src")
    return pa.build(pack, tmp_path / "dist").tarball.read_bytes()


def test_install_community_verifies_and_extracts(genesis, tmp_path):
    genesis.bundle = _bundle(tmp_path)
    genesis.header_sha = genesis.record_sha = hashlib.sha256(genesis.bundle).hexdigest()
    assert _install("community:comm.abc") == 0
    installed = tmp_path / "home" / ".aitheros" / "packs" / "dev.bought"
    assert (installed / ".toolpack.yaml").is_file()


def test_install_community_not_bought_installs_nothing(genesis, tmp_path, capsys):
    genesis.download_status = 403
    assert _install("community:comm.abc") == 1
    assert "not bought" in capsys.readouterr().out
    assert not (tmp_path / "home" / ".aitheros" / "packs").exists()


@pytest.mark.parametrize("tamper", ["header", "record"])
def test_install_community_sha_mismatch_installs_nothing(genesis, tmp_path, tamper):
    genesis.bundle = _bundle(tmp_path)
    good = hashlib.sha256(genesis.bundle).hexdigest()
    genesis.header_sha = "0" * 64 if tamper == "header" else good
    genesis.record_sha = "0" * 64 if tamper == "record" else good
    assert _install("community:comm.abc") == 1
    assert not (tmp_path / "home" / ".aitheros" / "packs" / "dev.bought").exists()


# install_bundle never escapes or overwrites what is not its own -------------------

def _tar_with_id(pack_id: str) -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("pk/.toolpack.yaml", f"id: '{pack_id}'\n".encode()),
                           ("pk/tool.py", b"x = 1\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _dl(data: bytes):
    from adk.community_publish import Download
    return Download(data=data, sha256=hashlib.sha256(data).hexdigest(), signature=None,
                    version="0.1.0")


@pytest.mark.parametrize("bad_id", ["..", ".", ".hidden", "...", "../../etc"])
def test_install_dotdot_manifest_id_stays_inside_packs_dir(tmp_path, bad_id):
    from adk.community_publish import install_bundle

    home = tmp_path / ".aitheros"
    packs = home / "packs"
    packs.mkdir(parents=True)
    secret = home / "secrets.txt"
    secret.write_text("keep me")
    target = install_bundle(_dl(_tar_with_id(bad_id)), "comm.abc", packs)
    assert secret.read_text() == "keep me"
    assert target.parent == packs.resolve()
    assert (target / "tool.py").is_file()


def test_install_refuses_to_overwrite_another_pack(tmp_path):
    from adk.community_publish import CommunityError, install_bundle

    packs = tmp_path / "packs"
    first_party = packs / "dev.core"
    first_party.mkdir(parents=True)
    (first_party / "keep.py").write_text("mine")
    with pytest.raises(CommunityError):
        install_bundle(_dl(_tar_with_id("dev.core")), "comm.evil", packs)
    assert (first_party / "keep.py").read_text() == "mine"


def test_install_refuses_another_listings_install(tmp_path):
    from adk.community_publish import CommunityError, install_bundle

    packs = tmp_path / "packs"
    install_bundle(_dl(_tar_with_id("dev.shared")), "comm.one", packs)
    with pytest.raises(CommunityError):
        install_bundle(_dl(_tar_with_id("dev.shared")), "comm.two", packs)


def test_reinstall_of_the_same_listing_replaces_it(tmp_path):
    from adk.community_publish import install_bundle

    packs = tmp_path / "packs"
    first = install_bundle(_dl(_tar_with_id("dev.mine")), "comm.one", packs)
    (first / "stale.py").write_text("old")
    again = install_bundle(_dl(_tar_with_id("dev.mine")), "comm.one", packs)
    assert again == first and not (again / "stale.py").exists()
