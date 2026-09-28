"""The awnix air-gap profile seals an awdk process (awdk-on-awnix, AFRL G10).

The guard itself is covered by test_airgap_egress_guard.py. These tests pin the
CONTRACT the appliance relies on: the shipped profile
(.DEPLOYMENT/standalone/bootc/awdk/air_gap.yaml, selected by AITHER_AIR_GAP_CONFIG)
turns strict mode on; strict refuses a TEST-NET dial and a hostname that does not
resolve to loopback, allows a loopback listener, audits every refusal, and the
``--probe`` CLI the ``awnix awdk health`` verb runs exits 0 only when sealed.
Blocked dials use TEST-NET-3 (203.0.113.0/24); nothing dials the internet.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from adk.compliance import air_gap, egress_guard
from adk.compliance.egress_guard import EgressBlocked

AWDK_ROOT = Path(__file__).resolve().parents[1]
SHIPPED = AWDK_ROOT.parent / ".DEPLOYMENT" / "standalone" / "bootc" / "awdk" / "air_gap.yaml"
FALLBACK = ("enabled: true\nenforcement: strict\nallowed_subnets:\n"
            "  - 127.0.0.0/8\n  - ::1/128\n")
_ENV = ("AITHER_DATA_DIR", "AITHER_AIR_GAP", "AITHER_AIR_GAP_CONFIG", "AITHER_CLOUD_MODE",
        "AITHER_LLM_OFFLINE_MODE", "AITHER_PHONEHOME_DISABLED", "AITHER_AUDIT_SIGNING_KEY")


def _profile(tmp_path: Path) -> Path:
    """The shipped profile when this is the monorepo, else the same text."""
    if SHIPPED.is_file():
        return SHIPPED
    p = tmp_path / "air_gap.yaml"
    p.write_text(FALLBACK, encoding="utf-8")
    return p


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AITHER_DATA_DIR", str(tmp_path))
    prev = air_gap.set_enforcer(None)
    was = dict(egress_guard._INSTALLED)
    yield tmp_path
    if not any(was.values()):
        egress_guard.uninstall_egress_guard()
    air_gap.set_enforcer(prev)


@pytest.fixture
def sealed(isolated, monkeypatch):
    monkeypatch.setenv("AITHER_AIR_GAP_CONFIG", str(_profile(isolated)))
    assert egress_guard.install_if_enforced() is True
    return isolated


def _audit(tmp_path: Path):
    p = tmp_path / "compliance" / "audit.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if r["event"]["action"] == "air_gap_violation"]


def test_shipped_profile_is_strict_loopback_only(tmp_path):
    enf = air_gap.AirGapEnforcer(config_path=_profile(tmp_path))
    assert enf.is_enforced()
    assert enf.get_mode() == "strict"
    assert set(enf.allowed_subnets) <= {"127.0.0.0/8", "::1/128"}


def test_status_contract(sealed):
    st = egress_guard.status()
    for key in ("installed", "enforced", "mode", "violations"):
        assert key in st
    assert st["enforced"] is True and st["mode"] == "strict"
    assert st["installed"]["socket"] is True and st["installed"]["httpx"] is True


def test_strict_blocks_testnet_and_audits(sealed):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        with pytest.raises(EgressBlocked):
            s.connect(("203.0.113.1", 443))
    finally:
        s.close()
    rows = _audit(sealed)
    assert any("203.0.113.1" in r["event"]["metadata"].get("detail", "") for r in rows), rows


def test_strict_blocks_a_name_that_is_not_loopback(sealed):
    """A hostname is only allowed when EVERY address it resolves to is allowed.

    ``example.invalid`` never resolves (RFC 6761), so it is refused.
    """
    with pytest.raises(EgressBlocked):
        socket.create_connection(("203.0.113.2", 80), timeout=2)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises((EgressBlocked, OSError)):
            s.connect(("example.invalid", 443))
    finally:
        s.close()
    assert not air_gap.get_air_gap_enforcer().is_host_allowed("example.invalid")


def test_loopback_listener_still_works(sealed):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        c = socket.create_connection(srv.getsockname(), timeout=2)
        c.close()
    finally:
        srv.close()


def test_httpx_to_remote_raises_before_sending(sealed):
    sent = []

    def handler(request):  # a transport that would record any request reaching it
        sent.append(request.url)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(air_gap.AirGapViolation):
            c.get("http://203.0.113.9/v1/chat/completions")
    assert sent == [], "a blocked request reached the transport"
    assert not isinstance(air_gap.AirGapViolation("x", "y"), httpx.HTTPError)


def test_audit_mode_records_but_allows(isolated, monkeypatch):
    cfg = isolated / "audit.yaml"
    cfg.write_text("enabled: true\nenforcement: audit\n", encoding="utf-8")
    monkeypatch.setenv("AITHER_AIR_GAP_CONFIG", str(cfg))
    assert egress_guard.install_if_enforced() is True
    air_gap.get_air_gap_enforcer().enforce_destination("http://203.0.113.5/")  # no raise
    assert _audit(isolated), "audit mode must still record the violation"


def test_disabled_patches_nothing(tmp_path):
    """No config, no env: install_if_enforced() patches nothing (fresh process)."""
    code = ("import socket; orig = socket.socket.connect; "
            "from adk.compliance import egress_guard as e; "
            "r = e.install_if_enforced(); "
            "print(r, any(e._INSTALLED.values()), socket.socket.connect is orig)")
    env = {k: v for k, v in os.environ.items() if k not in _ENV}
    env.update({"AITHER_AIR_GAP_CONFIG": str(tmp_path / "absent.yaml"),
                "AITHER_DATA_DIR": str(tmp_path),
                "PYTHONPATH": str(AWDK_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")})
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120, env=env,
                       cwd=str(AWDK_ROOT))
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == ["False", "False", "True"]


def test_uninstall_restores(sealed):
    egress_guard.uninstall()
    assert not any(egress_guard._INSTALLED.values())
    assert "socket.socket.connect" not in egress_guard._ORIGINALS


def _run_probe(env_extra):
    env = {k: v for k, v in os.environ.items() if k not in _ENV}
    env.update(env_extra)
    env["PYTHONPATH"] = str(AWDK_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "adk.compliance.egress_guard", "--probe",
                           "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=env,
                          cwd=str(AWDK_ROOT))


def test_cli_probe_sealed_with_shipped_profile(tmp_path):
    p = _run_probe({"AITHER_AIR_GAP_CONFIG": str(_profile(tmp_path)),
                    "AITHER_DATA_DIR": str(tmp_path)})
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads(p.stdout.strip().splitlines()[-1])["verdict"] == "blocked"


def test_cli_probe_reports_egress_possible_without_profile(tmp_path):
    p = _run_probe({"AITHER_AIR_GAP_CONFIG": str(tmp_path / "absent.yaml"),
                    "AITHER_DATA_DIR": str(tmp_path)})
    assert p.returncode == 1, p.stdout + p.stderr
    assert json.loads(p.stdout.strip().splitlines()[-1])["verdict"] == "egress-possible"
