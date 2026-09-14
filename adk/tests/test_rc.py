"""`adk rc` — the one paste-able command that makes this machine's sessions reachable.

The assertion that matters is a REFUSAL: if a scoped token cannot be minted, the
command must FAIL rather than advertise the daemon's root token. That token can
`POST /sessions`, which spawns a coding agent with filesystem access on this
machine; putting it on a public path would make one stolen header remote code
execution on the owner's laptop. Falling back "so the feature still works" is the
whole point of the command, inverted.

Second: it refuses to enrol without an identity. `adk enroll` already refuses,
and it must — an unauthenticated enrol binds the device to the tenant "personal"
and looks like it worked.
"""

import pytest

from adk import rc as RC


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ══════════════════════════════════════════════════════════════════════════
# harness url resolution
# ══════════════════════════════════════════════════════════════════════════

def test_default_harness_url(monkeypatch):
    monkeypatch.delenv("AITHER_HARNESS_URL", raising=False)
    monkeypatch.delenv("AITHER_HARNESS_PORT", raising=False)
    assert RC.default_harness_url() == "http://127.0.0.1:8362"


def test_harness_url_env_overrides(monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_URL", "http://127.0.0.1:9999/")
    assert RC.default_harness_url() == "http://127.0.0.1:9999"


def test_harness_port_env_overrides(monkeypatch):
    monkeypatch.delenv("AITHER_HARNESS_URL", raising=False)
    monkeypatch.setenv("AITHER_HARNESS_PORT", "9001")
    assert RC.default_harness_url() == "http://127.0.0.1:9001"


# ══════════════════════════════════════════════════════════════════════════
# the refusals
# ══════════════════════════════════════════════════════════════════════════

def test_not_signed_in_refuses(monkeypatch, capsys):
    monkeypatch.setattr(RC, "_signed_in", lambda: False)
    rc = RC.cmd_rc(_Args(node_class="laptop"))
    assert rc == 1
    assert "Not signed in" in capsys.readouterr().out


def test_a_failed_mint_refuses_rather_than_using_the_root_token(monkeypatch, capsys):
    """The load-bearing refusal. There is no fallback path here on purpose."""
    monkeypatch.setattr(RC, "_signed_in", lambda: True)

    from adk.harnesses import daemon as D

    def _boom(*a, **kw):
        raise OSError("read-only home directory")

    monkeypatch.setattr(D, "mint_scoped_token", _boom)
    rc = RC.cmd_rc(_Args(node_class="laptop"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "Refusing to advertise" in out
    assert "root token" in out


def test_the_advertised_token_is_scoped_not_the_root_token(monkeypatch, tmp_path):
    """What `adk rc` hands the link must resolve to a SCOPED principal."""
    from adk.harnesses import daemon as D

    reg = tmp_path / "harness_tokens.json"
    monkeypatch.setattr(D, "PRINCIPALS_PATH", reg)
    token = D.mint_scoped_token("node:this-device", path=reg)
    principal = D.resolve_principal(token, "the-root-bearer")
    assert principal.paths == D.SCOPED_LINK_PATHS
    assert principal.may_reach("/sessions/unified") is True
    assert principal.may_reach("/awrun/submit") is False
    assert token != "the-root-bearer"


# ══════════════════════════════════════════════════════════════════════════
# probe_harness — "ready" means the SCOPED token reaches it
# ══════════════════════════════════════════════════════════════════════════

def test_probe_reports_not_ready_when_nothing_listens():
    ready, detail = RC.probe_harness("http://127.0.0.1:1", "tok", timeout=0.5)
    assert ready is False
    assert detail


def test_probe_reports_not_ready_on_a_403(monkeypatch):
    """A daemon that is UP but refuses the scoped token is not ready.

    Probing an unauthenticated route (or the root token) would prove the daemon
    is alive while the thing the phone actually does still 403s.
    """
    import urllib.error

    def _raise(*a, **kw):
        raise urllib.error.HTTPError("u", 403, "forbidden", {}, None)

    monkeypatch.setattr(RC, "__name__", RC.__name__)  # keep module identity
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    ready, detail = RC.probe_harness("http://127.0.0.1:8362", "tok")
    assert ready is False
    assert "403" in detail


def test_probe_counts_sessions_on_a_200(monkeypatch):
    import io
    import urllib.request

    class _R(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **kw: _R(b'{"sessions":[{"id":"a"},{"id":"b"}]}'))
    ready, detail = RC.probe_harness("http://127.0.0.1:8362", "tok")
    assert ready is True
    assert "2 session" in detail


# ══════════════════════════════════════════════════════════════════════════
# the verb is registered and its flags exist
# ══════════════════════════════════════════════════════════════════════════

def _manifest():
    """The SAME introspection the reference generator and AitherShell use, so a
    verb that is invisible to them is visible here as a failure."""
    from adk.cli import build_command_manifest

    return {c["name"]: c for c in build_command_manifest()}


def test_rc_is_a_registered_verb_with_the_documented_flags():
    m = _manifest()
    assert "rc" in m, "adk rc is not registered -- the Add-device command does not exist"
    flags = {f for a in m["rc"].get("args", []) for f in (a.get("flags") or [])}
    for f in ("--node-class", "--harness-url", "--token-ttl-days", "--once"):
        assert f in flags, f


def test_enroll_carries_the_link_opt_out():
    m = _manifest()
    flags = {f for a in m["enroll"].get("args", []) for f in (a.get("flags") or [])}
    assert "--no-link" in flags
