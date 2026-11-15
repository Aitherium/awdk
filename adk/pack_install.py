"""Install an external agent pack into ``~/.aither/agents/<agent>``.

``adk install`` accepts only the packs bundled inside the adk wheel, so a pack
that came from the pack registry (``Library/packs/<company>/pack/<agent>``) had
no installer and the runtime — which discovers personas solely under
``~/.aither/agents/<name>`` — could not see it. A registry pack deployed as a
managed twin therefore answered as the generic ``AitherAgent``.

This module is the installer for those directory-shaped packs. It is importable
(``install_pack_dir`` / ``install_from``) and runnable as::

    python -m adk.pack_install --from <dir> [--agents-root DIR] [--json]

``adk/cli.py`` is not touched: its ``install`` verb is a large, separately-owned
function and this entry point needs no parsing there. The admin console exposes
the same installer at ``POST /admin/packs/install``.

Guarantees:

* **idempotent** — installing the same pack twice leaves ONE directory and a
  byte-identical ``agent.yaml``;
* **normalizing** — the written ``agent.yaml`` carries a TOP-LEVEL
  ``name``/``identity``/``system_prompt``, the only shape ``adk.server`` reads
  (the registry manifest keeps the model/tools nested under ``spec``);
* **contained** — the destination name comes from the pack itself (sanitized),
  never from a path; the resolved destination must stay inside the agents root and a
  symlink at the destination is refused, so a pack cannot write outside the root;
* **plain files only** — a pack containing a symlink is refused. ``shutil.copytree``
  dereferences links by default, so ``leak -> ~/.ssh/id_rsa`` copied the private key's
  CONTENT into the agent directory, where the agent could read and surface it. The
  write direction was contained; the READ direction was unbounded;
* **never adopts a stranger** — an existing agent directory this installer did not
  create (or one whose ``agent.yaml`` carries a different identity) is a COLLISION:
  refused unless ``overwrite=True`` (CLI ``--force``, admin ``{"overwrite": true}``).
  A third-party pack whose declared identity matched a trusted agent used to rewrite
  that agent's ``system_prompt`` while leaving its ``oplog/`` in place, and reported
  it as an ordinary update. A re-install of the SAME pack stays idempotent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "PackCollisionError",
    "install_catalog_entry",
    "install_from",
    "install_pack_dir",
    "normalize_agent_manifest",
    "pack_roots",
    "resolve_catalog_id",
]

#: Top-level key this installer stamps into the ``agent.yaml`` it writes. It is the
#: provenance mark that separates "a directory I installed" from "somebody's live
#: agent that happens to have the same name" -- without it, a pack could adopt (and
#: rewrite the persona of) a trusted agent and report the takeover as an update.
MARKER_KEY = "installed_from_pack"


class PackCollisionError(ValueError):
    """An install would replace an agent directory this installer did not create.

    Distinct from a plain ``ValueError`` so a caller (the CLI, the admin route) can
    tell a COLLISION from a malformed pack, and so the operator is told a collision
    happened instead of reading an ordinary "updated".
    """

    def __init__(self, agent: str, dest: Path, reason: str, *, overwrite_hint: bool = True):
        self.agent = agent
        self.dest = dest
        self.reason = reason
        hint = " (re-run with overwrite to replace it)" if overwrite_hint else ""
        super().__init__(f"pack collision for agent '{agent}' at {dest}: {reason}{hint}")

#: Anything that is not a safe single path segment is collapsed to a dash. A
#: pack's declared identity is untrusted input: ``../../escape`` must install as
#: ``escape``, not outside the agents root.
_UNSAFE_NAME = re.compile(r"[^a-z0-9._-]+")


def pack_roots() -> list[Path]:
    """Directories a pack registry is read from, in lookup order.

    Same sources the discovery scans use (``AITHER_PACKS_ROOT``, the user's own
    pack dir, the monorepo tree, the Genesis container mount). The admin route
    uses this list to refuse an arbitrary local path as an install source.
    """
    roots: list[Path] = []
    env = os.environ.get("AITHER_PACKS_ROOT", "").strip()
    if env:
        roots.append(Path(env).expanduser())
    roots.append(Path.home() / ".aither" / "packs")
    roots.append(Path(__file__).resolve().parents[2] / "AitherOS" / "Library" / "packs")
    roots.append(Path("/app/AitherOS/Library/packs"))
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML mapping; a missing or broken file is simply empty."""
    if not path.is_file():
        return {}
    try:
        import yaml
    except ImportError:  # pragma: no cover - yaml is an adk dependency
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — a broken pack must not abort the install
        return {}
    return data if isinstance(data, dict) else {}


