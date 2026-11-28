"""Comprehensive offline unit & in-process :8287 failover tests for Google AI Plan."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from adk.commands.auth import cmd_auth, register_parser
from adk.harnesses.local_bridge import BridgeRuntime, UpstreamSpec, create_server
from adk.llm import LLMRouter, Message, ToolCall
from adk.llm.google_ai_plan import (
    DEFAULT_MODEL,
    INTERACTIONS_ENDPOINT,
    SKIP_THOUGHT_SIGNATURE_SENTINEL,
    GoogleAIPlanClientBlockedError,
    GoogleAIPlanEligibilityError,
    GoogleAIPlanProvider,
    GoogleAIPlanQuotaError,
    GoogleAIPlanRateLimitError,
    GoogleAIPlanStreamError,
    build_interaction_request,
    clamp_thinking_level,
    parse_google_interactions_error,
)
from adk.llm.google_ai_plan_auth import (
    GOOGLE_AI_PLAN_REQUIRED_SCOPE,
    GOOGLE_AI_PLAN_SCOPES,
    GOOGLE_TOKEN_URL,
    GoogleAIPlanAuthError,
    GoogleAIPlanConfigError,
    GoogleAIPlanCredential,
    GoogleAIPlanCredentialStore,
    b64url_decode,
    b64url_encode,
    build_authorize_url,
    code_exchange_jti,
    create_dpop_proof,
    exchange_authorization_code,
    generate_dpop_jwk,
    generate_pkce_pair,
    get_valid_access_token,
    jwk_thumbprint,
    public_jwk,
    refresh_credential,
    resolve_oauth_client,
    validate_id_token,
)


def _make_fake_id_token(
    *,
    iss: str = "https://accounts.google.com",
    aud: str = "test-client-id",
    nonce: str = "test-nonce",
    exp: float | None = None,
    email: str = "user@example.com",
) -> str:
    hdr = b64url_encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8"))
    payload = {
        "iss": iss,
        "aud": aud,
        "nonce": nonce,
        "exp": int(time.time() + 3600) if exp is None else int(exp),
        "email": email,
    }
    body = b64url_encode(json.dumps(payload).encode("utf-8"))
    return f"{hdr}.{body}.fakesig"


def _decode_jws(jws: str) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    parts = jws.split(".")
    assert len(parts) == 3
    hdr = json.loads(b64url_decode(parts[0]).decode("utf-8"))
    payload = json.loads(b64url_decode(parts[1]).decode("utf-8"))
    sig = b64url_decode(parts[2])
    return hdr, payload, sig


def _verify_p256_raw_rs(jws: str, header_jwk: dict[str, str]) -> None:
    hdr_b64, pay_b64, sig_b64 = jws.split(".")
    raw_sig = b64url_decode(sig_b64)
    assert len(raw_sig) == 64, f"Expected raw 64-byte IEEE P1363 R||S, got {len(raw_sig)}"
    r = int.from_bytes(raw_sig[:32], "big")
    s = int.from_bytes(raw_sig[32:], "big")
    der_sig = encode_dss_signature(r, s)
    x = int.from_bytes(b64url_decode(header_jwk["x"]), "big")
    y = int.from_bytes(b64url_decode(header_jwk["y"]), "big")
    pub_key = ec.EllipticCurvePublicNumbers(x=x, y=y, curve=ec.SECP256R1()).public_key()
    pub_key.verify(der_sig, f"{hdr_b64}.{pay_b64}".encode("ascii"), ec.ECDSA(hashes.SHA256()))


# ── 1. DPoP Cryptography & OAuth Protocol Tests ──────────────────────────────


def test_dpop_proof_strict_4_field_jwk_and_raw_64_byte_p1363_signature() -> None:
    jwk = generate_dpop_jwk()
    assert set(jwk.keys()) == {"kty", "crv", "x", "y", "d"}
    pub = public_jwk(jwk)
    assert set(pub.keys()) == {"kty", "crv", "x", "y"}

    proof = create_dpop_proof(jwk, jti="jti-test-123", iat=1764285600, nonce="nonce-abc")
    hdr, payload, raw_sig = _decode_jws(proof)

    assert hdr["typ"] == "dpop+jwt"
    assert hdr["alg"] == "ES256"
    assert set(hdr["jwk"].keys()) == {"kty", "crv", "x", "y"}
    for forbidden in ("d", "ext", "key_ops", "alg", "kid"):
        assert forbidden not in hdr["jwk"]

    assert payload == {
        "jti": "jti-test-123",
        "htm": "POST",
        "htu": GOOGLE_TOKEN_URL,
        "iat": 1764285600,
        "nonce": "nonce-abc",
    }
    assert len(raw_sig) == 64
    _verify_p256_raw_rs(proof, hdr["jwk"])


def test_authorize_url_and_pkce_s256() -> None:
    verifier, challenge = generate_pkce_pair()
    assert challenge == b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    url = build_authorize_url(
        client_id="aither-client",
        redirect_uri="http://127.0.0.1:54321/oauth2callback",
        code_challenge=challenge,
        state="st123",
        nonce="no123",
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "code_challenge_method=S256" in url
    assert "access_type=" not in url
    assert "prompt=" not in url


def test_code_exchange_jti_sha256_omits_nonce_and_ignores_exchange_dpop_nonce() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["dpop"] = request.headers.get("DPoP")
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            headers={"DPoP-Nonce": "exchange-nonce-must-not-be-saved"},
            json={
                "access_token": "ya29.test-access",
                "refresh_token": "1//test-refresh",
                "expires_in": 3600,
                "scope": GOOGLE_AI_PLAN_SCOPES,
                "id_token": _make_fake_id_token(aud="cid-1", nonce="nonce-1"),
            },
        )

    cred = asyncio.run(
        exchange_authorization_code(
            code="auth-code-xyz",
            code_verifier="ver-1",
            redirect_uri="http://127.0.0.1:12345/oauth2callback",
            client_id="cid-1",
            client_secret="csec-1",
            expected_nonce="nonce-1",
            transport=httpx.MockTransport(handler),
        )
    )

    assert seen["url"] == GOOGLE_TOKEN_URL
    hdr, payload, _ = _decode_jws(seen["dpop"])
    _verify_p256_raw_rs(seen["dpop"], hdr["jwk"])
    assert payload["jti"] == code_exchange_jti("auth-code-xyz")
    assert "nonce" not in payload
    assert cred.dpop_nonce == ""  # exchange DPoP-Nonce is intentionally NOT saved
    assert "email" not in cred.to_dict()


def test_code_exchange_rejects_missing_peruserquota_scope() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.a",
                "refresh_token": "1//r",
                "expires_in": 3600,
                "scope": "openid email profile",
                "id_token": _make_fake_id_token(aud="cid-1", nonce="n-1"),
            },
        )

    with pytest.raises(GoogleAIPlanAuthError) as exc_info:
        asyncio.run(
            exchange_authorization_code(
                code="c",
                code_verifier="v",
                redirect_uri="http://127.0.0.1:9999/oauth2callback",
                client_id="cid-1",
                client_secret="csec-1",
                expected_nonce="n-1",
                transport=httpx.MockTransport(handler),
            )
        )
    assert exc_info.value.scope_missing is True


def test_oidc_id_token_validation_rejects_bad_claims() -> None:
    good = _make_fake_id_token(aud="cid", nonce="n1")
    claims = validate_id_token(good, client_id="cid", expected_nonce="n1")
    assert claims["aud"] == "cid"

    with pytest.raises(GoogleAIPlanAuthError):
        validate_id_token(_make_fake_id_token(iss="https://evil.example", aud="cid", nonce="n1"), client_id="cid", expected_nonce="n1")
    with pytest.raises(GoogleAIPlanAuthError):
        validate_id_token(_make_fake_id_token(aud="wrong-cid", nonce="n1"), client_id="cid", expected_nonce="n1")
    with pytest.raises(GoogleAIPlanAuthError):
        validate_id_token(_make_fake_id_token(aud="cid", nonce="wrong-n"), client_id="cid", expected_nonce="n1")
    with pytest.raises(GoogleAIPlanAuthError):
        validate_id_token(_make_fake_id_token(aud="cid", nonce="n1", exp=time.time() - 120), client_id="cid", expected_nonce="n1")


def test_refresh_handles_use_dpop_nonce_and_clock_skew_within_2_retries() -> None:
    jwk = generate_dpop_jwk()
    cred = GoogleAIPlanCredential(
        client_id="cid-1",
        access_token="old-access",
        refresh_token="1//refresh-bound",
        expires_at=time.time() - 10,
        scope=GOOGLE_AI_PLAN_SCOPES,
        dpop_jwk=jwk,
        dpop_nonce="",
    )

    seen_jtis: list[str] = []
    seen_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        proof = request.headers["DPoP"]
        hdr, payload, _ = _decode_jws(proof)
        _verify_p256_raw_rs(proof, hdr["jwk"])
        seen_jtis.append(payload["jti"])
        seen_payloads.append(payload)

        if len(seen_payloads) == 1:
            return httpx.Response(
                400,
                headers={"DPoP-Nonce": "fresh-nonce-1"},
                json={"error": "use_dpop_nonce"},
            )
        if len(seen_payloads) == 2:
            return httpx.Response(
                400,
                headers={"Date": "Fri, 27 Nov 2026 23:00:00 GMT"},
                json={"error": "invalid_dpop_proof"},
            )
        return httpx.Response(
            200,
            headers={"DPoP-Nonce": "fresh-nonce-2"},
            json={
                "access_token": "ya29.new-access",
                "expires_in": 3600,
            },
        )

    refreshed = asyncio.run(
        refresh_credential(cred, client_secret="csec-1", transport=httpx.MockTransport(handler))
    )
    assert len(seen_jtis) == 3
    assert len(set(seen_jtis)) == 3  # fresh random jti on every attempt
    assert "nonce" not in seen_payloads[0]
    assert seen_payloads[1]["nonce"] == "fresh-nonce-1"
    assert seen_payloads[2]["nonce"] == "fresh-nonce-1"
    assert seen_payloads[2]["iat"] == 1795820400  # Fri, 27 Nov 2026 23:00:00 GMT
    assert refreshed.access_token == "ya29.new-access"
    assert refreshed.refresh_token == "1//refresh-bound"
    assert refreshed.dpop_nonce == "fresh-nonce-2"


@pytest.mark.parametrize("err_code", ["invalid_grant", "unauthorized_client"])
def test_refresh_terminal_dead_grant_no_retry(err_code: str) -> None:
    cred = GoogleAIPlanCredential(
        client_id="cid-1",
        access_token="old",
        refresh_token="1//dead",
        expires_at=0.0,
        scope=GOOGLE_AI_PLAN_SCOPES,
        dpop_jwk=generate_dpop_jwk(),
    )
    calls = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": err_code})

    with pytest.raises(GoogleAIPlanAuthError) as exc_info:
        asyncio.run(refresh_credential(cred, client_secret="csec", transport=httpx.MockTransport(handler)))
    assert exc_info.value.dead_grant is True
    assert calls == 1


def test_concurrent_refresh_triggers_exactly_one_token_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AITHER_GOOGLE_AI_PLAN_CLIENT_ID", "cid-concurrent")
    monkeypatch.setenv("AITHER_GOOGLE_AI_PLAN_CLIENT_SECRET", "csec-concurrent")
    store = GoogleAIPlanCredentialStore(auth_path=tmp_path / "auth.json", use_keyring=False)
    cred = GoogleAIPlanCredential(
        client_id="cid-concurrent",
        access_token="stale",
        refresh_token="1//r",
        expires_at=time.time() + 60,  # inside 300s proactive lead window
        scope=GOOGLE_AI_PLAN_SCOPES,
        dpop_jwk=generate_dpop_jwk(),
    )
    store.save(cred, write_binding=False)

    calls = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"access_token": "ya29.fresh", "expires_in": 3600})

    transport = httpx.MockTransport(handler)

    async def _run():
        return await asyncio.gather(
            *[get_valid_access_token(store, transport=transport) for _ in range(5)]
        )

    results = asyncio.run(_run())
    assert calls == 1
    assert all(tok == "ya29.fresh" for tok, _ in results)


# ── 2. Storage & awseal Device Binding Tests ─────────────────────────────────


def test_private_file_storage_0600_and_unsealed_honest_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adk.llm.google_ai_plan_auth as auth_mod

    monkeypatch.setattr(auth_mod, "_try_awseal_modules", lambda: None)
    store = GoogleAIPlanCredentialStore(auth_path=tmp_path / "google_ai_plan_auth.json", use_keyring=False)
    cred = GoogleAIPlanCredential(
        client_id="cid-1",
        access_token="tok-1",
        refresh_token="ref-1",
        expires_at=time.time() + 3600,
        scope=GOOGLE_AI_PLAN_SCOPES,
        dpop_jwk=generate_dpop_jwk(),
    )
    backend = store.save(cred, write_binding=True)
    assert backend == "private-file"
    assert store.auth_path.exists()
    assert not store.binding_path.exists()
    if sys.platform != "win32":
        mode = stat.S_IMODE(os.stat(store.auth_path).st_mode)
        assert mode == 0o600
    assert store.binding_status(cred) == "unsealed"
    loaded = store.load(verify_binding=True)
    assert loaded is not None and loaded.access_token == "tok-1"


def test_keyring_storage_preferred_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import adk.llm.google_ai_plan_auth as auth_mod

    bucket: dict[tuple[str, str], str] = {}

    class _FakeKeyring:
        def set_password(self, svc: str, acct: str, val: str) -> None:
            bucket[(svc, acct)] = val

        def get_password(self, svc: str, acct: str) -> str | None:
            return bucket.get((svc, acct))

        def delete_password(self, svc: str, acct: str) -> None:
            bucket.pop((svc, acct), None)

    monkeypatch.setattr(auth_mod, "_try_keyring", lambda: _FakeKeyring())
    monkeypatch.setattr(auth_mod, "_try_awseal_modules", lambda: None)
    store = GoogleAIPlanCredentialStore(data_dir=tmp_path, use_keyring=True)
    cred = GoogleAIPlanCredential(
        client_id="cid-kr",
        access_token="tok-kr",
        refresh_token="ref-kr",
        expires_at=time.time() + 3600,
        scope=GOOGLE_AI_PLAN_SCOPES,
        dpop_jwk=generate_dpop_jwk(),
    )
    assert store.save(cred, write_binding=False) == "os-keyring"
    assert not store.auth_path.exists()
    loaded = store.load()
    assert loaded is not None and loaded.access_token == "tok-kr"
    assert store.delete() is True
    assert store.load() is None


def test_awseal_binding_verifies_and_fails_closed_on_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adk.llm.google_ai_plan_auth as auth_mod

    priv = ed25519.Ed25519PrivateKey.generate()

    class _FakeKeys:
        @staticmethod
        def load_private_key():
            return priv

        @staticmethod
        def public_key_hex(k) -> str:
            return k.public_key().public_bytes_raw().hex()

    class _FakeSeal:
        @staticmethod
        def canonical(payload: dict[str, Any]) -> bytes:
            return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    monkeypatch.setattr(auth_mod, "_try_awseal_modules", lambda: (_FakeKeys, _FakeSeal))
    monkeypatch.setattr(auth_mod, "_get_machine_id", lambda: "machine-aaa")

    store = GoogleAIPlanCredentialStore(auth_path=tmp_path / "auth.json", use_keyring=False)
    cred = GoogleAIPlanCredential(
        client_id="cid-seal",
        access_token="tok-seal",
        refresh_token="ref-seal",
        expires_at=time.time() + 3600,
        scope=GOOGLE_AI_PLAN_SCOPES,
        dpop_jwk=generate_dpop_jwk(),
    )
    store.save(cred, write_binding=True)
    assert store.binding_path.exists()
    assert store.binding_status(cred) == "verified"
    assert store.load(verify_binding=True) is not None

    # Mutate machine_id -> fail closed
    monkeypatch.setattr(auth_mod, "_get_machine_id", lambda: "machine-bbb")
    with pytest.raises(GoogleAIPlanAuthError, match="machine_id"):
        store.load(verify_binding=True)

    # Restore machine_id, mutate DPoP key -> fail closed on thumbprint mismatch
    monkeypatch.setattr(auth_mod, "_get_machine_id", lambda: "machine-aaa")
    raw = json.loads(store.auth_path.read_text(encoding="utf-8"))
    raw["dpop_jwk"] = generate_dpop_jwk()
    store.auth_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(GoogleAIPlanAuthError, match="thumbprint"):
        store.load(verify_binding=True)


# ── 3. Interactions API Wire & Error Classification Tests ────────────────────


def test_thinking_level_clamping_per_model() -> None:
    assert clamp_thinking_level("gemini-4-hydrogen", thinking_level="minimal") == "low"
    assert clamp_thinking_level("gemini-4-hydrogen", effort=1) == "low"
    assert clamp_thinking_level("gemini-4-hydrogen", effort=10) == "xhigh"
    assert clamp_thinking_level("gemini-4-carbon", thinking_level="xhigh") == "high"
    assert clamp_thinking_level("gemini-4-carbon", effort=10) == "high"
    assert clamp_thinking_level("gemini-3.5-flash-lite", thinking_level="xhigh") == "high"
    assert clamp_thinking_level("gemini-3.5-flash-lite", effort=1) == "minimal"


def test_build_interaction_request_stateless_no_sampling_and_thought_sentinel() -> None:
    # Turn 1: signed thought preserved; Turn 2: unsigned tool call gets skip sentinel
    messages = [
        Message(role="system", content="Be concise."),
        Message(role="user", content="Run tool"),
        Message(
            role="assistant",
            content="",
            content_blocks=[{"type": "google_thought", "signature": "sig-real-1", "summary": [{"type": "text", "text": "thinking"}]}],
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})],
        ),
        Message(role="tool", tool_call_id="c1", name="read_file", content="ok"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c2", name="read_file", arguments={"path": "b.py"})],
        ),
    ]
    req = build_interaction_request(
        messages,
        model="gemini-4-hydrogen",
        max_tokens=999999,
        tool_choice="required",
        stream=True,
        effort=1,
    )
    assert req["store"] is False
    assert "previous_interaction_id" not in req
    assert "continuation_token" not in req
    assert "background" not in req
    assert "temperature" not in req["generation_config"]
    assert "top_p" not in req["generation_config"]
    assert req["generation_config"]["max_output_tokens"] == 65536
    assert req["generation_config"]["thinking_level"] == "low"  # clamped from minimal
    assert req["generation_config"]["tool_choice"] == "any"

    steps = req["input"]
    # Real thought preserved before c1
    assert steps[1] == {"type": "thought", "signature": "sig-real-1", "summary": [{"type": "text", "text": "thinking"}]}
    assert steps[2]["type"] == "function_call" and steps[2]["id"] == "c1"
    # Skip sentinel injected before unsigned c2
    assert steps[4] == {"type": "thought", "signature": SKIP_THOUGHT_SIGNATURE_SENTINEL}
    assert steps[5]["type"] == "function_call" and steps[5]["id"] == "c2"


def test_provider_wire_headers_prohibited_stripped_and_sse_streaming(tmp_path: Path) -> None:
    store = GoogleAIPlanCredentialStore(auth_path=tmp_path / "auth.json", use_keyring=False)
    store.save(
        GoogleAIPlanCredential(
            client_id="cid",
            access_token="ya29.valid",
            refresh_token="1//r",
            expires_at=time.time() + 3600,
            scope=GOOGLE_AI_PLAN_SCOPES,
            dpop_jwk=generate_dpop_jwk(),
        ),
        write_binding=False,
    )

    seen_req: dict[str, Any] = {}
    sse_payload = (
        'event: interaction.created\ndata: {"event_type":"interaction.created","interaction":{"id":"int_1"}}\n\n'
        'event: step.start\ndata: {"event_type":"step.start","index":0,"step":{"type":"thought"}}\n\n'
        'event: step.delta\ndata: {"event_type":"step.delta","index":0,"delta":{"type":"thought_signature","signature":"sig_abc"}}\n\n'
        'event: step.stop\ndata: {"event_type":"step.stop","index":0}\n\n'
        'event: step.start\ndata: {"event_type":"step.start","index":1,"step":{"type":"model_output"}}\n\n'
        'event: step.delta\ndata: {"event_type":"step.delta","index":1,"delta":{"type":"text","text":"Hello "}}\n\n'
        'event: step.delta\ndata: {"event_type":"step.delta","index":1,"delta":{"type":"text","text":"world"}}\n\n'
        'event: step.stop\ndata: {"event_type":"step.stop","index":1}\n\n'
        'event: step.start\ndata: {"event_type":"step.start","index":2,"step":{"type":"function_call","id":"call_9","name":"ping"}}\n\n'
        'event: step.delta\ndata: {"event_type":"step.delta","index":2,"delta":{"type":"arguments_delta","arguments":"{\\"host\\":"}}\n\n'
        'event: step.delta\ndata: {"event_type":"step.delta","index":2,"delta":{"type":"arguments_delta","arguments":"\\"127.0.0.1\\"}"}}\n\n'
        'event: step.stop\ndata: {"event_type":"step.stop","index":2}\n\n'
        'event: interaction.completed\ndata: {"event_type":"interaction.completed","interaction":{"id":"int_1","status":"requires_action","usage":{"total_input_tokens":100,"total_cached_tokens":40,"total_output_tokens":20,"total_thought_tokens":30}}}\n\n'
        'event: done\ndata: [DONE]\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen_req["url"] = str(request.url)
        seen_req["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen_req["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=sse_payload.encode("utf-8"))

    prov = GoogleAIPlanProvider(store=store, transport=httpx.MockTransport(handler))

    async def _collect():
        out = []
        async for ch in prov.chat_stream([Message(role="user", content="hi")], temperature=0.9, top_p=0.5):
            out.append(ch)
        return out

    chunks = asyncio.run(_collect())
    assert seen_req["url"] == INTERACTIONS_ENDPOINT
    assert "?key=" not in seen_req["url"]
    assert seen_req["headers"]["authorization"] == "Bearer ya29.valid"
    assert seen_req["headers"]["user-agent"].startswith("awdk/")
    assert "dpop" not in seen_req["headers"]
    assert "x-goog-api-key" not in seen_req["headers"]
    assert "x-goog-user-project" not in seen_req["headers"]
    assert seen_req["body"]["store"] is False
    assert "temperature" not in seen_req["body"]["generation_config"]
    assert "top_p" not in seen_req["body"]["generation_config"]

    text = "".join(c.content for c in chunks if not c.done)
    assert text == "Hello world"
    final = chunks[-1]
    assert final.done is True
    assert final.finish_reason == "tool_calls"
    assert len(final.tool_calls) == 1
    assert final.tool_calls[0] == ToolCall(id="call_9", name="ping", arguments={"host": "127.0.0.1"})


def test_sse_premature_eof_raises_stream_error(tmp_path: Path) -> None:
    store = GoogleAIPlanCredentialStore(auth_path=tmp_path / "auth.json", use_keyring=False)
    store.save(
        GoogleAIPlanCredential(
            client_id="cid",
            access_token="ya29.valid",
            refresh_token="1//r",
            expires_at=time.time() + 3600,
            scope=GOOGLE_AI_PLAN_SCOPES,
            dpop_jwk=generate_dpop_jwk(),
        ),
        write_binding=False,
    )
    partial_sse = (
        'event: step.start\ndata: {"event_type":"step.start","index":0,"step":{"type":"model_output"}}\n\n'
        'event: step.delta\ndata: {"event_type":"step.delta","index":0,"delta":{"type":"text","text":"partial"}}\n\n'
    )
    prov = GoogleAIPlanProvider(
        store=store,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=partial_sse.encode("utf-8"))),
    )

    async def _run():
        got = []
        async for ch in prov.chat_stream([Message(role="user", content="hi")]):
            got.append(ch)
        return got

    with pytest.raises(GoogleAIPlanStreamError, match="interaction.completed"):
        asyncio.run(_run())


def test_error_classifier_all_three_wire_formats() -> None:
    # 1. Flat JSON daily limit 429
    e1 = parse_google_interactions_error(
        429,
        {"error": {"code": "too_many_requests", "message": "You've reached today's AI Ultra limit for gemini-4-hydrogen."}},
        headers={"Retry-After": "3600"},
        model="gemini-4-hydrogen",
    )
    assert isinstance(e1, GoogleAIPlanQuotaError) and e1.scope == "daily" and e1.circuit_scope == "gemini-4-hydrogen"

    # 2. RPC array QuotaFailure limit: 0
    e2 = parse_google_interactions_error(
        429,
        [{"error": {"code": 429, "message": "Quota exceeded", "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": "GenerateRequestsPerDayPerUserPerModel", "quotaValue": "0"}]}]}}],
        model="gemini-4-carbon",
    )
    assert isinstance(e2, GoogleAIPlanQuotaError) and e2.scope == "not_on_plan" and e2.failover_cooldown_s == 3600.0

    # 3. Burst 429
    e3 = parse_google_interactions_error(
        429,
        {"error": {"code": "too_many_requests", "message": "Too many requests."}},
        headers={"retry-after-ms": "2500"},
    )
    assert isinstance(e3, GoogleAIPlanRateLimitError) and e3.retry_after_s == pytest.approx(2.5)

    # 4. 400 not supported
    e4 = parse_google_interactions_error(
        400,
        'event: error\ndata: {"event_type":"error","error":{"code":"invalid_request","message":"Model is not supported for per-user quota."}}\n\n',
        model="gemini-x",
    )
    assert isinstance(e4, GoogleAIPlanQuotaError) and e4.scope == "not_on_plan"

    # 5. 401
    e5 = parse_google_interactions_error(401, [{"error": {"code": 401, "message": "UNAUTHENTICATED"}}])
    assert isinstance(e5, GoogleAIPlanAuthError)

    # 6. 403 scope insufficient
    e6 = parse_google_interactions_error(
        403,
        [{"error": {"code": 403, "message": "Permission denied", "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}],
    )
    assert isinstance(e6, GoogleAIPlanAuthError) and e6.scope_missing is True

    # 7. 403 SERVICE_DISABLED kill switch
    e7 = parse_google_interactions_error(
        403,
        [{"error": {"code": 403, "message": "Disabled", "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED"}]}}],
    )
    assert isinstance(e7, GoogleAIPlanClientBlockedError)

    # 8. 402 eligibility
    e8 = parse_google_interactions_error(402, {"error": {"message": "Upgrade required"}})
    assert isinstance(e8, GoogleAIPlanEligibilityError)


# ── 4. End-to-End Failover to Real In-Process local_bridge Server (:8287) ────


def _start_stub_local_bridge():
    """Start a real in-process local_bridge server with a deterministic stub upstream."""
    upstreams = [
        UpstreamSpec(
            name="bonsai2",
            base_url="http://127.0.0.1:18092",
            default_model="bonsai2-27b",
            aliases=("bonsai2", "bonsai2-27b"),
        ),
        UpstreamSpec(
            name="bonsai4b",
            base_url="http://127.0.0.1:18267",
            default_model="bonsai-4b",
            aliases=("bonsai-4b",),
        ),
    ]
    calls: list[dict[str, Any]] = []

    def stub_transport(
        spec: UpstreamSpec,
        path: str,
        payload: dict[str, Any],
        stream: bool,
    ):
        calls.append({"upstream": spec.name, "model": payload.get("model"), "stream": stream})
        if stream:
            def _gen():
                yield b'data: {"id":"cmpl-1","object":"chat.completion.chunk","model":"bonsai2-27b","choices":[{"index":0,"delta":{"role":"assistant","content":"local "},"finish_reason":null}]}\n\n'
                yield b'data: {"id":"cmpl-1","object":"chat.completion.chunk","model":"bonsai2-27b","choices":[{"index":0,"delta":{"content":"5090"},"finish_reason":"stop"}]}\n\n'
                yield b"data: [DONE]\n\n"
            return 200, {"Content-Type": "text/event-stream"}, _gen()

        if any("call_tool" in str(m.get("content", "")) for m in payload.get("messages") or []):
            doc = {
                "id": "cmpl-tool",
                "model": spec.default_model,
                "choices": [{
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "local_call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
            return 200, {"Content-Type": "application/json"}, json.dumps(doc).encode("utf-8")

        doc = {
            "id": "cmpl-ok",
            "model": spec.default_model,
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": f"served-by-{spec.default_model}"},
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
        }
        return 200, {"Content-Type": "application/json"}, json.dumps(doc).encode("utf-8")

    runtime = BridgeRuntime(upstreams=upstreams, transport=stub_transport)
    runtime._health_cache = {"bonsai2": True, "bonsai4b": True}
    server = create_server("127.0.0.1", 0, runtime=runtime)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}", calls


def test_e2e_failover_chat_circuit_breaker_recovery_stream_and_tool_continuity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, bridge_url, bridge_calls = _start_stub_local_bridge()
    monkeypatch.setenv("AITHER_LOCAL_BRIDGE_URL", bridge_url)
    monkeypatch.setenv("AITHER_DATA_DIR", str(tmp_path))

    store = GoogleAIPlanCredentialStore(auth_path=tmp_path / "auth.json", use_keyring=False)
    store.save(
        GoogleAIPlanCredential(
            client_id="cid",
            access_token="ya29.valid",
            refresh_token="1//r",
            expires_at=time.time() + 3600,
            scope=GOOGLE_AI_PLAN_SCOPES,
            dpop_jwk=generate_dpop_jwk(),
        ),
        write_binding=False,
    )

    google_mode = {"mode": "daily_429"}
    google_hits = {"count": 0}
    google_bodies: list[dict[str, Any]] = []

    def google_handler(request: httpx.Request) -> httpx.Response:
        google_hits["count"] += 1
        body = json.loads(request.content.decode("utf-8"))
        google_bodies.append(body)
        mode = google_mode["mode"]

        if mode == "daily_429":
            return httpx.Response(
                429,
                headers={"Retry-After": "120"},
                json={"error": {"code": "too_many_requests", "message": "You've reached today's AI Ultra limit for gemini-4-hydrogen."}},
            )
        if mode == "mid_stream_eof":
            partial = (
                'event: step.start\ndata: {"event_type":"step.start","index":0,"step":{"type":"model_output"}}\n\n'
                'event: step.delta\ndata: {"event_type":"step.delta","index":0,"delta":{"type":"text","text":"half-sentence "}}\n\n'
            )
            return httpx.Response(200, content=partial.encode("utf-8"))

        # Success unary
        return httpx.Response(
            200,
            json={
                "id": "int_ok",
                "model": body["model"],
                "status": "completed",
                "steps": [{"type": "model_output", "content": [{"type": "text", "text": "served-by-google"}]}],
                "usage": {"total_input_tokens": 20, "total_cached_tokens": 5, "total_output_tokens": 10, "total_thought_tokens": 4},
            },
        )

    try:
        router = LLMRouter(provider="google-ai-plan", model=DEFAULT_MODEL)
        router._provider = GoogleAIPlanProvider(store=store, transport=httpx.MockTransport(google_handler))

        # Test 4a: chat() daily 429 -> 0 retries, immediate failover to local bridge
        r1 = asyncio.run(router.chat([Message(role="user", content="hello")], model=DEFAULT_MODEL))
        assert google_hits["count"] == 1
        assert r1.content == "served-by-bonsai2-27b"
        assert bridge_calls[-1]["upstream"] == "bonsai2"

        # Verify cost log recorded 0.0 for local-bridge
        cost_lines = (tmp_path / "cloud_costs.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert json.loads(cost_lines[-1])["cost_usd"] == 0.0

        # Test 4b: Circuit-breaker sticky window — 2nd call skips Google completely!
        r2 = asyncio.run(router.chat([Message(role="user", content="call_tool")], model=DEFAULT_MODEL))
        assert google_hits["count"] == 1  # NOT incremented!
        assert len(r2.tool_calls) == 1
        assert r2.tool_calls[0].id == "local_call_1"

        # Test 4d: chat_stream() while circuit open transparently streams from local bridge
        async def _stream_open():
            return [c async for c in router.chat_stream([Message(role="user", content="stream me")], model=DEFAULT_MODEL)]

        stream_chunks = asyncio.run(_stream_open())
        assert "".join(c.content for c in stream_chunks) == "local 5090"
        assert google_hits["count"] == 1

        # Test 4d-2: chat_stream() when circuit is closed and primary fails pre-chunk -> fails over
        router._primary_circuit_open_until.clear()
        stream_chunks2 = asyncio.run(_stream_open())
        assert "".join(c.content for c in stream_chunks2) == "local 5090"
        assert google_hits["count"] == 2  # hit Google once, failed pre-chunk, fell over to local bridge

        # Test 4c & 4f: Half-open recovery + cross-failover tool continuity
        # Expire cooldown, switch Google to ok, replay unsigned local tool call from r2
        router._primary_circuit_open_until[DEFAULT_MODEL] = time.time() - 1.0
        google_mode["mode"] = "ok"
        r3 = asyncio.run(
            router.chat(
                [
                    Message(role="user", content="call_tool"),
                    Message(role="assistant", content="", tool_calls=r2.tool_calls),
                    Message(role="tool", tool_call_id="local_call_1", name="read_file", content="# README"),
                ],
                model=DEFAULT_MODEL,
            )
        )
        assert r3.content == "served-by-google"
        assert r3.completion_tokens == 14  # 10 output + 4 thought
        assert r3.cache_read_tokens == 5
        assert not router._primary_circuit_open_until  # circuit closed on recovery!
        # Confirm skip_thought_signature_validator was injected before local_call_1
        last_steps = google_bodies[-1]["input"]
        assert {"type": "thought", "signature": SKIP_THOUGHT_SIGNATURE_SENTINEL} in last_steps

        # Test 4e: Mid-stream EOF after first chunk raises GoogleAIPlanStreamError without restarting on local bridge
        google_mode["mode"] = "mid_stream_eof"
        bridge_count_before = len(bridge_calls)

        async def _stream_mid_eof():
            return [c async for c in router.chat_stream([Message(role="user", content="hi")], model=DEFAULT_MODEL)]

        with pytest.raises(GoogleAIPlanStreamError):
            asyncio.run(_stream_mid_eof())
        assert len(bridge_calls) == bridge_count_before  # NO mid-sentence failover!
    finally:
        server.shutdown()
        server.server_close()


# ── 5. Security, Clean-Room & CLI Hygiene Gate ───────────────────────────────


def test_clean_room_zero_hardcoded_oauth_credentials() -> None:
    root = Path(__file__).resolve().parents[1] / "adk"
    files = [
        root / "llm" / "google_ai_plan_auth.py",
        root / "llm" / "google_ai_plan.py",
        root / "commands" / "auth.py",
    ]
    forbidden_patterns = [
        re.compile(r"GOCSPX-[A-Za-z0-9_-]+"),
        re.compile(r"[0-9]{6,}-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com"),
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pat in forbidden_patterns:
            assert not pat.search(text), f"Hardcoded OAuth credential pattern {pat.pattern} found in {path}"


def test_resolve_oauth_client_fails_closed_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    import adk.llm.google_ai_plan_auth as auth_mod

    monkeypatch.delenv("AITHER_GOOGLE_AI_PLAN_CLIENT_ID", raising=False)
    monkeypatch.delenv("AITHER_GOOGLE_AI_PLAN_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(auth_mod, "_try_keyring", lambda: None)
    monkeypatch.setattr("adk.config.load_saved_config", lambda: {})

    with pytest.raises(GoogleAIPlanConfigError):
        resolve_oauth_client()


def test_cli_status_json_never_leaks_tokens_or_private_d_scalar(tmp_path: Path) -> None:
    store = GoogleAIPlanCredentialStore(auth_path=tmp_path / "auth.json", use_keyring=False)
    jwk = generate_dpop_jwk()
    cred = GoogleAIPlanCredential(
        client_id="1234567890-aitherium.apps.example",
        access_token="SECRET_ACCESS_TOKEN_DO_NOT_LEAK",
        refresh_token="SECRET_REFRESH_TOKEN_DO_NOT_LEAK",
        expires_at=time.time() + 1800,
        scope=GOOGLE_AI_PLAN_SCOPES,
        dpop_jwk=jwk,
    )
    store.save(cred, write_binding=False)

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    register_parser(sub)
    args = parser.parse_args(["auth", "google-ai-plan", "status", "--json"])

    lines: list[str] = []
    rc = cmd_auth(args, out=lines.append, store=store)
    assert rc == 0
    raw_out = "\n".join(lines)
    assert "SECRET_ACCESS_TOKEN_DO_NOT_LEAK" not in raw_out
    assert "SECRET_REFRESH_TOKEN_DO_NOT_LEAK" not in raw_out
    assert jwk["d"] not in raw_out
    doc = json.loads(raw_out)
    assert doc["signed_in"] is True
    assert doc["jwk_thumbprint"] == jwk_thumbprint(jwk)
