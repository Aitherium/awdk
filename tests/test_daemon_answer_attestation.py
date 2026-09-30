"""POST /decisions/{id}/answer: the daemon STORES the signed receipt; it attests nothing.

* the owner bearer (``*``) is readable by every local agent, so it can never label
  someone else as the answerer (``daemon:owner``), and no boolean it sends means
  anything: ``answer_attested`` is not a field, and on the card it is derived
  (advisory) from whether a receipt is attached;
* a registry principal holding ``decisions:attest`` BY NAME (Genesis) may LABEL the
  answerer -- a label only;
* ``answer_receipt`` is stored verbatim from any caller (the daemon cannot verify
  it; awstorage.attest does), but a malformed or oversized one is dropped;
* ``answered_surface`` keeps the surface label (defaults to ``via``);
* a hand-edited ``answer_attested: true`` in the card JSON reads back False.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

ROOT = "root-bearer-for-tests"
GENESIS = "genesis-registry-token"
SCOPED = "scoped-no-attest-token"
RECEIPT = {"alg": "ed25519", "kid": "ab" * 8, "sig": "cd" * 64,
           "receipt": {"v": 1, "card_id": "d-2n4x", "choice": "approve"}}


def _digest(tok: str) -> str:
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", ROOT)
    import adk.decisions.store as store_mod
    import adk.harnesses.daemon as daemon

    monkeypatch.setattr(store_mod, "_STORE", None, raising=False)
    reg = tmp_path / "harness_tokens.json"
    reg.write_text(json.dumps({
        _digest(GENESIS): {"principal": "genesis", "plan": "owner",
                           "entitlements": ["decisions:attest"], "paths": ["/decisions"]},
        _digest(SCOPED): {"principal": "helper", "plan": "owner",
                          "entitlements": ["*"], "paths": ["/decisions"]},
    }), encoding="utf-8")
    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", reg)
    try:
        import adk.decisions.notify as notify_mod

        monkeypatch.setattr(notify_mod, "notify", lambda *a, **k: None)
    except ImportError:
        pass
    from adk.decisions.store import DecisionCard, DecisionOption, get_store

    get_store().create(DecisionCard(id="d-2n4x", title="Approve?", default_key="approve",
                                    options=[DecisionOption(key="approve", label="yes"),
                                             DecisionOption(key="reject", label="no")]))
    return TestClient(daemon.create_app(token=ROOT))


def _answer(client, token, **extra):
    return client.post("/decisions/d-2n4x/answer",
                       json={"choice": "approve", "via": "desk", **extra},
                       headers={"Authorization": f"Bearer {token}"})


def test_owner_bearer_cannot_label_or_attest(env):
    r = _answer(env, ROOT, answered_by="david", answer_attested=True)
    assert r.status_code == 200, r.text
    card = r.json()["decision"]
    assert card["answered_by"] == "daemon:owner"
    assert card["answer_attested"] is False and card["answer_receipt"] is None


def test_wildcard_registry_principal_cannot_label(env):
    card = _answer(env, SCOPED, answered_by="david").json()["decision"]
    assert card["answered_by"] == "daemon:helper"


def test_attest_entitlement_is_a_label_only(env):
    card = _answer(env, GENESIS, answered_by="david", answer_attested=True).json()["decision"]
    assert card["answered_by"] == "david"
    assert card["answer_attested"] is False  # no receipt -> nothing attested


def test_receipt_is_stored_verbatim_and_the_flag_is_derived(env):
    card = _answer(env, GENESIS, answered_by="david", answer_receipt=RECEIPT,
                   answered_surface="desk").json()["decision"]
    assert card["answer_receipt"] == RECEIPT and card["answer_attested"] is True
    assert card["answered_surface"] == "desk" and card["answered_via"] == "desk"
    from adk.decisions.store import get_store

    stored = get_store().get("d-2n4x")
    assert stored.answer_receipt == RECEIPT and stored.answer_attested is True


def test_any_caller_may_attach_a_receipt_verifiers_judge_it(env):
    # The owner bearer can attach one; it cannot FORGE one -- awstorage.attest checks
    # the signature. Storing it is harmless.
    card = _answer(env, ROOT, answer_receipt=RECEIPT).json()["decision"]
    assert card["answer_receipt"] == RECEIPT and card["answered_by"] == "daemon:owner"


@pytest.mark.parametrize("bad", [
    {"alg": "ed25519", "sig": "00"},                        # no receipt body
    {"receipt": {}, "sig": "00"},                           # no alg
    {"alg": "ed25519", "receipt": {"pad": "x" * 9000}, "sig": "00"},   # oversized
])
def test_malformed_receipts_are_dropped(env, bad):
    card = _answer(env, GENESIS, answer_receipt=bad).json()["decision"]
    assert card["answer_receipt"] is None and card["answer_attested"] is False


def test_surface_defaults_to_via(env):
    card = _answer(env, ROOT).json()["decision"]
    assert card["answered_surface"] == "desk"


def test_hand_edited_boolean_reads_back_false():
    from adk.decisions.store import DecisionCard

    base = {"id": "d-x", "title": "t", "answered_by": "david"}
    for fake in (True, "true", 1):
        assert DecisionCard.from_dict({**base, "answer_attested": fake}).answer_attested is False
    with_receipt = DecisionCard.from_dict({**base, "answer_receipt": RECEIPT})
    assert with_receipt.answer_attested is True  # advisory: a receipt is attached


def test_store_answer_records_labels_and_receipt(tmp_path):
    from adk.decisions.store import DecisionCard, DecisionStore

    st = DecisionStore(tmp_path / "d")
    st.create(DecisionCard(id="d-2n4x", title="t", kind="info"))
    card = st.answer("d-2n4x", "ok", via="phone", deliver=False, answered_by="david",
                     answer_receipt=RECEIPT)
    assert card.answered_surface == "phone" and card.answer_receipt == RECEIPT
    again = st.get("d-2n4x")
    assert again.answer_receipt == RECEIPT and again.answered_by == "david"
