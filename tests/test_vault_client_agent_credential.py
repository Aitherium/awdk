"""An agent run's vault client sends its agent credential and nothing wider."""
import adk.vault_lockbox as vl
import pytest


def _headers(monkeypatch, **env):
    for k in ("AITHER_AGENT_CREDENTIAL", "AITHER_INTERNAL_SECRET"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(vl, "_get_stored", lambda account: None)
    monkeypatch.setattr(vl, "vault_url", lambda: "https://vault.test:8111")
    with vl._client() as c:
        return dict(c.headers)


def test_an_agent_credential_is_the_only_credential_sent(monkeypatch):
    h = _headers(monkeypatch, AITHER_AGENT_CREDENTIAL="agent-tok",
                 AITHER_INTERNAL_SECRET="master")
    assert h.get("authorization") == "Bearer agent-tok"
    assert "x-api-key" not in h  # never escalates to the master key


def test_without_one_the_operator_path_is_unchanged(monkeypatch):
    h = _headers(monkeypatch, AITHER_INTERNAL_SECRET="master")
    assert h.get("x-api-key") == "master" and "authorization" not in h


def test_an_agent_run_uses_the_live_vault_even_when_not_logged_in(monkeypatch):
    monkeypatch.setenv("AITHER_AGENT_CREDENTIAL", "agent-tok")
    monkeypatch.setattr(vl, "_get_stored", lambda account: None)
    monkeypatch.setattr(vl, "_logged_in", lambda: False)
    use, why = vl.remote_mode()
    assert use is True and "agent" in why


def test_a_run_credential_wins_over_the_env_and_is_dropped_after(monkeypatch):
    monkeypatch.setenv("AITHER_AGENT_CREDENTIAL", "env-tok")
    monkeypatch.setattr(vl, "mint_agent_credential", lambda a, g, t=900: f"run-{a}")
    assert vl._agent_credential() == "env-tok"
    with vl.agent_credential("researcher", ["secret:K"]):
        assert vl._agent_credential() == "run-researcher"
    assert vl._agent_credential() == "env-tok"


def test_minting_sends_the_owner_login_to_identity_only(monkeypatch):
    import httpx

    from adk.shell import auth as shauth

    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"credential": "agent-cred"}

    def _post(url, json=None, headers=None, **kw):
        seen.update(url=url, body=json, auth=headers.get("Authorization"))
        return _Resp()

    monkeypatch.setattr(shauth.AuthStore, "get_active_token", staticmethod(lambda: "owner-login"))
    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setenv("AITHER_IDENTITY_URL", "https://identity.example")
    assert vl.mint_agent_credential("researcher", ["secret:K"], 60) == "agent-cred"
    assert seen["url"].endswith("/v1/agent-credentials")
    assert seen["body"] == {"agent_id": "researcher", "grants": ["secret:K"], "ttl_seconds": 60}
    assert seen["auth"] == "Bearer owner-login"


def test_minting_fails_closed_when_not_signed_in(monkeypatch):
    from adk.shell import auth as shauth

    monkeypatch.setattr(shauth.AuthStore, "get_active_token", staticmethod(lambda: None))
    with pytest.raises(RuntimeError):
        vl.mint_agent_credential("researcher", ["secret:K"])


def _agent_with(grants, monkeypatch, mint):
    from adk.agent import AitherAgent
    from adk.tools import ToolRegistry

    monkeypatch.setattr(vl, "mint_agent_credential", mint)
    agent = AitherAgent.__new__(AitherAgent)
    agent.name, agent.credential_grants = "researcher", list(grants)
    agent._tools = ToolRegistry()
    seen = []

    async def _raw(name, arguments, auth=None, **kw):
        seen.append(vl._agent_credential())
        return "ok"

    agent._tools.execute = _raw
    agent._wrap_tools_with_agent_credential()
    return agent, seen


def test_every_tool_call_runs_under_the_agents_credential(monkeypatch):
    """Not only chat(): stream_react / stream_respond / swarm all call _tools.execute."""
    import asyncio

    minted = []

    def _mint(a, g, t=900):
        minted.append(a)
        return f"cred-{a}-{g[0]}"

    agent, seen = _agent_with(["secret:K"], monkeypatch, _mint)

    async def two_calls():
        await agent._tools.execute("vault_get", {})
        await agent._tools.execute("vault_get", {})

    asyncio.run(two_calls())
    assert seen == ["cred-researcher-secret:K"] * 2
    assert minted == ["researcher"]  # cached until a minute before expiry
    assert vl._RUN_CREDENTIAL.get() == ""


def test_a_tool_call_whose_minting_fails_never_runs(monkeypatch):
    import asyncio

    def _refuse(*a, **k):
        raise RuntimeError("agent credential refused (403)")

    agent, seen = _agent_with(["secret:K"], monkeypatch, _refuse)
    with pytest.raises(RuntimeError):
        asyncio.run(agent._tools.execute("vault_get", {}))
    assert seen == []
