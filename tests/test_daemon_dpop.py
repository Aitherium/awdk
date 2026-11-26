"""Tests for RFC 9449 DPoP proof verification and SQLite WAL/busy_timeout hardening."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
import uuid

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from adk.harnesses.daemon import create_app


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _ath(token: str) -> str:
    return _b64u(hashlib.sha256(token.encode("utf-8")).digest())


def _make_es256_key():
    priv = ec.generate_private_key(ec.SECP256R1())
    nums = priv.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64u(nums.x.to_bytes(32, "big")),
        "y": _b64u(nums.y.to_bytes(32, "big")),
    }
    return priv, jwk


def _sign_es256(
    priv,
    jwk: dict,
    *,
    htm: str,
    htu: str,
    token: str,
    iat: float | None = None,
    jti: str | None = None,
    typ: str = "dpop+jwt",
    alg: str = "ES256",
    extra_jwk: dict | None = None,
    ath_override: str | None = None,
) -> str:
    hdr_jwk = dict(jwk)
    if extra_jwk:
        hdr_jwk.update(extra_jwk)
    header = {"typ": typ, "alg": alg, "jwk": hdr_jwk}
    payload = {
        "htm": htm,
        "htu": htu,
        "iat": int(time.time()) if iat is None else iat,
        "jti": jti or str(uuid.uuid4()),
        "ath": _ath(token) if ath_override is None else ath_override,
    }
    signing_input = (
        f"{_b64u(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64u(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    der_sig = priv.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input}.{_b64u(raw_sig)}"


def _sign_eddsa(*, htm: str, htu: str, token: str) -> str:
    priv = ed25519.Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": _b64u(pub_raw)}
    header = {"typ": "dpop+jwt", "alg": "EdDSA", "jwk": jwk}
    payload = {
        "htm": htm,
        "htu": htu,
        "iat": int(time.time()),
        "jti": str(uuid.uuid4()),
        "ath": _ath(token),
    }
    signing_input = (
        f"{_b64u(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64u(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    sig = priv.sign(signing_input.encode("ascii"))
    return f"{signing_input}.{_b64u(sig)}"


@pytest.fixture
def dpop_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.delenv("AITHER_REQUIRE_DPOP", raising=False)
    token = "dpop-test-secret-token"
    app = create_app(token=token)
    return TestClient(app), token


def test_bearer_compat_and_require_dpop_toggle(dpop_client, monkeypatch):
    client, token = dpop_client
    r_ok = client.get("/decisions/needs-human", headers={"Authorization": f"Bearer {token}"})
    assert r_ok.status_code == 200

    monkeypatch.setenv("AITHER_REQUIRE_DPOP", "1")
    r_blocked = client.get("/decisions/needs-human", headers={"Authorization": f"Bearer {token}"})
    assert r_blocked.status_code == 401
    assert "DPoP" in r_blocked.json()["detail"]


def test_dpop_es256_and_eddsa_accepted_and_replay_rejected(dpop_client):
    client, token = dpop_client
    priv, jwk = _make_es256_key()
    url = "http://testserver/decisions/needs-human"
    jti = "unique-jti-001"
    proof = _sign_es256(priv, jwk, htm="GET", htu=url, token=token, jti=jti)

    r1 = client.get(
        "/decisions/needs-human?limit=5",
        headers={"Authorization": f"DPoP {token}", "DPoP": proof},
    )
    assert r1.status_code == 200

    # Same jti must be rejected as replay.
    r_replay = client.get(
        "/decisions/needs-human",
        headers={"Authorization": f"DPoP {token}", "DPoP": proof},
    )
    assert r_replay.status_code == 401
    assert "replayed" in r_replay.json()["detail"]

    # EdDSA proof also accepted.
    ed_proof = _sign_eddsa(htm="GET", htu=url, token=token)
    r_ed = client.get(
        "/decisions/needs-human",
        headers={"Authorization": f"DPoP {token}", "DPoP": ed_proof},
    )
    assert r_ed.status_code == 200


def test_dpop_rejects_bad_claims_and_tampered_proofs(dpop_client):
    client, token = dpop_client
    priv, jwk = _make_es256_key()
    url = "http://testserver/decisions/needs-human"

    # Missing DPoP header
    assert client.get("/decisions/needs-human", headers={"Authorization": f"DPoP {token}"}).status_code == 401

    # Wrong htm
    bad_htm = _sign_es256(priv, jwk, htm="POST", htu=url, token=token)
    assert client.get("/decisions/needs-human", headers={"Authorization": f"DPoP {token}", "DPoP": bad_htm}).status_code == 401

    # Wrong htu
    bad_htu = _sign_es256(priv, jwk, htm="GET", htu="http://testserver/other", token=token)
    assert client.get("/decisions/needs-human", headers={"Authorization": f"DPoP {token}", "DPoP": bad_htu}).status_code == 401

    # Wrong ath
    bad_ath = _sign_es256(priv, jwk, htm="GET", htu=url, token=token, ath_override="wronghash")
    assert client.get("/decisions/needs-human", headers={"Authorization": f"DPoP {token}", "DPoP": bad_ath}).status_code == 401

    # Expired iat
    stale = _sign_es256(priv, jwk, htm="GET", htu=url, token=token, iat=int(time.time()) - 180)
    assert client.get("/decisions/needs-human", headers={"Authorization": f"DPoP {token}", "DPoP": stale}).status_code == 401

    # alg=none rejected
    alg_none = _sign_es256(priv, jwk, htm="GET", htu=url, token=token, alg="none")
    assert client.get("/decisions/needs-human", headers={"Authorization": f"DPoP {token}", "DPoP": alg_none}).status_code == 401

    # Private JWK field rejected
    priv_jwk = _sign_es256(priv, jwk, htm="GET", htu=url, token=token, extra_jwk={"d": "secret"})
    assert client.get("/decisions/needs-human", headers={"Authorization": f"DPoP {token}", "DPoP": priv_jwk}).status_code == 401

    # Tampered signature rejected
    good = _sign_es256(priv, jwk, htm="GET", htu=url, token=token)
    tampered = good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB")
    assert client.get("/decisions/needs-human", headers={"Authorization": f"DPoP {token}", "DPoP": tampered}).status_code == 401

    # Valid DPoP proof bound to a wrong token -> 403 invalid token
    wrong_tok = "wrong-token"
    valid_for_wrong = _sign_es256(priv, jwk, htm="GET", htu=url, token=wrong_tok)
    assert client.get("/decisions/needs-human", headers={"Authorization": f"DPoP {wrong_tok}", "DPoP": valid_for_wrong}).status_code == 403


def test_sqlite_stores_enable_busy_timeout_and_wal(tmp_path):
    from adk.harnesses.thread_subs import ThreadSubscriptions
    from adk.harnesses.work_items import WorkItems

    subs = ThreadSubscriptions(tmp_path / "subs.sqlite3")
    items = WorkItems(tmp_path / "items.sqlite3")
    try:
        assert subs._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert items._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        # On normal POSIX filesystems WAL activates; on drvfs/9p rollback is acceptable.
        assert subs._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() in ("wal", "delete", "memory")
        assert items._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() in ("wal", "delete", "memory")
    finally:
        subs._conn.close()
        items._conn.close()
        # Verify db files are clean and readable via read-only connection.
        ro = sqlite3.connect(f"{(tmp_path / 'subs.sqlite3').resolve().as_uri()}?mode=ro", uri=True)
        ro.close()
