"""First-run downloads must verify against certifi, not only what OpenSSL sees of the OS store.

Measured 2026-11-13 by the newcomer-journey gate (NJ001) in a clean Windows Sandbox:

    adk quickstart-local
      ERROR: GitHub API failed: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]
      certificate verify failed: unable to get local issuer certificate>

A fresh Windows hands OpenSSL a near-empty ROOT store (Windows fetches roots lazily
through CryptoAPI, which OpenSSL never triggers), and llamacpp_setup used plain
``urllib.request.urlopen`` with no context. httpx was fine -- it uses certifi. The fix is
``adk._tls.urllib_context()``: the OS store PLUS the httpx trust decision (certifi /
SSL_CERT_FILE / the AitherNet bundle). These tests pin it with no network.
"""

from __future__ import annotations

import ast
import io
import json
import ssl
from pathlib import Path

import certifi
import pytest

from adk import _tls, llamacpp_setup

ADK = Path(__file__).resolve().parents[1] / "adk"


@pytest.fixture(autouse=True)
def _clean_tls_env(monkeypatch):
    for k in ("AITHER_TLS_VERIFY", "AITHER_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(_tls, "_ca_bundle_path", lambda: None)
    _tls._URLLIB_CTX.clear()
    yield
    _tls._URLLIB_CTX.clear()


def _certifi_count() -> int:
    return Path(certifi.where()).read_text(encoding="utf-8").count("BEGIN CERTIFICATE")


def test_context_verifies_and_carries_certifi_roots():
    ctx = _tls.urllib_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    # Every certifi root is loaded (the OS store may add more, never fewer).
    assert ctx.cert_store_stats()["x509_ca"] >= _certifi_count() > 100


def test_context_is_cached_per_trust_decision():
    assert _tls.urllib_context() is _tls.urllib_context()


def test_ssl_cert_file_is_honoured(monkeypatch, tmp_path):
    bundle = tmp_path / "corp.pem"
    bundle.write_text(Path(certifi.where()).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    seen = {}
    real = ssl.SSLContext.load_verify_locations

    def spy(self, cafile=None, capath=None, cadata=None):
        seen["cafile"] = cafile
        return real(self, cafile=cafile, capath=capath, cadata=cadata)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", spy)
    assert isinstance(_tls.urllib_context(), ssl.SSLContext)
    assert seen["cafile"] == str(bundle)


def test_verify_off_still_means_off(monkeypatch):
    monkeypatch.setenv("AITHER_TLS_VERIFY", "false")
    ctx = _tls.urllib_context()
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False


def test_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("no trust store")

    monkeypatch.setattr(_tls, "_env_trust_store", boom)
    assert _tls.urllib_context() is None  # urllib's own default, never a crash


class _Resp(io.BytesIO):
    headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch, body: bytes):
    calls = []

    def fake_urlopen(req, timeout=None, context=None, **kw):
        calls.append(context)
        r = _Resp(body)
        r.headers = {"Content-Length": str(len(body))}
        return r

    monkeypatch.setattr(llamacpp_setup.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_github_release_query_uses_the_context(monkeypatch, tmp_path):
    monkeypatch.setattr(llamacpp_setup, "LLAMACPP_DIR", tmp_path / "llamacpp")
    calls = _capture(monkeypatch, json.dumps([]).encode())
    accel = llamacpp_setup.AccelInfo(os_family="windows", arch="x86_64", kind="cpu")
    assert llamacpp_setup.install_llamacpp(accel) is None  # no server builds listed
    assert calls and isinstance(calls[0], ssl.SSLContext)


def test_hf_listing_uses_the_context(monkeypatch):
    listing = {"siblings": [{"rfilename": "m.Q4_K_M.gguf"}]}
    calls = _capture(monkeypatch, json.dumps(listing).encode())
    assert llamacpp_setup._hf_list_gguf_files("org/repo") == ["m.Q4_K_M.gguf"]
    assert isinstance(calls[0], ssl.SSLContext)


def test_download_uses_the_context(monkeypatch, tmp_path):
    calls = _capture(monkeypatch, b"x" * 10)
    assert llamacpp_setup._download("https://example.invalid/f", tmp_path / "f", "f")
    assert isinstance(calls[0], ssl.SSLContext)


# Every remote urlopen in the first-run download path passes a context. Loopback calls
# (http://localhost health and smoke checks) are the only exemption.
FIRST_RUN_DOWNLOADERS = {
    "llamacpp_setup.py": {"status", "smoke_test"},
    "models/download.py": set(),
    "unsloth_gguf_download.py": set(),
    "bonsai_native.py": {"_http_json"},
    "models/mirror.py": set(),  # its __main__ probe is a dev script, not a function
}


@pytest.mark.parametrize("rel", sorted(FIRST_RUN_DOWNLOADERS))
def test_every_remote_urlopen_passes_a_context(rel):
    tree = ast.parse((ADK / rel).read_text(encoding="utf-8"))
    exempt = FIRST_RUN_DOWNLOADERS[rel]
    missing = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name in exempt:
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "urlopen"
                    and not any(k.arg == "context" for k in node.keywords)):
                missing.append(f"{fn.name}:{node.lineno}")
    assert not missing, f"{rel}: urlopen without context= at {missing}"
