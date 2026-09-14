#!/usr/bin/env python3
"""Scaffold an agent pack for an upstream project, with credit read from GitHub.

Wrapping somebody else's app is going to be a repeated act, and the part that
must never be improvised is the attribution. So the `upstream:` block is built
from the GitHub API — author, licence, description, homepage, and the PARENT
repo when the URL given is a fork.

That last part is the one a human gets wrong. Handing this a fork and having it
credit the forker would put our own name on someone else's work in a published
page. It resolves `parent` and credits the original, every time.

    python packaging/scaffold_pack.py https://github.com/wbern/bead-space
    python packaging/scaffold_pack.py wizzense/persona --name persona

Refuses rather than guesses:
  * no licence detected            -> we may not have the right to redistribute
  * a licence we do not vendor under -> same, said explicitly
  * the pack directory already exists -> never silently overwrites a real pack
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PACKS = Path(__file__).resolve().parent.parent / "adk" / "packs"

#: Licences we are willing to vendor and redistribute a copy under. Deliberately
#: a short allowlist: "permissive-looking" is not a licence review, and a wrong
#: guess here is a legal problem rather than a bug.
VENDORABLE = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense"}

#: A palette per pack so each page reads as the upstream project's own rather
#: than as a row in our catalogue. Overridable with --accent.
DEFAULT_ACCENT = "#5b8def"


def gh(path: str) -> dict:
    """One GitHub API call. Raises with the reason rather than returning {}."""
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"github api {path} failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def normalise(target: str) -> str:
    """Accept a URL or owner/repo."""
    m = re.search(r"github\.com[/:]([^/]+/[^/.]+)", target)
    slug = m.group(1) if m else target.strip("/")
    if slug.count("/") != 1:
        raise SystemExit(f"expected owner/repo or a GitHub URL, got {target!r}")
    return slug


def resolve(slug: str) -> dict:
    """Repo metadata, following a fork to the project that actually wrote it."""
    data = gh(f"repos/{slug}")
    if data.get("fork") and data.get("parent"):
        parent = data["parent"]["full_name"]
        print(f"  {slug} is a fork -> crediting {parent}")
        data = gh(f"repos/{parent}")
    return data


def build_block(repo: dict, accent: str) -> dict:
    owner = repo.get("owner") or {}
    spdx = ((repo.get("license") or {}).get("spdx_id") or "").strip()

    if not spdx or spdx == "NOASSERTION":
        raise SystemExit(
            f"{repo['full_name']} has no detectable licence.\n"
            "Refusing to scaffold: without one we have no right to vendor or "
            "redistribute it. Ask the author, or link to it instead of packing it."
        )
    if spdx not in VENDORABLE:
        raise SystemExit(
            f"{repo['full_name']} is {spdx}, which is not in the vendorable set "
            f"({', '.join(sorted(VENDORABLE))}).\n"
            "Refusing to scaffold. This needs a human licence decision, not a default."
        )

    return {
        "name": repo["name"],
        "author": owner.get("login", ""),
        "repo": repo["html_url"],
        "site": repo.get("homepage") or repo["html_url"],
        "license": spdx,
        "tagline": (repo.get("description") or "").strip(),
        "accent": accent,
    }


def write_pack(name: str, block: dict, title: str) -> Path:
    d = PACKS / name
    if d.exists():
        raise SystemExit(f"{d} already exists — refusing to overwrite a real pack")
    d.mkdir(parents=True)

    lines = [
        "# " + "=" * 74,
        f"# {title} — an aither-adk agent pack for {block['name']}",
        "# " + "=" * 74,
        f"# {block['tagline']}" if block["tagline"] else "#",
        "#",
        f"# {block['name']} is created by {block['author']} and licensed",
        f"# {block['license']}. This pack is an engine that runs behind it: their",
        "# application is not modified and not redistributed here, and this pack is",
        "# not affiliated with or endorsed by them.",
        "",
        f"pack_title: {title}",
        f"app_name: {title}",
        "company_name: Aitherium",
        f"identity: {name}",
        "",
        "# Built from the GitHub API rather than typed, and following the fork to the",
        "# project that actually wrote it — crediting a forker would put the wrong",
        "# name on somebody else's work in a published page.",
        "upstream:",
    ]
    for k in ("name", "author", "repo", "site", "license", "tagline", "accent"):
        v = str(block.get(k, "")).replace('"', "'")
        lines.append(f'  {k}: "{v}"')
    lines += [
        "",
        "system_prompt: |",
        f"  You are a working companion for {block['name']}.",
        "  Keep answers grounded in what the user is actually doing, and say when you",
        "  do not know rather than inventing a plausible answer.",
        "",
    ]
    (d / "brain_pack.yaml").write_text("\n".join(lines), encoding="utf-8", newline="")
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="GitHub URL or owner/repo")
    ap.add_argument("--name", help="pack directory name (default: the repo name)")
    ap.add_argument("--title", help="release title (default: the repo name)")
    ap.add_argument("--accent", default=DEFAULT_ACCENT)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.target:
        ap.error("a GitHub URL or owner/repo is required (or --self-test)")

    repo = resolve(normalise(args.target))
    block = build_block(repo, args.accent)
    name = args.name or repo["name"].lower()
    d = write_pack(name, block, args.title or repo["name"])

    print(f"  scaffolded {d}")
    print(f"    credits {block['author']} ({block['license']}) at {block['repo']}")
    return 0


def self_test() -> int:
    ok = True

    def check(label, cond):
        nonlocal ok
        if not cond:
            print(f"  FAIL  {label}")
            ok = False
        else:
            print(f"  PASS  {label}")

    check("URL normalises", normalise("https://github.com/a/b") == "a/b")
    check("owner/repo normalises", normalise("a/b") == "a/b")
    check("ssh URL normalises", normalise("git@github.com:a/b.git") == "a/b")
    try:
        normalise("nope")
        check("a bare word is refused", False)
    except SystemExit:
        check("a bare word is refused", True)

    good = {"full_name": "a/b", "name": "b", "html_url": "u", "homepage": "",
            "description": "d", "owner": {"login": "ada"}, "license": {"spdx_id": "MIT"}}
    check("MIT scaffolds", build_block(good, "#fff")["license"] == "MIT")

    for spdx, why in ((None, "no licence"), ("NOASSERTION", "unidentified"), ("GPL-3.0", "copyleft")):
        bad = dict(good, license=({"spdx_id": spdx} if spdx else {}))
        try:
            build_block(bad, "#fff")
            check(f"{why} is refused", False)
        except SystemExit:
            check(f"{why} is refused", True)

    check("homepage falls back to the repo url",
          build_block(dict(good, homepage=None), "#fff")["site"] == "u")
    print("self-test: PASS" if ok else "self-test: FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
