"""Extension pairing: POST /local/extension-pair/start -> owner approval -> poll.

The Awconnect extension cannot read ``~/.aither/daemon-token``, so with
AITHER_LOCAL_AUTH=required every Local-mode route (chat, agents, MCP) answered
401. The fix is a pairing the OWNER approves out of band with that credential
(adk/extension_pair.py); the token it earns opens only SCOPE_PATHS, expires, is
stored hashed, and is refused from any non-extension Origin. Pinned here, on the
real app in required mode:

- start/poll answer ONLY an allowlisted extension Origin on a loopback Host;
- nothing opens before the owner approves, and approval needs the local token;
- the token opens /chat/stream, /mcp (past the MCP key too), /agents and the
  identity routes -- and nothing else (/cli/execute, /chat stay 401);
- issued exactly once, refused from another origin, revocable, expiring.
"""

import hashlib
import json
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from adk import extension_pair
from adk.agent import AitherAgent
from adk.extension_id import PINNED_EXTENSION_ID, STORE_EXTENSION_ID
from adk.local_auth import HEADER, read_token
from adk.server import create_app

PINNED = f"chrome-extension://{PINNED_EXTENSION_ID}"
STORE = f"chrome-extension://{STORE_EXTENSION_ID}"
OTHER = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
LOOP = {"host": "127.0.0.1:9001"}
MCP_KEY = "mcp-key-for-extension-pair-tests"


