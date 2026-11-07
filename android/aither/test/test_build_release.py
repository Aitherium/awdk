"""build.py release signing: a release is never debug-signed, and a wrong certificate fails.

    python -m pytest awdk/android/aither/test/test_build_release.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
BUILD_PY = HERE.parent / "build.py"


def _build():
    spec = importlib.util.spec_from_file_location("aither_build_under_test", BUILD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_sdk(mod, monkeypatch, certs_stdout: str):
    """apksigner/zipalign never run: sign/verify succeed, --print-certs prints `certs_stdout`."""
    ran: list[list[str]] = []
    monkeypatch.setattr(mod, "tool", lambda bt, name: name)
    monkeypatch.setattr(mod, "run", lambda cmd: ran.append(list(cmd)))
    monkeypatch.setattr(
        mod,
        "debug_keystore",
        lambda: pytest.fail("a release build touched the debug keystore"),
    )

    def fake_run(cmd, **kw):
        ran.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=certs_stdout, stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return ran


def test_release_signed_by_another_cert_fails(tmp_path, monkeypatch):
    mod = _build()
    ran = _fake_sdk(
        mod, monkeypatch, "Signer #1 certificate SHA-256 digest: " + "ab" * 32 + "\n"
    )
    with pytest.raises(SystemExit) as e:
        mod.sign(tmp_path, tmp_path / "base.apk", tmp_path / "aither.apk", "release.p12", "pass.txt")
    assert "not signed by the Aither release key" in str(e.value)
    # the release key was the one used; the debug key never was
    assert any("--ks" in c and "release.p12" in c for c in ran)


def test_release_signed_by_the_release_cert_passes(tmp_path, monkeypatch):
    mod = _build()
    cert = mod.RELEASE_CERT_SHA256.replace(":", "").lower()
    _fake_sdk(mod, monkeypatch, f"Signer #1 certificate SHA-256 digest: {cert}\n")
    out = mod.sign(tmp_path, tmp_path / "base.apk", tmp_path / "aither.apk", "release.p12", "pass.txt")
    assert out.name == "aither.apk"


def test_release_without_a_keystore_is_refused_not_debug_signed(monkeypatch, capsys):
    mod = _build()
    monkeypatch.setattr(mod, "build", lambda *a, **k: pytest.fail("built without a release key"))
    with pytest.raises(SystemExit) as e:
        mod.main(["--release"])
    assert e.value.code == 2
    assert "never debug-signed" in capsys.readouterr().err


def test_release_from_secret_delegates_to_the_vault_signer(monkeypatch):
    mod = _build()
    seen: list[list[str]] = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda cmd, **kw: seen.append(list(cmd)) or subprocess.CompletedProcess(cmd, 2),
    )
    monkeypatch.setattr(mod, "build", lambda *a, **k: pytest.fail("built without the vault key"))
    # 2 = the key could not be fetched: the exit code is passed through, nothing falls back
    assert mod.main(["--release-from-secret", "--wear"]) == 2
    assert seen == [[sys.executable, str(mod.SIGN_TOOL), "--wear"]]
    assert mod.SIGN_TOOL.name == "sign_aither_apk.py" and mod.SIGN_TOOL.exists()


def test_release_from_secret_takes_no_keystore_argument():
    mod = _build()
    with pytest.raises(SystemExit) as e:
        mod.main(["--release-from-secret", "--keystore", "x.p12", "--storepass-file", "p"])
    assert e.value.code == 2


def test_the_vault_signer_calls_back_with_release():
    src = (HERE.parents[3] / "AitherOS" / "dev" / "tools" / "sign_aither_apk.py").read_text(encoding="utf-8")
    assert '"--release",' in src
