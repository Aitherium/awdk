"""Tests for M3.4 (F16.M3): AwzealZeroLibcProvider, SSRF/numeric fixes, awseal, awrun & standalone boundary."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import shlex
import shutil
import signal
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from adk.bots.actions import validate_action_url
from adk.bots.deploy import (
    _host_port_from_url,
    build_awrun_job_spec,
    deploy_bot,
    derive_bot_egress_hosts,
    verify_bot_bundle,
)
from adk.bots.manifest import load_bot_manifest, scaffold_bot
from adk.bots.runtime import BotRuntime, create_bot_app
from adk.llm.awzeal import AwzealZeroLibcProvider
from adk.llm.base import LLMProvider, Message


def _clear_awzeal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "AWZEAL_BINARY",
        "ASM_LLM_BINARY",
        "AWZEAL_MODEL",
        "AWZEAL_MODEL_PATH",
        "AWZEAL_URL",
        "AWZEAL_ENDPOINT",
        "AWZEAL_MODEL_SHA256",
        "AWZEAL_SHARED_RO_MMAP",
        "AWZEAL_TARGET_SURFACE",
        "AWZEAL_THREADS",
        "AWZEAL_CPU_PIN",
        "AWZEAL_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _write_stub_binary(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_awzeal_provider_unconfigured_non_raising_and_env_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_awzeal_env(monkeypatch)
    prov = AwzealZeroLibcProvider()
    assert isinstance(prov, LLMProvider)
    assert prov.binary == ""
    assert prov.endpoint == ""
    assert prov.base_url == ""
    assert prov.model_sha256 is None
    assert prov.shared_ro_mmap is False
    assert prov.target_surface == "standard"

    fake_model = tmp_path / "bonsai.gguf"
    fake_model.write_bytes(b"GGUF-DATA")
    monkeypatch.setenv("ASM_LLM_BINARY", "/usr/local/bin/asm-llm")
    monkeypatch.setenv("AWZEAL_MODEL_PATH", str(fake_model))
    monkeypatch.setenv("AWZEAL_ENDPOINT", "http://127.0.0.1:8090/")
    monkeypatch.setenv("AWZEAL_MODEL_SHA256", "A" * 64)
    monkeypatch.setenv("AWZEAL_SHARED_RO_MMAP", "true")
    monkeypatch.setenv("AWZEAL_TARGET_SURFACE", "awzeal")
    monkeypatch.setenv("AWZEAL_THREADS", "8")
    monkeypatch.setenv("AWZEAL_CPU_PIN", "0-3")
    monkeypatch.setenv("AWZEAL_API_KEY", "local-tok")

    env_prov = AwzealZeroLibcProvider()
    assert env_prov.binary == "/usr/local/bin/asm-llm"
    assert env_prov.model_path == str(fake_model)
    assert env_prov.endpoint == "http://127.0.0.1:8090"
    assert env_prov.base_url == "http://127.0.0.1:8090"
    assert env_prov.model_sha256 == "a" * 64
    assert env_prov.shared_ro_mmap is True
    assert env_prov.target_surface == "awzeal"
    assert env_prov.threads == 8
    assert env_prov.cpu_pin == "0-3"
    assert env_prov.api_key == "local-tok"


@pytest.mark.asyncio
async def test_awzeal_provider_subprocess_chat_flags_tools_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_awzeal_env(monkeypatch)
    model_file = tmp_path / "qwen3.gguf"
    model_bytes = b"GGUF-STUB-PAYLOAD"
    model_file.write_bytes(model_bytes)
    model_sha = hashlib.sha256(model_bytes).hexdigest()

    args_log = tmp_path / "argv.json"
    stdin_log = tmp_path / "stdin.txt"
    stub_bin = _write_stub_binary(
        tmp_path / "stub-awzeal",
        f"""
import json, sys
from pathlib import Path
Path({str(args_log)!r}).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
Path({str(stdin_log)!r}).write_text(sys.stdin.read(), encoding="utf-8")
if "--json" in sys.argv:
    print(json.dumps({{"name": "lookup_doc", "arguments": {{"query": "zero-libc"}}}}))