def _new_client(monkeypatch, tmp_path, **env):
    for k in ("AITHER_OFFLINE", "AITHER_SERVER_API_KEY", "AITHER_CORS_ORIGINS",
              "AITHER_BROWSER_HANDOFF", "AITHER_TRUSTED_EXTENSION_IDS",
              "AITHER_EXTENSION_IDS", "AITHER_MCP_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv("AITHER_LOCAL_AUTH", "required")
    # The token file is HOME-based, not AITHER_HOME-based: pin it to the tmp dir
    # so a test can never read or rotate the real owner's credential.
    monkeypatch.setenv("AITHER_LOCAL_TOKEN_FILE", str(tmp_path / "daemon-token"))
    # A pairing raises an owner card: keep it in a tmp store and never pop a window.
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    import adk.decisions.notify as notify_mod
    import adk.decisions.store as store_mod

    if hasattr(store_mod, "_STORE"):
        monkeypatch.setattr(store_mod, "_STORE", None, raising=False)
    monkeypatch.setattr(notify_mod, "notify", lambda *a, **k: None)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    agent = MagicMock(spec=AitherAgent)
    agent.name = "test-agent"
    agent.llm = MagicMock()
    agent.llm.provider_name = "mock"
    return TestClient(create_app(agent=agent), client=("127.0.0.1", 50123),
                      raise_server_exceptions=False)


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    return _new_client(monkeypatch, tmp_path)


def _start(c, origin=PINNED, host=LOOP):
    return c.post("/local/extension-pair/start", headers={"origin": origin, **host})


def _poll(c, pair_id, origin=PINNED):
    return c.post("/local/extension-pair/poll", json={"pair_id": pair_id},
                  headers={"origin": origin, **LOOP})


def _approve(c, code, token=None):
    return c.post("/local/extension-pair/approve", json={"code": code},
                  headers={HEADER: read_token() if token is None else token})


def _pair(c, origin=PINNED):
    return _approve_and_take(c, _start(c, origin=origin).json(), origin=origin)


def _approve_and_take(c, started, origin=PINNED):
    assert _approve(c, started["code"]).status_code == 200
    done = _poll(c, started["pair_id"], origin=origin)
    assert done.status_code == 200, done.text
    return done.json()["token"]


def test_the_whole_flow_issues_a_scoped_token_exactly_once(daemon, tmp_path):
    started = _start(daemon)
    assert started.status_code == 200, started.text
    body = started.json()
    assert len(body["code"]) == 6 and body["code"].isdigit()
    assert body["expires_in"] == extension_pair.PAIR_TTL_S
    assert body["command"] == f"adk awconnect pair approve {body['code']}"

    # Nothing is granted before the owner approves.
    assert _poll(daemon, body["pair_id"]).json()["status"] == "pending"
    tok = _approve_and_take(daemon, body)
    assert tok.startswith(extension_pair.TOKEN_PREFIX)

    # Stored hashed, never in plaintext, and never the local token.
    raw = extension_pair.store_path().read_text(encoding="utf-8")
    assert tok not in raw and read_token() not in raw
    stored = json.loads(raw)["tokens"]
    assert hashlib.sha256(tok.encode("utf-8")).hexdigest() in stored
    assert stored[hashlib.sha256(tok.encode("utf-8")).hexdigest()]["origin"] == PINNED

    # Issued once: the same pair_id is gone.
    assert _poll(daemon, body["pair_id"]).status_code == 410


def test_the_token_opens_the_local_surface_and_nothing_else(daemon):
    tok = _pair(daemon)
    auth = {"Authorization": f"Bearer {tok}"}
    # The real chat lane answers its OWN validation (400 empty_message), which is
    # the proof the auth gate opened without running an agent turn.
    r = daemon.post("/chat/stream", json={"message": ""}, headers=auth)
    assert r.status_code == 400 and r.json()["detail"] == "empty_message"
    # And the gates that were shut stay shut.
    assert daemon.post("/chat/stream", json={"message": "x"}).status_code == 401
    assert daemon.post("/cli/execute", json={"command": "status"}, headers=auth).status_code == 401
    assert daemon.post("/chat", json={"message": "x"}, headers=auth).status_code == 401
    assert daemon.post("/x-session/import", json={}, headers=auth).status_code == 401


def test_the_c1_overlay_routes_open_for_a_paired_token(daemon):
    """Contract C1: the overlay sends GET /v1/models, POST /v1/chat/completions
    and POST /mcp to the adk base with the paired bearer. A 401 on any of them
    reads as "try the next port", so each must clear the auth gate here or the
    paired daemon is skipped for awnode without a word."""
    from adk.mcp_server import MCPServer

    MCPServer(server_name="c1-test").mount(daemon.app)
    tok = _pair(daemon)
    hdrs = {"Authorization": f"Bearer {tok}", "origin": PINNED}
    assert daemon.get("/v1/models").status_code == 401           # no token, still shut
    assert daemon.get("/v1/models", headers=hdrs).status_code == 200
    # Past the gate the route answers its OWN validation (an empty body), not 401.
    assert daemon.post("/v1/chat/completions", json={}, headers=hdrs).status_code != 401
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert daemon.post("/mcp", json=rpc, headers=hdrs).status_code == 200
    # /health is a skip-auth path; a route outside the scope stays refused.
    assert daemon.get("/health", headers=hdrs).status_code == 200
    assert daemon.post("/cli/execute", json={"command": "status"},
                       headers=hdrs).status_code == 401


def test_an_mcp_session_works_even_though_the_mcp_key_is_set(monkeypatch, tmp_path):
    from adk.mcp_server import MCPServer

    # required local auth AND an API/MCP key: two independent gates, both shut.
    c = _new_client(monkeypatch, tmp_path, AITHER_MCP_KEY=MCP_KEY,
                    AITHER_SERVER_API_KEY=MCP_KEY)
    MCPServer(server_name="pair-test").mount(c.app)
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    # Unpaired: the local-auth middleware answers 401 before MCP is reached.
    assert c.post("/mcp", json=rpc).status_code == 401
    tok = _pair(c)
    r = c.post("/mcp", json=rpc, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert "result" in r.json()
    # The MCP key still works, unchanged.
    r2 = c.post("/mcp", json=rpc, headers={"Authorization": f"Bearer {MCP_KEY}"})
    assert r2.status_code == 200, r2.text


def test_the_token_is_refused_from_any_other_origin(daemon):
    tok = _pair(daemon)
    # A first-party web page passes CSRF but must not inherit the extension's token.
    r = daemon.post("/chat/stream", json={"message": ""},
                    headers={"Origin": "https://aitherium.com", "Authorization": f"Bearer {tok}"})
    assert r.status_code == 401
    r2 = daemon.post("/chat/stream", json={"message": ""},
                     headers={"Origin": OTHER, "Authorization": f"Bearer {tok}"})
    assert r2.status_code == 401
    # A paired extension POSTs with its own Origin and GETs without one: both work.
    r3 = daemon.post("/chat/stream", json={"message": ""},
                     headers={"Origin": PINNED, "Authorization": f"Bearer {tok}"})
    assert r3.status_code == 400 and r3.json()["detail"] == "empty_message"
    r4 = daemon.get("/identity/whoami", headers={"Authorization": f"Bearer {tok}"})
    assert r4.status_code == 200


def test_a_forged_or_unknown_token_is_401(daemon):
    for bad in ("", "aep_" + "x" * 40, "abt_whatever", "not-a-token"):
        h = {"Authorization": f"Bearer {bad}"} if bad else {}
        assert daemon.post("/chat/stream", json={"message": ""}, headers=h).status_code == 401


def test_start_and_poll_refuse_anything_but_an_allowlisted_extension(daemon):
    assert _start(daemon, origin=OTHER).status_code == 403
    assert _start(daemon, origin="https://aitherium.com").status_code == 403
    assert daemon.post("/local/extension-pair/start", headers=LOOP).status_code == 403
    assert _start(daemon, host={"host": "evil.example:9001"}).status_code == 403
    started = _start(daemon).json()
    assert _poll(daemon, started["pair_id"], origin=OTHER).status_code == 403


def test_the_store_builds_id_pairs_too(daemon):
    # The Chrome Web Store install has a different (first-party) id; it must pair.
    assert _start(daemon, origin=STORE).status_code == 200


def test_approval_demands_the_owners_local_credential(daemon):
    started = _start(daemon).json()
    # No credential, a wrong one, and the paired token itself all fail.
    assert daemon.post("/local/extension-pair/approve",
                       json={"code": started["code"]}).status_code == 401
    assert _approve(daemon, started["code"], token="wrong").status_code == 401
    assert _approve(daemon, "000000", token=" ") .status_code == 401
    assert _approve(daemon, "000000").status_code == 404  # right credential, no such code
    assert extension_pair.store_path().exists() is False  # nothing minted, nothing written
    # Only the owner's credential approves.
    assert _approve(daemon, started["code"]).status_code == 200
    tok = _poll(daemon, started["pair_id"]).json()["token"]
    r = daemon.post("/local/extension-pair/approve", json={"code": "000000"},
                    headers={HEADER: tok})
    assert r.status_code == 401


def test_pending_and_revoke_are_owner_routes(daemon):
    started = _start(daemon).json()
    assert daemon.get("/local/extension-pair/pending").status_code == 401
    r = daemon.get("/local/extension-pair/pending", headers={HEADER: read_token()})
    assert r.status_code == 200
    (row,) = r.json()["pending"]
    assert row["code"] == started["code"] and row["origin"] == PINNED

    tok = _pair(daemon)
    auth = {"Authorization": f"Bearer {tok}"}
    assert daemon.post("/chat/stream", json={"message": ""}, headers=auth).status_code == 400
    assert daemon.post("/local/extension-pair/revoke",
                       headers={HEADER: read_token()}).json()["revoked"] == 1
    assert daemon.post("/chat/stream", json={"message": ""}, headers=auth).status_code == 401


def test_an_expired_code_or_pair_id_is_gone(daemon, monkeypatch):
    started = _start(daemon).json()
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + extension_pair.PAIR_TTL_S + 1)
    assert _approve(daemon, started["code"]).status_code == 404
    assert _poll(daemon, started["pair_id"]).status_code == 410


def test_an_expired_token_is_401(daemon):
    tok = _pair(daemon)
    auth = {"Authorization": f"Bearer {tok}"}
    raw = json.loads(extension_pair.store_path().read_text(encoding="utf-8"))
    (digest,) = raw["tokens"]
    raw["tokens"][digest]["expires_at"] = time.time() - 1
    extension_pair.store_path().write_text(json.dumps(raw), encoding="utf-8")
    assert daemon.post("/chat/stream", json={"message": ""}, headers=auth).status_code == 401


def test_pending_pairings_are_bounded(daemon):
    for _ in range(3):
        assert _start(daemon).status_code == 200
    assert _start(daemon).status_code == 429


def test_scope_matching_fails_closed():
    assert extension_pair.path_allowed("/chat/stream", "POST")
    assert extension_pair.path_allowed("/agents", "GET")
    assert extension_pair.path_allowed("/agents/other/sessions", "GET")
    assert extension_pair.path_allowed("/mcp", "POST")
    assert extension_pair.path_allowed("/v1/models", "GET")
    assert not extension_pair.path_allowed("/v1/models", "POST")
    assert not extension_pair.path_allowed("/agents/x/chat", "POST")   # GET-only entry
    assert not extension_pair.path_allowed("/cli/execute", "POST")
    assert not extension_pair.path_allowed("/chat/stream", None)
    assert not extension_pair.path_allowed("/chat", "POST")
    assert not extension_pair.path_allowed("/local/browser-pair", "POST")


def test_the_browser_and_extension_token_families_do_not_cross_scopes(daemon):
    """browser_grant (``abt_``, origin-bound, a signed-in PAGE) and extension_pair
    (``aep_``, owner-approved, the first-party EXTENSION) are separate scopes on
    purpose. The page token still never reaches the agent loop or /mcp -- the
    guarantee SCOPE_PATHS' ``node`` scope documents -- and the extension token
    opens none of the browser surfaces."""
    from adk import browser_grant

    tok = _pair(daemon)
    ext = {"Authorization": f"Bearer {tok}"}
    # The extension token opens no browser surface, even from a first-party page origin.
    assert daemon.get("/kvholder/status",
                      headers={**ext, "Origin": "https://desktop.aitherium.com"}).status_code == 401
    # (/v1/models is open to BOTH families since contract C1: the browser
    # ``node`` scope and the overlay's model probe read the same listing.)
    # A page token opens no extension surface -- not the agent loop, not the tools.
    page = browser_grant.issue_token("https://desktop.aitherium.com", "kvholder")[0]
    ph = {"Authorization": f"Bearer {page}", "Origin": "https://desktop.aitherium.com"}
    assert daemon.post("/chat/stream", json={"message": ""}, headers=ph).status_code == 401
    assert daemon.get("/agents", headers=ph).status_code == 401
    assert daemon.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers=ph).status_code == 401


