"""The awdk air-gap enforcer is wired into every egress choke point.

Transport/handler/adapter call counters prove a blocked request is refused
BEFORE any byte reaches a transport. No test dials a non-loopback address
that the guard lets through; blocked dials use TEST-NET-3 (203.0.113.0/24).
"""

from __future__ import annotations

import asyncio
import errno
import http.client
import http.server
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.request
import urllib.response
from pathlib import Path

import httpx
import pytest

from adk.compliance import air_gap
from adk.compliance import egress_guard
from adk.compliance.air_gap import AirGapEnforcer, AirGapViolation
from adk.compliance.egress_guard import EgressBlocked

AWDK_ROOT = Path(__file__).resolve().parents[1]
HAS_REQUESTS = importlib.util.find_spec("requests") is not None
BLOCKED_URL = "http://203.0.113.9/v1/messages"

_ENV_KEYS = (
    "AITHER_DATA_DIR", "AITHER_AIR_GAP", "AITHER_AIR_GAP_CONFIG",
    "AITHER_CLOUD_MODE", "AITHER_LLM_OFFLINE_MODE", "AITHER_PHONEHOME_DISABLED",
    "AITHER_AUDIT_SIGNING_KEY",
)


@pytest.fixture
def isolated(tmp_path):
    """Snapshot env + singleton + guard; everything restored afterwards."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["AITHER_DATA_DIR"] = str(tmp_path)
    os.environ["AITHER_AIR_GAP_CONFIG"] = str(tmp_path / "air_gap.yaml")
    prev = air_gap.set_enforcer(None)
    was = dict(egress_guard._INSTALLED)
    yield tmp_path
    if not any(was.values()):
        egress_guard.uninstall_egress_guard()
    air_gap.set_enforcer(prev)
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _make(tmp_path: Path, mode: str = "strict", enabled: bool = True,
          subnets=None) -> AirGapEnforcer:
    lines = [f"enabled: {'true' if enabled else 'false'}", f"enforcement: {mode}"]
    if subnets:
        lines.append("allowed_subnets:")
        lines += [f"  - {s}" for s in subnets]
    (tmp_path / "air_gap.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    enf = AirGapEnforcer()
    air_gap.set_enforcer(enf)
    egress_guard.install_egress_guard()
    return enf


def _audit_rows(tmp_path: Path):
    p = tmp_path / "compliance" / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]


def _egress_rows(tmp_path: Path):
    return [r for r in _audit_rows(tmp_path)
            if r["event"]["action"] == "air_gap_violation"
            and r["event"]["metadata"]["subsystem"] == "network_egress"]


class _Counter:
    def __init__(self):
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        return httpx.Response(200, text="ok")


# ---- enforcer semantics ---------------------------------------------------


def test_loopback_always_allowed_even_with_no_subnets(isolated):
    enf = _make(isolated)
    assert enf.check_destination_allowed("127.0.0.1")
    assert enf.check_destination_allowed("::1")
    assert enf.is_host_allowed("localhost")
    assert not enf.check_destination_allowed("10.0.0.1")
    enf.enforce_destination("http://127.0.0.1:8080/health")  # no raise


def test_subnet_allow_and_mixed_resolution(isolated, monkeypatch):
    enf = _make(isolated, subnets=["10.0.0.0/8"])
    assert enf.is_host_allowed("10.1.2.3")
    assert not enf.is_host_allowed("192.168.1.1")

    def fake_gai(host, *a, **k):
        table = {
            "inside.lan": ["10.9.9.9"],
            "split.lan": ["10.9.9.9", "8.8.8.8"],
        }
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "nope")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in table[host]]

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    assert enf.is_host_allowed("inside.lan")
    assert not enf.is_host_allowed("split.lan")  # every address must be inside
    assert not enf.is_host_allowed("nothere.invalid")  # DNS failure = fail closed
    with pytest.raises(AirGapViolation):
        enf.enforce_destination("https://nothere.invalid/x")


def test_env_override_and_system_config_order(isolated, monkeypatch):
    os.environ["AITHER_AIR_GAP"] = "audit"
    enf = AirGapEnforcer()
    assert enf.is_enforced() and enf.get_mode() == "audit"
    os.environ["AITHER_AIR_GAP"] = "0"
    (isolated / "air_gap.yaml").write_text("enabled: true\n", encoding="utf-8")
    assert not AirGapEnforcer().is_enforced()


def test_no_signing_key_means_empty_signature(isolated):
    enf = _make(isolated)
    assert enf.get_attestation_state().signature == ""
    os.environ["AITHER_AUDIT_SIGNING_KEY"] = "k" * 32
    signed = AirGapEnforcer()
    assert len(signed.get_attestation_state().signature) == 64


def test_violation_is_not_an_httpx_error():
    assert not issubclass(AirGapViolation, httpx.HTTPError)
    exc = EgressBlocked("network_egress", "x")
    assert isinstance(exc, AirGapViolation) and isinstance(exc, OSError)
    assert exc.errno == errno.ECONNREFUSED and exc.subsystem == "network_egress"


# ---- httpx ----------------------------------------------------------------


@pytest.mark.parametrize("mode,expect_block", [("strict", True), ("audit", False)])
def test_httpx_sync(isolated, mode, expect_block):
    _make(isolated, mode=mode)
    counter = _Counter()
    with httpx.Client(transport=httpx.MockTransport(counter)) as c:
        if expect_block:
            with pytest.raises(AirGapViolation):
                c.get(BLOCKED_URL)
            assert counter.calls == 0
        else:
            assert c.get(BLOCKED_URL).status_code == 200
            assert counter.calls == 1
        c.get("http://127.0.0.1:9/ok")  # loopback always passes
    assert len(_egress_rows(isolated)) == 1


@pytest.mark.parametrize("mode,expect_block", [("strict", True), ("audit", False)])
def test_httpx_async(isolated, mode, expect_block):
    _make(isolated, mode=mode)
    counter = _Counter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(counter)) as c:
            return await c.get(BLOCKED_URL)

    if expect_block:
        with pytest.raises(AirGapViolation):
            asyncio.run(go())
        assert counter.calls == 0
    else:
        assert asyncio.run(go()).status_code == 200
        assert counter.calls == 1


def test_httpx_in_process_asgi_transport_is_not_judged(isolated):
    """An ASGI app served in-process sends no byte; strict must not refuse it
    (FastAPI TestClient / httpx.ASGITransport under an air gap)."""
    _make(isolated)
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["server"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as c:
            return await c.get("/x")

    assert asyncio.run(go()).status_code == 200
    assert seen and _egress_rows(isolated) == []


def test_httpx_transport_subclass_is_still_judged(isolated):
    """The exemption is by EXACT class: a subclass may dial the network."""
    _make(isolated)
    counter = _Counter()

    class Sneaky(httpx.ASGITransport):
        def __init__(self):
            super().__init__(app=None)

        async def handle_async_request(self, request):  # pragma: no cover - must not run
            counter.calls += 1
            return httpx.Response(200)

    async def go():
        async with httpx.AsyncClient(transport=Sneaky()) as c:
            return await c.get(BLOCKED_URL)

    with pytest.raises(AirGapViolation):
        asyncio.run(go())
    assert counter.calls == 0


def test_httpx_module_level_blocked(isolated):
    _make(isolated)
    with pytest.raises(AirGapViolation):
        httpx.get(BLOCKED_URL, timeout=2)
    assert len(_egress_rows(isolated)) == 1


def test_disabled_is_inert(isolated):
    enf = _make(isolated, enabled=False)
    assert not enf.is_enforced()
    counter = _Counter()
    with httpx.Client(transport=httpx.MockTransport(counter)) as c:
        c.get(BLOCKED_URL)
    assert counter.calls == 1
    assert _egress_rows(isolated) == []


def test_httpx_real_loopback_server_under_strict(isolated):
    _make(isolated)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            return None

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/"
        r = httpx.get(url, timeout=5, trust_env=False)
        assert r.status_code == 200
        # No ProxyHandler({}) would let a Windows registry proxy route this off-box.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=5) as resp:
            assert resp.status == 200
    finally:
        srv.shutdown()
        srv.server_close()


# ---- urllib / requests / http.client / socket -----------------------------


class _CountingHTTP(urllib.request.BaseHandler):
    handler_order = 100
    calls = 0

    def http_open(self, req):
        type(self).calls += 1
        resp = urllib.response.addinfourl(io.BytesIO(b"ok"), {}, req.full_url, 200)
        resp.msg = "OK"  # HTTPErrorProcessor reads .msg
        return resp


@pytest.mark.parametrize("mode,expect_block", [("strict", True), ("audit", False)])
def test_urllib(isolated, mode, expect_block):
    _make(isolated, mode=mode)
    _CountingHTTP.calls = 0
    opener = urllib.request.build_opener(_CountingHTTP())
    if expect_block:
        with pytest.raises(AirGapViolation):
            opener.open(BLOCKED_URL, timeout=2)
        assert _CountingHTTP.calls == 0
    else:
        opener.open(BLOCKED_URL, timeout=2)
        assert _CountingHTTP.calls == 1


@pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
@pytest.mark.parametrize("mode,expect_block", [("strict", True), ("audit", False)])
def test_requests(isolated, mode, expect_block):
    import requests
    from requests.adapters import BaseAdapter

    class CountingAdapter(BaseAdapter):
        calls = 0

        def send(self, request, **kw):
            type(self).calls += 1
            r = requests.Response()
            r.status_code = 200
            r.url = request.url
            return r

        def close(self):
            return None

    _make(isolated, mode=mode)
    s = requests.Session()
    s.mount("http://", CountingAdapter())
    if expect_block:
        with pytest.raises(AirGapViolation):
            s.get(BLOCKED_URL, timeout=2)
        assert CountingAdapter.calls == 0
    else:
        assert s.get(BLOCKED_URL, timeout=2).status_code == 200
        assert CountingAdapter.calls == 1


def test_http_client_blocked_by_socket_backstop(isolated):
    _make(isolated)
    conn = http.client.HTTPConnection("203.0.113.9", 80, timeout=2)
    with pytest.raises(EgressBlocked) as ei:
        conn.request("GET", "/")
    assert isinstance(ei.value, OSError)
    conn.close()


def test_raw_socket_connect_and_connect_ex(isolated):
    _make(isolated)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        with pytest.raises(EgressBlocked):
            s.connect(("203.0.113.9", 443))
        assert s.connect_ex(("203.0.113.9", 443)) == errno.ECONNREFUSED
    finally:
        s.close()
    with pytest.raises(EgressBlocked):
        socket.create_connection(("203.0.113.9", 443), timeout=2)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no AF_UNIX on this platform")
def test_af_unix_untouched(isolated):
    _make(isolated)
    path = str(isolated / "g.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
        srv.listen(1)
        cli.connect(path)  # the guard never judges AF_UNIX
    finally:
        cli.close()
        srv.close()
    assert _egress_rows(isolated) == []


def test_udp_sendto_blocked(isolated):
    _make(isolated)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(EgressBlocked):
            u.sendto(b"hello", ("203.0.113.9", 53))
        with pytest.raises(EgressBlocked):
            u.sendto(b"hello", 0, ("203.0.113.9", 53))
        if hasattr(u, "sendmsg"):
            with pytest.raises(EgressBlocked):
                u.sendmsg([b"hello"], [], 0, ("203.0.113.9", 53))
    finally:
        u.close()


def test_udp_loopback_sendto_allowed(isolated):
    _make(isolated)
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        srv.bind(("127.0.0.1", 0))
        srv.settimeout(2)
        assert cli.sendto(b"hi", srv.getsockname()) == 2
        assert srv.recvfrom(16)[0] == b"hi"
    finally:
        cli.close()
        srv.close()


def test_hostname_refused_before_any_dns_query(monkeypatch, isolated):
    """Loopback-only strict: a cloud hostname is refused with NO resolver call."""
    calls = []
    real = socket.getaddrinfo

    def spy(host, *a, **k):
        calls.append(host)
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", spy)  # the guard wraps the spy
    _make(isolated)
    with pytest.raises(AirGapViolation) as ei:
        httpx.get("https://api.anthropic.com/v1/messages")
    assert "before DNS" in str(ei.value)
    with pytest.raises(EgressBlocked):
        socket.create_connection(("api.anthropic.com", 443), timeout=2)
    with pytest.raises(EgressBlocked):
        socket.gethostbyname("api.anthropic.com")
    with pytest.raises(EgressBlocked):
        socket.gethostbyaddr("8.8.8.8")  # a PTR query is DNS too
    assert calls == []
    socket.getaddrinfo("localhost", 80)  # local names still resolve
    assert calls == ["localhost"]


def test_hosts_file_names_resolve_locally(isolated, monkeypatch):
    hosts = isolated / "hosts"
    hosts.write_text("127.0.0.1 box.internal  # local\n", encoding="utf-8")
    monkeypatch.setattr(air_gap, "HOSTS_FILE_PATH", hosts)
    enf = _make(isolated)
    assert enf.dns_permitted("box.internal")
    assert not enf.dns_permitted("api.anthropic.com")


def test_subnets_turn_resolution_on_unless_configured_off(isolated):
    enf = _make(isolated, subnets=["10.0.0.0/8"])
    assert enf.resolves_hostnames and enf.dns_permitted("inside.lan")
    (isolated / "air_gap.yaml").write_text(
        "enabled: true\nallowed_subnets: [10.0.0.0/8]\nresolve_hostnames: false\n",
        encoding="utf-8")
    assert not AirGapEnforcer().dns_permitted("inside.lan")


# ---- the system floor, env semantics, fail closed --------------------------


def _floor(tmp_path, monkeypatch, text="enabled: true\nenforcement: strict\n"):
    sys_cfg = tmp_path / "etc-air_gap.yaml"
    sys_cfg.write_text(text, encoding="utf-8")
    monkeypatch.setattr(air_gap, "SYSTEM_CONFIG_PATH", sys_cfg)
    return sys_cfg


def test_user_config_cannot_disable_system_floor(isolated, monkeypatch):
    _floor(isolated, monkeypatch)
    (isolated / "air_gap.yaml").write_text(
        "enabled: false\nallowed_subnets: [0.0.0.0/0]\n", encoding="utf-8")
    enf = AirGapEnforcer()
    assert enf.is_enforced() and enf.get_mode() == "strict" and enf.system_floor_enforced
    assert enf.allowed_subnets == []  # the user could not widen it
    assert not enf.check_destination_allowed("8.8.8.8")


def test_missing_config_env_cannot_disable_system_floor(isolated, monkeypatch):
    _floor(isolated, monkeypatch)
    os.environ["AITHER_AIR_GAP_CONFIG"] = str(isolated / "does-not-exist.yaml")
    assert AirGapEnforcer().is_enforced()


@pytest.mark.parametrize("val", ["", "0", "false", "audit"])
def test_env_cannot_lower_system_floor(isolated, monkeypatch, val):
    _floor(isolated, monkeypatch)
    os.environ["AITHER_AIR_GAP"] = val
    enf = AirGapEnforcer()
    assert enf.is_enforced() and enf.get_mode() == "strict"
    assert AirGapEnforcer(enabled=False, mode="audit").get_mode() == "strict"


def test_disable_refused_under_system_floor(isolated, monkeypatch):
    _floor(isolated, monkeypatch)
    enf = AirGapEnforcer()
    enf.disable()
    assert enf.is_enforced()


def test_empty_env_is_unset(isolated):
    (isolated / "air_gap.yaml").write_text("enabled: true\n", encoding="utf-8")
    os.environ["AITHER_AIR_GAP"] = ""
    assert AirGapEnforcer().is_enforced()
    os.environ["AITHER_AIR_GAP"] = "   "
    assert AirGapEnforcer().is_enforced()


@pytest.mark.parametrize("body", [
    "enabled: true\nallowed_subnets: [10.0.0.0/8\n",  # unclosed bracket
    "- just\n- a list\n",                             # not a mapping
    "enabled: false\nattestation_interval: [\n",
])
def test_malformed_config_fails_closed(isolated, body):
    (isolated / "air_gap.yaml").write_text(body, encoding="utf-8")
    enf = AirGapEnforcer()
    assert enf.is_enforced() and enf.get_mode() == "strict"
    assert enf.config_error
    assert enf.allowed_subnets == []


def test_malformed_system_floor_fails_closed(isolated, monkeypatch):
    _floor(isolated, monkeypatch, "enabled: true\nallowed_subnets: [10.0.0.0/8\n")
    enf = AirGapEnforcer()
    assert enf.is_enforced() and enf.config_error and enf.allowed_subnets == []


def test_install_is_idempotent_and_uninstall_restores(isolated):
    before = socket.socket.connect
    r1 = egress_guard.install_egress_guard()
    wrapped = socket.socket.connect
    r2 = egress_guard.install_egress_guard()
    assert r1 == r2 and r1["socket"] and r1["urllib"] and r1["httpx"]
    assert socket.socket.connect is wrapped
    egress_guard.uninstall_egress_guard()
    assert socket.socket.connect is before


def test_enable_installs_guard(isolated):
    enf = AirGapEnforcer()
    assert not enf.is_enforced()
    air_gap.set_enforcer(enf)
    enf.enable()
    assert egress_guard.egress_guard_status()["installed"]["socket"]
    with pytest.raises(AirGapViolation):
        enf.enforce_destination(BLOCKED_URL)


# ---- subprocess: import-time autoinstall + CLI ---------------------------


def _run(args, tmp_path, **env_over):
    env = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
    env["AITHER_DATA_DIR"] = str(tmp_path)
    env["AITHER_AIR_GAP_CONFIG"] = str(tmp_path / "absent.yaml")
    env["PYTHONPATH"] = str(AWDK_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(env_over)
    return subprocess.run([sys.executable, *args], cwd=str(AWDK_ROOT), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)


_STATUS = ("import json, adk; from adk.compliance.egress_guard import egress_guard_status "
           "as s; print(json.dumps(s()))")


def test_import_adk_without_air_gap_patches_nothing(tmp_path):
    r = _run(["-c", _STATUS], tmp_path)
    assert r.returncode == 0, r.stderr
    st = json.loads(r.stdout.strip().splitlines()[-1])
    assert not any(st["installed"].values())


def test_import_adk_with_strict_patches_everything(tmp_path):
    r = _run(["-c", _STATUS], tmp_path, AITHER_AIR_GAP="strict")
    assert r.returncode == 0, r.stderr
    st = json.loads(r.stdout.strip().splitlines()[-1])
    assert st["enforced"] and st["mode"] == "strict"
    assert st["installed"]["httpx"] and st["installed"]["urllib"] and st["installed"]["socket"]


def test_cli_probe_blocks_and_audits(tmp_path):
    r = _run(["-m", "adk.compliance.egress_guard", "--probe", "https://203.0.113.7/v1"],
             tmp_path, AITHER_AIR_GAP="strict")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert len(_egress_rows(tmp_path)) == 1
    # one guard state: runpy must not re-execute an already-imported module
    assert "found in sys.modules" not in r.stderr, r.stderr
    r = _run(["-m", "adk.compliance.egress_guard", "--probe", "http://127.0.0.1:1"],
             tmp_path, AITHER_AIR_GAP="strict")
    assert r.returncode == 0, (r.stdout, r.stderr)


def test_cli_self_test(tmp_path):
    r = _run(["-m", "adk.compliance.egress_guard", "--self-test"], tmp_path)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "SELF-TEST PASS" in r.stdout


def test_cli_bare_probe_reports_sealed(tmp_path):
    r = _run(["-m", "adk.compliance.egress_guard", "--probe", "--json"],
             tmp_path, AITHER_AIR_GAP="strict")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert json.loads(r.stdout.strip().splitlines()[-1])["verdict"] == "blocked"
    r = _run(["-m", "adk.compliance.egress_guard", "--probe"], tmp_path)
    assert r.returncode == 1, (r.stdout, r.stderr)  # not enforced: egress possible
