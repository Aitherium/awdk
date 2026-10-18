"""One credential door: every close carries a signed vault receipt.

Pinned here (2026-10-14):

* ``POST /decisions/{id}/answer`` with the CREDENTIAL_ANSWER marker and no
  receipt is a 409 — the marker is one constant string anybody holding the
  bearer can send (MCP ``decisions_answer`` included), so alone it closes
  nothing. A receipt signed by an UNTRUSTED key is a 409 too; one signed by
  this host's awseal key closes the card.
* A card closed through the host script's ``--value-stdin`` door (vault
  stubbed) carries ``credential_receipt.signed == True`` with the value's
  sha256 and never the value.
* ``reconcile_present`` closes a card whose secret is already in the vault
  ("already in vault (len N)"), resolves alias/superseded names through a
  recipe, never closes on "could not tell", never closes a ``cred:`` card
  refused-value or rotation ask — and never logs the value.

No test here may reach a real vault or put a window on a screen: the vault is
a stub, ``PYTEST_CURRENT_TEST`` holds the GUI guard, and the signing key is a
throwaway generated under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[2] / "AitherOS" / "packages" / "awseal"
try:
    import awseal  # noqa: F401
except ImportError:  # the monorepo carries it beside awdk
    sys.path.insert(0, str(_PKG))

# Signing needs awseal (+ cryptography). The monorepo has it beside awdk; a bare
# awdk install does not, and there these tests have nothing to sign with.
keygen = pytest.importorskip("awseal").keygen

from adk.decisions import secure_prompt  # noqa: E402
from adk.decisions.store import DecisionCard  # noqa: E402

TOKEN = "test-token-credential-door"
VALUE = "s3cret-value-NEVER-LOGGED"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated store + steer dirs, a throwaway signing key, a fresh singleton."""
    import adk.decisions.store as store_module

    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    monkeypatch.setenv("AWSEAL_KEY_PATH", str(keygen(tmp_path / "keys" / "signing.key")))
    monkeypatch.delenv("AITHER_CREDENTIAL_RECEIPT_KEYS", raising=False)
    monkeypatch.setattr(store_module, "_STORE", None)
    monkeypatch.setattr(secure_prompt, "_HOST_VAULT", [])
    return store_module.get_store()


def _cred(store, name="TEST_DOOR_KEY", scope="platform", dedupe_key=""):
    return store.create(DecisionCard.from_dict({
        "id": "", "title": f"need {name}", "kind": "credential",
        "secret_name": name, "credential_format": "api_key",
        "credential_scope": scope, "credential_description": "test",
        "dedupe_key": dedupe_key,
    }))


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient

    from adk.harnesses.daemon import create_app

    c = TestClient(create_app())
    c.headers = {"Authorization": f"Bearer {TOKEN}"}
    return c


# ── the daemon refuses a receipt-less close ─────────────────────────────────


def test_answer_with_the_bare_marker_is_409_and_the_card_stays_open(client, env):
    card = _cred(env)
    resp = client.post(f"/decisions/{card.id}/answer",
                       json={"choice": DecisionCard.CREDENTIAL_ANSWER})
    assert resp.status_code == 409, resp.text
    assert "/prompt" in resp.json()["detail"], "the refusal must name the way in"
    assert env.get(card.id).is_open


def test_a_receipt_signed_by_an_untrusted_key_is_409(client, env, tmp_path, monkeypatch):
    card = _cred(env)
    monkeypatch.setenv("AWSEAL_KEY_PATH", str(keygen(tmp_path / "other" / "k.key")))
    forged = secure_prompt._sign_receipt({"card_id": card.id, "secret_name": card.secret_name,
                                          "scope": "platform", "value_sha256": "0" * 64})
    monkeypatch.setenv("AWSEAL_KEY_PATH", str(tmp_path / "keys" / "signing.key"))
    assert forged["signed"] is True
    resp = client.post(f"/decisions/{card.id}/answer",
                       json={"choice": DecisionCard.CREDENTIAL_ANSWER,
                             "credential_receipt": forged})
    assert resp.status_code == 409, resp.text
    assert "untrusted" in resp.json()["detail"]
    assert env.get(card.id).is_open


def test_a_receipt_for_another_card_is_409(client, env):
    card = _cred(env)
    other = _cred(env, name="TEST_DOOR_OTHER")
    receipt = secure_prompt._sign_receipt({"card_id": other.id,
                                           "secret_name": card.secret_name})
    resp = client.post(f"/decisions/{card.id}/answer",
                       json={"choice": DecisionCard.CREDENTIAL_ANSWER,
                             "credential_receipt": receipt})
    assert resp.status_code == 409
    assert env.get(card.id).is_open


