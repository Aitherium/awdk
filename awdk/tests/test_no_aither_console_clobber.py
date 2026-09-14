"""Test guard: awdk must NEVER register `aither` console_script.

The `aither` command is owned by the npm @aitheros/shell-cli (TypeScript REPL).
The Python aithershell package uses `aither-py`. The awdk SDK uses `adk`.

This test fails if a future PR re-adds `aither = ...` under [project.scripts].
"""
from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore


REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def test_aither_console_script_not_registered() -> None:
    """Reserve the `aither` binary for the npm shell-cli REPL.

    See awdk/pyproject.toml [project.scripts] for the rule.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    scripts = data.get("project", {}).get("scripts", {})
    forbidden = {"aither", "aithershell"}
    collisions = forbidden & set(scripts.keys())
    assert not collisions, (
        f"awdk MUST NOT register {sorted(collisions)} as console_scripts. "
        f"The `aither` command belongs to the npm @aitheros/shell-cli REPL. "
        f"Use `adk`, `adk-bug`, `adk-serve` for SDK CLIs. "
        f"Found in [project.scripts]: {dict(scripts)}"
    )


#: The distribution's own name. `uvx <package>` runs the console script whose
#: name MATCHES the package, and the ACP registry's uvx distribution is exactly
#: that shape — without this entry `uvx awdk acp serve` fails with "no
#: such executable" while the registry entry validates fine (it only checks that
#: the PyPI package exists, never that it runs).
#:
#: It is a NARROW exception, not a loosening: `awdk` is a distinct binary
#: from `aither`, so the invariant this file exists for — the `aither` command
#: belongs to the npm shell-cli — is untouched and still asserted above.
DISTRIBUTION_SCRIPT = "awdk"

#: The pre-rename distribution name. PyPI served `aither-adk` up to 3.6.0 and the
#: ACP registry's uvx entry for it is already in the wild — `uvx aither-adk …`
#: runs the console script whose name matches THAT package, so dropping the
#: script strands every registry consumer while the redirect package still
#: installs fine (the registry validates existence, never runnability).
#: pyproject.toml documents the same decision beside the entry itself.
#: Still not a loosening of the `aither` fence: `aither-adk` is a distinct
#: binary from `aither`, which stays owned by the npm shell-cli.
LEGACY_ALIAS_SCRIPT = "aither-adk"


def test_only_adk_prefixed_scripts() -> None:
    """All awdk console_scripts must be `adk`, `adk-*`, or the dist name."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    scripts = data.get("project", {}).get("scripts", {})
    allowed = {"adk", DISTRIBUTION_SCRIPT, LEGACY_ALIAS_SCRIPT}
    bad = [
        name for name in scripts
        if not (name in allowed or name.startswith("adk-"))
    ]
    assert not bad, (
        f"awdk console_scripts must be `adk`, `adk-*`, `{DISTRIBUTION_SCRIPT}`, "
        f"or the legacy alias `{LEGACY_ALIAS_SCRIPT}`. "
        f"Found non-conforming entries: {bad}. "
        f"All scripts: {list(scripts.keys())}"
    )


def test_uvx_entrypoint_is_present() -> None:
    """The exception above must actually be USED, not merely permitted.

    Allowing `awdk` without registering it is the worst of both: the fence
    is widened and `uvx awdk` still cannot launch.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    scripts = data.get("project", {}).get("scripts", {})
    assert DISTRIBUTION_SCRIPT in scripts, (
        f"`{DISTRIBUTION_SCRIPT}` console script missing — the ACP registry's "
        f"uvx distribution cannot launch this package"
    )
