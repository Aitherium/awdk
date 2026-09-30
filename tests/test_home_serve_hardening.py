"""``adk home serve`` start-up hardening: egress audit default, A2A trust default,
and the Aither Learn tutor tools on a signed-in home.

The serve CLI is driven through its seams (agent, transports, run loop faked), so
nothing here opens a socket.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("cryptography")

from adk import a2a_trust, approval, receipts  # noqa: E402
from adk.home import cli as home_cli  # noqa: E402
from adk.home import config as hc  # noqa: E402
from adk.home import connector_tools as ct  # noqa: E402
from adk.home import hearth, serve  # noqa: E402
from adk.home.life_tools import ALWAYS_ASK, FollowupStore  # noqa: E402
from adk.tools import ToolRegistry  # noqa: E402

TUTOR_TOOLS = {"tutor_learners", "tutor_report", "tutor_assign", "tutor_set_focus"}
BEARER = "guardian-bearer-xyz"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "agent-home"
    monkeypatch.setenv(hc.HOME_ENV, str(root))
    monkeypatch.setenv("AITHER_DATA_DIR", str(tmp_path / "data"))
    for name in ("AITHER_AIR_GAP", "AITHER_AIR_GAP_CONFIG", "AITHER_RECEIPTS_PATH",
                 serve.A2A_TRUST_ENV, *serve.TUTOR_URL_ENV):
        monkeypatch.setenv(name, "")                # recorded, so serve's setdefault
        monkeypatch.delenv(name)                    # is undone at teardown
    monkeypatch.delenv(receipts.KEY_ENV, raising=False)
    monkeypatch.setattr(receipts, "_home_dir", lambda: root)
    monkeypatch.setattr(receipts, "_awseal_private_key", lambda: None)
    monkeypatch.setattr(approval, "_STORE", approval.ApprovalStore(tmp_path / "paused.json"))
    monkeypatch.setattr("adk.config.load_saved_config", lambda *a, **k: {})
    monkeypatch.setattr(home_cli, "_keychain_has", lambda cls, fn: False)
    return root


class FakeTransport:
    def __init__(self, name):
        self.name = name

    async def start(self, core):
        return None

    async def stop(self):
        return None

    async def send(self, user_id, text):
        return True


def _serve(monkeypatch, capsys) -> str:
    """Run ``adk home serve --channels telegram`` up to the loop; return stdout."""
    hc.init_home(name="hearth-test")
    monkeypatch.setenv("HEARTH_TELEGRAM_TOKEN", "tg-tok")
    hearth.OwnerRegistry(hearth.owner_path()).bind("telegram", "123456789")
    monkeypatch.setattr(serve, "build_serve_agent", lambda *a, **k: SimpleNamespace(
        name="hearth-test"))
    monkeypatch.setattr(serve, "tool_names", lambda agent: [])
    monkeypatch.setattr(hearth.HearthCore, "_wrap_execute", lambda self: None)
    monkeypatch.setattr(home_cli, "_build_transport",
                        lambda channel, cls: FakeTransport(channel))

    async def fake_run(core, tick):
        return home_cli.EXIT_OK

    monkeypatch.setattr(home_cli, "_run_hearth", fake_run)
    assert home_cli.main(["serve", "--channels", "telegram"]) == home_cli.EXIT_OK
    return capsys.readouterr().out


# ── egress audit default ─────────────────────────────────────────────────────────

def test_first_serve_writes_the_audit_config_and_says_so(home, monkeypatch, capsys):
    target = home_cli.air_gap_path()
    assert not target.exists()
    out = _serve(monkeypatch, capsys)
    assert target.read_text(encoding="utf-8") == home_cli.AIR_GAP_AUDIT   # the trust writer
    from adk.compliance.air_gap import AirGapEnforcer

    cfg, _, err = AirGapEnforcer._read_layer(target)
    assert err is None and cfg["enabled"] is True and cfg["enforcement"] == "audit"
    egress = next(line for line in out.splitlines() if line.strip().startswith("egress:"))
    assert "audit" in egress and str(target) in egress


def test_an_existing_air_gap_config_is_never_overwritten(home, monkeypatch, capsys):
    target = home_cli.air_gap_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("enabled: true\nenforcement: strict\n", encoding="utf-8")
    out = _serve(monkeypatch, capsys)
    assert "strict" in target.read_text(encoding="utf-8")
    assert "egress:   strict" in out


def test_an_env_override_decides_and_nothing_is_written(home, monkeypatch, capsys):
    monkeypatch.setenv("AITHER_AIR_GAP", "false")
    out = _serve(monkeypatch, capsys)
    assert not home_cli.air_gap_path().exists()
    assert "AITHER_AIR_GAP=false" in out


# ── A2A trust default ─────────────────────────────────────────────────────────

def test_serve_requires_a2a_trust_by_default(home, monkeypatch, capsys):
    assert not a2a_trust.should_require_a2a_trust()
    out = _serve(monkeypatch, capsys)
    import os

    assert os.environ[serve.A2A_TRUST_ENV] == "true"
    assert a2a_trust.should_require_a2a_trust()          # read at call time, not import
    assert "AITHER_A2A_REQUIRE_TRUST=true" in out


def test_an_explicit_a2a_mode_is_kept(home, monkeypatch, capsys):
    monkeypatch.setenv(serve.A2A_TRUST_ENV, "audit")
    _serve(monkeypatch, capsys)
    assert a2a_trust.should_audit_a2a_trust() and not a2a_trust.should_require_a2a_trust()


# ── tutor tools ───────────────────────────────────────────────────────────────

class _Agent:
    name = "hearth-test"

    def __init__(self):
        self._tools = ToolRegistry()


@pytest.fixture
def bare_tools(monkeypatch):
    for name in ("ADK_BUILTIN_TOOL_CATEGORIES", "AITHER_TOOL_PACKS", "ADK_APP_PROXY_URL"):
        monkeypatch.delenv(name, raising=False)


def test_tutor_tools_register_only_on_a_signed_in_home(home, bare_tools, monkeypatch,
                                                       tmp_path):
    store = FollowupStore(tmp_path / "f.json")
    monkeypatch.setattr(ct, "_saved_bearer", lambda: "")
    assert not TUTOR_TOOLS & set(serve.register_serve_tools(_Agent(), store))
    monkeypatch.setattr(ct, "_saved_bearer", lambda: BEARER)
    assert TUTOR_TOOLS <= set(serve.register_serve_tools(_Agent(), store))
    assert not TUTOR_TOOLS & set(serve.register_serve_tools(_Agent(), store,
                                                            connectors=False))


def test_tutor_assign_always_asks_and_the_readers_do_not():
    assert "tutor_assign" in ALWAYS_ASK and "tutor_set_focus" in ALWAYS_ASK
    assert "tutor_learners" not in ALWAYS_ASK and "tutor_report" not in ALWAYS_ASK


def test_registered_tutor_tools_use_the_owner_bearer_and_tutor_url(home, bare_tools,
                                                                   monkeypatch, tmp_path):
    monkeypatch.setattr(ct, "_saved_bearer", lambda: BEARER)
    monkeypatch.setenv("AITHER_TUTOR_URL", "https://genesis.test/")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, json=[{"lid": "k1", "alias": "Sam"}])

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real_client(
        transport=httpx.MockTransport(handler)))
    agent = _Agent()
    serve.register_serve_tools(agent, FollowupStore(tmp_path / "f.json"))
    import asyncio

    out = json.loads(asyncio.run(agent._tools.execute("tutor_learners", {})))
    assert out == [{"lid": "k1", "alias": "Sam"}]
    assert calls == [("GET", "https://genesis.test/api/v1/tutor/family/learners",
                      f"Bearer {BEARER}")]


def test_the_serve_agent_is_told_about_the_tutor_tools_when_signed_in(home, monkeypatch):
    monkeypatch.setattr(ct, "_saved_bearer", lambda: BEARER)
    hc.init_home(name="hearth-test")
    seen = {}

    class FakeAgent:
        def __init__(self, **kw):
            seen.update(kw)
            self.name = kw["name"]
            self._tools = ToolRegistry()

    import adk.agent

    monkeypatch.setattr(adk.agent, "AitherAgent", FakeAgent)
    monkeypatch.setattr(serve, "register_serve_tools", lambda *a, **k: [])
    serve.build_serve_agent(hc.load_config(), FollowupStore(home / "f.json"), llm=object())
    assert "tutor_assign" in seen["system_prompt"]
    assert hearth.UNTRUSTED_PROMPT in seen["system_prompt"]
