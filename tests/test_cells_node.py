"""A node serving cells over REAL TLS: uvicorn + a self-signed cert, a hashed token file,
and the memory cell reached through HttpsTransport with the node's CA."""

from __future__ import annotations

import datetime
import ipaddress
import socket
import threading
import time

import pytest
import yaml

from adk.cells import ANONYMOUS, CellCallError, Cells, HttpsTransport, Scope
from adk.cells.memory import MemoryCell
from adk.cells.node import TokenFileError, build_node_app, host_cells, load_callers, token_hash

pytest.importorskip("awm")
pytest.importorskip("uvicorn")
x509 = pytest.importorskip("cryptography.x509")

OPERATOR_TOKEN = "op-" + "1" * 40
DANA_TOKEN = "dana-" + "2" * 40


def write_tokens(path):
    path.write_text(yaml.safe_dump({"tokens": {
        token_hash(OPERATOR_TOKEN): {"subject": "ops", "scopes": ["operator"]},
        token_hash(DANA_TOKEN): {"subject": "dana", "scopes": ["workspace"],
                                 "workspace": "acme:dana:proj"},
    }}), encoding="utf-8")
    return path


def self_signed(tmp_path):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "node.pem", tmp_path / "node.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return str(cert_path), str(key_path)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_node(tmp_path):
    import uvicorn

    cert, key = self_signed(tmp_path)
    authenticate = load_callers(write_tokens(tmp_path / "tokens.yaml"))
    app = build_node_app(host_cells([f"memory={tmp_path / 'memory.db'}"]), authenticate,
                         node_name="test-node")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, ssl_certfile=cert, ssl_keyfile=key,
        log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("node did not start")
        time.sleep(0.05)
    yield f"https://127.0.0.1:{port}", cert
    server.should_exit = True
    thread.join(timeout=10)


async def test_memory_over_real_tls_with_the_node_ca(live_node):
    url, ca = live_node
    dana = Cells().remote(MemoryCell, HttpsTransport(url, token=DANA_TOKEN, ca=ca))
    me = dana.as_caller(dana_view())
    await me.memory.remember(key="db", value="postgres 17")
    hits = await me.memory.recall(query="db")
    assert [(h.scope, h.value) for h in hits] == [("acme:dana:proj", "postgres 17")]

    stranger = Cells().remote(MemoryCell, HttpsTransport(url, token="nope", ca=ca))
    with pytest.raises(CellCallError) as err:
        await stranger.as_caller(dana_view()).memory.recall()
    assert err.value.status == 403


async def test_wrong_ca_is_refused(live_node, tmp_path):
    url, _ = live_node
    import httpx

    (tmp_path / "other").mkdir()
    other_ca, _ = self_signed(tmp_path / "other")
    cells = Cells().remote(MemoryCell, HttpsTransport(url, token=DANA_TOKEN, ca=other_ca))
    with pytest.raises(httpx.ConnectError, match="(?i)certificate"):
        await cells.as_caller(dana_view()).memory.recall()


async def test_node_inventory_is_operator_only(live_node):
    import ssl

    import httpx

    url, ca = live_node
    ctx = ssl.create_default_context(cafile=ca)
    async with httpx.AsyncClient(verify=ctx) as client:
        denied = await client.get(url + "/cells/_node",
                                  headers={"Authorization": f"Bearer {DANA_TOKEN}"})
        allowed = await client.get(url + "/cells/_node",
                                   headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"})
    assert denied.status_code == 403
    entry = allowed.json()["nodes"]["test-node"]
    assert entry["cpu"] >= 1


def test_token_file_holds_hashes_only(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump({"tokens": {DANA_TOKEN: {"scopes": ["workspace"]}}}),
                    encoding="utf-8")
    with pytest.raises(TokenFileError, match="sha256"):
        load_callers(path)
    authenticate = load_callers(write_tokens(tmp_path / "ok.yaml"))
    assert authenticate(DANA_TOKEN).workspace == "acme:dana:proj"
    assert authenticate(OPERATOR_TOKEN).scopes == frozenset({Scope.operator})
    assert authenticate(token_hash(DANA_TOKEN)) is ANONYMOUS  # the hash is not a token
    assert authenticate(None) is ANONYMOUS


def dana_view():
    # Client-side identity is irrelevant to the server; it acts on the token.
    from adk.cells import Caller

    return Caller(subject="whoever", scopes=frozenset({Scope.workspace}))


def test_serve_refuses_missing_cert(tmp_path, capsys):
    from adk.cells.__main__ import main

    rc = main(["serve", "--cell", f"memory={tmp_path / 'm.db'}",
               "--tokens", str(write_tokens(tmp_path / "t.yaml")),
               "--cert", str(tmp_path / "none.pem"), "--key", str(tmp_path / "none.key")])
    assert rc == 2 and "does not exist" in capsys.readouterr().err


def test_unknown_cell_spec_is_refused():
    with pytest.raises(ValueError, match="unknown cell"):
        host_cells(["relay=/x"])
