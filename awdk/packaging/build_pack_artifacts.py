#!/usr/bin/env python3
"""Build one standalone, linkable release artifact per agent pack.

Packs used to exist only as folders inside the wheel. That works for `pip
install aither-adk`, and it is useless for the thing people actually ask for:
a link to ONE pack. There was nothing to point at — no version, no download, no
page — so "here is our GobboNet pack" meant "install the whole framework and
look in a subdirectory".

This produces, per pack:

    dist/packs/<name>-<version>.tar.gz   the pack, plus a bootstrap installer
    dist/packs/<name>-<version>.sha256   so a download can be verified
    dist/packs/index.json                the manifest the site renders from

Each tarball is self-bootstrapping on purpose. A release artifact that requires
the reader to already know where packs live is a file, not a release:

    tar xzf gobbonet-1.0.0.tar.gz
    python gobbonet/install.py

That copies the pack to ~/.aither/packs/<name>/ — a location adk discovers with
no config — and then VERIFIES the pack is discoverable rather than asserting it.
If adk is not installed yet the installer says so and still places the files, so
the order of operations does not matter.

Versions come from `version:` in brain_pack.yaml. A pack without one rides the
adk package version, and that is PRINTED rather than silently defaulted: a pack
whose version never moves while its contents do is the same class of lie as a
hand-maintained figure on a landing page.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path

ADK_ROOT = Path(__file__).resolve().parent.parent
PACKS_DIR = ADK_ROOT / "adk" / "packs"
SKIP = {"__pycache__"}

INSTALLER = '''#!/usr/bin/env python3
"""Install this pack so adk can find it. No config, no path to remember."""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = HERE.name


def main() -> int:
    dest_root = Path.home() / ".aither" / "packs"
    dest = dest_root / NAME
    dest_root.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(HERE, dest, ignore=shutil.ignore_patterns("install.py", "__pycache__"))
    print(f"installed {NAME} -> {dest}")

    # VERIFY, never assert. Copying files and printing "done" is exactly how a
    # broken install reads as a working one.
    try:
        from adk.pack_discovery import list_available_packs
    except ImportError:
        print("aither-adk is not installed yet, so discovery cannot be checked.")
        print("Run `pip install aither-adk` and the pack will be found — the files")
        print("are already in the right place; installation order does not matter.")
        return 0

    names = {p.get("name") for p in list_available_packs()}
    if NAME in names:
        print(f"verified: adk discovers '{NAME}'")
        return 0
    print(f"WARNING: files copied, but adk does not list '{NAME}'.")
    print(f"  discovered: {sorted(n for n in names if n)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''


def adk_version() -> str:
    text = (ADK_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("could not read version from pyproject.toml")
    return m.group(1)


def header_summary(text: str) -> str | None:
    """The pack's own one-line summary, taken from its header comment.

    These files carry no `description:` key — their summary is the first real
    line of the banner comment, e.g.

        # ====================================
        # GobboNet Companion — an agent harness for a local-first chat client
        # ====================================

    Read it rather than inventing one. A generated page whose "what it is"
    column is empty tells a reader nothing, and a description written by the
    generator would describe the generator's guess, not the pack.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("#"):
            break  # past the header block; body keys start here
        body = line.lstrip("#").strip()
        # Skip rule-off banners (===, ---) and blank comment lines.
        if not body or set(body) <= set("=-_ "):
            continue
        return body
    return None


def pack_meta(pack: Path, fallback_version: str) -> dict:
    """Read what the site and the manifest need. Never invents a description."""
    bp = pack / "brain_pack.yaml"
    text = bp.read_text(encoding="utf-8", errors="replace")

    def scalar(key: str) -> str | None:
        m = re.search(rf'^{key}\s*:\s*["\']?([^"\'\n#]+)', text, re.M)
        return m.group(1).strip() if m else None

    version = scalar("version")
    return {
        "name": pack.name,
        "version": version or fallback_version,
        "version_source": "pack" if version else "adk",
        # `pack_title` is the RELEASE name — what the page and a shared link
        # call this pack. It is deliberately separate from `app_name`, which is
        # what the agent calls itself at runtime: a pack can be branded
        # "GobboPack" while the companion inside it still introduces itself as
        # "GobboNet Companion". Neither renames the directory, because that name
        # is the discovery identity and changing it would orphan every existing
        # ~/.aither/packs/<name> install.
        "display_name": (scalar("pack_title") or scalar("app_name")
                         or scalar("display_name") or scalar("name") or pack.name),
        "description": scalar("description") or header_summary(text) or "",
        "has_agent_yaml": (pack / "agent.yaml").is_file(),
        "has_skills": (pack / "skills").is_dir(),
        "has_code": any(p.suffix == ".py" for p in pack.rglob("*.py")),
        # Everything below feeds the pack's OWN page. A per-pack page that only
        # repeats the index row is not worth a URL, so it carries the pack's
        # real README and its actual contents.
        "skills": sorted(p.stem for p in (pack / "skills").glob("*.md"))
                  if (pack / "skills").is_dir() else [],
        "files": sorted(
            p.relative_to(pack).as_posix()
            for p in pack.rglob("*")
            if p.is_file() and not SKIP & set(p.relative_to(pack).parts)
        ),
        "readme": ((pack / "README.md").read_text(encoding="utf-8", errors="replace")
                   if (pack / "README.md").is_file() else ""),
        "overview": header_block(text),
        # Who actually wrote the app this pack serves. Parsed as real YAML
        # rather than by regex: it is a nested block, and a flat scalar scan
        # would silently pick up the wrong keys — producing a page that credits
        # the wrong people, which is worse than a page with no credit.
        "upstream": _upstream(text),
    }