def _safe_agent_name(raw: str, fallback: str = "agent") -> str:
    name = _UNSAFE_NAME.sub("-", str(raw or "").strip().lower()).strip(".-_")
    return name or fallback


def _pack_identity(src: Path, brain: dict[str, Any], manifest: dict[str, Any]) -> str:
    declared = brain.get("identity") or brain.get("name")
    if not declared:
        meta = manifest.get("metadata")
        meta = meta if isinstance(meta, dict) else {}
        declared = manifest.get("name") or meta.get("name")
    return _safe_agent_name(str(declared or ""), _safe_agent_name(src.name))


def normalize_agent_manifest(
    name: str,
    brain: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Build the top-level ``agent.yaml`` that ``adk.server`` actually reads.

    A registry manifest nests the runtime/prompt under ``metadata``/``spec``;
    the runtime reads ``system_prompt``, ``tool_grants``, ``name`` and
    ``identity`` at the TOP level. Portable keys worth keeping (description,
    capabilities, enabled_domains) are preserved.
    """
    meta = manifest.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    out: dict[str, Any] = {}
    if "spec" in manifest or meta:
        if meta.get("description"):
            out["description"] = meta["description"]
    else:
        out.update({k: v for k, v in manifest.items() if isinstance(k, str)})
    out["name"] = name
    out["identity"] = name
    out["system_prompt"] = (
        out.get("system_prompt")
        or brain.get("system_prompt")
        or f"You are {name}."
    )
    if not isinstance(out.get("tool_grants"), dict):
        out["tool_grants"] = {}
    # Provenance: lets a re-install of THIS pack stay idempotent while a pack that
    # merely reuses the name of an agent installed some other way is a collision.
    out[MARKER_KEY] = name
    return out


def _is_pack_dir(path: Path) -> bool:
    return (path / "brain_pack.yaml").is_file() or (path / "agent.yaml").is_file()


def _assert_no_symlinks(src: Path) -> None:
    """Refuse a pack that contains a symlink, BEFORE anything is copied.

    ``shutil.copytree(..., symlinks=False)`` (the default) DEREFERENCES every link,
    so a pack carrying ``stolen_key -> ~/.ssh/id_rsa`` wrote the private key's content
    into ``agents/<name>/stolen_key`` -- a read of any file the adk process can read.
    ``symlinks=True`` would be worse (it plants links inside the agents root), so a
    registry pack, which is plain files by construction, is refused instead.
    """
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        # A symlinked directory is listed in dirnames; os.walk will not descend into
        # it (followlinks=False), and the explicit check below still refuses it.
        for entry in list(dirnames) + list(filenames):
            candidate = Path(dirpath) / entry
            if candidate.is_symlink():
                rel = candidate.relative_to(src)
                raise ValueError(f"pack contains a symlink ({rel}); "
                                 f"a registry pack must be plain files")


def _contained_destination(root: Path, dest: Path) -> None:
    """Fail closed unless ``dest`` is a real path inside ``root`` (findings 3).

    ``dest.exists()`` follows a link, so a pre-existing symlink at ``root/<name>``
    made ``shutil.copytree`` write the pack OUTSIDE the agents root while the call
    reported ``created=False`` -- the containment guarantee in the docstring was
    false in the one direction that matters.
    """
    if dest.is_symlink():
        raise ValueError(f"refusing to install through a symlink at {dest}")
    try:
        root_r = root.resolve()
        dest_r = dest.resolve()
    except OSError as exc:  # an unresolvable path cannot be proven contained
        raise ValueError(f"cannot resolve the install destination {dest}: {exc}") from None
    if dest_r != root_r and root_r not in dest_r.parents:
        raise ValueError(f"install destination {dest} resolves outside the agents root")


def install_pack_dir(src: Path | str, agents_root: Path | str | None = None, *,
                     overwrite: bool = False) -> dict[str, Any]:
    """Install ONE agent pack directory; returns what changed.

    Idempotent: the second call rewrites nothing and reports ``changed=False``.
    ``agent.yaml`` is deliberately excluded from the copy and written only as the
    normalized manifest, so a re-install cannot resurrect the un-normalized file.

    ``overwrite=False`` (the default) refuses to touch an agent directory this
    installer did not create -- see :class:`PackCollisionError` -- because a pack
    whose declared identity matched a trusted agent used to replace that agent's
    persona while keeping its ``oplog/`` and reporting an ordinary update.
    """
    src = Path(src).expanduser()
    if not src.is_dir():
        raise ValueError(f"not a pack directory: {src}")
    if not _is_pack_dir(src):
        raise ValueError(f"no brain_pack.yaml or agent.yaml in {src}")
    _assert_no_symlinks(src)

    brain = _load_yaml(src / "brain_pack.yaml")
    manifest = _load_yaml(src / "agent.yaml")
    name = _pack_identity(src, brain, manifest)

    root = (Path(agents_root).expanduser() if agents_root
            else Path.home() / ".aither" / "agents")
    dest = root / name
    try:
        same = src.resolve() == dest.resolve()
    except OSError:
        # A path that cannot be resolved cannot be the destination we are
        # about to create; the copy below is the real containment check.
        same = False
    if same:
        raise ValueError(f"source and destination are the same directory: {dest}")
    root.mkdir(parents=True, exist_ok=True)
    _contained_destination(root, dest)
    created = not dest.exists()

    if not created and not overwrite:
        prior = _load_yaml(dest / "agent.yaml")
        prior_identity = str(prior.get("identity") or prior.get("name") or "").strip()
        installed_from = str(prior.get(MARKER_KEY) or "").strip()
        if installed_from != name:
            raise PackCollisionError(
                name, dest,
                "an agent directory already exists and was not installed from this pack "
                f"(installed_from={installed_from or 'unknown'})")
        if prior_identity and prior_identity != name:
            raise PackCollisionError(
                name, dest, f"the existing agent declares identity '{prior_identity}'")

    # Copy skills/, tools/, brain_pack.yaml verbatim. Existing files the source
    # does not carry — a live agent's oplog/ — are left alone.
    shutil.copytree(src, dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("agent.yaml"))

    normalized = normalize_agent_manifest(name, brain, manifest)
    try:
        import yaml
        text = yaml.safe_dump(normalized, sort_keys=False, allow_unicode=True)
    except ImportError:  # pragma: no cover - yaml is an adk dependency
        raise ValueError("PyYAML is required to write agent.yaml") from None

    agent_yaml = dest / "agent.yaml"
    changed = True
    if agent_yaml.is_file():
        try:
            changed = agent_yaml.read_text(encoding="utf-8") != text
        except OSError:
            changed = True
    if changed:
        staging = agent_yaml.with_name("agent.yaml.installing")
        staging.write_text(text, encoding="utf-8")
        os.replace(staging, agent_yaml)

    return {"agent": name, "installed_to": str(dest),
            "created": created, "changed": changed}


def install_from(src: Path | str, agents_root: Path | str | None = None, *,
                 overwrite: bool = False) -> list[dict[str, Any]]:
    """Install one pack dir, or every pack under a registry/pack container.

    Accepts ``<company>/pack/<agent>``, the ``<company>/pack`` directory (all of
    its agents), the ``<company>`` directory, or a plain directory of pack dirs.

    ``overwrite=True`` replaces an agent directory this installer did not create;
    the default refuses with :class:`PackCollisionError` instead.
    """
    src = Path(src).expanduser()
    if not src.is_dir():
        raise ValueError(f"not a directory: {src}")
    if _is_pack_dir(src):
        return [install_pack_dir(src, agents_root=agents_root, overwrite=overwrite)]

    agent_dirs: list[Path] = []
    for child in sorted(src.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if _is_pack_dir(child):
            agent_dirs.append(child)
        elif (child / "pack").is_dir():
            agent_dirs.extend(
                p for p in sorted((child / "pack").iterdir())
                if p.is_dir() and _is_pack_dir(p)
            )
    if not agent_dirs:
        raise ValueError(f"no agent packs found under {src}")
    return [install_pack_dir(p, agents_root=agents_root, overwrite=overwrite)
            for p in agent_dirs]


def resolve_catalog_id(ref: str) -> Path | None:
    """The local pack DIRECTORY for a catalog id (or a name that resolves to one).

    Accepts the id spellings ``adk catalog`` prints — ``brain:<company>/<name>``,
    ``agent:<name>``, ``awpack:<id>`` and a bare pack id — so a caller holding a
    catalog id no longer has to know which registry directory it came from.
    Returns ``None`` when the id is unknown or has no local directory (an app, an
    MCP server, an entry installed from a hosted marketplace).
    """
    from adk.catalog import _install_source_dir, get_entry
    item = get_entry(ref)
    if item is None:
        return None
    return _install_source_dir(item)


def install_catalog_entry(item_id: str, agents_root: "Path | str | None" = None, *,
                          signature: str = "", dry_run: bool = False) -> dict:
    """Install one catalog entry; ``ok`` False is a NON-ZERO exit for the caller.

    Free first-party entries are accepted on their tree DIGEST (no network, no
    signature); anything paid requires ``signature`` and fails closed otherwise.
    ``dry_run`` reports the plan and writes nothing. The returned ``exit_code``
    is the code a CLI should return: 0 ok, 1 refused, 2 unjudgeable.
    """
    from adk.catalog import install_entry
    result = install_entry(item_id, "workspace", dry_run=dry_run,
                           signature=signature, agents_root=agents_root)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m adk.pack_install",
        description="Install agent pack directories from a pack registry "
                    "into ~/.aither/agents.",
    )
    parser.add_argument("--from", dest="src", default=None,
                        help="pack/agent dir, or a directory that contains packs "
                             "(<company>/pack)")
    parser.add_argument("--catalog", default=None, metavar="ID",
                        help="a catalog id from `adk catalog list` "
                             "(brain:<company>/<name>, agent:<name>, awpack:<id>, "
                             "or a bare pack id) instead of --from")
    parser.add_argument("--signature", default="",
                        help="hex Ed25519 signature; required for a PAID entry")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the plan and write nothing")
    parser.add_argument("--agents-root", default=None,
                        help="install root (default: ~/.aither/agents)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--force", action="store_true",
                        help="replace an agent directory that was not installed from "
                             "this pack (default: refuse and report a collision)")
    args = parser.parse_args(argv)
    if bool(args.src) == bool(args.catalog):
        parser.error("give exactly one of --from or --catalog")

    if args.catalog:
        result = install_catalog_entry(args.catalog, args.agents_root,
                                       signature=args.signature, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            for line in result.get("plan") or []:
                print(f"  {line}")
            print(f"  {result.get('detail')}")
        # A free install verifies by DIGEST and a paid one by SIGNATURE; either
        # refusal is exit 1, never a silent 0 with "nothing installed".
        return int(result.get("exit_code", 1))

    try:
        results = install_from(args.src, agents_root=args.agents_root,
                               overwrite=args.force)
    except PackCollisionError as exc:
        print(f"COLLISION: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"installed": results}, indent=2))
        return 0
    for result in results:
        if result["created"]:
            state = "installed"
        elif result["changed"]:
            state = "updated"
        else:
            state = "unchanged"
        print(f"  {state}: {result['agent']} -> {result['installed_to']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