def test_a_receipt_signed_by_this_host_closes_the_card(client, env):
    card = _cred(env)
    receipt = secure_prompt._sign_receipt({
        "card_id": card.id, "secret_name": card.secret_name, "scope": "platform",
        "value_sha256": hashlib.sha256(VALUE.encode()).hexdigest(), "chars": len(VALUE)})
    resp = client.post(f"/decisions/{card.id}/answer",
                       json={"choice": DecisionCard.CREDENTIAL_ANSWER,
                             "credential_receipt": receipt})
    assert resp.status_code == 200, resp.text
    closed = env.get(card.id)
    assert not closed.is_open
    assert closed.credential_receipt["signed"] is True
    assert closed.answer_note == f"stored, {len(VALUE)} chars"


def test_the_prompt_route_is_owner_only_and_never_opens_a_window_under_test(client, env):
    card = _cred(env)
    resp = client.post(f"/decisions/{card.id}/prompt")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["launched"] is False and "suppressed under test" in body["detail"]
    anon = client.post(f"/decisions/{card.id}/prompt", headers={"Authorization": "Bearer x"})
    assert anon.status_code in (401, 403)


# ── the host script's stdin door closes through vault_credential ───────────


def _host_script():
    root = secure_prompt.find_repo_root()
    if root is None:
        return None
    path = root.joinpath(*secure_prompt.GUI_PROMPT_REL)
    spec = importlib.util.spec_from_file_location("secret_prompt_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SP = _host_script()


class _Stdin:
    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)

    def isatty(self):
        return False


@pytest.mark.skipif(_SP is None, reason="the host script lives in the monorepo checkout")
def test_value_stdin_closes_the_card_with_a_signed_digest_receipt(env, monkeypatch, capsys):
    card = _cred(env)
    held: dict = {}
    monkeypatch.setattr(_SP, "vault_set",
                        lambda n, v, scope="platform": (held.update({n: v}) or True, "stub"))
    monkeypatch.setattr(_SP, "vault_len", lambda n, scope="platform": len(held.get(n, "")))
    monkeypatch.setattr(_SP, "_card_store", lambda: (
        type(env), DecisionCard, secure_prompt.DecisionError))
    monkeypatch.setattr(_SP, "_door", lambda: secure_prompt)

    rc = _SP.run_value_stdin("platform", card.id, stream=_Stdin(VALUE.encode() + b"\n"))

    assert rc == 0, capsys.readouterr().err
    closed = env.get(card.id)
    assert not closed.is_open
    receipt = closed.credential_receipt
    assert receipt["signed"] is True
    assert receipt["payload"]["value_sha256"] == hashlib.sha256(VALUE.encode()).hexdigest()
    assert receipt["payload"]["card_id"] == card.id
    out = capsys.readouterr()
    assert VALUE not in json.dumps(closed.to_dict()) + out.out + out.err
    assert held == {card.secret_name: VALUE}


@pytest.mark.skipif(_SP is None, reason="the host script lives in the monorepo checkout")
def test_value_stdin_write_refused_leaves_the_card_open(env, monkeypatch, capsys):
    card = _cred(env)
    monkeypatch.setattr(_SP, "vault_set", lambda n, v, scope="platform": (False, "HTTP 403"))
    monkeypatch.setattr(_SP, "vault_len", lambda n, scope="platform": 0)
    monkeypatch.setattr(_SP, "_card_store", lambda: (
        type(env), DecisionCard, secure_prompt.DecisionError))
    monkeypatch.setattr(_SP, "_door", lambda: secure_prompt)

    rc = _SP.run_value_stdin("platform", card.id, stream=_Stdin(VALUE.encode()))

    assert rc == 1
    assert env.get(card.id).is_open
    err = capsys.readouterr().err
    assert "HTTP 403" in err and VALUE not in err


# ── a secret already in the vault closes its own card ───────────────────────


def test_reconcile_closes_a_card_whose_secret_is_present_and_never_logs_it(env):
    card = _cred(env, name="AITHER_POSTAL_ADDRESS")
    vault = {"AITHER_POSTAL_ADDRESS": VALUE}
    lines: list[str] = []

    closed = secure_prompt.reconcile_present(
        env, presence=lambda n, scope: len(vault.get(n, "")), recipes={}, log=lines.append)

    assert closed == [(card.id, "AITHER_POSTAL_ADDRESS", len(VALUE))]
    after = env.get(card.id)
    assert not after.is_open
    assert after.answer_note == f"already in vault (len {len(VALUE)})"
    assert after.credential_receipt["payload"]["present"] == {
        "AITHER_POSTAL_ADDRESS": len(VALUE)}
    assert VALUE not in "\n".join(lines) + json.dumps(after.to_dict())


