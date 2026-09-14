"""Moat-boundary guard — the published SDK must not leak the internal moat.

These assertions are also the contract the publish CI enforces before a wheel
is pushed to PyPI. If one fails, do not publish.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ADK_DIR = Path(__file__).resolve().parent.parent / "adk"
REPO = Path(__file__).resolve().parent.parent


def _py_files():
    return [p for p in ADK_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_internal_moat_imports():
    """No published module may import the internal `aither_adk` moat package."""
    bad = re.compile(r"^\s*(from|import)\s+aither_adk(\.|\s|$)")
    offenders = []
    for f in _py_files():
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if bad.match(line):
                offenders.append(f"{f.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, "Internal moat imported by published SDK:\n" + "\n".join(offenders)


def test_licensing_keystone_present():
    """The entitlement keystone must ship — it's what makes gates fail-closed."""
    assert (ADK_DIR / "licensing.py").is_file()
    import adk.licensing as lic  # importable
    assert hasattr(lic, "get_license_manager")


def test_wheel_excludes_moat_artifacts():
    """pyproject must keep premium identities + nanogpt out of the wheel."""
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    # Crude but robust: the exclude block must mention both.
    assert "adk/identities/*.yaml" in pyproject
    assert "adk/nanogpt.py" in pyproject


@pytest.mark.parametrize(
    "module,needle",
    [
        ("fleet.py", 'require("fleet"'),
        ("channels.py", 'require("channels"'),
        ("cron.py", 'require("cron"'),
        ("builtin_tools.py", "can_use_swarm"),
        ("forge.py", 'require("fleet"'),
        ("agent.py", "clamp_effort"),
        ("agent.py", "can_use_auto_neurons"),
    ],
)
def test_scale_capabilities_are_gated(module, needle):
    """Each scale capability must reference a license gate in its source."""
    src = (ADK_DIR / module).read_text(encoding="utf-8")
    assert needle in src, f"{module} is missing license gate: {needle}"