else:
    print("awzeal-subprocess-ok")
""",
    )

    prov = AwzealZeroLibcProvider(
        binary=str(stub_bin),
        model=str(model_file),
        model_sha256=model_sha,
        shared_ro_mmap=True,
        cpu_pin="0,2",
        threads=6,
    )
    assert await prov.health_check() is True
    assert await prov.list_models() == ["qwen3.gguf"]

    resp = await prov.chat(
        [
            Message(role="system", content="Be brief."),
            Message(role="user", content="Status?"),
        ]
    )
    assert resp.content == "awzeal-subprocess-ok"
    assert resp.model == "qwen3.gguf"
    argv = json.loads(args_log.read_text(encoding="utf-8"))
    assert "--shared-ro-mmap" in argv
    assert "--cpu-pin" in argv and "0,2" in argv
    assert "-j" in argv and "6" in argv
    chatml = stdin_log.read_text(encoding="utf-8")
    assert "<|im_start|>system\nBe brief.<|im_end|>" in chatml
    assert "<|im_start|>user\nStatus?<|im_end|>" in chatml
    assert chatml.endswith("<|im_start|>assistant\n")

    # Tool-call JSON mode
    tool_resp = await prov.chat(
        [Message(role="user", content="Find doc")],
        tools=[{"type": "function", "function": {"name": "lookup_doc"}}],
    )
    assert tool_resp.finish_reason == "tool_calls"
    assert len(tool_resp.tool_calls) == 1
    assert tool_resp.tool_calls[0].name == "lookup_doc"
    assert tool_resp.tool_calls[0].arguments == {"query": "zero-libc"}
    assert "--json" in json.loads(args_log.read_text(encoding="utf-8"))

    # Streaming wraps chat cleanly
    chunks = [
        c
        async for c in prov.chat_stream([Message(role="user", content="Stream?")])
    ]
    assert len(chunks) == 1
    assert chunks[0].done is True
    assert chunks[0].content == "awzeal-subprocess-ok"

    # Fail-closed SHA-256 mismatch
    bad_sha_prov = AwzealZeroLibcProvider(
        binary=str(stub_bin),
        model=str(model_file),
        model_sha256="0" * 64,
    )
    assert await bad_sha_prov.health_check() is False
    with pytest.raises(RuntimeError, match="mismatch"):
        await bad_sha_prov.chat([Message(role="user", content="hi")])

    # Fail-closed missing model file when model_sha256 is set
    missing_model_prov = AwzealZeroLibcProvider(
        binary=str(stub_bin),
        model=str(tmp_path / "missing.gguf"),
        model_sha256=model_sha,
    )
    assert await missing_model_prov.health_check() is False
    with pytest.raises(RuntimeError, match="missing"):
        await missing_model_prov.chat([Message(role="user", content="hi")])

    # Nonzero exit code and missing binary raise RuntimeError
    fail_bin = _write_stub_binary(
        tmp_path / "fail-awzeal",
        "import sys; sys.stderr.write('boom'); sys.exit(2)\n",
    )
    fail_prov = AwzealZeroLibcProvider(binary=str(fail_bin), model=str(model_file))
    with pytest.raises(RuntimeError, match="code 2"):
        await fail_prov.chat([Message(role="user", content="hi")])

    missing_bin_prov = AwzealZeroLibcProvider(
        binary=str(tmp_path / "no-such-bin"), model=str(model_file)
    )
    assert await missing_bin_prov.health_check() is False
    with pytest.raises(RuntimeError, match="not found"):
        await missing_bin_prov.chat([Message(role="user", content="hi")])

    # Unconfigured provider raises RuntimeError on chat() and returns False/[]
    unconf = AwzealZeroLibcProvider()
    assert await unconf.health_check() is False
    assert await unconf.list_models() == []
    with pytest.raises(RuntimeError, match="not configured"):
        await unconf.chat([Message(role="user", content="hi")])


@pytest.mark.asyncio
async def test_awzeal_provider_http_loopback_mode_via_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_awzeal_env(monkeypatch)
    seen_requests: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        seen_requests.append((request.method, str(request.url), body))
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200, json={"object": "list", "data": [{"id": "bonsai2-27b"}]}
            )
        if request.url.path == "/v1/chat/completions":
            assert request.headers.get("authorization") == "Bearer test-token"
            return httpx.Response(
                200,
                json={
                    "model": "bonsai2-27b",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "loopback-http-ok",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 4,
                        "total_tokens": 15,
                    },
                },
            )
        return httpx.Response(404)

    prov = AwzealZeroLibcProvider(
        model="bonsai2-27b",
        endpoint="http://127.0.0.1:8091",
        api_key="test-token",
        transport=httpx.MockTransport(handler),
    )
    assert await prov.health_check() is True
    assert await prov.list_models() == ["bonsai2-27b"]
    resp = await prov.chat([Message(role="user", content="Ping")], top_p=0.9)
    assert resp.content == "loopback-http-ok"
    assert resp.model == "bonsai2-27b"
    assert resp.prompt_tokens == 11
    assert resp.completion_tokens == 4
    assert resp.tokens_used == 15
    assert any(path.endswith("/v1/chat/completions") for _, path, _ in seen_requests)

    chunks = [c async for c in prov.chat_stream([Message(role="user", content="Stream")])]
    assert len(chunks) == 1
    assert chunks[0].content == "loopback-http-ok"
    assert chunks[0].done is True


@pytest.mark.parametrize("backend", ["awzeal", "zero-libc"])
def test_bot_runtime_503_unconfigured_and_200_configured_real_awzeal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    _clear_awzeal_env(monkeypatch)
    bot_dir = tmp_path / f"bot-{backend}"
    scaffold_bot(
        f"Real {backend}",
        "gemini" if backend == "awzeal" else "openai",
        bot_dir,
        backend=backend,
        target_surface="awzeal",
        shared_ro_mmap=True,
    )

    app = create_bot_app(bot_dir)
    client = TestClient(app)

    # Unconfigured -> 503 across Gemini, Gemini SSE, OpenAI sync/stream, Assistants runs
    r_gem = client.post(
        "/v1beta/models/bonsai2-27b:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "ping"}]}]},
    )
    assert r_gem.status_code == 503
    assert r_gem.json()["error"]["code"] == 503

    r_gem_stream = client.post(
        "/v1beta/models/bonsai2-27b:streamGenerateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "ping"}]}]},
    )
    assert r_gem_stream.status_code == 503

    r_oai = client.post(
        "/v1/chat/completions",
        json={"model": "qwen3-0.6b", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r_oai.status_code == 503

    r_oai_sse = client.post(
        "/v1/chat/completions",
        json={
            "model": "qwen3-0.6b",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r_oai_sse.status_code == 200
    assert "503" in r_oai_sse.text

    t_id = client.post("/v1/threads", json={}).json()["id"]
    client.post(f"/v1/threads/{t_id}/messages", json={"role": "user", "content": "hi"})
    r_run = client.post(f"/v1/threads/{t_id}/runs", json={})
    assert r_run.status_code == 503

    # Configure stub binary + model -> 200 through real AwzealZeroLibcProvider
    model_file = tmp_path / f"{backend}.gguf"
    model_file.write_bytes(b"GGUF")
    stub_bin = _write_stub_binary(
        tmp_path / f"bin-{backend}",
        f"import sys; _ = sys.stdin.read(); print('real-{backend}-ok')\n",
    )
    monkeypatch.setenv("AWZEAL_BINARY", str(stub_bin))
    monkeypatch.setenv("AWZEAL_MODEL", str(model_file))

    ok_gem = client.post(
        "/v1beta/models/bonsai2-27b:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
    )
    assert ok_gem.status_code == 200
    assert f"real-{backend}-ok" in ok_gem.text

    ok_oai = client.post(
        "/v1/chat/completions",
        json={"model": "qwen3-0.6b", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert ok_oai.status_code == 200
    assert ok_oai.json()["choices"][0]["message"]["content"] == f"real-{backend}-ok"

    # Mismatched model_sha256 in bot.yaml -> 503 fail-closed
    bad_dir = tmp_path / f"bad-sha-{backend}"
    scaffold_bot(
        f"Bad SHA {backend}",
        "gemini",
        bad_dir,
        backend=backend,
        model_sha256="f" * 64,
    )
    bad_client = TestClient(create_bot_app(bad_dir))
    bad_resp = bad_client.post(
        "/v1beta/models/bonsai2-27b:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
    )
    assert bad_resp.status_code == 503


@pytest.mark.parametrize(
    "blocked_url",
    [
        "http://localhost./",
        "http://LOCALHOST.:8080/admin",
        "http://printer.local./status",
        "http://metadata.google.internal./computeMetadata/v1/",
        "http://sub.localhost./api",
    ],
)
def test_validate_action_url_blocks_trailing_dot_even_with_skip_dns(
    blocked_url: str,
) -> None:
    ok, reason = validate_action_url(blocked_url, skip_dns=True)
    assert ok is False, f"Expected SSRF block for {blocked_url!r}, got ok=True"
    assert reason


def test_validate_action_url_allowlist_strips_trailing_dot() -> None:
    ok, _ = validate_action_url(
        "https://api.example.com./v1",
        allowlisted_hosts=["api.example.com"],
        skip_dns=True,
    )
    assert ok is True


@pytest.mark.parametrize(
    ("bad_field", "bad_value"),
    [
        ("temperature", "not-a-float"),
        ("temperature", True),
        ("temperature", "NaN"),
        ("temperature", "Inf"),
        ("temperature", {}),
        ("top_p", "bad"),
        ("top_p", False),
        ("top_p", "nan"),
        ("max_tokens", "bad"),
        ("max_tokens", True),
        ("max_tokens", 1.5),
    ],
)
def test_runtime_rejects_invalid_numeric_sampling_params_with_400(
    tmp_path: Path, bad_field: str, bad_value: object
) -> None:
    bot_dir = tmp_path / "param-bot"
    scaffold_bot("Param Bot", "openai", bot_dir, backend="mock")
    rt = BotRuntime(bot_dir, backend="mock")
    assert rt.manifest.backend == "mock"
    client = TestClient(create_bot_app(bot_dir, backend="mock"))

    # OpenAI sync + stream=True must both return HTTP 400 before SSE starts
    for stream_flag in (False, True):
        r_oai = client.post(
            "/v1/chat/completions",
            json={
                "model": "mock-bot-1",
                "stream": stream_flag,
                "messages": [{"role": "user", "content": "hi"}],
                bad_field: bad_value,
            },
        )
        assert r_oai.status_code == 400, (stream_flag, bad_field, bad_value, r_oai.text)

    # Gemini sync + streamGenerateContent + ?alt=sse must return HTTP 400
    gem_field = {
        "temperature": "temperature",
        "top_p": "topP",
        "max_tokens": "maxOutputTokens",
    }[bad_field]
    for endpoint in (
        "/v1beta/models/mock-bot-1:generateContent",
        "/v1beta/models/mock-bot-1:streamGenerateContent",
        "/v1beta/models/mock-bot-1:streamGenerateContent?alt=sse",
    ):
        r_gem = client.post(
            endpoint,
            json={
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "generationConfig": {gem_field: bad_value},
            },
        )
        assert r_gem.status_code == 400, (endpoint, gem_field, bad_value, r_gem.text)


def test_f16_awseal_signing_and_awrun_confinement_when_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    awseal = pytest.importorskip("awseal")
    awrun_confine = pytest.importorskip("awrun.confine")
    awrun_deploy = pytest.importorskip("awrun.deploy")
    awrun_store = pytest.importorskip("awrun.store")

    key_path = tmp_path / "keys" / "seal.key"
    awrun_dir = tmp_path / "awrun"
    monkeypatch.setenv("AWSEAL_KEY_PATH", str(key_path))
    monkeypatch.setenv("AITHER_AWRUN_DIR", str(awrun_dir))

    awseal.keys.generate(key_path)
    pub_hex = awseal.keys.public_key_hex(path=key_path)
    other_key = tmp_path / "keys" / "other.key"
    awseal.keys.generate(other_key)
    other_pub = awseal.keys.public_key_hex(path=other_key)

    bot_dir = tmp_path / "zeal-bot"
    scaffold_bot(
        "Zeal Bot",
        "gemini",
        bot_dir,
        backend="awzeal",
        model_sha256="c" * 64,
        shared_ro_mmap=True,
        target_surface="awzeal",
    )
    manifest = load_bot_manifest(bot_dir)
    assert derive_bot_egress_hosts(manifest, bot_dir) == []

    out_dir = tmp_path / "dist" / "zeal-bot"
    dep = deploy_bot(bot_dir, target="local", out_dir=out_dir, port=8099)
    assert dep.ok
    assert (out_dir / "awseal.json").is_file()
    assert dep.payload["confinement"]["egress"]["hosts"] == []
    assert awrun_deploy.DeploymentStore(awrun_dir / "deployments").list()

    item = awrun_store.RunItem(
        id="r-f16-test",
        kind="agent",
        spec=build_awrun_job_spec(bot_dir, manifest)["spec"],
        egress=dep.payload["confinement"]["egress"],
    )
    assert awrun_confine.enforcement_gap(item) is None
    wrapped = awrun_confine.wrap_argv(item, ["python3.11", "run_bot.py"])
    assert "--network" in wrapped and "none" in wrapped

    assert verify_bot_bundle(out_dir, expect_key=pub_hex)["ok"] is True
    wrong = verify_bot_bundle(out_dir, expect_key=other_pub)
    assert wrong["ok"] is False and wrong["exit_code"] == 1

    (out_dir / "README.md").write_text("tampered\n", encoding="utf-8")
    tampered = verify_bot_bundle(out_dir, expect_key=pub_hex)
    assert tampered["ok"] is False and tampered["exit_code"] == 1


def test_standalone_boundary_without_awseal_awrun_or_lib_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot_dir = tmp_path / "standalone-zeal"
    scaffold_bot("Standalone Zeal", "openai", bot_dir, backend="zero-libc")

    # Block awseal and awrun completely
    monkeypatch.setitem(sys.modules, "awseal", None)
    monkeypatch.setitem(sys.modules, "awrun", None)
    monkeypatch.setitem(sys.modules, "awrun.deploy", None)
    monkeypatch.setitem(sys.modules, "awrun.store", None)

    out_dir = tmp_path / "dist" / "standalone-zeal"
    dep = deploy_bot(bot_dir, target="local", out_dir=out_dir)
    assert dep.ok
    assert not (out_dir / "awseal.json").exists()
    manifest_file = out_dir / "bundle-manifest.sha256.json"
    assert manifest_file.is_file()
    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest_data["sealed"] is False
    assert manifest_data["format"] == "sha256"
    assert dep.payload["awrun_deployment"]["recorded"] is False

    with zipfile.ZipFile(dep.bundle_zip, "r") as zf:
        names = set(zf.namelist())
    assert "standalone-zeal/awseal.json" not in names
    assert "standalone-zeal/bundle-manifest.sha256.json" in names

    ver = verify_bot_bundle(out_dir)
    assert ver["ok"] is False and ver["exit_code"] == 2

    awrun_fail = deploy_bot(bot_dir, target="awrun", dry_run=False)
    assert awrun_fail.ok is False

    # AST check: zero `lib.*` imports in adk/llm/awzeal.py and adk/bots/*.py
    awdk_root = Path(__file__).resolve().parents[1]
    targets = [awdk_root / "adk" / "llm" / "awzeal.py"] + sorted(
        (awdk_root / "adk" / "bots").glob("*.py")
    )
    for py_file in targets:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not (
                        alias.name == "lib" or alias.name.startswith("lib.")
                    ), f"Forbidden lib import {alias.name} in {py_file}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not (
                    mod == "lib" or mod.startswith("lib.")
                ), f"Forbidden from {mod} import in {py_file}"


def test_awzeal_model_path_precedence_with_awzeal_model_and_runtime_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_awzeal_env(monkeypatch)
    model_file = tmp_path / "bonsai2.gguf"
    model_bytes = b"GGUF-PRECEDENCE-PAYLOAD"
    model_file.write_bytes(model_bytes)
    model_sha = hashlib.sha256(model_bytes).hexdigest()

    args_log = tmp_path / "argv.json"
    stub_bin = _write_stub_binary(
        tmp_path / "stub-awzeal-prec",
        f"""
