"""Agent Home: setup, persona, model, harness, license gate, CLI."""

from __future__ import annotations

import base64
import json
import time

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    PublicFormat,
)

from adk.home import cli as home_cli  # noqa: E402
from adk.home import config as hc  # noqa: E402
from adk.home import entitlement, harness, models  # noqa: E402
from adk.licensing import LicenseError  # noqa: E402


def _envelope(key: Ed25519PrivateKey, packs, tier="community", expires_at=0):
    payload = json.dumps({"tier": tier, "packs": list(packs), "tenant_id": "t1",
                          "issued_at": time.time(), "expires_at": expires_at}).encode()
    return {"payload": base64.b64encode(payload).decode(),
            "signature": key.sign(payload).hex()}


@pytest.fixture
def signing_key(monkeypatch, tmp_path):
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    monkeypatch.setenv("AITHER_LICENSE_PUBLIC_KEY", pub)
    monkeypatch.setenv("AITHER_LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.delenv("AITHER_LICENSE_KEY", raising=False)
    monkeypatch.delenv("AITHER_LICENSE_ENFORCE", raising=False)
    monkeypatch.delenv("AITHER_TENANT_SLUG", raising=False)
    return key


@pytest.fixture
def home(monkeypatch, tmp_path, signing_key):
    root = tmp_path / "agent-home"
    monkeypatch.setenv(hc.HOME_ENV, str(root))
    return root


# ── config + persona ─────────────────────────────────────────────────────────

def test_init_creates_editable_persona_and_is_idempotent(home):
    cfg = hc.init_home(name="pip")
    assert cfg.name == "pip"
    for f in hc.PERSONA_FILES:
        assert (home / "persona" / f).is_file()
    hc.write_persona_file("persona.md", "# Pip\n- loves puzzles")
    again = hc.init_home(name="other")          # no --force: keeps everything
    assert again.name == "pip"
    assert "loves puzzles" in hc.compose_system_prompt()
    assert "You are pip" in hc.compose_system_prompt()


def test_write_persona_rejects_unknown_file(home):
    hc.init_home()
    with pytest.raises(hc.HomeError):
        hc.write_persona_file("../escape.md", "x")


def test_load_config_without_init_says_how_to_fix(home):
    with pytest.raises(hc.HomeError, match="adk home init"):
        hc.load_config()


# ── model choice ─────────────────────────────────────────────────────────────

def test_choose_model_local_and_byo_defaults():
    local = models.choose_model("bonsai")
    assert (local.mode, local.base_url) == ("local", "http://127.0.0.1:8080/v1")
    byo = models.choose_model("deepseek")
    assert (byo.mode, byo.api_key_env, byo.model) == ("byo", "DEEPSEEK_API_KEY",
                                                      "deepseek-chat")
    with pytest.raises(hc.HomeError):
        models.choose_model("gpt-9000")


def test_key_env_refuses_a_literal_key():
    with pytest.raises(hc.HomeError, match="NAME"):
        models.choose_model("openai", api_key_env="not a name!")


def test_build_llm_byo_without_key_raises(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(hc.HomeError, match="DEEPSEEK_API_KEY"):
        models.build_llm(models.choose_model("deepseek"))


def test_build_llm_local_builds_router():
    llm = models.build_llm(models.choose_model("ollama"))
    assert llm.__class__.__name__ == "LLMRouter"


def test_config_never_stores_key_value(home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-value-should-never-persist")
    hc.init_home()
    assert home_cli.main(["model", "--byo", "openai"]) == 0
    text = (home / "home.json").read_text()
    assert "OPENAI_API_KEY" in text
    assert "test-value-should" not in text


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["openclaw", "hermes"])
def test_connect_framework_harness_points_at_chosen_model(home, kind):
    cfg = hc.init_home(name="pip")
    cfg.model = models.choose_model("deepseek", model="deepseek-reasoner")
    cfg.harness = harness.choose_harness(kind)
    out = harness.connect_harness(cfg)
    text = (home / "harness" / f"{kind}.yaml").read_text()
    assert "https://api.deepseek.com/v1" in text
    assert "deepseek-reasoner" in text
    assert "${DEEPSEEK_API_KEY}" in text          # a reference, never a value
    assert "You are pip" in (home / "harness" / f"{kind}-system-prompt.md").read_text()
    assert out["status"]["available"] is True


def test_connect_claude_writes_persona_claude_md(home):
    cfg = hc.init_home(name="pip")
    cfg.harness = harness.choose_harness("claude")
    harness.connect_harness(cfg)
    assert "You are pip" in (home / "harness" / "CLAUDE.md").read_text()


def test_unknown_harness_rejected():
    with pytest.raises(hc.HomeError):
        harness.choose_harness("skynet")


# ── license gate ─────────────────────────────────────────────────────────────

def test_premium_refused_without_license(home):
    assert entitlement.has_pack() is False
    with pytest.raises(LicenseError, match="agent-home"):
        entitlement.require("game_learning")


def test_install_signed_license_unlocks(home, signing_key, tmp_path):
    lic_file = tmp_path / "bought.json"
    lic_file.write_text(json.dumps(_envelope(signing_key, ["agent-home"])))
    res = entitlement.install_license(str(lic_file))
    assert res["agent_home"] is True
    assert entitlement.has_pack() is True
    entitlement.require("multi_agent")           # no raise


def test_install_accepts_pasted_base64(home, signing_key):
    env = _envelope(signing_key, ["agent-home"])
    entitlement.install_license(base64.b64encode(json.dumps(env).encode()).decode())
    assert entitlement.has_pack() is True


def test_forged_license_is_refused_and_not_saved(home, tmp_path):
    forger = Ed25519PrivateKey.generate()          # not the trusted key
    with pytest.raises(ValueError, match="did not verify"):
        entitlement.install_license(json.dumps(_envelope(forger, ["agent-home"])))
    assert not entitlement.license_path().exists()
    assert entitlement.has_pack() is False


def test_other_pack_license_does_not_unlock(home, signing_key):
    entitlement.install_license(json.dumps(_envelope(signing_key, ["deep-research"])))
    assert entitlement.has_pack() is False


def test_second_license_keeps_the_first_ones_packs(home, signing_key):
    """Offline licenses sit side by side: installing one never switches another off."""
    entitlement.install_license(json.dumps(_envelope(signing_key, ["deep-research"])))
    res = entitlement.install_license(json.dumps(_envelope(signing_key, ["agent-home"])))
    assert res["packs_no_longer_active"] == []
    assert entitlement.has_pack() is True
    assert set(entitlement.status()["packs"]) >= {"deep-research", "agent-home"}
    assert not entitlement.license_path().exists()     # the account file is not touched
    assert len(list((entitlement.license_path().parent / "licenses").glob("*.json"))) == 2


def test_cli_license_installs_side_by_side(home, signing_key, tmp_path):
    old = tmp_path / "dr.json"
    old.write_text(json.dumps(_envelope(signing_key, ["deep-research"])))
    new = tmp_path / "ah.json"
    new.write_text(json.dumps(_envelope(signing_key, ["agent-home"])))
    assert home_cli.main(["license", str(old)]) == 0
    assert home_cli.main(["license", str(new)]) == 0
    assert entitlement.has_pack() is True


def test_cli_signin_runs_device_flow_and_unlocks(home, signing_key, monkeypatch):
    """`adk home signin`: device flow (mocked), then the ACCOUNT license unlocks."""
    from adk import cli as adk_cli

    env = _envelope(signing_key, ["agent-home"])
    key = base64.b64encode(json.dumps(env).encode()).decode()
    monkeypatch.setattr(adk_cli, "_device_flow_login",
                        lambda url, client_name="adk": {"access_token": "tok",
                                                        "license_key": key})
    monkeypatch.setattr(adk_cli, "complete_device_login",
                        lambda url, result, sync=True: "pip")
    assert home_cli.main(["signin"]) == 0
    assert entitlement.license_path().is_file()
    assert entitlement.has_pack() is True


def test_expired_license_refused(home, signing_key):
    with pytest.raises(ValueError):
        entitlement.install_license(json.dumps(
            _envelope(signing_key, ["agent-home"], expires_at=time.time() - 10)))


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_flow_init_model_harness_status(home, capsys):
    assert home_cli.main(["init", "--name", "pip"]) == 0
    assert home_cli.main(["model", "--local", "ollama"]) == 0
    assert home_cli.main(["harness", "hermes"]) == 0
    assert home_cli.main(["persona", "set", "rules.md", "- be kind"]) == 0
    capsys.readouterr()
    assert home_cli.main(["status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "pip"
    assert out["model"]["provider"] == "ollama"
    assert out["harness"]["kind"] == "hermes"
    assert out["license"]["owned"] is False
    assert "be kind" in hc.compose_system_prompt()


def test_cli_without_init_exits_setup(home):
    assert home_cli.main(["model", "--local", "bonsai"]) == home_cli.EXIT_SETUP


def test_register_parser_hooks_into_a_parent_cli():
    import argparse

    p = argparse.ArgumentParser(prog="adk")
    home_cli.register_parser(p.add_subparsers(dest="command"))
    args = p.parse_args(["home", "join", "saga+http://x?world=elysium", "--steps", "3"])
    assert (args.command, args.home_command, args.steps) == ("home", "join", 3)
