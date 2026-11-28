"""Tests for bot onboarding (BYOK key store), local/discord/fleet deployment, and bot CLI."""

from __future__ import annotations

import ast
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
import yaml

from adk.bots.cli_entry import bot_main
from adk.bots.deploy import (
    BotDeployerBackend,
    build_awrun_job_spec,
    deploy_bot,
    derive_bot_egress_hosts,
    verify_bot_bundle,
)
from adk.bots.manifest import load_bot_manifest, scaffold_bot
from adk.bots.onboard import (
    load_provider_keys,
    provider_keys_path,
    resolve_provider_key,
    run_bot_onboard,
    save_provider_key,
)


def test_byok_key_store_isolation_and_gemini_google_normalization(tmp_path: Path) -> None:
    # conftest sets AITHER_DATA_DIR inside tmp_path/.aither
    kp = provider_keys_path()
    assert str(tmp_path) in str(kp)

    fake_gem = "AIzaSyFakeKeyForTesting1234567890123456"
    saved = save_provider_key("google", fake_gem)
    assert saved == kp
    if os.name == "posix":
        mode = stat.S_IMODE(saved.stat().st_mode)
        assert mode == 0o600

    loaded = load_provider_keys()
    assert loaded["gemini"] == fake_gem
    assert resolve_provider_key("gemini") == fake_gem
    assert resolve_provider_key("google") == fake_gem


def test_run_bot_onboard_never_writes_key_to_bot_yaml(tmp_path: Path) -> None:
    bot_dir = tmp_path / "onboarded-gem"
    fake_key = "AIzaSyFakeOnboardKey1234567890123456789"
    res = run_bot_onboard(
        name="Onboarded Gem",
        style="gemini",
        target_dir=bot_dir,
        backend="gemini",
        api_key=fake_key,
        non_interactive=True,
    )
    assert res["ok"] is True
    assert res["key_saved"] is True
    assert res["validation"].ok

    # Secret MUST be in provider_keys.json and MUST NOT appear anywhere in bot_dir
    assert resolve_provider_key("gemini") == fake_key
    for fp in bot_dir.rglob("*"):
        if fp.is_file():
            assert fake_key not in fp.read_text(encoding="utf-8", errors="ignore")


def test_run_bot_onboard_interactive_prompts(tmp_path: Path) -> None:
    bot_dir = tmp_path / "interactive-gpt"
    answers = iter(["openai", "Interactive GPT", "openai", "", "Be concise."])
    fake_key = "sk-proj-InteractiveFakeKey1234567890abcdef"
    res = run_bot_onboard(
        target_dir=bot_dir,
        backend="",
        non_interactive=False,
        input_fn=lambda _p: next(answers),
        secret_fn=lambda _p: fake_key,
    )
    assert res["ok"] is True
    assert res["style"] == "openai"
    assert resolve_provider_key("openai") == fake_key


def test_deploy_local_bundle_and_refuses_leaky_bot(tmp_path: Path) -> None:
    bot_dir = tmp_path / "local-deploy-bot"
    run_bot_onboard(
        name="Local Deploy Bot",
        style="openai",
        target_dir=bot_dir,
        backend="mock",
        non_interactive=True,
    )

    out_dir = tmp_path / "bundle-out" / "local-deploy-bot"
    res = deploy_bot(bot_dir, target="local", out_dir=out_dir, port=8097)
    assert res.ok, res.error
    assert res.output_path == out_dir
    for fname in (
        "bot.yaml",
        "tools.py",
        "run_bot.py",
        "Dockerfile",
        "docker-compose.yml",
        "requirements.txt",
        ".env.example",
        "README.md",
        "local-deploy-bot.service",
    ):
        assert (out_dir / fname).is_file(), f"missing {fname}"

    run_src = (out_dir / "run_bot.py").read_text(encoding="utf-8")
    ast.parse(run_src)
    assert "from lib." not in run_src

    assert res.bundle_zip is not None and res.bundle_zip.is_file()
    with zipfile.ZipFile(res.bundle_zip, "r") as zf:
        names = set(zf.namelist())
        assert "local-deploy-bot/bot.yaml" in names
        assert "local-deploy-bot/run_bot.py" in names

    # Injecting a secret causes deploy_bot to refuse!
    (bot_dir / "knowledge" / "leak.txt").write_text(
        "AIzaSyLeakedKeyForDeployRefusal1234567890", encoding="utf-8"
    )
    bad_res = deploy_bot(bot_dir, target="local", out_dir=tmp_path / "bad-bundle")
    assert not bad_res.ok
    assert "BOT007" in bad_res.error


