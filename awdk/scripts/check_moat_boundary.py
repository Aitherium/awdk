#!/usr/bin/env python3
"""Publish-time moat guard — inspect built artifacts before they ship to PyPI.

Fails (non-zero exit) if the wheel OR the sdist leaks the internal moat:
  * imports the proprietary ``aither_adk`` package
  * bundles ``adk/nanogpt.py`` (on-device training IP)
  * bundles any premium identity YAML (only ``aither.yaml`` is free)
  * bundles the internal ``adk/platform`` toolkit (relocated out of the SDK)
    or the license-gated ``adk/formbridge`` product
  * bundles ``adk/aither_bridge.py`` (internal IRC↔chat bridge, stripped at sync)
  * is missing the ``adk/licensing.py`` entitlement keystone

These package-build checks are defense-in-depth behind ``tools/publish_gate.py``
(which gates the SYNC payload) — the two layers must stay in lockstep so a
reintroduced module or a broken hatch ``exclude`` is caught at BOTH the repo
sync and the built-artifact layer.

The sdist matters as much as the wheel: ``python -m build`` publishes BOTH, and an
sdist can include files the wheel excludes (audit 2026-06-12 P1-5).

Usage:
    python scripts/check_moat_boundary.py [dist/aither_adk-*.whl dist/*.tar.gz ...]

With no argument it picks the newest wheel AND the newest sdist in ``dist/``.
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path

_AITHER_ADK_IMPORT = re.compile(rb"^\s*(from|import)\s+aither_adk(\.|\s|$)", re.MULTILINE)


def _newest(pattern: str) -> Path | None:
    dist = Path(__file__).resolve().parent.parent / "dist"
    hits = sorted(dist.glob(pattern), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def _check_names(entries, *, strip_root: bool) -> tuple[list[str], bool]:
    """entries: iterable of (name, read_bytes_fn). Returns (violations, saw_licensing)."""
    violations: list[str] = []
    saw_licensing = False
    for raw, read in entries:
        name = raw.replace("\\", "/")
        if strip_root and "/" in name:
            # sdist members live under aither_adk-X.Y.Z/ — strip so the moat
            # namespace check doesn't false-positive on the version dir itself
            name = name.split("/", 1)[1]

        if name.endswith("adk/nanogpt.py"):
            violations.append(f"LEAK: training IP shipped: {raw}")

        if "adk/platform/" in name or name.endswith("adk/platform.py"):
            violations.append(f"LEAK: internal platform toolkit shipped: {raw}")

        if "adk/formbridge/" in name or name.endswith("adk/formbridge.py"):
            violations.append(f"LEAK: license-gated formbridge product shipped: {raw}")

        if name.endswith("adk/aither_bridge.py"):
            violations.append(f"LEAK: internal aither_bridge shipped: {raw}")

        if "/identities/" in name and name.endswith(".yaml") and not name.endswith("aither.yaml"):
            violations.append(f"LEAK: premium identity shipped: {raw}")

        if name.startswith("aither_adk/") or "/aither_adk/" in name:
            violations.append(f"LEAK: internal moat package shipped: {raw}")

        if name.endswith("adk/licensing.py"):
            saw_licensing = True

        if name.endswith(".py"):
            data = read()
            if data and _AITHER_ADK_IMPORT.search(data):
                violations.append(f"LEAK: imports internal moat: {raw}")
    return violations, saw_licensing


def _inspect_wheel(wheel: Path) -> tuple[list[str], bool]:
    with zipfile.ZipFile(wheel) as zf:
        entries = [(n, (lambda n=n: zf.read(n))) for n in zf.namelist()]
        return _check_names(entries, strip_root=False)


def _inspect_sdist(sdist: Path) -> tuple[list[str], bool]:
    with tarfile.open(sdist, "r:*") as tf:
        members = [m for m in tf.getmembers() if m.isfile()]

        def _reader(m):
            def _read():
                f = tf.extractfile(m)
                return f.read() if f else b""
            return _read

        entries = [(m.name, _reader(m)) for m in members]
        return _check_names(entries, strip_root=True)


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        artifacts = [Path(a) for a in argv[1:]]
    else:
        artifacts = [p for p in (_newest("*.whl"), _newest("*.tar.gz")) if p]

    wheels = [a for a in artifacts if a.suffix == ".whl"]
    if not wheels:
        print("MOAT GUARD: no wheel found (build first: python -m build)", file=sys.stderr)
        return 2

    failed = False
    for artifact in artifacts:
        if not artifact.is_file():
            print(f"MOAT GUARD: artifact not found: {artifact}", file=sys.stderr)
            return 2
        print(f"MOAT GUARD: inspecting {artifact.name}")
        if artifact.suffix == ".whl":
            violations, saw_licensing = _inspect_wheel(artifact)
        elif artifact.name.endswith((".tar.gz", ".tgz", ".tar")):
            violations, saw_licensing = _inspect_sdist(artifact)
        else:
            print(f"MOAT GUARD: unsupported artifact type: {artifact.name}", file=sys.stderr)
            return 2

        if not saw_licensing:
            violations.append(f"MISSING: adk/licensing.py (entitlement keystone) not in {artifact.name}")

        if violations:
            failed = True
            print(f"MOAT GUARD FAILED for {artifact.name}:", file=sys.stderr)
            for v in violations:
                print(f"  - {v}", file=sys.stderr)

    if failed:
        return 1
    print(f"MOAT GUARD PASSED: {len(artifacts)} artifact(s), no leaks, keystone present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