# --- the owner approves from a card (desk, phone, watch), not only a terminal --------

def _card_for(started):
    from adk.decisions.store import get_store

    cards = [c for c in get_store().list() if started["code"] in (c.summary or "")]
    assert len(cards) == 1, "a pairing raises exactly one owner card"
    return cards[0]


def test_a_pairing_raises_an_owner_card_with_the_code(daemon):
    started = _start(daemon).json()
    card = _card_for(started)
    assert card.dedupe_key.startswith("awconnect-pair:"), "the harness owner-only guard keys on this"
    assert card.default_key == "deny"
    assert [o.key for o in card.options] == ["allow", "deny"]


def test_allow_on_the_card_pairs_the_extension(daemon):
    from adk.decisions.store import get_store

    started = _start(daemon).json()
    assert _poll(daemon, started["pair_id"]).json() == {"status": "pending"}
    get_store().answer(_card_for(started).id, "allow", deliver=False)
    done = _poll(daemon, started["pair_id"])
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "approved" and done.json()["token"]


def test_deny_on_the_card_refuses_the_extension(daemon):
    from adk.decisions.store import get_store

    started = _start(daemon).json()
    get_store().answer(_card_for(started).id, "deny", deliver=False)
    assert _poll(daemon, started["pair_id"]).status_code == 403