def test_deploy_discord_and_fleet_r4_offline(tmp_path: Path) -> None:
    bot_dir = tmp_path / "fleet-bot"
    run_bot_onboard(
        name="Fleet Bot",
        style="gemini",
        target_dir=bot_dir,
        backend="mock",
        non_interactive=True,
    )

    # Discord dry_run prepares identity & offline invite_url
    disc_out = tmp_path / "discord-identities"
    d_res = deploy_bot(
        bot_dir,
        target="discord",
        out_dir=disc_out,
        discord_token="123456789012345678.abc.def",
        dry_run=True,
    )
    assert d_res.ok
    assert "client_id=123456789012345678" in d_res.payload["invite_url"]
    assert (disc_out / "fleet-bot.yaml").is_file()
    ident_data = yaml.safe_load((disc_out / "fleet-bot.yaml").read_text(encoding="utf-8"))
    assert ident_data["name"] == "fleet-bot"

    # Fleet dry_run writes preview file with zero network calls
    fleet_out = tmp_path / "fleet-preview"
    f_dry = deploy_bot(bot_dir, target="fleet", out_dir=fleet_out, port=8099, dry_run=True)
    assert f_dry.ok
    assert (fleet_out / "fleet-registration.json").is_file()
    reg = f_dry.payload["registration"]
    assert reg["agent_type"] == "adk-bot"
    assert reg["bot_style"] == "gemini"

    # Fleet emulated registration via injected httpx.Client(MockTransport)
    seen_reqs: list[dict] = []

    def mock_fleet_handler(request: httpx.Request) -> httpx.Response:
        seen_reqs.append(
            {
                "url": str(request.url),
                "body": json.loads(request.content.decode("utf-8")),
            }
        )
        return httpx.Response(200, json={"registered": True, "id": "fleet-bot"})

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_fleet_handler))
    f_live = deploy_bot(
        bot_dir,
        target="fleet",
        port=8099,
        dry_run=False,
        fleet_url="http://127.0.0.1:9999",
        token="test-token",
        http_client=mock_client,
    )
    assert f_live.ok, f_live.error
    assert len(seen_reqs) == 1
    assert seen_reqs[0]["url"].endswith("/api/genesis/v1/agent/fleet/register")
    assert seen_reqs[0]["body"]["name"] == "fleet-bot"


def test_bot_cli_main_verbs(tmp_path: Path, capsys) -> None:
    bot_dir = tmp_path / "cli-bot"
    rc_new = bot_main(
        [
            "new",
            "CLI Bot",
            "--style",
            "gemini",
            "--dir",
            str(bot_dir),
            "--backend",
            "mock",
        ]
    )
    assert rc_new == 0
    capsys.readouterr()

    rc_val = bot_main(["validate", str(bot_dir), "--json"])
    assert rc_val == 0
    val_out = json.loads(capsys.readouterr().out)
    assert val_out["ok"] is True

    rc_chat = bot_main(["chat", str(bot_dir), "-m", "Hello CLI", "--json"])
    assert rc_chat == 0
    chat_out = json.loads(capsys.readouterr().out)
    assert "Hello CLI" in chat_out["text"]

    out_bundle = tmp_path / "cli-bundle"
    rc_dep = bot_main(
        [
            "deploy",
            str(bot_dir),
            "--target",
            "local",
            "--out",
            str(out_bundle),
            "--json",
        ]
    )
    assert rc_dep == 0
    dep_out = json.loads(capsys.readouterr().out)
    assert dep_out["ok"] is True


def test_systemd_unit_uses_python311_and_onboard_matches_scaffold_default(
    tmp_path: Path,
) -> None:
    bot_dir = tmp_path / "mock-gem-onboard"
    res = run_bot_onboard(
        name="Mock Gem Onboard",
        style="gemini",
        target_dir=bot_dir,
        backend="mock",
        non_interactive=True,
    )
    assert res["ok"] is True
    assert res["model"] == "mock-bot-1"

    out_dir = tmp_path / "svc-bundle" / "mock-gem-onboard"
    dep = deploy_bot(bot_dir, target="local", out_dir=out_dir, port=8096)
    assert dep.ok
    svc_text = (out_dir / "mock-gem-onboard.service").read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/env python3.11 run_bot.py" in svc_text


