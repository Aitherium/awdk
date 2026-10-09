"""An agent run's vault client sends its agent credential and nothing wider."""
import adk.vault_lockbox as vl


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
