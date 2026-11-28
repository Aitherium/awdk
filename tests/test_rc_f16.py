"""Tests for M2.5 F16 integration, standalone boundaries, and SSE concurrency/cancel."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from adk import node_link as nl_mod
from adk import rc as rc_mod

_AWSEAL_PKG = Path(__file__).resolve().parents[2] / "AitherOS" / "packages" / "awseal"


@contextlib.contextmanager
def _scoped_repo_awseal(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Load in-repo ``awseal`` (0.1.2) without leaking ``sys.path`` or ``sys.modules``."""
    saved = {
        name: mod
        for name, mod in list(sys.modules.items())
        if name == "awseal" or name.startswith("awseal.")
    }
    for name in list(saved):
        sys.modules.pop(name, None)
    monkeypatch.syspath_prepend(str(_AWSEAL_PKG))
    try:
        yield importlib.import_module("awseal")
    finally:
        for name in [
            k for k in list(sys.modules) if k == "awseal" or k.startswith("awseal.")
        ]:
            sys.modules.pop(name, None)
        sys.modules.update(saved)


def test_awdk_standalone_imports_and_fallbacks_without_awseal_awrun_lib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """awdk M2 modules must import and degrade honestly when optional extras are absent."""
    monkeypatch.setitem(sys.modules, "awseal", None)
    monkeypatch.setitem(sys.modules, "awrun", None)
    monkeypatch.setitem(sys.modules, "lib", None)
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.delenv("AITHER_RC_STATE_FILE", raising=False)
    monkeypatch.delenv("AITHER_NODE_LINK_SIGNING_KEY", raising=False)
    monkeypatch.delenv("AWSEAL_KEY_PATH", raising=False)

    for mod_name in (
        "adk.node_link",
        "adk.rc",
        "adk.autolink",
        "adk.harnesses.rc_routes",
        "adk.harnesses.pairing",
        "adk.harnesses.session_verbs",
    ):
        mod = importlib.import_module(mod_name)
        importlib.reload(mod)

    # Posture resolution & invalid tier clamping
    assert nl_mod.resolve_runtime_posture() == ("standard", False)
    assert nl_mod.resolve_runtime_posture(runtime_tier="bogus-tier") == ("standard", False)
    assert nl_mod.resolve_runtime_posture(runtime_tier="awzeal") == ("awzeal", True)
    assert nl_mod.resolve_runtime_posture(zero_libc=True) == ("zero-libc", True)

    monkeypatch.setenv("AITHER_RUNTIME_TIER", "zero-libc")
    monkeypatch.setenv("AITHER_ZERO_LIBC", "1")
    hello = nl_mod.hello_frame_for(instance_id="stand-1", hostname="box", harnesses=["awdk"])
    assert hello["runtime_tier"] == "zero-libc"
    assert hello["zero_libc"] is True

    # Ed25519 hello signing uses cryptography fallback even with awseal blocked
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    signed = nl_mod.hello_frame_for(
        instance_id="stand-1",
        hostname="box",
        harnesses=["awdk"],
        node_id="node-stand",
        signing_key=priv,
    )
    ok, detail = nl_mod.verify_hello_payload(signed, expected_node_id="node-stand")
    assert ok is True, detail

    # Tampering fails closed
    tampered = dict(signed, instance_id="hijacked")
    ok_bad, _ = nl_mod.verify_hello_payload(tampered, expected_node_id="node-stand")
    assert ok_bad is False

    # attest=True without any key fails closed
    with pytest.raises(ValueError):
        nl_mod.hello_frame_for(instance_id="stand-1", attest=True)

    # rc.export_rc_bundle degrades honestly to sha256-only without awseal
    bundle_dir = tmp_path / "rc_bundle"
    exported = rc_mod.export_rc_bundle(bundle_dir)
    assert exported["sealed"] is False
    assert exported["seal_backend"] == "sha256-only"
    assert rc_mod.verify_rc_bundle(bundle_dir)["ok"] is True

    # Keyed export/verify fail closed when awseal is missing
    with pytest.raises(RuntimeError):
        rc_mod.export_rc_bundle(bundle_dir, key_path=tmp_path / "missing.key")
    with pytest.raises(RuntimeError):
        rc_mod.verify_rc_bundle(bundle_dir, require_seal=True)
    with pytest.raises(RuntimeError):
        rc_mod.verify_rc_bundle(bundle_dir, expect_key="00" * 32)
    (bundle_dir / "awseal.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError):
        rc_mod.verify_rc_bundle(bundle_dir)


def test_nodelink_blocks_awrun_and_permits_rc_status() -> None:
    assert nl_mod.path_target("harness/awrun/submit", method="POST") == ("", "")
    assert nl_mod.path_target("harness/awrun/queue", method="GET") == ("", "")
    assert nl_mod.path_target("harness/rc/status", method="GET") == ("harness", "rc/status")
    assert nl_mod.path_target("harness/rc/status", method="POST") == ("", "")


def test_nodelink_serve_once_concurrent_sse_post_and_cancel_no_hol_deadlock() -> None:
    """Open SSE stream must not block subsequent POSTs; cancel must unwind stream."""
    websockets = pytest.importorskip("websockets")

    stream_closed = threading.Event()
    post_hits: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/sessions/s1/stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    self.wfile.write(b"event: init\ndata: ready\n\n")
                    self.wfile.flush()
                    while not stream_closed.is_set():
                        time.sleep(0.05)
                except OSError as exc:
                    _ = exc
                    stream_closed.set()
                finally:
                    stream_closed.set()
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length > 0 else b""
            post_hits.append(self.path)
            payload = json.dumps({"ok": True, "echo": body.decode("utf-8", "replace")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    srv_thread.start()
    harness_url = f"http://127.0.0.1:{srv.server_address[1]}"

    async def _run() -> None:
        received: list[dict] = []

        async def _ws_handler(ws) -> None:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert hello["t"] == "hello"

            # 1. Start infinite SSE stream
            await ws.send(
                json.dumps(
                    {
                        "t": "req",
                        "id": "r-sse",
                        "method": "GET",
                        "path": "harness/sessions/s1/stream",
                        "query": "",
                        "headers": {},
                    }
                )
            )
            # Wait until SSE response + initial chunk arrive
            seen_sse_chunk = False
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not seen_sse_chunk:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                frame = json.loads(raw)
                received.append(frame)
                if frame.get("id") == "r-sse" and frame.get("t") == "chunk":
                    seen_sse_chunk = True
            assert seen_sse_chunk is True

            # 2. While SSE is STILL open, send a concurrent POST request
            await ws.send(
                json.dumps(
                    {
                        "t": "req",
                        "id": "r-post",
                        "method": "POST",
                        "path": "harness/sessions/s1/message",
                        "query": "",
                        "headers": {"content-type": "application/json"},
                        "body_b64": "eyJ0ZXh0IjoiaGkifQ==",
                    }
                )
            )
            seen_post_end = False
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not seen_post_end:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                frame = json.loads(raw)
                received.append(frame)
                if frame.get("id") == "r-post" and frame.get("t") == "end":
                    seen_post_end = True
            assert seen_post_end is True

            # 3. Cancel the SSE stream and confirm active_tasks drains
            await ws.send(json.dumps({"t": "cancel", "id": "r-sse"}))
            for _ in range(50):
                if not link._active_tasks:
                    break
                await asyncio.sleep(0.02)
            assert link._active_tasks == {}
            await ws.close()

        async with websockets.serve(_ws_handler, "127.0.0.1", 0) as ws_srv:
            port = ws_srv.sockets[0].getsockname()[1]
            link = nl_mod.NodeLink(
                tunnel_url=f"http://127.0.0.1:{port}",
                node_id="n-conc",
                token="tok",
                harness_url=harness_url,
                harness_token="h-tok",
            )
            await link._serve_once()

        assert post_hits == ["/sessions/s1/message"]
        sse_ends = [f for f in received if f.get("id") == "r-sse" and f.get("t") in ("end", "err")]
        assert sse_ends == []

    try:
        asyncio.run(_run())
    finally:
        stream_closed.set()
        srv.shutdown()
        srv.server_close()


def test_rc_cmd_runtime_tier_zero_libc_and_awseal_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    awseal_keys = pytest.importorskip("awseal.keys")

    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.delenv("AITHER_RC_STATE_FILE", raising=False)
    monkeypatch.delenv("AITHER_RUNTIME_TIER", raising=False)
    monkeypatch.delenv("AITHER_ZERO_LIBC", raising=False)
    monkeypatch.setattr(rc_mod, "_signed_in", lambda: True)
    monkeypatch.setattr(rc_mod, "probe_harness", lambda *_a, **_k: (True, "0 session(s)"))

    from adk import fleet_enroll
    from adk.harnesses import daemon as daemon_mod
    from adk.harnesses import rc_routes

    monkeypatch.setattr(daemon_mod, "mint_scoped_token", lambda *_a, **_k: "scoped-tok")
    fake_link = nl_mod.NodeLink(
        tunnel_url="https://tunnel.aitherium.com", node_id="n-z", token="t"
    )

    async def _fake_enroll(**_kw):
        return {"enrolled": True, "node_id": "n-z"}

    monkeypatch.setattr(fleet_enroll, "enroll_on_boot", _fake_enroll)
    monkeypatch.setattr(fleet_enroll, "active_node_link", lambda: fake_link)
    monkeypatch.setattr(fleet_enroll, "_node_link", fake_link, raising=False)

    code = rc_mod.main(
        ["--name", "zeal-cli", "--runtime-tier", "awzeal", "--zero-libc", "--once"]
    )
    assert code == 0
    capsys.readouterr()

    st = rc_mod.load_rc_state()
    assert st["runtime_tier"] == "awzeal"
    assert st["zero_libc"] is True
    assert fake_link.runtime_tier == "awzeal"
    assert fake_link.zero_libc is True

    fake_link.connected = True
    route_st = rc_routes.get_rc_status()
    assert route_st["runtime_tier"] == "awzeal"
    assert route_st["zero_libc"] is True

    key_path = tmp_path / "rc.key"
    awseal_keys.generate(key_path)
    pub_hex = awseal_keys.public_key_hex(awseal_keys.load_private_key(key_path))
    bundle_dir = tmp_path / "bundle"
    exp = rc_mod.export_rc_bundle(bundle_dir, key_path=key_path)
    assert exp["sealed"] is True
    assert exp["public_key"] == pub_hex

    ver = rc_mod.verify_rc_bundle(bundle_dir, expect_key=pub_hex)
    assert ver["ok"] is True
    assert ver["payload"]["runtime_tier"] == "awzeal"

    # Tamper 1 byte -> fail closed
    bundle_file = bundle_dir / "rc_bundle.json"
    bundle_file.write_bytes(bundle_file.read_bytes() + b" ")
    with pytest.raises(ValueError):
        rc_mod.verify_rc_bundle(bundle_dir, expect_key=pub_hex)


@pytest.mark.parametrize("bad_ts", ["nan", "inf", "-inf"])
def test_verify_hello_payload_rejects_non_finite_ts(bad_ts: str) -> None:
    """Non-finite ts ('nan', 'inf', '-inf') must fail closed even when validly signed."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    frame = nl_mod.hello_frame_for(
        instance_id="nan-slug",
        hostname="nan-host",
        harnesses=["awdk"],
        attest=False,
    )
    frame.update(
        {
            "node_id": "n-nan",
            "ts": bad_ts,
            "nonce": "nonce-nan-1234",
            "public_key": pub_hex,
            "sig_alg": "ed25519",
        }
    )
    frame["sig"] = base64.b64encode(priv.sign(nl_mod._canonical_hello_bytes(frame))).decode(
        "ascii"
    )
    ok, detail = nl_mod.verify_hello_payload(frame, expected_node_id="n-nan")
    assert ok is False
    assert detail == "missing or invalid ts"


def test_verify_rc_bundle_multifile_completeness_and_unsealed_sibling(tmp_path: Path) -> None:
    """Multi-file verify_rc_bundle fails on missing/tampered sealed files, allows extra files."""
    awseal = pytest.importorskip("awseal")
    seal_keys = pytest.importorskip("awseal.keys")

    key_path = tmp_path / "multi.key"
    seal_keys.generate(key_path)
    pub_hex = seal_keys.public_key_hex(seal_keys.load_private_key(key_path))

    bundle_dir = tmp_path / "multi_bundle"
    bundle_dir.mkdir()
    (bundle_dir / "rc_bundle.json").write_text(
        json.dumps({"schema": "aither.rc.bundle.v1", "instance_id": "m1"}) + "\n",
        encoding="utf-8",
    )
    sibling = bundle_dir / "sibling.txt"
    sibling.write_text("sealed-sibling-v1", encoding="utf-8")
    seal = awseal.sign(bundle_dir, key_path=key_path, subject="rc:m1")
    awseal.write(seal, bundle_dir)

    # Unrelated unsealed file in bundle_dir must not break verification
    (bundle_dir / "notes.tmp").write_text("unrelated", encoding="utf-8")
    assert rc_mod.verify_rc_bundle(bundle_dir, expect_key=pub_hex)["ok"] is True

    # Modified sealed sibling -> fail closed
    sibling.write_text("tampered-sibling", encoding="utf-8")
    with pytest.raises(ValueError):
        rc_mod.verify_rc_bundle(bundle_dir, expect_key=pub_hex)

    # Deleted sealed sibling -> fail closed
    sibling.unlink()
    with pytest.raises(ValueError):
        rc_mod.verify_rc_bundle(bundle_dir, expect_key=pub_hex)


def test_verify_rc_bundle_awseal_011_compat_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify_rc_bundle must verify and fail closed when awseal lacks verify_files (0.1.1)."""
    awseal_mod = pytest.importorskip("awseal")
    seal_keys = pytest.importorskip("awseal.keys")
    monkeypatch.delattr(awseal_mod, "verify_files", raising=False)

    key_a = tmp_path / "a.key"
    key_b = tmp_path / "b.key"
    seal_keys.generate(key_a)
    seal_keys.generate(key_b)
    pub_a = seal_keys.public_key_hex(seal_keys.load_private_key(key_a))
    pub_b = seal_keys.public_key_hex(seal_keys.load_private_key(key_b))

    bundle_dir = tmp_path / "compat_011"
    bundle_dir.mkdir()
    bundle_file = bundle_dir / "rc_bundle.json"
    bundle_file.write_text(
        json.dumps({"schema": "aither.rc.bundle.v1", "instance_id": "c011"}) + "\n",
        encoding="utf-8",
    )
    sibling = bundle_dir / "sibling.txt"
    sibling.write_text("sealed-011", encoding="utf-8")
    seal = awseal_mod.sign(bundle_dir, key_path=key_a, subject="rc:c011")
    awseal_mod.write(seal, bundle_dir)

    # Extra unsealed sibling passes under 0.1.1 compat
    (bundle_dir / "notes.tmp").write_text("extra", encoding="utf-8")
    res = rc_mod.verify_rc_bundle(bundle_dir, expect_key=pub_a)
    assert res["ok"] is True
    assert res["public_key"] == pub_a

    # Wrong expect_key -> fail closed
    with pytest.raises(ValueError):
        rc_mod.verify_rc_bundle(bundle_dir, expect_key=pub_b)

    # Corrupted signature -> fail closed
    seal_path = bundle_dir / "awseal.json"
    orig_seal_text = seal_path.read_text(encoding="utf-8")
    seal_doc = json.loads(orig_seal_text)
    seal_doc["signature"] = ("ff" * 64) if not seal_doc["signature"].startswith("ff") else "00" * 64
    seal_path.write_text(json.dumps(seal_doc), encoding="utf-8")
    with pytest.raises(ValueError):
        rc_mod.verify_rc_bundle(bundle_dir, expect_key=pub_a)
    seal_path.write_text(orig_seal_text, encoding="utf-8")

    # Tamper 1 byte in rc_bundle.json -> fail closed
    orig_bundle = bundle_file.read_bytes()
    bundle_file.write_bytes(orig_bundle + b" ")
    with pytest.raises(ValueError):
        rc_mod.verify_rc_bundle(bundle_dir, expect_key=pub_a)
    bundle_file.write_bytes(orig_bundle)

    # Tamper sealed sibling -> fail closed
    sibling.write_text("tampered-011", encoding="utf-8")
    with pytest.raises(ValueError):
        rc_mod.verify_rc_bundle(bundle_dir, expect_key=pub_a)

    # Missing sealed sibling -> fail closed
    sibling.unlink()
    with pytest.raises(ValueError):
        rc_mod.verify_rc_bundle(bundle_dir, expect_key=pub_a)


def test_verify_rc_bundle_repo_awseal_012_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-repo awseal 0.1.2 path passes and restores sys.modules cleanly on exit."""
    with _scoped_repo_awseal(monkeypatch) as awseal_012:
        assert callable(getattr(awseal_012, "verify_files", None))
        from awseal import keys as seal_keys

        key_a = tmp_path / "k12.key"
        seal_keys.generate(key_a)
        pub_a = seal_keys.public_key_hex(seal_keys.load_private_key(key_a))

        bundle_dir = tmp_path / "bundle_012"
        bundle_dir.mkdir()
        bundle_file = bundle_dir / "rc_bundle.json"
        bundle_file.write_text(
            json.dumps({"schema": "aither.rc.bundle.v1", "instance_id": "c012"}) + "\n",
            encoding="utf-8",
        )
        seal = awseal_012.sign(bundle_dir, key_path=key_a, subject="rc:c012")
        awseal_012.write(seal, bundle_dir)

        assert rc_mod.verify_rc_bundle(bundle_dir, expect_key=pub_a)["ok"] is True
        bundle_file.write_bytes(bundle_file.read_bytes() + b" ")
        with pytest.raises(ValueError):
            rc_mod.verify_rc_bundle(bundle_dir, expect_key=pub_a)
