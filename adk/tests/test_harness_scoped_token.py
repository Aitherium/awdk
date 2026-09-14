"""The per-node harness token is PATH-SCOPED, and the scope is fail-closed (D1).

WHY THIS GATE EXISTS

`adk rc` advertises this machine's session daemon through a public tunnel so a
phone can read the session list and answer a decision card. The daemon's ROOT
bearer cannot be what travels that path: it can `POST /sessions`, which spawns a
coding agent with filesystem access on this machine. One stolen header would be
remote code execution on the owner's laptop.

So `adk rc` mints a token scoped to `/sessions`, `/decisions` and
`/desk/fleet/status`, and the daemon refuses it everywhere else BEFORE the
handler runs. The direction of the default is the load-bearing decision: an
unrecognised path is REFUSED, so a route added tomorrow is not silently inside
every scoped token minted today.

The owner bearer is deliberately unscoped and stays that way — the owner holds
the box, the sessions and the filesystem those sessions write to, so scoping
them here would be theatre.
"""

import json

import pytest

from adk.harnesses import daemon as D


# ══════════════════════════════════════════════════════════════════════════
# Principal.may_reach
# ══════════════════════════════════════════════════════════════════════════

def _scoped(*paths):
    return D.Principal(id="node:n1", plan="link", paths=paths or D.SCOPED_LINK_PATHS)


def test_owner_is_unscoped():
    assert D.OWNER_PRINCIPAL.paths == ()
    for p in ("/sessions", "/awrun/submit", "/anything/at/all", "/"):
        assert D.OWNER_PRINCIPAL.may_reach(p) is True


@pytest.mark.parametrize("path", [
    "/sessions", "/sessions/unified", "/sessions/abc/stream", "/sessions/abc/input",
    "/decisions", "/decisions/1/wait", "/desk/fleet/status",
])
def test_scoped_token_reaches_its_own_surface(path):
    assert _scoped().may_reach(path) is True


@pytest.mark.parametrize("path", [
    # The routes that make the root token dangerous.
    "/awrun/submit", "/awrun/status", "/rooms", "/profiles", "/harnesses",
    "/agents", "/fs/read", "/auth/link/status/x",
    # Sibling-prefix confusion: matching must be on whole SEGMENTS.
    "/sessions-admin", "/decisionsx", "/desk/fleet/statuses",
    # The parent of an allowed leaf is not implied by the leaf.
    "/desk", "/desk/fleet", "/desk/app",
    # Traversal is refused outright rather than normalised.
    "/sessions/../awrun/submit", "/decisions/../../etc/passwd",
    "/", "",
])
def test_scoped_token_is_refused_everywhere_else(path):
    assert _scoped().may_reach(path) is False


def test_an_unknown_new_route_is_refused_by_default():
    """The direction that matters: a route nobody thought about is OUTSIDE the
    scope, not inside it."""
    assert _scoped().may_reach("/some/route/invented/tomorrow") is False


def test_case_is_not_a_bypass():
    assert _scoped().may_reach("/SESSIONS/unified") is True
    assert _scoped().may_reach("/AWRUN/submit") is False


# ══════════════════════════════════════════════════════════════════════════
# minting + registry round-trip
# ══════════════════════════════════════════════════════════════════════════

def test_mint_writes_only_a_hash(tmp_path):
    reg_path = tmp_path / "harness_tokens.json"
    token = D.mint_scoped_token("node:n1", path=reg_path)
    raw = reg_path.read_text(encoding="utf-8")
    assert token not in raw, "the plaintext token must never touch disk"
    entry = json.loads(raw)
    assert len(entry) == 1
    (meta,) = entry.values()
    assert meta["paths"] == list(D.SCOPED_LINK_PATHS)
    assert meta["principal"] == "node:n1"
    assert meta["expires_at"] > 0


def test_minted_token_resolves_to_a_scoped_principal(tmp_path, monkeypatch):
    reg_path = tmp_path / "harness_tokens.json"
    monkeypatch.setattr(D, "PRINCIPALS_PATH", reg_path)
    token = D.mint_scoped_token("node:n1", path=reg_path)
    p = D.resolve_principal(token, "the-owner-bearer")
    assert p is not None
    assert p.id == "node:n1"
    assert p.paths == D.SCOPED_LINK_PATHS
    assert p.may_reach("/sessions/unified") is True
    assert p.may_reach("/awrun/submit") is False


def test_the_owner_bearer_survives_a_registry(tmp_path, monkeypatch):
    """Adding a scoped token must not lock the operator out of their own daemon."""
    reg_path = tmp_path / "harness_tokens.json"
    monkeypatch.setattr(D, "PRINCIPALS_PATH", reg_path)
    D.mint_scoped_token("node:n1", path=reg_path)
    owner = D.resolve_principal("the-owner-bearer", "the-owner-bearer")
    assert owner is D.OWNER_PRINCIPAL


def test_an_expired_scoped_token_is_refused(tmp_path, monkeypatch):
    reg_path = tmp_path / "harness_tokens.json"
    monkeypatch.setattr(D, "PRINCIPALS_PATH", reg_path)
    token = D.mint_scoped_token("node:n1", ttl_days=0, path=reg_path)
    # ttl_days=0 stores expires_at=0, which means "no expiry" in this registry.
    assert D.resolve_principal(token, "owner") is not None
    # A real expiry in the past is refused, and does NOT fall through to owner.
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    (digest,) = reg
    reg[digest]["expires_at"] = 1.0
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    assert D.resolve_principal(token, "owner") is None


def test_minting_twice_keeps_both_and_drops_the_expired(tmp_path, monkeypatch):
    reg_path = tmp_path / "harness_tokens.json"
    monkeypatch.setattr(D, "PRINCIPALS_PATH", reg_path)
    a = D.mint_scoped_token("node:a", path=reg_path)
    # Age the first one out so the mint below must prune it.
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    (digest,) = reg
    reg[digest]["expires_at"] = 1.0
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    b = D.mint_scoped_token("node:b", path=reg_path)
    assert D.resolve_principal(a, "owner") is None
    assert D.resolve_principal(b, "owner").id == "node:b"
    assert len(json.loads(reg_path.read_text(encoding="utf-8"))) == 1


# ══════════════════════════════════════════════════════════════════════════
# the gate, through the real app
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def scoped_client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    reg_path = tmp_path / "harness_tokens.json"
    monkeypatch.setattr(D, "PRINCIPALS_PATH", reg_path)
    monkeypatch.setattr(D, "TOKEN_PATH", tmp_path / "harness_token")
    token = D.mint_scoped_token("node:n1", path=reg_path)
    app = D.create_app(token="owner-bearer-for-this-test")
    return TestClient(app), token


def test_scoped_token_is_403_on_an_out_of_scope_route(scoped_client):
    client, token = scoped_client
    r = client.get("/harnesses", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert "not scoped" in r.text


def test_scoped_token_reaches_sessions(scoped_client):
    client, token = scoped_client
    r = client.get("/sessions", headers={"Authorization": f"Bearer {token}"})
    # Whatever the handler answers, it must not be the SCOPE refusal.
    assert r.status_code != 403 or "not scoped" not in r.text


def test_owner_bearer_still_reaches_everything(scoped_client):
    client, _ = scoped_client
    r = client.get("/harnesses", headers={"Authorization": "Bearer owner-bearer-for-this-test"})
    assert r.status_code == 200


def test_no_credential_is_still_401(scoped_client):
    client, _ = scoped_client
    assert client.get("/harnesses").status_code == 401