def test_reconcile_closes_alias_and_superseded_cards_through_the_recipe(env):
    recipes = {"proton_bridge": {
        "names": ["proton_bridge_pw_{user}"],
        "aliases": ["AITHER_IMAP_PASS"], "supersedes": ["AITHER_IMAP_USER"]}}
    canonical = _cred(env, name="proton_bridge_pw_owner@example.com")
    alias = _cred(env, name="AITHER_IMAP_PASS")
    stale = _cred(env, name="AITHER_IMAP_USER")
    vault = {"proton_bridge_pw_owner@example.com": "x" * 16}

    secure_prompt.reconcile_present(env, presence=lambda n, s: len(vault.get(n, "")),
                                    recipes=recipes, log=lambda m: None)
    assert not env.get(canonical.id).is_open
    # No config user: an alias card has no concrete canonical name to probe, so
    # it stays open rather than guessing a template.
    assert env.get(alias.id).is_open and env.get(stale.id).is_open


def test_reconcile_resolves_the_recipe_user_from_config(env, tmp_path):
    (tmp_path / "mail.yaml").write_text("admin:\n  email: owner@example.com\n",
                                        encoding="utf-8")
    recipes_file = tmp_path / "credential_recipes.yaml"
    recipes = {"proton_bridge": {
        "names": ["proton_bridge_pw_{user}"], "aliases": ["AITHER_IMAP_PASS"],
        "supersedes": ["AITHER_IMAP_USER"], "user_from": "mail.yaml#admin.email"}}
    alias = _cred(env, name="AITHER_IMAP_PASS")
    stale = _cred(env, name="AITHER_IMAP_USER")
    vault = {"proton_bridge_pw_owner@example.com": "x" * 16}

    secure_prompt.reconcile_present(env, presence=lambda n, s: len(vault.get(n, "")),
                                    recipes=recipes, recipes_file=recipes_file,
                                    log=lambda m: None)

    assert not env.get(alias.id).is_open, "the canonical key supersedes the alias card"
    assert not env.get(stale.id).is_open, "the canonical key supersedes the user card"


def test_reconcile_never_closes_on_could_not_tell(env):
    card = _cred(env)
    secure_prompt.reconcile_present(env, presence=lambda n, s: -1, recipes={},
                                    log=lambda m: None)
    assert env.get(card.id).is_open


def test_reconcile_never_closes_a_card_raised_because_the_stored_value_was_refused(env):
    card = env.create(DecisionCard.from_dict({
        "id": "", "title": "Proton Bridge refused the stored password for owner@example.com",
        "kind": "credential", "secret_name": "proton_bridge_pw_owner@example.com",
        "credential_format": "password", "credential_scope": "platform",
        "credential_description": "refused"}))
    secure_prompt.reconcile_present(env, presence=lambda n, s: 16, recipes={},
                                    log=lambda m: None)
    assert env.get(card.id).is_open, "present AND refused: presence is what is wrong"


def test_reconcile_never_closes_a_rotation_ask(env):
    card = env.create(DecisionCard.from_dict({
        "id": "", "title": "Rotate the Cloudflare token", "kind": "credential",
        "secret_name": "CLOUDFLARE_API_TOKEN", "credential_format": "api_key",
        "credential_scope": "platform", "credential_description": "rotation"}))
    secure_prompt.reconcile_present(env, presence=lambda n, s: 40, recipes={},
                                    log=lambda m: None)
    assert env.get(card.id).is_open, "the OLD value being present is the card's premise"


def test_the_store_refuses_a_receipt_less_credential_close(env):
    card = _cred(env)
    with pytest.raises(secure_prompt.DecisionError, match="vault receipt"):
        env.answer(card.id, DecisionCard.CREDENTIAL_ANSWER)
    assert env.get(card.id).is_open


def test_reconcile_never_closes_an_ask_that_names_a_failure(env):
    # 2026-10-14: this exact title was closed "already in vault (len 22)" while mail
    # stayed dead on that stored value.
    card = env.create(DecisionCard.from_dict({
        "id": "", "title": "Proton Bridge password for owner@example.com (inbound mail stopped)",
        "kind": "credential", "secret_name": "proton_bridge_pw_owner@example.com",
        "credential_format": "password", "credential_scope": "platform",
        "credential_description": "bridge"}))
    secure_prompt.reconcile_present(env, presence=lambda n, s: 22, recipes={},
                                    log=lambda m: None)
    assert env.get(card.id).is_open, "an ask about a failure is answered by a NEW value"
