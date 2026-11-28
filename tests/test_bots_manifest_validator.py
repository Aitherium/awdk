"""Tests for bot manifest schema, scaffolding, and BOT001-BOT008 static validation."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
import yaml

from adk.bots.manifest import BotManifest, load_bot_manifest, scaffold_bot
from adk.bots.validator import validate_bot


def _codes(report) -> list[str]:
    return [f.code for f in report.findings]


@pytest.mark.parametrize("style", ["gemini", "openai"])
def test_scaffold_and_validate_clean(tmp_path: Path, style: str) -> None:
    bot_dir = tmp_path / f"my-{style}-bot"
    out = scaffold_bot(f"My {style.title()} Bot", style, bot_dir)
    assert out == bot_dir
    assert (bot_dir / "bot.yaml").is_file()
    assert (bot_dir / "knowledge" / "README.md").is_file()
    assert (bot_dir / "actions" / ".gitkeep").is_file()
    assert (bot_dir / "tools.py").is_file()
    assert (bot_dir / ".env.example").is_file()
    assert (bot_dir / "README.md").is_file()
    assert (bot_dir / "tests" / "test_bot.py").is_file()

    manifest = load_bot_manifest(bot_dir)
    assert isinstance(manifest, BotManifest)
    assert manifest.style == style
    assert manifest.instructions

    pub = manifest.to_public_dict()
    assert "instructions" not in pub or isinstance(pub, dict)
    assert pub["style"] == style
    assert "api_key" not in str(pub).lower()

    rep = validate_bot(bot_dir)
    assert rep.ok, [str(f) for f in rep.findings]
    assert rep.exit_code == 0


def test_scaffold_refuses_non_empty_without_overwrite(tmp_path: Path) -> None:
    bot_dir = tmp_path / "existing-bot"
    scaffold_bot("Existing Bot", "gemini", bot_dir)
    with pytest.raises(FileExistsError):
        scaffold_bot("Existing Bot", "gemini", bot_dir)
    # overwrite=True succeeds
    scaffold_bot("Existing Bot", "openai", bot_dir, overwrite=True)
    assert load_bot_manifest(bot_dir).style == "openai"


def test_validate_missing_dir_returns_not_judged(tmp_path: Path) -> None:
    rep = validate_bot(tmp_path / "does-not-exist")
    assert not rep.ok
    assert not rep.judged
    assert rep.exit_code == 2
    assert "BOT000" in _codes(rep)


def test_bot001_missing_and_invalid_yaml(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    rep = validate_bot(empty_dir)
    assert not rep.ok
    assert "BOT001" in _codes(rep)

    bad_yaml = tmp_path / "bad-yaml"
    bad_yaml.mkdir()
    (bad_yaml / "bot.yaml").write_text(":::\n  - [broken", encoding="utf-8")
    rep2 = validate_bot(bad_yaml)
    assert "BOT001" in _codes(rep2)

    non_map = tmp_path / "list-yaml"
    non_map.mkdir()
    (non_map / "bot.yaml").write_text("- item1\n- item2\n", encoding="utf-8")
    rep3 = validate_bot(non_map)
    assert "BOT001" in _codes(rep3)


def test_bot002_bot003_bot004_schema_checks(tmp_path: Path) -> None:
    bot_dir = tmp_path / "bad-fields"
    scaffold_bot("Good Name", "gemini", bot_dir)
    data = yaml.safe_load((bot_dir / "bot.yaml").read_text(encoding="utf-8"))
    data["id"] = "INVALID_ID!"
    data["version"] = "not-semver"
    data["style"] = "claude"
    data["backend"] = "unknown-backend"
    data["instructions"] = "   "
    data["conversation_starters"] = "not-a-list"
    (bot_dir / "bot.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    rep = validate_bot(bot_dir)
    codes = set(_codes(rep))
    assert {"BOT002", "BOT003", "BOT004"}.issubset(codes)
    with pytest.raises(ValueError):
        load_bot_manifest(bot_dir)


def test_bot005_never_executes_tools_and_validates_syntax_and_openapi(tmp_path: Path) -> None:
    bot_dir = tmp_path / "exec-proof"
    scaffold_bot("Exec Proof", "openai", bot_dir)
    marker = bot_dir / "ran.marker"

    # Write top-level side effect in tools.py — validate_bot must NEVER execute it!
    (bot_dir / "tools.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('RAN')\nraise RuntimeError('executed!')\n",
        encoding="utf-8",
    )
    rep = validate_bot(bot_dir)
    assert rep.ok, [str(f) for f in rep.findings]
    assert not marker.exists()

    # Syntax error in tools.py triggers BOT005 without executing
    (bot_dir / "tools.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    rep_syntax = validate_bot(bot_dir)
    assert "BOT005" in _codes(rep_syntax)

    # Restore valid tools.py and add OpenAPI action with bad spec / SSRF URL
    (bot_dir / "tools.py").write_text("def register(r):\n    pass\n", encoding="utf-8")
    (bot_dir / "actions" / "orders.yaml").write_text(
        yaml.safe_dump(
            {
                "openapi": "2.0",
                "servers": [{"url": "http://169.254.169.254/latest"}],
                "paths": {},
            }
        ),
        encoding="utf-8",
    )
    data = yaml.safe_load((bot_dir / "bot.yaml").read_text(encoding="utf-8"))
    data["actions"] = [
        {
            "name": "orders",
            "openapi_spec": "actions/orders.yaml",
            "auth_type": "none",
        }
    ]
    (bot_dir / "bot.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    rep_action = validate_bot(bot_dir)
    assert "BOT005" in _codes(rep_action)


def test_bot006_traversal_and_symlink_rejected(tmp_path: Path) -> None:
    bot_dir = tmp_path / "traversal-bot"
    scaffold_bot("Traversal Bot", "gemini", bot_dir)

    outside = tmp_path / "secret_outside.txt"
    outside.write_text("secret", encoding="utf-8")

    data = yaml.safe_load((bot_dir / "bot.yaml").read_text(encoding="utf-8"))
    data["knowledge"]["paths"] = ["../secret_outside.txt"]
    (bot_dir / "bot.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    rep = validate_bot(bot_dir)
    assert "BOT006" in _codes(rep)

    # Restore knowledge path and place a symlink inside knowledge/
    data["knowledge"]["paths"] = ["knowledge"]
    (bot_dir / "bot.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    link = bot_dir / "knowledge" / "escape.md"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlinks not supported on filesystem")
    rep_sym = validate_bot(bot_dir)
    assert "BOT006" in _codes(rep_sym)


@pytest.mark.parametrize(
    "secret_literal",
    [
        "AIzaSyDummyGeminiKeyForTest123456789012345",
        "sk-proj-DummyOpenAIProjectKey1234567890abcdef",
        "sk-DummyLegacyOpenAIKey1234567890abcdefghijkl",
        "ghp_DummyGitHubToken1234567890abcdefghijkl",
        "DISCORD_BOT_TOKEN=MTIzNDU2Nzg5MDEyMzQ1Njc4.abcdef.ghijklmnopqrstuvwxyz1234",
    ],
)
def test_bot007_secret_detection_never_echoes_secret(
    tmp_path: Path, secret_literal: str
) -> None:
    bot_dir = tmp_path / "leaky-bot"
    scaffold_bot("Leaky Bot", "gemini", bot_dir)
    (bot_dir / "knowledge" / "notes.md").write_text(
        f"# Notes\nkey={secret_literal}\n", encoding="utf-8"
    )
    rep = validate_bot(bot_dir)
    assert "BOT007" in _codes(rep)
    joined = "\n".join(str(f) for f in rep.findings)
    assert secret_literal not in joined
    assert "knowledge/notes.md:2" in joined


def test_bot007_dot_env_file_rejected(tmp_path: Path) -> None:
    bot_dir = tmp_path / "env-bot"
    scaffold_bot("Env Bot", "openai", bot_dir)
    (bot_dir / ".env").write_text("OPENAI_API_KEY=placeholder\n", encoding="utf-8")
    rep = validate_bot(bot_dir)
    assert "BOT007" in _codes(rep)


def test_bot008_generation_config_and_safety_bounds(tmp_path: Path) -> None:
    bot_dir = tmp_path / "bounds-bot"
    scaffold_bot("Bounds Bot", "gemini", bot_dir)
    data = yaml.safe_load((bot_dir / "bot.yaml").read_text(encoding="utf-8"))
    data["generation_config"] = {
        "temperature": 2.5,
        "top_p": 0.0,
        "max_output_tokens": 0,
    }
    data["safety_settings"] = [{"category": "", "threshold": "BLOCK_NONE"}]
    (bot_dir / "bot.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    rep = validate_bot(bot_dir)
    assert "BOT008" in _codes(rep)


def test_bot005_offline_dns_allows_public_url_and_checks_path_and_op_servers(
    tmp_path: Path, monkeypatch
) -> None:
    bot_dir = tmp_path / "offline-openapi-bot"
    scaffold_bot("Offline OpenAPI Bot", "openai", bot_dir)

    def failing_getaddrinfo(*args, **kwargs):
        raise socket.gaierror("offline test environment")

    monkeypatch.setattr(socket, "getaddrinfo", failing_getaddrinfo)

    (bot_dir / "actions" / "public.yaml").write_text(
        yaml.safe_dump(
            {
                "openapi": "3.0.3",
                "servers": [{"url": "https://api.example.com"}],
                "paths": {
                    "/items": {
                        "get": {"operationId": "list_items", "summary": "List items"}
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    data = yaml.safe_load((bot_dir / "bot.yaml").read_text(encoding="utf-8"))
    data["actions"] = [
        {
            "name": "public_api",
            "openapi_spec": "actions/public.yaml",
            "auth_type": "none",
        }
    ]
    (bot_dir / "bot.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    rep_ok = validate_bot(bot_dir)
    assert rep_ok.ok, [str(f) for f in rep_ok.findings]

    # Path-item server with metadata IP MUST trigger BOT005
    (bot_dir / "actions" / "public.yaml").write_text(
        yaml.safe_dump(
            {
                "openapi": "3.0.3",
                "servers": [{"url": "https://api.example.com"}],
                "paths": {
                    "/items": {
                        "servers": [{"url": "http://169.254.169.254/latest"}],
                        "get": {"operationId": "list_items", "summary": "List items"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    rep_path_srv = validate_bot(bot_dir)
    assert "BOT005" in _codes(rep_path_srv)

    # Operation-level server with localhost MUST trigger BOT005
    (bot_dir / "actions" / "public.yaml").write_text(
        yaml.safe_dump(
            {
                "openapi": "3.0.3",
                "servers": [{"url": "https://api.example.com"}],
                "paths": {
                    "/items": {
                        "get": {
                            "operationId": "list_items",
                            "summary": "List items",
                            "servers": [{"url": "http://127.0.0.1:8080/internal"}],
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    rep_op_srv = validate_bot(bot_dir)
    assert "BOT005" in _codes(rep_op_srv)


def test_bot005_validates_tools_packs_and_mcp_servers(tmp_path: Path) -> None:
    bot_dir = tmp_path / "tools-lists-bot"
    scaffold_bot("Tools Lists Bot", "gemini", bot_dir)
    data = yaml.safe_load((bot_dir / "bot.yaml").read_text(encoding="utf-8"))

    data["tools"]["packs"] = "not-a-list"
    data["tools"]["mcp_servers"] = [""]
    (bot_dir / "bot.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    rep_bad = validate_bot(bot_dir)
    assert _codes(rep_bad).count("BOT005") >= 2

    data["tools"]["packs"] = ["deep_research"]
    data["tools"]["mcp_servers"] = ["aither-local"]
    (bot_dir / "bot.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    rep_good = validate_bot(bot_dir)
    assert rep_good.ok, [str(f) for f in rep_good.findings]


@pytest.mark.parametrize("backend", ["awzeal", "zero-libc"])
def test_awzeal_zero_libc_manifest_and_validator(tmp_path: Path, backend: str) -> None:
    bot_dir = tmp_path / f"bot-{backend}"
    digest = "a" * 64
    scaffold_bot(
        f"Sovereign {backend}",
        "gemini",
        bot_dir,
        backend=backend,
        model_sha256=digest,
        shared_ro_mmap=True,
        target_surface="awzeal",
    )
    rep = validate_bot(bot_dir)
    assert rep.ok, [str(f) for f in rep.findings]

    manifest = load_bot_manifest(bot_dir)
    assert manifest.backend == backend
    assert manifest.model_sha256 == digest
    assert manifest.shared_ro_mmap is True
    assert manifest.target_surface == "awzeal"
    pub = manifest.to_public_dict()
    assert pub["model_sha256"] == digest
    assert pub["shared_ro_mmap"] is True
    assert pub["target_surface"] == "awzeal"

    # Invalid sha256, target_surface, and shared_ro_mmap caught by validator + loader
    data = yaml.safe_load((bot_dir / "bot.yaml").read_text(encoding="utf-8"))
    data["model_sha256"] = "not-a-sha256"
    data["target_surface"] = "unknown-surface"
    data["shared_ro_mmap"] = "yes"
    (bot_dir / "bot.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    bad_rep = validate_bot(bot_dir)
    bad_codes = set(_codes(bad_rep))
    assert "BOT003" in bad_codes
    assert "BOT004" in bad_codes
    with pytest.raises(ValueError):
        load_bot_manifest(bot_dir)


def test_bot_null_byte_paths_and_leading_dash_model_rejected(tmp_path: Path) -> None:
    bot_dir = tmp_path / "null-and-dash-bot"
    scaffold_bot("Null And Dash", "gemini", bot_dir, backend="mock")
    data = yaml.safe_load((bot_dir / "bot.yaml").read_text(encoding="utf-8"))
    data["model"] = "-help"
    data["knowledge"] = {"paths": ["knowledge/doc\x00.md"], "chunk_size": 800, "top_k": 5}
    data["actions"] = [{"name": "a", "openapi_spec": "actions/spec\x00.yaml"}]
    data["tools"] = {"modules": ["tools\x00.py"]}
    (bot_dir / "bot.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    rep = validate_bot(bot_dir)
    assert not rep.ok
    codes = set(_codes(rep))
    assert {"BOT004", "BOT005", "BOT006"}.issubset(codes)