def test_awseal_bundle_signing_verification_tamper_and_missing_fallback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    awseal = pytest.importorskip("awseal")
    bot_dir = tmp_path / "sealed-bot"
    scaffold_bot("Sealed Bot", "gemini", bot_dir, backend="mock")

    # 1. Without signing key -> honest SHA-256 fallback, NEVER forged awseal.json
    missing_key = tmp_path / "no-such-key.pem"
    monkeypatch.setenv("AWSEAL_KEY_PATH", str(missing_key))
    unsealed_dir = tmp_path / "unsealed-out" / "sealed-bot"
    res_unsealed = deploy_bot(bot_dir, target="local", out_dir=unsealed_dir)
    assert res_unsealed.ok
    assert res_unsealed.payload["seal"]["sealed"] is False
    assert res_unsealed.payload["seal"]["format"] == "sha256"
    assert not (unsealed_dir / "awseal.json").exists()
    assert (unsealed_dir / "bundle-manifest.sha256.json").is_file()
    ver_unsealed = verify_bot_bundle(unsealed_dir)
    assert ver_unsealed["ok"] is False
    assert ver_unsealed["exit_code"] == 2

    # 2. With Ed25519 key -> awseal.json created, zipped, and passes verify with expect_key
    key_path = tmp_path / "keys" / "signing.key"
    awseal.keys.generate(key_path)
    pub_hex = awseal.keys.public_key_hex(path=key_path)
    monkeypatch.setenv("AWSEAL_KEY_PATH", str(key_path))

    sealed_out = tmp_path / "sealed-out" / "sealed-bot"
    res_sealed = deploy_bot(bot_dir, target="local", out_dir=sealed_out)
    assert res_sealed.ok
    assert res_sealed.payload["seal"]["sealed"] is True
    assert (sealed_out / "awseal.json").is_file()
    with zipfile.ZipFile(res_sealed.bundle_zip, "r") as zf:
        assert "sealed-bot/awseal.json" in set(zf.namelist())

    ver_ok = verify_bot_bundle(sealed_out, expect_key=pub_hex)
    assert ver_ok["ok"] is True
    assert ver_ok["exit_code"] == 0
    assert ver_ok["key_trusted"] is True

    # Wrong publisher key fails closed
    other_key = tmp_path / "keys" / "other.key"
    awseal.keys.generate(other_key)
    other_pub = awseal.keys.public_key_hex(path=other_key)
    ver_wrong = verify_bot_bundle(sealed_out, expect_key=other_pub)
    assert ver_wrong["ok"] is False
    assert ver_wrong["exit_code"] == 1
    assert ver_wrong["key_trusted"] is False

    # CLI verify subcommand exit code 0
    assert bot_main(["verify", str(sealed_out), "--key", pub_hex, "--json"]) == 0
    cli_ver = json.loads(capsys.readouterr().out)
    assert cli_ver["ok"] is True

    # Tampering any file invalidates verification
    (sealed_out / "README.md").write_text("tampered!", encoding="utf-8")
    ver_tampered = verify_bot_bundle(sealed_out, expect_key=pub_hex)
    assert ver_tampered["ok"] is False
    assert ver_tampered["exit_code"] == 1
    assert ver_tampered["content_ok"] is False
    assert bot_main(["verify", str(sealed_out), "--key", pub_hex]) == 1

    # 3. Missing awseal package -> honest SHA-256 fallback & fail-closed verify
    monkeypatch.setitem(sys.modules, "awseal", None)
    no_pkg_out = tmp_path / "no-awseal-pkg" / "sealed-bot"
    res_no_pkg = deploy_bot(bot_dir, target="local", out_dir=no_pkg_out)
    assert res_no_pkg.ok
    assert res_no_pkg.payload["seal"]["sealed"] is False
    assert res_no_pkg.payload["seal"]["reason"] == "awseal-not-installed"
    assert not (no_pkg_out / "awseal.json").exists()
    ver_no_pkg = verify_bot_bundle(sealed_out)
    assert ver_no_pkg["ok"] is False
    assert ver_no_pkg["exit_code"] == 2


