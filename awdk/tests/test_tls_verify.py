"""TLS verification policy + a ratchet against bare ``verify=False``.

The adk is a public SDK; ``verify=False`` disables certificate verification and
exposes auth tokens / secrets to MITM. All HTTP calls must route their verify
value through ``adk._tls.tls_verify()`` (or the template's local equivalent),
which defaults to verifying and trusts the AitherNet CA bundle when present.
"""

import ast
import os
import re
from pathlib import Path

import pytest

from adk._tls import tls_verify

ADK_ROOT = Path(__file__).resolve().parent.parent / "adk"
_BARE_VERIFY_FALSE = re.compile(r"verify\s*=\s*False\b")


def _bare_verify_false_sites(root: Path) -> list[str]:
    """Find real ``verify=False`` KEYWORD ARGUMENTS via the AST.

    The previous version regex-scanned raw lines, so it also flagged docstrings
    and comments that merely NAME the antipattern (`adk/forkd_client.py` and
    `adk/notebook_tools.py` document "never verify=False" and were reported as
    offenders). That made the gate cry wolf while a genuine call in `adk/cli.py`
    sat in the same failure list — noise hiding a real MITM hole.

    Parsing keyword arguments instead means prose cannot trip it, and a real call
    cannot hide behind formatting (`verify = False`, a line break, an alias).
    """
    offenders: list[str] = []
    for py in root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:  # not our problem here; other gates catch it
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "verify"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is False
                ):
                    # as_posix(): the offender list is a human-facing report and
                    # must read the same on Windows as in CI.
                    rel = py.relative_to(root.parent).as_posix()
                    offenders.append(f"{rel}:{kw.value.lineno}")
    return sorted(offenders)


def test_no_bare_verify_false_in_adk_package():
    """No call site may pass verify=False (use tls_verify())."""
    offenders = _bare_verify_false_sites(ADK_ROOT)
    assert not offenders, (
        "verify=False passed to a call (disables TLS verification — MITM risk). "
        "Use tls_verify() instead:\n  " + "\n  ".join(offenders)
    )


def test_the_ratchet_actually_detects_a_real_call(tmp_path):
    """A gate that cannot fail is worthless — prove this one catches the thing."""
    pkg = tmp_path / "adk"
    pkg.mkdir()
    (pkg / "bad.py").write_text(
        "import requests\nrequests.get('https://x', verify=False)\n", encoding="utf-8"
    )
    assert _bare_verify_false_sites(pkg) == ["adk/bad.py:2"]


def test_the_ratchet_ignores_prose_that_names_the_antipattern(tmp_path):
    """Docstrings and comments documenting the rule must not be flagged."""
    pkg = tmp_path / "adk"
    pkg.mkdir()
    (pkg / "doc.py").write_text(
        '"""Transport is injectable; the default never uses verify=False."""\n'
        "# never verify=False by default\n"
        "OK = True\n",
        encoding="utf-8",
    )
    assert _bare_verify_false_sites(pkg) == []


def test_the_ratchet_still_catches_odd_formatting(tmp_path):
    """`verify = False` and multi-line calls must not slip past the AST check."""
    pkg = tmp_path / "adk"
    pkg.mkdir()
    (pkg / "odd.py").write_text(
        "import requests\n"
        "requests.get(\n"
        "    'https://x',\n"
        "    verify = False,\n"
        ")\n",
        encoding="utf-8",
    )
    assert _bare_verify_false_sites(pkg) == ["adk/odd.py:4"]


def test_verify_true_and_helper_calls_are_not_flagged(tmp_path):
    pkg = tmp_path / "adk"
    pkg.mkdir()
    (pkg / "good.py").write_text(
        "import requests\n"
        "from adk._tls import tls_verify\n"
        "requests.get('https://x', verify=True)\n"
        "requests.get('https://y', verify=tls_verify())\n",
        encoding="utf-8",
    )
    assert _bare_verify_false_sites(pkg) == []


def test_tls_verify_defaults_to_true(monkeypatch, tmp_path):
    monkeypatch.delenv("AITHER_TLS_VERIFY", raising=False)
    monkeypatch.delenv("AITHER_CA_BUNDLE", raising=False)
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))  # no bundle present
    assert tls_verify() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", "FALSE", "Off"])
def test_tls_verify_can_be_disabled_for_dev(monkeypatch, val):
    monkeypatch.setenv("AITHER_TLS_VERIFY", val)
    assert tls_verify() is False


def test_tls_verify_uses_ca_bundle_when_present(monkeypatch, tmp_path):
    monkeypatch.delenv("AITHER_TLS_VERIFY", raising=False)
    monkeypatch.delenv("AITHER_CA_BUNDLE", raising=False)
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    bundle = tmp_path / "aithernet-ca-bundle.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    assert tls_verify() == str(bundle)


def test_explicit_ca_bundle_env_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("AITHER_TLS_VERIFY", raising=False)
    explicit = tmp_path / "custom-ca.pem"
    explicit.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setenv("AITHER_CA_BUNDLE", str(explicit))
    assert tls_verify() == str(explicit)
