"""Harness children and connector tokens (connector slice S5).

A child starts from the daemon's ``os.environ``. These pin what reaches it:

* nothing connector-shaped is ever INHERITED (``CONNECTOR_*``, ``GH_TOKEN``,
  ``GITHUB_TOKEN``) -- a stale token in the daemon env used to reach every child;
* nor any Aitherium bearer (``AITHER_API_KEY`` & co.), and inside a child the
  saved ``adk login`` is no fallback: either would let a child that never opted
  in mint any connector token through ``/connectors/resolve``;
* with ``SessionConfig.connectors`` empty (the default) no resolve is made;
* opted in with ``["github"]``, the child gets the connected GitHub token as
  ``CONNECTOR_GITHUB_TOKEN`` + ``GH_TOKEN`` and a git credential helper that
  reads it from the environment -- and nothing else (no personal connector);
* the resolve carries the saved ``adk login`` bearer when the env has none, and
  defaults to the public portal, not the in-fleet Genesis host;
* any resolve failure REFUSES the spawn with the reason, never a silent empty env.

Every request goes through an ``httpx.MockTransport``; the git helper is proven
by running the real ``git credential fill`` with the env the child would get.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import httpx
import pytest

from adk import connectors as cn
from adk.harnesses.registry import HarnessSpec, Transport
from adk.harnesses.session import HarnessSession, SessionConfig, SessionState

GH = "gho_TEST-connected-github-token-0123456789"
BEARER = "aither-login-bearer-xyz"


class FakeResolve:
    def __init__(self, status: int = 200, env: dict | None = None):
        self.status = status
        self.env = {"CONNECTOR_GITHUB_TOKEN": GH} if env is None else env
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "connector:github:git not granted"})
        return httpx.Response(200, json={"success": True, "env": self.env})

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content or b"{}") for r in self.requests]


_TOKEN_VARS = ("CONNECTOR_GMAIL_TOKEN", "CONNECTOR_GITHUB_TOKEN", "GH_TOKEN",
               "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
_BEARERS = ("AITHER_API_KEY", "AITHER_IDENTITY_BEARER", "AITHER_SESSION_BEARER")


@pytest.fixture
def clean_env(monkeypatch):
    for name in (*_BEARERS, *_TOKEN_VARS, "AITHER_CONNECTORS_URL", "AITHER_GENESIS_URL",
                 "GENESIS_URL", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                 "GIT_CONFIG_VALUE_0", cn.HARNESS_CHILD_MARKER, "GIT_TERMINAL_PROMPT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cn, "_saved_bearer", lambda: "")
    return monkeypatch


@pytest.fixture
def resolver(clean_env):
    fake = FakeResolve()
    real = httpx.Client

    def _client(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)

    clean_env.setattr(httpx, "Client", _client)
    clean_env.setenv("AITHER_API_KEY", BEARER)
    return fake


def _spec() -> HarnessSpec:
    return HarnessSpec(
        id="env-probe", label="Env Probe", description="prints nothing",
        transport=Transport.ONESHOT_PER_TURN, binary=sys.executable, adapter="text",
        build_argv=lambda spec, launch: [spec.binary, "-c", "pass"],
    )


def _session(tmp_path, **cfg) -> HarnessSession:
    return HarnessSession(_spec(), SessionConfig(harness="env-probe", **cfg), None,
                          root=tmp_path)


def _connectorish(env: dict) -> dict:
    return {k: v for k, v in env.items()
            if k.startswith("CONNECTOR_") or k in cn.GITHUB_TOKEN_ENV}


# ── inheritance ──────────────────────────────────────────────────────────────

def test_no_opt_in_means_no_connector_env_and_no_resolve(resolver, monkeypatch, tmp_path):
    for name in _TOKEN_VARS:
        monkeypatch.setenv(name, f"stale-{name}")
    env = _session(tmp_path)._child_env()
    assert _connectorish(env) == {}
    assert resolver.requests == []


def test_strip_drops_every_connector_and_github_token():
    env = {"CONNECTOR_X_TOKEN": "1", "CONNECTOR_GITHUB_TOKEN": "2", "GH_TOKEN": "3",
           "GITHUB_TOKEN": "4", "GITHUB_ENTERPRISE_TOKEN": "5", "PATH": "p"}
    assert cn.strip_inherited_connector_env(env) == {"PATH": "p"}


# ── opted in ─────────────────────────────────────────────────────────────────

def test_opt_in_github_injects_only_github_for_git_and_gh(resolver, monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "ghp_daemon-stale")
    monkeypatch.setenv("CONNECTOR_GMAIL_TOKEN", "ya29.daemon-stale")
    resolver.env = {"CONNECTOR_GITHUB_TOKEN": GH, "CONNECTOR_GMAIL_TOKEN": "ya29.mail",
                    "CONNECTOR_GOOGLE_DRIVE_TOKEN": "ya29.drive"}
    env = _session(tmp_path, connectors=["github"], agent="demiurge")._child_env()
    assert _connectorish(env) == {"CONNECTOR_GITHUB_TOKEN": GH, "GH_TOKEN": GH}
    assert resolver.bodies == [{"connectors": ["github"], "purpose": "git",
                                "agent_id": "demiurge"}]
    req = resolver.requests[0]
    assert req.headers["authorization"] == f"Bearer {BEARER}"
    assert req.headers["x-aither-agent"] == "demiurge"
    # git: an empty helper resets inherited ones, then the env-reading helper
    n = int(env["GIT_CONFIG_COUNT"])
    pairs = [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(n)]
    assert pairs == [("credential.https://github.com.helper", ""),
                     ("credential.https://github.com.helper", cn.GIT_CREDENTIAL_HELPER)]
    assert GH not in cn.GIT_CREDENTIAL_HELPER and "$GH_TOKEN" in cn.GIT_CREDENTIAL_HELPER


def test_git_config_entries_append_to_existing_ones(resolver, monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    env = _session(tmp_path, connectors=["github"])._child_env()
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_0"] == "core.autocrlf"
    assert env["GIT_CONFIG_KEY_2"] == "credential.https://github.com.helper"


def test_resolve_is_cached_across_spawns_of_one_session(resolver, tmp_path):
    sess = _session(tmp_path, connectors=["github"])
    sess._child_env()
    sess._child_env()
    assert len(resolver.requests) == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_really_gets_the_token_from_the_helper(resolver, tmp_path):
    env = _session(tmp_path, connectors=["github"])._child_env()
    env["HOME"] = str(tmp_path)              # no user gitconfig helper interferes
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    out = subprocess.run(["git", "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, encoding="utf-8",
                         env=env, timeout=30, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert f"password={GH}" in out.stdout.splitlines()
    assert "username=x-access-token" in out.stdout.splitlines()


# ── off-fleet resolve ────────────────────────────────────────────────────────

def test_bearer_comes_from_the_saved_login_when_env_is_empty(resolver, monkeypatch,
                                                              tmp_path):
    for name in _BEARERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cn, "_saved_bearer", lambda: "saved-login-bearer")
    _session(tmp_path, connectors=["github"])._child_env()
    assert resolver.requests[0].headers["authorization"] == "Bearer saved-login-bearer"


def test_the_saved_login_is_read_from_the_config_file(monkeypatch, tmp_path):
    """The real reader, not a stub: ``adk login`` writes ``api_key`` to config.json."""
    import adk.config as config

    monkeypatch.setattr(config, "load_saved_config", lambda *a, **k: {"api_key": "from-file"})
    assert cn._saved_bearer() == "from-file"


def test_default_base_is_the_public_portal_not_the_fleet_host(resolver, tmp_path):
    assert cn.resolve_base() == "https://api.aitherium.com/api"
    _session(tmp_path, connectors=["github"])._child_env()
    assert str(resolver.requests[0].url) == "https://api.aitherium.com/api/connectors/resolve"


# ── failures are loud ────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,env,needle", [
    (403, None, "not granted"),
    (502, None, "HTTP 502"),
    (200, {}, "not connected"),
])
def test_resolve_failure_refuses_the_spawn(resolver, tmp_path, status, env, needle):
    resolver.status = status
    if env is not None:
        resolver.env = env
    with pytest.raises(cn.ConnectorEnvError, match=needle):
        _session(tmp_path, connectors=["github"])._child_env()


def test_no_sign_in_refuses_without_a_request(resolver, monkeypatch, tmp_path):
    for name in _BEARERS:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(cn.ConnectorEnvError, match="adk login"):
        _session(tmp_path, connectors=["github"])._child_env()
    assert resolver.requests == []


def test_a_non_injectable_connector_is_refused(resolver, tmp_path):
    with pytest.raises(cn.ConnectorEnvError, match="never env-injected"):
        _session(tmp_path, connectors=["gmail"])._child_env()
    assert resolver.requests == []


def test_a_refused_turn_says_why_and_starts_nothing(resolver, tmp_path):
    resolver.status = 403
    sess = _session(tmp_path, connectors=["github"])
    sess.start()
    assert sess.send("go") is True
    import time

    deadline = time.time() + 20
    while time.time() < deadline and sess.state == SessionState.BUSY:
        time.sleep(0.05)
    errors = [e for e in sess.events_since(0) if e["kind"] == "error"]
    assert errors and "not granted" in errors[-1]["text"]
    assert GH not in json.dumps(sess.events_since(0))


# ── a child holds no Aitherium bearer (it could mint ANY connector token) ────

def test_no_child_inherits_an_aitherium_bearer(resolver, monkeypatch, tmp_path):
    """Any of these bearers can call /connectors/resolve and mint every connector
    token -- a child that kept one would be opted in to everything."""
    for name in _BEARERS:
        monkeypatch.setenv(name, f"owner-{name}")
    for connectors in ([], ["github"]):
        env = _session(tmp_path, connectors=connectors)._child_env()
        assert {n: env[n] for n in _BEARERS if n in env} == {}, connectors
    # The daemon's own resolve still carried its bearer.
    assert resolver.requests[0].headers["authorization"].startswith("Bearer owner-")


def test_strip_drops_the_bearers_too():
    env = {name: "b" for name in _BEARERS}
    env["PATH"] = "p"
    assert cn.strip_inherited_connector_env(env) == {"PATH": "p"}


def test_a_child_that_never_opted_in_cannot_resolve_with_the_saved_login(
        resolver, monkeypatch, tmp_path):
    """Inside a child the saved ``adk login`` on disk is NOT a fallback bearer:
    with the env bearers stripped, an un-opted child's resolve carries none."""
    import asyncio

    monkeypatch.setattr(cn, "_saved_bearer", lambda: "owner-saved-login")
    child = _session(tmp_path)._child_env()
    for name in _BEARERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(cn.HARNESS_CHILD_MARKER, child[cn.HARNESS_CHILD_MARKER])
    assert cn._bearer() == ""
    real_async = httpx.AsyncClient

    def _async_client(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = httpx.MockTransport(resolver.handler)
        return real_async(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _async_client)
    asyncio.run(cn.resolve_connectors(["gmail"]))
    assert "authorization" not in resolver.requests[-1].headers
    with pytest.raises(cn.ConnectorEnvError, match="adk login"):
        cn.resolve_git_env("x")


def test_the_daemon_still_resolves_with_the_saved_login_inside_a_session(
        resolver, monkeypatch, tmp_path):
    """A daemon started from inside another harness session carries the marker;
    its opted-in resolve uses daemon_bearer, not the child-side _bearer."""
    for name in _BEARERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(cn.HARNESS_CHILD_MARKER, "outer")
    monkeypatch.setattr(cn, "_saved_bearer", lambda: "saved-login-bearer")
    env = _session(tmp_path, connectors=["github"])._child_env()
    assert env["GH_TOKEN"] == GH
    assert resolver.requests[0].headers["authorization"] == "Bearer saved-login-bearer"


def test_git_never_prompts_in_a_headless_child(resolver, tmp_path):
    env = _session(tmp_path, connectors=["github"])._child_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"


# ── refusals on the persistent and pty spawn paths ──────────────────────────

def test_a_refused_persistent_spawn_fails_the_session_and_says_why(resolver, tmp_path):
    """STRUCTURED_BIDI (claude stream-json): the refusal must not escape start()
    -- the daemon would answer POST /sessions with a 500."""
    resolver.status = 403
    spec = HarnessSpec(
        id="bidi-probe", label="Bidi Probe", description="never starts",
        transport=Transport.STRUCTURED_BIDI, binary=sys.executable, adapter="text",
        build_argv=lambda spec, launch: [spec.binary, "-c", "pass"],
    )
    sess = HarnessSession(spec, SessionConfig(harness="bidi-probe", connectors=["github"]),
                          None, root=tmp_path)
    sess.start()
    assert sess.state == SessionState.FAILED
    assert sess._proc is None
    errors = [e for e in sess.events_since(0) if e["kind"] == "error"]
    assert errors and "not started" in errors[-1]["text"]
    assert "not granted" in errors[-1]["text"]


def test_a_refused_pty_spawn_fails_the_terminal_and_opens_nothing(resolver, monkeypatch,
                                                                   tmp_path):
    from adk.harnesses import pty_session

    def _never(*a, **k):
        raise AssertionError("a refused session must not open a terminal")

    monkeypatch.setattr(pty_session._PtyBackend, "spawn", staticmethod(_never))
    resolver.status = 403
    spec = HarnessSpec(
        id="pty-probe", label="Pty Probe", description="never starts",
        transport=Transport.PTY_STREAM, binary=sys.executable, adapter="text",
        build_argv=lambda spec, launch: [spec.binary, "-c", "pass"],
    )
    sess = pty_session.PtyHarnessSession(
        spec, SessionConfig(harness="pty-probe", connectors=["github"]), None,
        root=tmp_path)
    sess.start()
    assert sess.state == SessionState.FAILED
    errors = [e for e in sess.events_since(0) if e["kind"] == "error"]
    assert errors and errors[-1]["text"].startswith("terminal not started:")
