#!/usr/bin/env python3
"""Sync version from pyproject.toml into all package manifests.

Reads the canonical version from awdk/pyproject.toml and updates:
  - packaging/npm/package.json
  - packaging/brew/awdk.rb
  - packaging/winget/*.yaml  (AitherShell — ASSERTED, not bumped: it versions separately)
  - server.json (MCP Registry entry — carries the version TWICE)

Usage:
    python packaging/sync_versions.py           # Sync versions
    python packaging/sync_versions.py --check   # Check without modifying
    python packaging/sync_versions.py --digests # Fill brew sha256 from PyPI

Version bumps necessarily precede the PyPI publish, so the brew formula's own
sha256 is written as a PLACEHOLDER at bump time and must be filled once the
sdist exists. Nobody ever did that, so every release shipped a formula that
`brew install` cannot verify. `--digests` fills it from PyPI, and `--check`
FAILS when the version is published but the digest is still a placeholder — so
the omission is now loud instead of silent.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PLACEHOLDER = "PLACEHOLDER_SHA256"


def fetch_sdist_sha256(version: str, *, timeout: float = 15.0) -> str | None:
    """Return the PyPI sdist sha256 for *version*, or None if not published.

    Network failures return None rather than raising: a transient outage must
    not fail a release, it just leaves the digest unfilled (which --check then
    reports).
    """
    url = f"https://pypi.org/pypi/awdk/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.load(resp)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None
    except json.JSONDecodeError:
        return None
    for entry in data.get("urls", []):
        if entry.get("packagetype") == "sdist":
            return entry.get("digests", {}).get("sha256")
    return None


def sync_brew_digest(version: str, check: bool, fetch=fetch_sdist_sha256) -> bool:
    """Fill (or verify) the brew formula's own sha256 against PyPI.

    Only the FIRST sha256 in the formula is the package's own; the rest belong
    to `resource` blocks and must never be touched (see sync_brew).
    """
    path = Path(__file__).parent / "brew" / "awdk.rb"
    text = path.read_text(encoding="utf-8")
    match = re.search(r'sha256 "([^"]*)"', text)
    if not match:
        print("  brew digest: no sha256 line (skipped)")
        return True

    current = match.group(1)
    published = fetch(version)

    if published is None:
        if current.startswith("PLACEHOLDER"):
            print(f"  brew digest: {version} not on PyPI yet — placeholder retained")
        else:
            print("  brew digest: not on PyPI yet — leaving existing digest")
        return True

    if current == published:
        print("  brew digest: already correct")
        return True
    if check:
        print(f"  brew digest: {current[:16]}… -> {published[:16]}… (needs update)")
        return False

    text = text.replace(f'sha256 "{current}"', f'sha256 "{published}"', 1)
    path.write_text(text, encoding="utf-8")
    print(f"  brew digest: filled from PyPI ({published[:16]}…)")
    return True


def get_version() -> str:
    """Read version from pyproject.toml."""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("Could not find version in pyproject.toml")
    return match.group(1)


def sync_server_json(version: str, check: bool) -> bool:
    """Update server.json — the MCP Registry entry.

    TWO versions live in this file and BOTH must match the package: the server
    version and `packages[0].version`. The registry rejects a package version
    that is not on PyPI, so a half-bumped file fails the publish with a message
    about the registry rather than about us.

    Also asserts the ownership marker in README.md still names this server. The
    registry proves ownership by fetching the PUBLISHED PyPI description and
    looking for `mcp-name: <name>`; if the two drift, publishing fails with an
    opaque verification error and nothing local shows a problem.
    """
    root = Path(__file__).parent.parent
    path = root / "server.json"
    if not path.exists():
        print("  server.json: absent (skipped)")
        return True
    data = json.loads(path.read_text(encoding="utf-8"))
    pkgs = data.get("packages") or [{}]

    ok = True
    marker = f"mcp-name: {data.get('name', '')}"
    readme = root / "README.md"
    if readme.exists() and marker not in readme.read_text(encoding="utf-8"):
        print(f"  server.json: README.md has no '{marker}' — MCP Registry "
              f"ownership verification WILL fail")
        ok = False

    if data.get("version") == version and pkgs[0].get("version") == version:
        print(f"  server.json: already {version}")
        return ok
    if check:
        print(f"  server.json: {data.get('version')}/{pkgs[0].get('version')} "
              f"-> {version} (needs update)")
        return False
    data["version"] = version
    if pkgs and pkgs[0]:
        pkgs[0]["version"] = version
        data["packages"] = pkgs
    path.write_text(json.dumps(data, indent=2) + chr(10), encoding="utf-8")
    print(f"  server.json: updated to {version}")
    return ok


def sync_npm(version: str, check: bool) -> bool:
    """Update packaging/npm/package.json."""
    path = Path(__file__).parent / "npm" / "package.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") == version:
        print(f"  npm: already {version}")
        return True
    if check:
        print(f"  npm: {data.get('version')} -> {version} (needs update)")
        return False
    data["version"] = version
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"  npm: updated to {version}")
    return True


def sync_brew(version: str, check: bool) -> bool:
    """Update packaging/brew/awdk.rb."""
    path = Path(__file__).parent / "brew" / "awdk.rb"
    text = path.read_text(encoding="utf-8")

    # Accepts BOTH filenames on purpose: the formula still carries an
    # `aither_adk-<v>.tar.gz` url from before the awdk rename, and this
    # function's job is to read the CURRENT version out of it. Matching only
    # the new name would make old_ver None on the transition release, which
    # reads as "already up to date" and silently skips the bump.
    old_url = re.search(
        r'url "https://files\.pythonhosted\.org/.*?(?:aither_adk|awdk)-([^"]+)\.tar\.gz"', text
    )
    old_ver = old_url.group(1) if old_url else None

    # The formula's own `test do` block asserts a version too, and nothing kept it
    # in step with the url. Measured 2026-08-16: url said 3.3.0 while the test
    # asserted 3.0.4 — three releases stale — so `brew test` fails on a formula
    # that installs perfectly.
    #
    # It is checked BEFORE the "already at this version" short-circuit below,
    # because that early return is precisely what hid it: once the url matched,
    # sync_brew declared success without looking at anything else. A check that
    # stops at the first agreeing field cannot see a second field disagreeing.
    ok_assert = True
    m_assert = re.search(r'assert_match "([^"]+)", shell_output\("#\{bin\}/adk --version"\)', text)
    if m_assert and m_assert.group(1) != version:
        if check:
            print(f"  brew test assertion: {m_assert.group(1)} -> {version} (needs update)")
            ok_assert = False
        else:
            text = text.replace(
                f'assert_match "{m_assert.group(1)}", shell_output("#{{bin}}/adk --version")',
                f'assert_match "{version}", shell_output("#{{bin}}/adk --version")',
                1,
            )
            path.write_text(text, encoding="utf-8", newline="")
            print(f"  brew test assertion: {m_assert.group(1)} -> {version}")

    if old_ver == version:
        print(f"  brew: already {version}")
        return ok_assert
    if check:
        print(f"  brew: {old_ver} -> {version} (needs update)")
        return False

    new_url = f'https://files.pythonhosted.org/packages/source/a/awdk/awdk-{version}.tar.gz'
    # count=1: ONLY the formula's own `url` may be rewritten. Without it this
    # repointed every `resource "<dep>"` block at the awdk tarball too, so
    # `brew install` fetched the adk sdist and called it httpx — silently
    # corrupting the formula on every single release.
    text = re.sub(
        r'url "https://files\.pythonhosted\.org/[^"]*"',
        f'url "{new_url}"',
        text,
        count=1,
    )
    text = re.sub(
        r'sha256 "[^"]*"',
        'sha256 "PLACEHOLDER_SHA256"',
        text,
        count=1,
    )
    path.write_text(text, encoding="utf-8")
    print(f"  brew: updated to {version}")
    print("  brew: SHA256 set to PLACEHOLDER — update after PyPI publish")
    return True


def sync_winget(version: str, check: bool) -> bool:
    """ASSERT the winget manifests are consistent. Does NOT bump them.

    This used to stamp awdk's version and a fabricated
    `https://aitherium.com/download/awdk-<v>-win64.exe` into a manifest —
    for a product that has no Windows binary. That URL 404s, and winget installs
    executables, not pip packages, so `Aitherium.ADK` was unsubmittable by
    construction while a version-sync kept it looking maintained.

    The winget package is **AitherShell**, which versions INDEPENDENTLY of the
    adk (1.16.0 vs 3.0.5). Tying it to this repo's version would be wrong on
    every release. So: assert internal consistency, bump nothing.

    Checked: all three files exist and agree on PackageIdentifier and
    PackageVersion, and the installer carries a real sha256 rather than a
    placeholder — a placeholder is a submission winget's validator rejects after
    downloading the artifact.
    """
    d = Path(__file__).parent / "winget"
    files = sorted(d.glob("*.yaml")) if d.exists() else []
    if not files:
        print("  winget: no manifests (skipped)")
        return True

    ids, vers, ok = set(), set(), True
    for f in files:
        t = f.read_text(encoding="utf-8")
        m = re.search(r"^PackageIdentifier:\s*(\S+)", t, re.M)
        v = re.search(r"^PackageVersion:\s*(\S+)", t, re.M)
        if m:
            ids.add(m.group(1))
        if v:
            vers.add(v.group(1).strip('"'))
        if "PLACEHOLDER" in t:
            print(f"  winget: {f.name} still has a PLACEHOLDER — winget downloads "
                  f"the installer and hash-checks it, so this would be rejected")
            ok = False
    if len(ids) > 1:
        print(f"  winget: manifests disagree on PackageIdentifier: {sorted(ids)}")
        ok = False
    if len(vers) > 1:
        print(f"  winget: manifests disagree on PackageVersion: {sorted(vers)}")
        ok = False
    if ok:
        print(f"  winget: {', '.join(sorted(ids)) or '?'} "
              f"{', '.join(sorted(vers)) or '?'} consistent "
              f"(versioned with AitherShell, not this package)")
    return ok


def sync_init(version: str, check: bool) -> bool:
    """Sync the source-checkout fallback __version__ in adk/__init__.py.

    The installed package reads pyproject metadata, but a source checkout (and
    the public repo's check_exports.py CI gate) sees the literal fallback —
    leaving it stale broke public CI on v2.27.0 (fallback said 2.24.0)."""
    path = Path(__file__).parent.parent / "adk" / "__init__.py"
    text = path.read_text(encoding="utf-8")
    pattern = r'(__version__ = ")([^"]+)(")'
    m = re.search(pattern, text)
    if not m:
        print("  init: no fallback __version__ found (skipped)")
        return True
    if m.group(2) == version:
        print(f"  init: already {version}")
        return True
    if check:
        print(f"  init: {m.group(2)} -> {version} (needs update)")
        return False
    path.write_text(re.sub(pattern, rf"\g<1>{version}\g<3>", text, count=1), encoding="utf-8")
    print(f"  init: updated to {version}")
    return True


def main():
    check = "--check" in sys.argv
    version = get_version()
    print(f"Canonical version: {version}")
    print()

    digests_only = "--digests" in sys.argv

    all_ok = True
    if not digests_only:
        all_ok &= sync_init(version, check)
        all_ok &= sync_npm(version, check)
        all_ok &= sync_brew(version, check)
        all_ok &= sync_winget(version, check)
        all_ok &= sync_server_json(version, check)

    # Runs in --check too: a PUBLISHED version whose formula still carries a
    # placeholder digest is a broken `brew install`, and used to pass silently.
    all_ok &= sync_brew_digest(version, check)

    if check and not all_ok:
        print("\nManifest mismatch detected. Run without --check to fix.")
        sys.exit(1)
    elif not check:
        print("\nAll manifests synced.")


if __name__ == "__main__":
    main()
