"""`adk pack install` refuses a pack whose signature does not verify."""
from __future__ import annotations

import io
import tarfile
import types
from pathlib import Path

import httpx


def _tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"id: dev.sig\nversion: 0.1.0\n"
        info = tarfile.TarInfo(".toolpack.yaml")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _install(monkeypatch, tmp_path: Path, headers: dict) -> int:
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
            return httpx.Response(200, content=blob, headers=headers,
                                  request=httpx.Request("GET", "http://x" + url))

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: None)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli, "_get_genesis_url", lambda: "http://genesis.invalid")
    monkeypatch.delenv("AITHER_PACK_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("AITHER_PACK_REQUIRE_SIGNING", raising=False)
    args = types.SimpleNamespace(pack_command="install", pack_id="dev.sig")
    return cli._cmd_pack(args)


def test_forged_signature_is_refused_and_nothing_is_installed(monkeypatch, tmp_path):
    rc = _install(monkeypatch, tmp_path, {"X-Aither-Pack-Signature": "ab" * 64})
    assert rc == 1
    assert not (tmp_path / ".aitheros" / "packs" / "dev.sig").exists()


def test_unsigned_pack_keeps_the_existing_policy(monkeypatch, tmp_path):
    rc = _install(monkeypatch, tmp_path, {})
    assert rc in (0, None)
    assert (tmp_path / ".aitheros" / "packs" / "dev.sig" / ".toolpack.yaml").is_file()