# --- one step: the signed-in extension pairs with an Identity grant -----------------

KEY_HEX = "ab" * 32


def _signed_grant(nonce, origin=STORE, node="pc-1", scope="extension", key=KEY_HEX, ttl=120):
    from adk import browser_grant, node_commands

    t = int(time.time())
    g = {"kind": "browser-grant/v1", "node_id": node, "tenant_id": "t", "user_id": "u",
         "origin": origin, "nonce": nonce, "scope": scope,
         "issued_at": t, "expires_at": t + ttl}
    g["sig"] = node_commands.sign(key, node_commands.canonical(g, browser_grant.GRANT_FIELDS))
    return g


@pytest.fixture
def enrolled(daemon, monkeypatch):
    import adk.fleet_enroll as fe
    from adk import node_commands

    monkeypatch.setattr(fe, "_load_node_auth", lambda: {"node_id": "pc-1"})
    monkeypatch.setattr(node_commands, "load_key", lambda node_id: KEY_HEX if node_id == "pc-1" else "")
    return daemon


def _challenge(c, origin=STORE):
    return c.get("/local/extension-pair/challenge", headers={"origin": origin, **LOOP})


def _grant(c, grant, origin=STORE):
    return c.post("/local/extension-pair/grant", json={"grant": grant},
                  headers={"origin": origin, **LOOP})


def test_a_signed_in_extension_pairs_in_one_step(enrolled):
    ch = _challenge(enrolled)
    assert ch.status_code == 200, ch.text
    assert ch.json()["node_id"] == "pc-1"
    r = _grant(enrolled, _signed_grant(ch.json()["nonce"]))
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    # The same narrow token a card approval gives: it opens Local mode chat.
    ok = enrolled.post("/chat/stream", json={"message": "hi"},
                       headers={"Authorization": f"Bearer {token}", "origin": STORE, **LOOP})
    assert ok.status_code != 401


@pytest.mark.parametrize("why,grant_kw,origin", [
    ("forged signature", {"key": "cd" * 32}, STORE),
    ("another device", {"node": "someone-else"}, STORE),
    ("a page scope", {"scope": "kvholder"}, STORE),
    ("expired", {"ttl": -5}, STORE),
    ("granted to the other extension id", {"origin": PINNED}, STORE),
])
def test_a_bad_grant_earns_nothing(enrolled, why, grant_kw, origin):
    nonce = _challenge(enrolled, origin).json()["nonce"]
    r = _grant(enrolled, _signed_grant(nonce, **grant_kw), origin=origin)
    assert r.status_code == 401, why


def test_a_grant_is_spent_once(enrolled):
    nonce = _challenge(enrolled).json()["nonce"]
    g = _signed_grant(nonce)
    assert _grant(enrolled, g).status_code == 200
    assert _grant(enrolled, g).status_code == 401


def test_challenge_and_grant_answer_only_our_extension_on_loopback(enrolled):
    assert _challenge(enrolled, OTHER).status_code == 403
    assert enrolled.get("/local/extension-pair/challenge",
                        headers={"origin": STORE, "host": "evil.example"}).status_code == 403
    assert _grant(enrolled, {}, origin="https://app.aitherium.com").status_code == 403