def _upstream(text: str) -> dict:
    """The `upstream:` block, or {} when the pack wraps nothing external."""
    try:
        import yaml
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001 - a pack we cannot parse simply has no credit block
        return {}
    up = data.get("upstream")
    return up if isinstance(up, dict) else {}


def header_block(text: str) -> str:
    """The pack's full header comment, as prose.

    The one-line summary drives the index; a pack's own page deserves the whole
    explanation its author already wrote. Reading it beats generating a
    description that would only restate the filename.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("#"):
            break
        body = line.lstrip("#").strip()
        if set(body) <= set("=-_ ") and body:
            continue  # rule-off banner
        lines.append(body)
    # Drop the leading title line (already the page heading) and trailing blanks.
    while lines and not lines[0]:
        lines.pop(0)
    if lines:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()


def build_one(pack: Path, out_dir: Path, fallback_version: str) -> dict:
    meta = pack_meta(pack, fallback_version)
    stem = f"{meta['name']}-{meta['version']}"
    tar_path = out_dir / f"{stem}.tar.gz"

    staging = out_dir / "_staging" / meta["name"]
    if staging.exists():
        shutil.rmtree(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(pack, staging, ignore=shutil.ignore_patterns(*SKIP))
    (staging / "install.py").write_text(INSTALLER, encoding="utf-8", newline="")

    if tar_path.exists():
        tar_path.unlink()
    with tarfile.open(tar_path, "w:gz") as tf:
        # arcname is the pack name so `tar xzf` yields <name>/, which is what the
        # documented `python <name>/install.py` then refers to.
        tf.add(staging, arcname=meta["name"])
    shutil.rmtree(staging.parent, ignore_errors=True)

    digest = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    (out_dir / f"{stem}.sha256").write_text(
        f"{digest}  {tar_path.name}\n", encoding="utf-8", newline=""
    )

    meta["artifact"] = tar_path.name
    meta["sha256"] = digest
    meta["bytes"] = tar_path.stat().st_size
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ADK_ROOT / "dist" / "packs"))
    ap.add_argument("--pack", help="build one pack instead of all")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if not PACKS_DIR.is_dir():
        print(f"DEAD: no packs directory at {PACKS_DIR}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    fallback = adk_version()

    candidates = sorted(
        p for p in PACKS_DIR.iterdir()
        if p.is_dir() and p.name not in SKIP and (p / "brain_pack.yaml").is_file()
    )
    if args.pack:
        candidates = [p for p in candidates if p.name == args.pack]
        if not candidates:
            print(f"DEAD: no pack named {args.pack!r}", file=sys.stderr)
            return 2
    if not candidates:
        # An empty build is never a successful build: it would publish a site
        # advertising zero packs and report success doing it.
        print("DEAD: no packs with a brain_pack.yaml were found", file=sys.stderr)
        return 2

    built = [build_one(p, out_dir, fallback) for p in candidates]
    riding = [m["name"] for m in built if m["version_source"] == "adk"]

    (out_dir / "index.json").write_text(
        json.dumps({"adk_version": fallback, "packs": built}, indent=2) + "\n",
        encoding="utf-8", newline="",
    )

    for m in built:
        print(f"  {m['artifact']:<34} {m['bytes']:>8,} bytes  {m['sha256'][:12]}")
    print(f"\n{len(built)} pack artifact(s) -> {out_dir}")
    if riding:
        # Printed, never silent: a pack version that cannot move independently
        # will not track its own contents.
        print(f"riding the adk version ({fallback}), no `version:` of their own: "
              f"{', '.join(riding)}")
    return 0


def self_test() -> int:
    """Prove a built artifact is actually installable and complete."""
    import tempfile

    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pack = root / "packs" / "demo"
        (pack / "skills").mkdir(parents=True)
        (pack / "brain_pack.yaml").write_text(
            "name: demo\nversion: 9.9.9\ndescription: a demo pack\n", encoding="utf-8")
        (pack / "skills" / "s.md").write_text("skill", encoding="utf-8")

        out = root / "out"
        out.mkdir()
        meta = build_one(pack, out, "0.0.0")

        def check(label: str, got, want) -> None:
            nonlocal ok
            if got != want:
                print(f"  FAIL  {label}: {got!r} != {want!r}")
                ok = False
            else:
                print(f"  PASS  {label}")

        check("pack version wins over the adk fallback", meta["version"], "9.9.9")
        check("version source recorded", meta["version_source"], "pack")

        tar_path = out / meta["artifact"]
        check("artifact exists", tar_path.is_file(), True)

        with tarfile.open(tar_path) as tf:
            names = set(tf.getnames())
        # The installer MUST be inside, or the documented bootstrap command
        # refers to a file the download does not contain.
        check("installer shipped inside the artifact", "demo/install.py" in names, True)
        check("pack content shipped", "demo/brain_pack.yaml" in names, True)
        check("skills shipped", "demo/skills/s.md" in names, True)

        digest = hashlib.sha256(tar_path.read_bytes()).hexdigest()
        check("sha256 file matches the artifact",
              (out / "demo-9.9.9.sha256").read_text(encoding="utf-8").split()[0], digest)

        # A pack with no version rides the fallback, and says so.
        (pack / "brain_pack.yaml").write_text(
            "name: demo\ndescription: no version here\n", encoding="utf-8")
        meta2 = build_one(pack, out, "1.2.3")
        check("no pack version falls back to adk", meta2["version"], "1.2.3")
        check("fallback is recorded, not hidden", meta2["version_source"], "adk")

    print("self-test: PASS" if ok else "self-test: FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