import json, sys
from pathlib import Path
_ = sys.stdin.read()
Path({str(args_log)!r}).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
print("precedence-ok")
""",
    )

    monkeypatch.setenv("AWZEAL_BINARY", str(stub_bin))
    monkeypatch.setenv("AWZEAL_MODEL", "bonsai2-27b")
    monkeypatch.setenv("AWZEAL_MODEL_PATH", str(model_file))

    prov = AwzealZeroLibcProvider(model_sha256=model_sha)
    assert prov.model_path == str(model_file)
    assert prov.model == "bonsai2-27b"
    assert prov.default_model == "bonsai2-27b"
    prov.verify_model_sha256()

    bot_dir = tmp_path / "bot-prec"
    scaffold_bot(
        "Precedence Bot",
        "gemini",
        bot_dir,
        backend="awzeal",
        model="bonsai2-27b",
        model_sha256=model_sha,
    )
    client = TestClient(create_bot_app(bot_dir))
    resp = client.post(
        "/v1beta/models/bonsai2-27b:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    )
    assert resp.status_code == 200, resp.text
    assert "precedence-ok" in resp.text
    argv = json.loads(args_log.read_text(encoding="utf-8"))
    assert argv[0] == str(model_file)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX os.kill(pid, 0) liveness probe and #! stub",
)
@pytest.mark.asyncio
async def test_awzeal_subprocess_timeout_kills_and_reaps_child_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_awzeal_env(monkeypatch)
    model_file = tmp_path / "sleep.gguf"
    model_file.write_bytes(b"GGUF")
    pid_file = tmp_path / "child.pid"

    slow_bin = tmp_path / "slow-awzeal"
    if shutil.which("sh"):
        slow_bin.write_text(
            "#!/bin/sh\n"
            "trap '' TERM\n"
            f"printf '%s\\n' \"$$\" > {shlex.quote(str(pid_file))}\n"
            "exec sleep 10\n",
            encoding="utf-8",
        )
    else:
        slow_bin.write_text(
            f"#!{sys.executable} -S\n"
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"with open({str(pid_file)!r}, 'w', encoding='utf-8') as fh:\n"
            "    fh.write(str(os.getpid()))\n"
            "time.sleep(10)\n",
            encoding="utf-8",
        )
    slow_bin.chmod(0o755)

    spawned_procs: list[asyncio.subprocess.Process] = []
    orig_create_exec = asyncio.create_subprocess_exec

    async def _spy_create_exec(
        *args: object, **kwargs: object
    ) -> asyncio.subprocess.Process:
        proc = await orig_create_exec(*args, **kwargs)
        spawned_procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy_create_exec)

    prov = AwzealZeroLibcProvider(
        binary=str(slow_bin),
        model=str(model_file),
        timeout=1.5,
    )
    with pytest.raises(RuntimeError, match="timed out"):
        await prov.chat([Message(role="user", content="hang")])

    assert len(spawned_procs) == 1
    assert pid_file.is_file()
    child_pid = int(pid_file.read_text(encoding="utf-8").strip())
    assert spawned_procs[0].pid == child_pid
    assert spawned_procs[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX grandchild pipe inheritance and #! stub",
)
@pytest.mark.asyncio
async def test_awzeal_subprocess_timeout_does_not_hang_on_open_grandchild_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_awzeal_env(monkeypatch)
    model_file = tmp_path / "grandchild.gguf"
    model_file.write_bytes(b"GGUF")
    pid_file = tmp_path / "parent.pid"
    gc_pid_file = tmp_path / "grandchild.pid"

    stub_bin = tmp_path / "grandchild-awzeal"
    if shutil.which("sh"):
        stub_bin.write_text(
            "#!/bin/sh\n"
            "trap '' TERM\n"
            "sleep 6 &\n"
            f"printf '%s\\n' \"$!\" > {shlex.quote(str(gc_pid_file))}\n"
            f"printf '%s\\n' \"$$\" > {shlex.quote(str(pid_file))}\n"
            "exec sleep 10\n",
            encoding="utf-8",
        )
    else:
        stub_bin.write_text(
            f"#!{sys.executable} -S\n"
            "import os, signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "gc = subprocess.Popen([sys.executable, '-S', '-c', 'import time; time.sleep(6)'])\n"
            f"with open({str(gc_pid_file)!r}, 'w', encoding='utf-8') as fh:\n"
            "    fh.write(str(gc.pid))\n"
            f"with open({str(pid_file)!r}, 'w', encoding='utf-8') as fh:\n"
            "    fh.write(str(os.getpid()))\n"
            "time.sleep(10)\n",
            encoding="utf-8",
        )
    stub_bin.chmod(0o755)

    spawned_procs: list[asyncio.subprocess.Process] = []
    orig_create_exec = asyncio.create_subprocess_exec

    async def _spy_create_exec(
        *args: object, **kwargs: object
    ) -> asyncio.subprocess.Process:
        proc = await orig_create_exec(*args, **kwargs)
        spawned_procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy_create_exec)

    prov = AwzealZeroLibcProvider(
        binary=str(stub_bin),
        model=str(model_file),
        timeout=1.0,
    )
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            await asyncio.wait_for(
                prov.chat([Message(role="user", content="hang-with-grandchild")]),
                timeout=4.0,
            )

        assert len(spawned_procs) == 1
        assert pid_file.is_file()
        parent_pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert spawned_procs[0].pid == parent_pid
        assert spawned_procs[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(parent_pid, 0)
    finally:
        if gc_pid_file.is_file():
            try:
                gc_pid = int(gc_pid_file.read_text(encoding="utf-8").strip())
                os.kill(gc_pid, signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


@pytest.mark.asyncio
async def test_awzeal_subprocess_empty_tool_calls_unwraps_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_awzeal_env(monkeypatch)
    model_file = tmp_path / "tools.gguf"
    model_file.write_bytes(b"GGUF")

    stub_bin = _write_stub_binary(
        tmp_path / "empty-tc-awzeal",
        """