def test_awrun_egress_deployment_store_confine_and_zerolibc_bundle(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    awrun_deploy = pytest.importorskip("awrun.deploy")
    awrun_store = pytest.importorskip("awrun.store")
    awrun_confine = pytest.importorskip("awrun.confine")

    awrun_dir = tmp_path / "awrun-root"
    monkeypatch.setenv("AITHER_AWRUN_DIR", str(awrun_dir))

    # Zero-libc / awzeal bot emits empty egress.hosts -> passes enforcement_gap
    digest = "b" * 64
    zero_dir = tmp_path / "zero-bot"
    scaffold_bot(
        "Zero Bot",
        "gemini",
        zero_dir,
        backend="awzeal",
        model_sha256=digest,
        shared_ro_mmap=True,
        target_surface="awzeal",
    )
    zero_manifest = load_bot_manifest(zero_dir)
    assert derive_bot_egress_hosts(zero_manifest, zero_dir) == []

    # Cloud bot derives expected cloud egress host
    oai_dir = tmp_path / "oai-bot"
    scaffold_bot("Cloud GPT", "openai", oai_dir, backend="openai")
    assert derive_bot_egress_hosts(load_bot_manifest(oai_dir), oai_dir) == [
        "api.openai.com"
    ]

    job_spec = build_awrun_job_spec(zero_dir, zero_manifest, port=8098)
    assert job_spec["egress"] == {"hosts": []}
    run_item = awrun_store.RunItem(
        id="r-test1234",
        kind=job_spec["kind"],
        spec=job_spec["spec"],
        paths=job_spec["paths"],
        lineage=job_spec["lineage"],
        egress=job_spec["egress"],
    )
    assert awrun_confine.enforcement_gap(run_item) is None

    # Local deploy writes zero-libc .env.example, bundle-metadata.json & DeploymentStore
    zero_bundle = tmp_path / "zero-dist" / "zero-bot"
    res_local = deploy_bot(zero_dir, target="local", out_dir=zero_bundle, port=8098)
    assert res_local.ok
    assert res_local.payload["runtime_tier"] == "zero-libc"
    assert res_local.payload["target_surface"] == "awzeal"
    assert res_local.payload["confinement"] == {"egress": {"hosts": []}}
    assert res_local.payload["awrun_deployment"]["recorded"] is True

    env_example = (zero_bundle / ".env.example").read_text(encoding="utf-8")
    assert "AWZEAL_BINARY=" in env_example
    assert "ASM_LLM_BINARY=" in env_example
    assert "AWZEAL_MODEL=" in env_example
    assert f"AWZEAL_MODEL_SHA256={digest}" in env_example
    assert "AWZEAL_SHARED_RO_MMAP=1" in env_example

    meta = json.loads((zero_bundle / "bundle-metadata.json").read_text(encoding="utf-8"))
    assert meta["runtime_tier"] == "zero-libc"
    assert meta["confinement"] == {"egress": {"hosts": []}}

    ps_rows = awrun_deploy.ps(probe=False)
    assert any(r["agent"] == "zero-bot" for r in ps_rows)

    # target="awrun" dry_run=True is pure (no RunStore item queued)
    dry_awrun = deploy_bot(zero_dir, target="awrun", dry_run=True, port=8098)
    assert dry_awrun.ok
    assert dry_awrun.payload["dry_run"] is True
    assert dry_awrun.payload["egress_hosts"] == []
    assert awrun_store.RunStore().list() == []

    # target="awrun" dry_run=False submits to RunStore
    rc_awrun = bot_main(
        ["deploy", str(zero_dir), "--target", "awrun", "--no-dry-run", "--json"]
    )
    assert rc_awrun == 0
    awrun_out = json.loads(capsys.readouterr().out)
    assert awrun_out["ok"] is True
    assert awrun_out["payload"]["run_id"].startswith("r-")
    queued_items = awrun_store.RunStore().list()
    assert len(queued_items) == 1
    assert queued_items[0].egress == {"hosts": []}

    # BotDeployerBackend duck-typed plugin works cleanly
    backend = BotDeployerBackend(out_dir=tmp_path / "plugin-out", port=8099)
    plug_res = backend.deploy(str(zero_dir), "local")
    assert plug_res["ok"] is True
    assert backend.status({"status": "deployed"})["source"] == "awdk.bots"
    assert backend.teardown({"id": "d-1234abcd"})["remote_touched"] is False

    # Missing awrun degrades cleanly for local, fails cleanly for target="awrun"
    monkeypatch.setitem(sys.modules, "awrun", None)
    monkeypatch.setitem(sys.modules, "awrun.deploy", None)
    monkeypatch.setitem(sys.modules, "awrun.store", None)
    res_no_awrun = deploy_bot(
        zero_dir, target="local", out_dir=tmp_path / "no-awrun-out" / "zero-bot"
    )
    assert res_no_awrun.ok
    assert res_no_awrun.payload["awrun_deployment"]["recorded"] is False

    res_awrun_fail = deploy_bot(zero_dir, target="awrun", dry_run=False)
    assert not res_awrun_fail.ok
    assert "awrun" in res_awrun_fail.error.lower()
