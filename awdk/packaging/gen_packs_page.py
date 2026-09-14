#!/usr/bin/env python3
"""Render the packs index page from the built artifacts.

The page is DERIVED from `dist/packs/index.json`, never hand-maintained. A
hand-written list of packs goes stale the first time one is added, and a page
advertising a download that does not exist is worse than no page — the reader
gets a 404 from something that looked official.

    python packaging/gen_packs_page.py --index dist/packs/index.json \\
        --out docs/packs.md --repo Aitherium/aither-adk --tag v3.3.0

Every download URL is built from the SAME manifest the release upload used, so
the page cannot advertise a filename the release does not carry.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HEADER = """# Agent packs

Each pack is a standalone download. Take one, run its installer, and adk finds
it — you do not need the rest of the framework to try a single pack.

```bash
tar xzf <pack>-<version>.tar.gz
python <pack>/install.py
```

That copies the pack to `~/.aither/packs/<name>/`, a location adk discovers with
no configuration, then **verifies** the pack is discoverable rather than assuming
it. If adk is not installed yet the installer says so and still places the files,
so the order does not matter:

```bash
pip install aither-adk
```

Every artifact ships a `.sha256` next to it. Verify before you trust it:

```bash
sha256sum -c <pack>-<version>.sha256
```

"""


def human(n: int) -> str:
    return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def render(index: dict, repo: str, tag: str) -> str:
    packs = index.get("packs", [])
    if not packs:
        # An empty page is not a page. Publishing "0 packs" as though it were a
        # finished document is the failure this generator exists to avoid.
        raise SystemExit("DEAD: manifest lists no packs — refusing to write an empty page")

    base = f"https://github.com/{repo}/releases/download/{tag}"
    out = [HEADER, f"Built from `{tag}` (adk {index.get('adk_version', '?')}).\n"]

    out.append("| Pack | Version | Download | Size | What it is |")
    out.append("|---|---|---|---|---|")
    for p in packs:
        url = f"{base}/{p['artifact']}"
        desc = (p.get("description") or "").replace("|", "\\|").strip() or "—"
        if len(desc) > 130:
            desc = desc[:127].rstrip() + "…"
        out.append(
            f"| **[{p['display_name']}](packs/{p['name']}.md)** | `{p['version']}` | "
            f"[{p['artifact']}]({url}) | {human(p['bytes'])} | {desc} |"
        )

    out.append("\n## Contents\n")
    for p in packs:
        bits = []
        if p.get("has_agent_yaml"):
            bits.append("agent config")
        if p.get("has_skills"):
            bits.append("skills")
        if p.get("has_code"):
            bits.append("Python")
        out.append(f"- **{p['name']}** `{p['version']}` — "
                   f"{', '.join(bits) if bits else 'brain pack only'}  \n"
                   f"  `sha256:{p['sha256'][:16]}…`")

    out.append("")
    return "\n".join(out)


def render_pack(p: dict, index: dict, repo: str, tag: str) -> str:
    """One pack's own page, so a single pack is a URL rather than a table row."""
    url = f"https://github.com/{repo}/releases/download/{tag}/{p['artifact']}"
    sha_url = f"https://github.com/{repo}/releases/download/{tag}/{p['name']}-{p['version']}.sha256"
    out = [
        f"# {p['display_name']}",
        "",
        f"`{p['name']}` · version `{p['version']}` · {human(p['bytes'])}",
        "",
        f"**[Download {p['artifact']}]({url})** · [checksum]({sha_url})",
        "",
        "```bash",
        f"curl -LO {url}",
        f"tar xzf {p['artifact']}",
        f"python {p['name']}/install.py",
        "```",
        "",
        f"Installs to `~/.aither/packs/{p['name']}/`, which adk discovers with no",
        "configuration. The installer verifies the pack is discoverable rather than",
        "assuming it. adk itself:",
        "",
        "```bash",
        "pip install aither-adk",
        "```",
        "",
    ]

    if p.get("overview"):
        out += ["## About", "", p["overview"], ""]

    if p.get("skills"):
        out += ["## Skills", ""]
        out += [f"- `{s}`" for s in p["skills"]]
        out.append("")

    out += ["## Contents", "", "```"]
    out += p.get("files", [])[:60]
    if len(p.get("files", [])) > 60:
        out.append(f"... and {len(p['files']) - 60} more")
    out += ["```", ""]

    if p.get("readme"):
        # The pack's own README, verbatim. Its author already explained the pack
        # better than a generator can; re-summarising it would only lose detail.
        out += ["---", "", p["readme"].strip(), ""]

    out += [
        "---",
        "",
        f"sha256 `{p['sha256']}`  ",
        f"Built from `{tag}` (adk {index.get('adk_version', '?')}). "
        f"[All packs](../packs.md)",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index")
    ap.add_argument("--out")
    ap.add_argument("--repo", default="Aitherium/aither-adk")
    ap.add_argument("--tag")
    ap.add_argument("--self-test", action="store_true")
    args, _ = ap.parse_known_args()

    if args.self_test:
        return self_test()

    if not (args.index and args.out and args.tag):
        ap.error("--index, --out and --tag are required (or use --self-test)")
    index_path = Path(args.index)
    if not index_path.is_file():
        print(f"DEAD: no manifest at {index_path}", file=sys.stderr)
        return 2

    index = json.loads(index_path.read_text(encoding="utf-8"))
    text = render(index, args.repo, args.tag)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="")
    print(f"wrote {out} ({len(text)} bytes)")

    # One page per pack, so a single pack is a URL rather than a table row.
    pages_dir = out.parent / "packs"
    pages_dir.mkdir(parents=True, exist_ok=True)
    for pk in index["packs"]:
        page = pages_dir / f"{pk['name']}.md"
        page.write_text(render_pack(pk, index, args.repo, args.tag),
                        encoding="utf-8", newline="")
        print(f"  wrote {page}")
    return 0


def self_test() -> int:
    ok = True

    def check(label: str, got, want) -> None:
        nonlocal ok
        if got != want:
            print(f"  FAIL  {label}: {got!r} != {want!r}")
            ok = False
        else:
            print(f"  PASS  {label}")

    idx = {"adk_version": "1.0.0", "packs": [{
        "name": "demo", "display_name": "Demo", "version": "9.9.9",
        "artifact": "demo-9.9.9.tar.gz", "sha256": "a" * 64, "bytes": 2048,
        "description": "a demo pack", "has_agent_yaml": True,
        "has_skills": False, "has_code": True,
    }]}
    text = render(idx, "Org/repo", "v1.0.0")
    check("download URL points at the release asset",
          "https://github.com/Org/repo/releases/download/v1.0.0/demo-9.9.9.tar.gz" in text, True)
    check("version rendered", "`9.9.9`" in text, True)
    check("bootstrap command documented", "install.py" in text, True)
    check("checksum verification documented", "sha256sum -c" in text, True)

    # An empty manifest must refuse rather than publish a page advertising nothing.
    try:
        render({"packs": []}, "Org/repo", "v1.0.0")
        print("  FAIL  empty manifest produced a page")
        ok = False
    except SystemExit:
        print("  PASS  empty manifest refuses to write a page")

    # A pipe in a description must not break the table.
    idx2 = json.loads(json.dumps(idx))
    idx2["packs"][0]["description"] = "does a | b"
    check("pipe escaped so the table survives",
          "does a \\| b" in render(idx2, "Org/repo", "v1.0.0"), True)

    print("self-test: PASS" if ok else "self-test: FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