import json, sys
_ = sys.stdin.read()
print(json.dumps({"content": "plain answer", "tool_calls": []}))
""",
    )

    prov = AwzealZeroLibcProvider(binary=str(stub_bin), model=str(model_file))
    resp = await prov.chat(
        [Message(role="user", content="Just answer")],
        tools=[{"type": "function", "function": {"name": "lookup_doc"}}],
    )
    assert resp.content == "plain answer"
    assert resp.tool_calls == []
    assert resp.finish_reason == "stop"


def test_host_port_from_url_normalizes_trailing_dot() -> None:
    assert _host_port_from_url("https://localhost./api") is None
    assert _host_port_from_url("https://api.example.com./v1") == "api.example.com"


def test_awzeal_rejects_leading_dash_and_non_model_host_file_coercion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_awzeal_env(monkeypatch)
    stub_bin = _write_stub_binary(
        tmp_path / "stub-awzeal",
        "import sys; print('should-not-run')\n",
    )
    with pytest.raises(ValueError, match="start with '-'"):
        AwzealZeroLibcProvider(binary=str(stub_bin), model="-help")

    non_model = tmp_path / "secret.txt"
    non_model.write_text("TOP-SECRET", encoding="utf-8")
    with pytest.raises(ValueError, match="not an allowed model file path"):
        AwzealZeroLibcProvider(binary=str(stub_bin), model=str(non_model))

    # BotRuntime must fail closed with 503 instead of coercing manifest.model to a host path
    real_gguf = tmp_path / "host.gguf"
    real_gguf.write_bytes(b"GGUF")
    bot_dir = tmp_path / "coercion-bot"
    scaffold_bot("Coercion Bot", "gemini", bot_dir, backend="awzeal", model=str(real_gguf))
    monkeypatch.setenv("AWZEAL_BINARY", str(stub_bin))
    resp = TestClient(create_bot_app(bot_dir)).post(
        "/v1beta/models/local:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "ping"}]}]},
    )
    assert resp.status_code == 503


