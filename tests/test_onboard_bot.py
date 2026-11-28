"""Tests for M3.2 awdk wiring: CLI, server, Discord bridge, and create_flow BotKind."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from adk import cli
from adk.bots.manifest import load_bot_manifest, scaffold_bot
from adk.bots.onboard import load_provider_keys, provider_keys_path
from adk.bots.runtime import BotRuntime, MockBotBackend
from adk.create_flow import CreateEngine, PlanStore, Principal
from adk.create_flow.kinds import KINDS, infer_kind
from adk.onboard_discord import (
    _BotDiscordAdapter,
    agent_reply,
    onboard_bot_to_discord,
    onboard_discord,
)
from adk.server import create_app


class _FakeMemory:
    name = "fake-mem"

    def __init__(self) -> None:
        self.rows: list[str] = []

    async def remember(self, category: str, content: str) -> list[str]:
        self.rows.append(content)
        return [self.name]

    async def recall(self, query: str, category: str = "") -> list[str]:
        return [r for r in self.rows if query.split("=")[0] in r]


# ---------------------------------------------------------------------------
# 1. CLI manifest, passthrough verbs, `adk bot`, `adk rc`, `adk run --bot`
# ---------------------------------------------------------------------------


def test_cli_manifest_registers_bot_and_extended_rc() -> None:
    manifest = {c["name"]: c for c in cli.build_command_manifest()}

    assert "bot" in manifest
    bot_subs = {sc["name"] for sc in manifest["bot"]["subcommands"]}
    assert {"new", "validate", "dev", "chat", "verify", "deploy"} <= bot_subs
    assert "bot" in dict(cli._CLI_HELP_GROUPS)["Agents, Packs & Tools"]

    assert "rc" in manifest
    rc_flags = {f for a in manifest["rc"]["args"] for f in (a.get("flags") or [])}
    assert {
        "--name",
        "--node-class",
        "--harness-url",
        "--token-ttl-days",
        "--once",
        "--runtime-tier",
        "--zero-libc",
    } <= rc_flags
    rc_subs = {sc["name"] for sc in manifest["rc"]["subcommands"]}
    assert {"status", "off"} <= rc_subs

    assert "rc" not in cli._PASSTHROUGH_VERBS
    assert cli._PASSTHROUGH_VERBS["bot"] == ("adk.bots.cli_entry", "bot_main")


def test_cli_bot_passthrough_new_validate_chat_verify_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bot_dir = tmp_path / "gem-cli-bot"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adk",
            "bot",
            "new",
            "Gem CLI Bot",
            "--style",
            "gemini",
            "--dir",
            str(bot_dir),
            "--backend",
            "mock",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert (bot_dir / "bot.yaml").is_file()

    capsys.readouterr()
    monkeypatch.setattr(
        sys,
        "argv",
        ["adk", "bot", "validate", str(bot_dir), "--json"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    val_out = json.loads(capsys.readouterr().out)
    assert val_out["ok"] is True

    monkeypatch.setattr(
        sys,
        "argv",
        ["adk", "bot", "chat", str(bot_dir), "-m", "Hello bot", "--backend", "mock", "--json"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    chat_out = json.loads(capsys.readouterr().out)
    assert chat_out["text"]

    # Unsigned source dir fails closed on `adk bot verify`
    monkeypatch.setattr(
        sys,
        "argv",
        ["adk", "bot", "verify", str(bot_dir), "--json"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code != 0
    ver_out = json.loads(capsys.readouterr().out)
    assert ver_out["ok"] is False

    out_dir = tmp_path / "dist"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adk",
            "bot",
            "deploy",
            str(bot_dir),
            "--target",
            "local",
            "--out",
            str(out_dir),
            "--json",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    dep_out = json.loads(capsys.readouterr().out)
    assert dep_out["ok"] is True
    assert Path(dep_out["output_path"]).is_dir()


def test_cli_rc_forwarding_status_off_and_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adk import rc as rc_mod

    seen: list[list[str] | None] = []

    def fake_rc_main(argv=None) -> int:
        seen.append(list(argv) if argv is not None else None)
        return 0

    monkeypatch.setattr(rc_mod, "main", fake_rc_main)

    for argv in (
        ["adk", "rc", "status", "--json"],
        ["adk", "rc", "off", "--harness-url", "http://127.0.0.1:9999"],
        ["adk", "rc", "--harness-url", "http://127.0.0.1:9999", "status", "--json"],
        ["adk", "rc", "--harness-url=http://127.0.0.1:9999", "status", "--json"],
        ["adk", "rc", "--harness-url", "http://127.0.0.1:9999", "off"],
        ["adk", "rc", "status", "--harness-url", "http://127.0.0.1:9999", "--json"],
        ["adk", "rc", "--name", "dev-box", "--node-class", "laptop", "--once"],
        ["adk", "rc", "--runtime-tier", "awzeal", "--zero-libc", "--once"],
        ["adk", "rc", "--future-flag", "val", "--once"],
        ["adk", "rc", "future-subcmd", "--foo"],
        ["adk", "rc", "--name", "status", "--once"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0

    assert seen == [
        ["status", "--json"],
        ["off", "--harness-url", "http://127.0.0.1:9999"],
        ["status", "--harness-url", "http://127.0.0.1:9999", "--json"],
        ["status", "--harness-url=http://127.0.0.1:9999", "--json"],
        ["off", "--harness-url", "http://127.0.0.1:9999"],
        ["status", "--harness-url", "http://127.0.0.1:9999", "--json"],
        ["--name", "dev-box", "--node-class", "laptop", "--once"],
        ["--runtime-tier", "awzeal", "--zero-libc", "--once"],
        ["--future-flag", "val", "--once"],
        ["future-subcmd", "--foo"],
        ["--name", "status", "--once"],
    ]


def test_cli_rc_flags_before_subcommand_and_help_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv("AITHER_RC_STATE_FILE", str(tmp_path / "rc-state.json"))
    monkeypatch.setenv("AITHER_OFFLINE", "1")
    monkeypatch.delenv("AITHER_HARNESS_URL", raising=False)
    monkeypatch.delenv("AITHER_HARNESS_TOKEN", raising=False)

    # `adk rc --help` lists status and off subcommands
    monkeypatch.setattr(sys, "argv", ["adk", "rc", "--help"])
    with pytest.raises(SystemExit) as exc_help:
        cli.main()
    assert exc_help.value.code == 0
    help_out = capsys.readouterr().out
    assert "--name" in help_out
    assert "--harness-url" in help_out
    assert "--runtime-tier" in help_out
    assert "--zero-libc" in help_out
    assert "status" in help_out
    assert "off" in help_out

    # `adk rc status --help` and `adk rc off --help` succeed
    for subcmd in ("status", "off"):
        monkeypatch.setattr(sys, "argv", ["adk", "rc", subcmd, "--help"])
        with pytest.raises(SystemExit) as exc_sub:
            cli.main()
        assert exc_sub.value.code == 0
        capsys.readouterr()

    # Flag BEFORE `status` subcommand reaches real adk.rc.main with harness_url preserved
    monkeypatch.setattr(
        sys,
        "argv",
        ["adk", "rc", "--harness-url", "http://127.0.0.1:9999", "status", "--json"],
    )
    with pytest.raises(SystemExit) as exc_st:
        cli.main()
    assert exc_st.value.code == 0
    st = json.loads(capsys.readouterr().out)
    assert st["harness_url"] == "http://127.0.0.1:9999"
    assert st["active"] is False

    # Flag BEFORE `off` subcommand reaches real adk.rc.main and exits 0
    monkeypatch.setattr(
        sys,
        "argv",
        ["adk", "rc", "--harness-url", "http://127.0.0.1:9999", "off"],
    )
    with pytest.raises(SystemExit) as exc_off:
        cli.main()
    assert exc_off.value.code == 0
    off_out = capsys.readouterr().out
    assert "stopped" in off_out.lower() or "not running" in off_out.lower()

    # `--runtime-tier awzeal --zero-libc --once` reaches real adk.rc.main end-to-end
    from adk import fleet_enroll
    from adk import rc as rc_mod
    from adk.harnesses import daemon as daemon_mod

    monkeypatch.delenv("AITHER_RUNTIME_TIER", raising=False)
    monkeypatch.delenv("AITHER_ZERO_LIBC", raising=False)
    monkeypatch.setattr(rc_mod, "_signed_in", lambda: True)
    monkeypatch.setattr(rc_mod, "probe_harness", lambda *_a, **_k: (True, "0 session(s)"))
    monkeypatch.setattr(daemon_mod, "mint_scoped_token", lambda *_a, **_k: "scoped-tok")

    async def _fake_enroll(**_kw: Any) -> dict[str, Any]:
        return {"enrolled": True, "node_id": "n-cli-e2e"}

    monkeypatch.setattr(fleet_enroll, "enroll_on_boot", _fake_enroll)
    monkeypatch.setattr(fleet_enroll, "active_node_link", lambda: None)

    monkeypatch.setattr(
        sys,
        "argv",
        ["adk", "rc", "--runtime-tier", "awzeal", "--zero-libc", "--once"],
    )
    with pytest.raises(SystemExit) as exc_tier:
        cli.main()
    assert exc_tier.value.code == 0
    capsys.readouterr()
    st_tier = json.loads((tmp_path / "rc-state.json").read_text(encoding="utf-8"))
    assert st_tier["runtime_tier"] == "awzeal"
    assert st_tier["zero_libc"] is True


def test_cli_run_forwards_bot_flags_to_server_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adk import server as server_mod

    bot_dir = scaffold_bot("Run Flag Bot", "openai", tmp_path / "run-bot", backend="mock")
    captured: dict[str, Any] = {}

    def fake_server_main(agent=None) -> None:
        captured["agent"] = agent
        captured["argv"] = list(sys.argv)

    monkeypatch.setattr(server_mod, "main", fake_server_main)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adk",
            "run",
            "--bot",
            str(bot_dir),
            "--bot-backend",
            "mock",
            "--bot-prefix",
            "/bots/run",
            "--port",
            "8199",
        ],
    )
    cli.main()
    assert captured["agent"] is None
    assert "--bot" in captured["argv"]
    assert str(bot_dir) in captured["argv"]
    assert "--bot-backend" in captured["argv"]
    assert "mock" in captured["argv"]
    assert "--bot-prefix" in captured["argv"]
    assert "/bots/run" in captured["argv"]


# ---------------------------------------------------------------------------
# 2. `adk onboard --bot` / `--gemini-bot` / `--openai-bot` + BYOK isolation
# ---------------------------------------------------------------------------


def test_onboard_bot_styles_and_byok_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gem_dir = tmp_path / "onboard-gem"
    oai_dir = tmp_path / "onboard-oai"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adk",
            "onboard",
            "--gemini-bot",
            "--non-interactive",
            "--name",
            "Research Gem",
            "--dir",
            str(gem_dir),
            "--backend",
            "gemini",
            "--provider-key",
            "gem-secret-for-test-only",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Configured gemini bot" in out
    assert "gem-secret-for-test-only" not in out

    manifest = load_bot_manifest(gem_dir)
    assert manifest.style == "gemini"
    assert "gem-secret-for-test-only" not in (gem_dir / "bot.yaml").read_text(encoding="utf-8")
    assert "gem-secret-for-test-only" not in (gem_dir / ".env.example").read_text(encoding="utf-8")

    keys = load_provider_keys()
    assert keys["gemini"] == "gem-secret-for-test-only"
    assert (provider_keys_path().stat().st_mode & 0o777) == 0o600

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adk",
            "onboard",
            "--openai-bot",
            "--non-interactive",
            "--name",
            "Ops GPT",
            "--dir",
            str(oai_dir),
            "--backend",
            "mock",
            "--target-surface",
            "awzeal",
            "--shared-ro-mmap",
        ],
    )
    with pytest.raises(SystemExit) as exc2:
        cli.main()
    assert exc2.value.code == 0
    oai_manifest = load_bot_manifest(oai_dir)
    assert oai_manifest.style == "openai"
    assert oai_manifest.target_surface == "awzeal"
    assert oai_manifest.shared_ro_mmap is True


def test_onboard_bot_rejects_conflicting_style_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for flags in (
        ["--gemini-bot", "--openai-bot"],
        ["--gemini-bot", "--style", "openai"],
        ["--openai-bot", "--style", "gemini"],
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            ["adk", "onboard", *flags, "--non-interactive", "--dir", str(tmp_path / "x")],
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2


def test_onboard_bot_with_discord_dry_run_makes_zero_network_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import httpx

    def _forbid(*args, **kwargs):
        raise AssertionError("R4 violation: live HTTP call attempted during dry-run")

    monkeypatch.setattr(httpx, "AsyncClient", _forbid)

    bot_dir = tmp_path / "discord-gem"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adk",
            "onboard",
            "--bot",
            "--style",
            "gemini",
            "--non-interactive",
            "--name",
            "Discord Gem",
            "--dir",
            str(bot_dir),
            "--backend",
            "mock",
            "--discord",
            "--token",
            "1234567890.abc.xyz",
            "--dry-run",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "client_id=1234567890" in out
    assert "dry-run" in out
    assert "1234567890.abc.xyz" not in (bot_dir / "bot.yaml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 3. `adk.server.create_app(bot_dir=...)` — dedicated, prefixed, sidecar
# ---------------------------------------------------------------------------


def test_server_dedicated_bot_mode_serves_ui_gemini_openai_and_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AITHER_OFFLINE", "1")
    monkeypatch.setenv("AITHER_SERVER_API_KEY", "test-server-secret")
    bot_dir = scaffold_bot(
        "Server Gem",
        "gemini",
        tmp_path / "srv-gem",
        backend="mock",
        instructions="Always cite local policy.",
    )
    app = create_app(
        bot_dir=bot_dir,
        bot_backend=MockBotBackend(responder=lambda _m, _meta: "Dedicated server mock reply"),
    )
    with TestClient(app) as tc:
        # UI shell is accessible without bearer even over proxied tunnel headers;
        # API endpoints stay bearer-protected.
        proxied = {"X-Forwarded-For": "203.0.113.10"}
        ui_resp = tc.get("/bot/ui", headers=proxied)
        assert ui_resp.status_code == 200
        assert "Server Gem" in ui_resp.text

        root_resp = tc.get("/", headers=proxied)
        assert root_resp.status_code == 200
        assert "Server Gem" in root_resp.text

        assert tc.get("/bot/info", headers=proxied).status_code == 401

        hdrs = {"Authorization": "Bearer test-server-secret", **proxied}
        info = tc.get("/bot/info", headers=hdrs).json()
        assert info["id"] == "server-gem"
        assert info["style"] == "gemini"

        health = tc.get("/health").json()
        assert health["status"] == "healthy"
        assert health["bot"]["id"] == "server-gem"
        assert health["bot"]["style"] == "gemini"

        gem = tc.post(
            "/v1beta/models/gemini-2.0-flash:generateContent",
            headers=hdrs,
            json={"contents": [{"role": "user", "parts": [{"text": "Hi"}]}]},
        )
        assert gem.status_code == 200, gem.text
        assert "Dedicated server mock reply" in gem.json()["candidates"][0]["content"]["parts"][0]["text"]

        oai = tc.post(
            "/v1/chat/completions",
            headers=hdrs,
            json={"model": "gemini-2.0-flash", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert oai.status_code == 200, oai.text
        assert "Dedicated server mock reply" in oai.json()["choices"][0]["message"]["content"]

        asst = tc.get("/v1/assistants", headers=hdrs)
        assert asst.status_code == 200
        assert len(asst.json()["data"]) >= 1


def test_server_prefixed_and_sidecar_bot_modes_avoid_shadowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock, MagicMock
    from adk.agent import AitherAgent
    from adk.llm.base import LLMResponse
    from adk.tools import ToolRegistry

    monkeypatch.setenv("AITHER_OFFLINE", "1")
    monkeypatch.setenv("AITHER_LOCAL_AUTH", "off")
    oai_dir = scaffold_bot("Prefixed GPT", "openai", tmp_path / "pref-gpt", backend="mock")
    app_prefixed = create_app(
        bot_dir=oai_dir,
        bot_prefix="/bots/helper",
        bot_backend=MockBotBackend(responder=lambda _m, _meta: "Prefixed reply"),
    )
    with TestClient(app_prefixed) as tc:
        assert tc.get("/bots/helper/bot/ui").status_code == 200
        assert tc.get("/bots/helper/bot/info").json()["id"] == "prefixed-gpt"
        resp = tc.post(
            "/bots/helper/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Ping"}]},
        )
        assert resp.status_code == 200
        assert "Prefixed reply" in resp.json()["choices"][0]["message"]["content"]

    fake_agent = MagicMock(spec=AitherAgent)
    fake_agent.name = "aither"
    fake_agent._identity = SimpleNamespace(
        name="aither", description="Primary", system_prompt="Hi", skills=[]
    )
    fake_agent._tools = ToolRegistry()
    fake_agent.llm = MagicMock()
    fake_agent.llm.provider_name = "mock"
    fake_agent.llm.get_provider = AsyncMock(return_value=None)
    fake_agent.chat = AsyncMock(
        return_value=LLMResponse(
            content="Agent primary reply",
            model="mock",
            tokens_used=5,
            prompt_tokens=2,
            completion_tokens=3,
            finish_reason="stop",
        )
    )
    app_sidecar = create_app(
        agent=fake_agent,
        bot_dir=oai_dir,
        bot_backend=MockBotBackend(responder=lambda _m, _meta: "Sidecar reply"),
    )
    with TestClient(app_sidecar) as tc:
        assert tc.get("/bot/info").json()["id"] == "prefixed-gpt"
        asst = tc.get("/v1/assistants")
        assert asst.status_code == 200
        # Sidecar mode preserves primary agent's /v1/chat/completions
        agent_oai = tc.post(
            "/v1/chat/completions",
            json={"model": "mock", "messages": [{"role": "user", "content": "Hello"}]},
        )
        assert agent_oai.status_code == 200
        assert agent_oai.json()["choices"][0]["message"]["content"] == "Agent primary reply"


def test_server_main_parses_bot_flags_without_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn
    from adk import server as server_mod

    bot_dir = scaffold_bot("Main Bot", "gemini", tmp_path / "main-bot", backend="mock")
    recorded: dict[str, Any] = {}

    def fake_uvicorn_run(app, host="127.0.0.1", port=8080, **kwargs):
        recorded["app"] = app
        recorded["host"] = host
        recorded["port"] = port

    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    monkeypatch.setenv("AITHER_OFFLINE", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["aither-serve", "--bot", str(bot_dir), "--bot-backend", "mock", "--port", "8911"],
    )
    server_mod.main()
    assert recorded["port"] == 8911
    assert recorded["app"].state.bot_runtime.manifest.id == "main-bot"
    banner = capsys.readouterr().out
    assert "/bot/ui" in banner
    assert "/v1beta/models" in banner


# ---------------------------------------------------------------------------
# 4. `adk.onboard_discord` bot bridge (R4 offline + mocked token validation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_onboard_bot_to_discord_dry_run_wires_tools_knowledge_and_blocks_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not call live Discord API")),
    )

    bot_dir = scaffold_bot(
        "Discord Helper",
        "gemini",
        tmp_path / "disc-bot",
        backend="mock",
        instructions="Always mention Policy Alpha.",
    )
    (bot_dir / "knowledge" / "policy.md").write_text(
        "Policy Alpha: reimbursements under $100 are auto-approved.", encoding="utf-8"
    )
    identity_dir = tmp_path / "identities"

    res = onboard_bot_to_discord(
        bot_dir,
        token="9988776655.part2.part3",
        dry_run=True,
        backend=MockBotBackend(responder=lambda _m, _meta: "Grounded Discord answer"),
        out_dir=identity_dir,
    )
    assert res["ok"] is True
    assert res["bot_id"] == "discord-helper"
    assert res["dry_run"] is True
    assert res["token_validated"] is False
    assert "client_id=9988776655" in res["invite_url"]
    assert "get_bot_status" in res["registered_tools"]
    assert "file_search" in res["registered_tools"]
    assert (identity_dir / "discord-helper.yaml").is_file()
    assert "9988776655.part2.part3" not in (bot_dir / "bot.yaml").read_text(encoding="utf-8")

    # Adapter works directly with agent_reply()
    rt = BotRuntime(bot_dir, backend=MockBotBackend(responder=lambda _m, _meta: "Adapter reply"))
    reply = await agent_reply(_BotDiscordAdapter(rt), "What is Policy Alpha?")
    assert "Adapter reply" in reply

    # Fail-closed on BOT007 secret leak
    leaky_dir = scaffold_bot("Leaky Bot", "openai", tmp_path / "leaky-bot", backend="mock")
    (leaky_dir / "tools.py").write_text(
        'API_KEY = "sk-proj-1234567890abcdefghij1234567890"\n', encoding="utf-8"
    )
    bad = onboard_bot_to_discord(leaky_dir, token="9988776655.a.b", dry_run=True)
    assert bad["ok"] is False
    assert any(f["code"] == "BOT007" for f in bad["findings"])


def test_onboard_bot_to_discord_live_gate_with_injected_validator(tmp_path: Path) -> None:
    bot_dir = scaffold_bot("Live Gate Bot", "openai", tmp_path / "live-gate", backend="mock")

    rejected = onboard_bot_to_discord(
        bot_dir,
        token="112233.b.c",
        dry_run=False,
        validate_token_fn=lambda tok: {"ok": False, "status": 401, "error": "Unauthorized"},
    )
    assert rejected["ok"] is False
    assert rejected["status"] == 401
    assert rejected["token_validated"] is False

    accepted = onboard_bot_to_discord(
        bot_dir,
        token="112233.b.c",
        dry_run=False,
        run=False,
        validate_token_fn=lambda tok: {
            "ok": True,
            "id": "112233",
            "username": "aither-test-bot",
            "bot": True,
        },
    )
    assert accepted["ok"] is True
    assert accepted["token_validated"] is True
    assert accepted["token_user"]["username"] == "aither-test-bot"

    rc = onboard_discord(
        SimpleNamespace(
            bot_dir=str(bot_dir),
            token="112233.b.c",
            dry_run=True,
            run=False,
        )
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# 5. `adk.create_flow` `BotKind` (`gemini` & `openai`)
# ---------------------------------------------------------------------------


def test_create_flow_infer_kind_distinguishes_bots_from_agents() -> None:
    assert "bot" in KINDS
    assert infer_kind("Make me a Gemini bot for receipts") == "bot"
    assert infer_kind("Build an OpenAI bot with actions") == "bot"
    assert infer_kind("Create an AitherGem helper") == "bot"
    assert infer_kind("Create a customer support agent") == "agent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("style", "prompt_name", "expected_brand"),
    [
        ("gemini", "Receipt Gem", "AitherGem"),
        ("openai", "Action GPT", "AitherGPT"),
    ],
)
async def test_create_flow_bot_kind_plan_approve_verify_undo(
    tmp_path: Path, style: str, prompt_name: str, expected_brand: str
) -> None:
    pack_root = tmp_path / "packs"
    eng = CreateEngine(
        runtime="awdk",
        store=PlanStore(tmp_path / "plans"),
        backend=None,
        memory=_FakeMemory(),
        pack_root=pack_root,
    )
    owner = Principal(user_id="u-bot", tenant_id="t-bot")

    # Seed a skill_pack in pack_root to prove BotKind uses packs-bots and does not collide
    (pack_root / "existing-skill").mkdir(parents=True)

    plan = await eng.plan(
        owner,
        kind="bot",
        name=prompt_name,
        description=f"A {style} bot for local policy lookups.",
        docs=[{"name": "handbook", "text": "Refunds within 30 days require receipt ID."}],
        options={"style": style, "backend": "mock"},
    )
    assert plan["status"] == "planned", plan["preview"].get("blockers")
    assert plan["preview"]["config"]["style"] == style
    assert plan["preview"]["config"]["brand"] == expected_brand
    assert plan["route"]["agent"] == "demiurge (declarative bot scaffolding)"

    done = await eng.approve(owner, plan["id"], wait=True)
    assert done["status"] == "deployed", done.get("error")
    assert done["verify"]["ok"] is True
    assert done["result"]["style"] == style

    bot_path = Path(done["result"]["path"])
    assert bot_path.parent == tmp_path / "packs-bots"
    assert (bot_path / "bot.yaml").is_file()
    assert (bot_path / "knowledge" / "handbook.md").is_file()
    assert load_bot_manifest(bot_path).style == style

    undone = await eng.undo(owner, plan["id"])
    assert undone["status"] == "undone"
    assert not bot_path.exists()
    assert (pack_root / "existing-skill").is_dir()


@pytest.mark.asyncio
async def test_create_flow_bot_kind_blocks_invalid_options(tmp_path: Path) -> None:
    eng = CreateEngine(
        runtime="awdk",
        store=PlanStore(tmp_path / "plans"),
        backend=None,
        memory=_FakeMemory(),
        pack_root=tmp_path / "packs",
    )
    owner = Principal(user_id="u-bot", tenant_id="t-bot")
    plan = await eng.plan(
        owner,
        kind="bot",
        name="Bad Bot",
        description="Invalid options test",
        options={"style": "bogus", "model_sha256": "not-sha256"},
    )
    assert plan["blocked"] is True
    assert len(plan["preview"]["blockers"]) >= 2


@pytest.mark.asyncio
async def test_onboard_bot_to_discord_inside_active_event_loop_and_close_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adk.onboard_discord as od

    bot_dir = tmp_path / "loop-gem"
    scaffold_bot("Loop Gem", "gemini", bot_dir, backend="mock")

    async def _async_ok(_tok: str) -> dict[str, Any]:
        return {"ok": True, "id": "445566", "username": "loop-bot", "bot": True}

    res = onboard_bot_to_discord(
        bot_dir,
        token="445566.b.c",
        dry_run=False,
        run=False,
        validate_token_fn=_async_ok,
    )
    assert res["ok"] is True
    assert res["token_validated"] is True
    assert res["token_user"]["username"] == "loop-bot"

    closed = {"count": 0}

    class _FakeClient:
        async def start(self, _tok: str) -> None:
            raise RuntimeError("boom-connect")

        async def close(self) -> None:
            closed["count"] += 1

    monkeypatch.setattr(od, "make_bot_discord_client", lambda _rt: _FakeClient())
    failed = onboard_bot_to_discord(
        bot_dir,
        token="445566.b.c",
        dry_run=False,
        run=True,
        validate_token_fn=_async_ok,
    )
    assert failed["ok"] is False
    assert "boom-connect" in failed["error"]
    assert closed["count"] == 1


@pytest.mark.asyncio
async def test_create_flow_bot_kind_avoids_knowledge_readme_collision(tmp_path: Path) -> None:
    pack_root = tmp_path / "packs"
    eng = CreateEngine(
        runtime="awdk",
        store=PlanStore(tmp_path / "plans"),
        backend=None,
        memory=_FakeMemory(),
        pack_root=pack_root,
    )
    owner = Principal(user_id="u-readme", tenant_id="t-readme")
    plan = await eng.plan(
        owner,
        kind="bot",
        name="Readme Safe Gem",
        description="Checks README doc filename collision avoidance.",
        docs=[{"name": "README", "text": "User supplied README knowledge."}],
        options={"style": "gemini", "backend": "mock"},
    )
    assert plan["status"] == "planned"
    assert "readme-safe-gem/knowledge/readme-doc.md" in plan["preview"]["files"]

    done = await eng.approve(owner, plan["id"], wait=True)
    assert done["status"] == "deployed", done.get("error")
    bot_path = Path(done["result"]["path"])
    assert (bot_path / "knowledge" / "README.md").is_file()
    assert (bot_path / "knowledge" / "readme-doc.md").is_file()
    assert "User supplied README knowledge." in (
        bot_path / "knowledge" / "readme-doc.md"
    ).read_text(encoding="utf-8")
