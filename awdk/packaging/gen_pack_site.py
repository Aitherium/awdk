#!/usr/bin/env python3
"""Render a page per pack, in the UPSTREAM project's branding.

A pack that wraps somebody else's application is, first, an advertisement for
their work. The page leads with THEIR name, THEIR tagline, THEIR links and
THEIR accent, and only then explains what the pack adds.

That is not politeness. A page presenting a wrapper as the product is how a
community concludes you are strip-mining their project — and it is also just
inaccurate: GobboNet is the reason the GobboPack page is worth visiting.

Every credited field comes from the pack's `upstream:` block, so credit cannot
drift out in a redesign: remove the block and the generator REPORTS the pack as
uncredited rather than quietly rendering it as ours.

    python packaging/gen_pack_site.py --index dist/packs/index.json \\
        --out docs --repo Aitherium/aither-adk --tag v3.3.0

Self-contained: no CDN, no webfont, no tracker. These pages are published for
other people's communities, and a page that phones somewhere on load would
contradict the local-first property every one of these packs protects.

The visual identity lives in `pack_site_theme.py`, so the design can be judged
as a design and a change to it cannot quietly become a change to what the page
CLAIMS.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

try:  # imported as part of a package
    from .pack_site_theme import css
except ImportError as _rel_exc:  # run as a script from anywhere
    # Scoped to this import rather than a module-level sys.path mutation, which
    # would leak into whatever else imports this module.
    #
    # The original error is CHAINED, not swallowed: if the relative import fails
    # for a reason INSIDE the theme module, the fallback fails too and the
    # operator would otherwise be told "No module named 'pack_site_theme'" —
    # a module that was never supposed to exist — while the real cause is gone.
    import importlib.util

    _path_ = Path(__file__).resolve().parent / "pack_site_theme.py"
    _spec_ = importlib.util.spec_from_file_location("pack_site_theme", _path_)
    if _spec_ is None or _spec_.loader is None:
        raise ImportError(f"cannot load the theme from {_path_}") from _rel_exc
    _mod_ = importlib.util.module_from_spec(_spec_)
    try:
        _spec_.loader.exec_module(_mod_)
    except Exception as _exc_:
        raise ImportError(f"the theme at {_path_} failed to load") from _exc_
    css = _mod_.css

ADK_ACCENT = "#4f7cf7"
ADK_ACCENT_DARK = "#8aa9fb"

NL = chr(10)


def _e(s) -> str:
    return html.escape(str(s or ""))


def _human(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def _terminal(lines: list[tuple[str, str]], title: str) -> str:
    """The hero. `lines` is [(kind, text)] with kind in cmd | out | comment.

    One `white-space: pre` block rather than a div per line, so a reader can
    select and copy the commands the way they would from a real terminal — the
    single thing this page exists to make them do.
    """
    body = []
    for kind, text in lines:
        if kind == "cmd":
            body.append(f'<span class="p">$</span> {_e(text)}')
        elif kind == "comment":
            body.append(f'<span class="c"># {_e(text)}</span>')
        else:
            body.append(f'<span class="o">{_e(text)}</span>')
    body.append('<span class="p">$</span> <span class="caret"></span>')
    return (
        '<div class="term">'
        '<div class="term-bar">'
        '<span class="dot live"></span><span class="dot"></span><span class="dot"></span>'
        f'<span class="term-name">{_e(title)}</span>'
        "</div>"
        f'<pre class="term-body">{NL.join(body)}</pre>'
        "</div>"
    )


def _lanes(posix: list[tuple[str, str]], win: list[tuple[str, str]],
           title: str) -> str:
    """Both shells, side by side, neither one the footnote.

    The page shipped POSIX only. That is not a cosmetic gap: `sha256sum` does
    not exist on Windows, so the verification step — the one a security-minded
    reader is most likely to actually run — failed with `command not found` and
    taught them the instructions were written for somebody else. And the pack's
    own upstream is a WINDOWS application, so Windows is not the minority
    audience here; it is the majority.

    Two static blocks rather than JS tabs: a tab hides half the answer behind a
    click, and the half it hides is whichever one the reader needs. Rendering
    both also means the truth check can assert both are present, which a
    scripted tab cannot be checked for without a browser.
    """
    return (
        '<div class="lanes">'
        f'<div class="lane"><span class="lane-tag">macOS / Linux</span>'
        f'{_terminal(posix, title)}</div>'
        f'<div class="lane"><span class="lane-tag">Windows</span>'
        f'{_terminal(win, title)}</div>'
        "</div>"
    )


def _spec(rows: list[tuple[str, str]]) -> str:
    cells = "".join(f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in rows if v)
    return f'<dl class="spec">{cells}</dl>'


FEATURES = [
    ("Web search, no account",
     "DuckDuckGo through a maintained client. No key, no sign-up, nothing hosted."),
    ("An agent behind the chat box",
     "Tools, memory and skills answer on the API the app already speaks, so its "
     "interface needs no changes."),
    ("Your own model",
     "llama.cpp, ollama, vLLM and LM Studio are discovered automatically; "
     "<code>--setup-model</code> installs one sized to the machine."),
    ("Weights without an account",
     "Resumable, rate-capped and size-verified, so a dropped connection costs "
     "minutes rather than the whole download."),
]


#: Elements that never take a closing tag. Anything else that opens must close.
_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


def wellformed_errors(page: str) -> list[str]:
    """Structural problems in generated HTML: [] when the page is sound.

    Every other assertion in the self-test asks whether a STRING is present,
    so none of them can see an unclosed tag or a stray closer — the page
    would ship broken with a green test. This walks the tag stack instead.
    """
    from html.parser import HTMLParser

    class _P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.errors = []

        def handle_starttag(self, tag, attrs):
            if tag not in _VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in _VOID:
                return
            if not self.stack:
                self.errors.append(f'stray </{tag}> with nothing open')
                return
            if self.stack[-1] != tag:
                # Report the mismatch rather than trying to recover: a
                # guessed recovery turns one real error into a cascade of
                # invented ones.
                self.errors.append(
                    f'</{tag}> closes <{self.stack[-1]}>')
                if tag in self.stack:
                    while self.stack and self.stack.pop() != tag:
                        pass
                return
            self.stack.pop()

    parser = _P()
    parser.feed(page)
    parser.close()
    if parser.stack:
        parser.errors.append('never closed: ' + ', '.join(parser.stack))
    return parser.errors


def render_pack_page(p: dict, index: dict, repo: str, tag: str) -> str:
    up = p.get("upstream") or {}
    accent = up.get("accent") or ADK_ACCENT
    accent_dark = up.get("accent_dark") or up.get("accent") or ADK_ACCENT_DARK

    base = f"https://github.com/{repo}/releases/download/{tag}"
    art = f"{base}/{p['artifact']}"
    sha_file = f"{p['name']}-{p['version']}.sha256"

    if up:
        title = up.get("name") or p["display_name"]
        eyebrow = f"An agent pack for <b>{_e(title)}</b>"
        lede = up.get("tagline") or ""
    else:
        title = p["display_name"]
        eyebrow = "aither-adk agent pack"
        lede = p.get("description") or ""

    jump = []
    if up.get("site"):
        jump.append(f'<a href="{_e(up["site"])}">Project site</a>')
    if up.get("repo"):
        jump.append(f'<a href="{_e(up["repo"])}">Source</a>')
    jump.append(f'<a href="{_e(art)}">Download</a>')

    term = _lanes([
        ("comment", "one command each; nothing else to configure"),
        ("cmd", f"curl -LO {art}"),
        ("cmd", f"tar xzf {p['artifact']}"),
        ("cmd", f"python {p['name']}/install.py"),
        ("out", f"installed {p['name']} -> ~/.aither/packs/{p['name']}"),
        ("out", f"verified: adk discovers '{p['name']}'"),
    ], [
        ("comment", "PowerShell. curl.exe and tar ship with Windows 10 1803+"),
        # curl.exe, not curl: in PowerShell 5.1 `curl` is an alias for
        # Invoke-WebRequest, which does not understand -LO and fails with an
        # error about a missing parameter rather than about the alias.
        ("cmd", f"curl.exe -LO {art}"),
        ("cmd", f"tar -xzf {p['artifact']}"),
        ("cmd", f"python {p['name']}\\install.py"),
        ("out", f"installed {p['name']} -> ~\\.aither\\packs\\{p['name']}"),
        ("out", f"verified: adk discovers '{p['name']}'"),
    ], f"{p['name']} — install")

    spec = _spec([
        ("Version", p["version"]),
        ("Size", _human(p["bytes"])),
        ("Licence", up.get("license") or "—"),
        ("sha256", p["sha256"][:16] + "…"),
    ])

    feats = "".join(f"<div><dt>{t}</dt><dd>{d}</dd></div>" for t, d in FEATURES)

    skills = ""
    if p.get("skills"):
        items = "".join(f"<div><dt><code>{_e(s)}</code></dt><dd></dd></div>"
                        for s in p["skills"])
        skills = ('<section><h2>Skills</h2>'
                  '<p class="sub">Included in the download.</p>'
                  f'<dl class="feat">{items}</dl></section>')

    about = ""
    if p.get("overview"):
        paras = "".join(f"<p>{_e(b.strip())}</p>"
                        for b in p["overview"].split(NL + NL) if b.strip())
        about = f'<section><h2>About</h2><div class="prose">{paras}</div></section>'

    colophon = ""
    if up:
        who = " / ".join(x for x in (up.get("author"), up.get("org")) if x)
        lic = f" Licensed {_e(up['license'])}." if up.get("license") else ""
        link = f'<a href="{_e(up.get("repo") or up.get("site"))}">{_e(up.get("name"))}</a>'
        colophon = (
            '<div class="colophon"><p>'
            f"{link} is created by <strong>{_e(who)}</strong>.{lic} "
            "This pack is an engine that runs behind it — their application is not "
            "modified, not redistributed here, and not affiliated with us. "
            "If you like what you see, the credit belongs upstream."
            "</p></div>"
        )

    verify = _lanes([
        ("comment", "every artifact ships a checksum; check it before you trust it"),
        ("cmd", f"sha256sum -c {sha_file}"),
        ("out", f"{p['artifact']}: OK"),
    ], [
        ("comment", "there is no sha256sum on Windows; Get-FileHash is the equivalent"),
        # The FULL digest, and compared rather than printed. Two reasons, both
        # learned the hard way elsewhere in this repo: a 64-character hex string
        # compared by eye is a check people believe they performed, and a
        # TRUNCATED digest in the comparison makes it print False on a perfectly
        # good download — a verification step that always fails is uninstallable
        # advice, and gets ignored rather than fixed.
        ("cmd", f"$want = '{p['sha256'].upper()}'"),
        ("cmd", f"(Get-FileHash {p['artifact']} -Algorithm SHA256).Hash -eq $want"),
        ("out", "True"),
    ], "verify")

    indie = ""
    if up:
        indie = ("<br>" + _e(up.get("name")) + " is an independent project. "
                 "This page links to it; it does not speak for it.")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} pack</title>
<meta name="description" content="{_e(lede)}">
<style>{css(accent, accent_dark)}</style>
</head>
<body>
<main class="shell">

  <header class="mast">
    <p class="eyebrow">{eyebrow}</p>
    <h1>{_e(title)}</h1>
    <p class="lede">{_e(lede)}</p>
    <nav class="jump">{"".join(jump)}</nav>
  </header>

  {term}
  {spec}

  <section>
    <h2>What the pack adds</h2>
    <p class="sub">The app keeps its own interface. This runs underneath it.</p>
    <dl class="feat">{feats}</dl>
  </section>

  {skills}
  {about}

  <section>
    <h2>Verify</h2>
    <p class="sub">sha256 <code>{_e(p["sha256"])}</code></p>
    {verify}
  </section>

  {colophon}

  <footer>
    Built from <code>{_e(tag)}</code> · adk {_e(index.get("adk_version", "?"))} ·
    <a href="../packs.html">All packs</a> ·
    <a href="https://github.com/{_e(repo)}">aither-adk</a>{indie}
  </footer>

</main>
</body>
</html>
"""


def render_index(index: dict, repo: str, tag: str) -> str:
    packs = index.get("packs", [])
    if not packs:
        raise SystemExit("DEAD: manifest lists no packs — refusing to write an empty index")

    rows = ""
    for p in packs:
        up = p.get("upstream") or {}
        who = " / ".join(x for x in (up.get("author"), up.get("org")) if x)
        by = f'<span class="by">by {_e(who)}</span>' if who else ""
        rows += (
            f'<a href="packs/{_e(p["name"])}.html">'
            f'<span><span class="nm">{_e(p["display_name"])}</span> {by}</span>'
            f'<span class="vs">{_e(p["version"])}</span>'
            f'<span class="ds">{_e((p.get("description") or "").strip())}</span>'
            "</a>"
        )

    term = _terminal([
        ("comment", "any pack, same three lines"),
        ("cmd", "tar xzf <pack>-<version>.tar.gz"),
        ("cmd", "python <pack>/install.py"),
        ("out", "installed -> ~/.aither/packs/<pack>"),
    ], "install any pack")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent packs</title>
<meta name="description"
      content="Standalone agent packs for aither-adk. Take one, run its installer.">
<style>{css(ADK_ACCENT, ADK_ACCENT_DARK)}</style>
</head>
<body>
<main class="shell">

  <header class="mast">
    <p class="eyebrow">aither-adk</p>
    <h1>Agent packs</h1>
    <p class="lede">Each pack is a standalone download. Take one, run its installer,
      and adk finds it — you do not need the rest of the framework to try a single pack.</p>
  </header>

  {term}

  <section>
    <h2>Available</h2>
    <p class="sub">Packs that wrap an independent project credit and link to it on
      their own page. Those projects are not affiliated with us.</p>
    <div class="packs">{rows}</div>
  </section>

  <footer>
    Built from <code>{_e(tag)}</code> ·
    <a href="https://github.com/{_e(repo)}">aither-adk</a>
  </footer>

</main>
</body>
</html>
"""


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

    ip = Path(args.index)
    if not ip.is_file():
        print(f"DEAD: no manifest at {ip}", file=sys.stderr)
        return 2

    index = json.loads(ip.read_text(encoding="utf-8"))
    out = Path(args.out)
    (out / "packs").mkdir(parents=True, exist_ok=True)

    (out / "packs.html").write_text(
        render_index(index, args.repo, args.tag), encoding="utf-8", newline="")
    print(f"wrote {out / 'packs.html'}")

    uncredited = []
    for p in index["packs"]:
        page = out / "packs" / f"{p['name']}.html"
        page.write_text(render_pack_page(p, index, args.repo, args.tag),
                        encoding="utf-8", newline="")
        print(f"  wrote {page}")
        if not p.get("upstream"):
            uncredited.append(p["name"])

    if uncredited:
        print(f"{NL}no upstream credit declared: {', '.join(uncredited)}")
        print("  (fine for packs that wrap nothing external — check that is true)")
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

    idx = {"adk_version": "1.0", "packs": [{
        "name": "demo", "display_name": "DemoPack", "version": "1.0",
        "artifact": "demo-1.0.tar.gz", "sha256": "a" * 64, "bytes": 2048,
        "description": "d", "skills": ["s1"], "files": [], "overview": "Para one.",
        "upstream": {"name": "TheirApp", "author": "Ada", "org": "Their Co",
                     "repo": "https://github.com/x/y", "site": "https://their.site",
                     "license": "MIT", "tagline": "Their tagline",
                     "accent": "#123456", "accent_dark": "#654321"},
    }]}
    page = render_pack_page(idx["packs"][0], idx, "Org/repo", "v1")

    check("upstream name is the H1, not ours", "<h1>TheirApp</h1>" in page)
    check("their tagline leads", "Their tagline" in page)
    check("author credited", "Ada" in page and "Their Co" in page)
    check("links to their site and source", "their.site" in page and "github.com/x/y" in page)
    check("their accent drives the theme", "#123456" in page and "#654321" in page)
    check("licence stated", "Licensed MIT" in page)
    check("non-affiliation stated", "not affiliated" in page)
    check("install command present", "install.py" in page)
    check("checksum verification present", "sha256sum -c" in page)

    # Both shells, asserted separately. The page shipped POSIX-only, and the
    # gap was invisible to every check above: the page rendered, the commands
    # were correct, and they simply did not run on the platform the pack's own
    # upstream targets. A reader on Windows hit `sha256sum: command not found`
    # on the verification step and learned the page was not written for them.
    check("Windows install lane present",
          "curl.exe -LO" in page and "tar -xzf" in page)
    check("Windows verification lane present", "Get-FileHash" in page)
    check("both lanes are labelled", "macOS / Linux" in page and ">Windows<" in page)
    # `curl` in PowerShell 5.1 is an alias for Invoke-WebRequest, which does not
    # understand -LO. The .exe suffix is what makes the command work at all.
    check("Windows fetch uses curl.exe, not the PowerShell alias",
          "curl.exe -LO" in page)
    # A truncated digest in the comparison prints False on a good download —
    # a verification step that always fails is advice people learn to skip.
    # Escaped, because that is what actually reaches the reader — asserting the
    # unescaped form tests a string this page never contains.
    check("Windows digest comparison uses the FULL sha256",
          html.escape("$want = '" + "A" * 64 + "'", quote=True) in page)

    # Theme correctness — the classic unreadable-page bug is a colour whose only
    # definition sits behind a media query or a [data-theme] stamp.
    check("dark tokens defined for stamped AND unstamped states",
          ':root[data-theme="dark"]' in page and ':root:not([data-theme="light"])' in page)
    check("body paints an explicit background token", "background: var(--ground)" in page)
    check("reduced motion respected", "prefers-reduced-motion" in page)
    check("focus is visible", "focus-visible" in page)
    check("self-contained: no CDN, webfont or import",
          "cdn." not in page and "fonts.googleapis" not in page and "@import" not in page)

    evil = json.loads(json.dumps(idx["packs"][0]))
    evil["upstream"]["author"] = "<script>alert(1)</script>"
    check("upstream fields are escaped",
          "<script>alert(1)</script>" not in render_pack_page(evil, idx, "o/r", "v1"))

    plain = json.loads(json.dumps(idx["packs"][0]))
    plain.pop("upstream")
    p2 = render_pack_page(plain, idx, "o/r", "v1")
    check("uncredited pack renders under its own name", "<h1>DemoPack</h1>" in p2)
    check("uncredited pack claims no byline", "not affiliated" not in p2)

    check("index links each pack page",
          'href="packs/demo.html"' in render_index(idx, "o/r", "v1"))

    # Structure, not just content. Nothing above would notice an unclosed tag.
    for label, doc in (("pack page", page),
                       ("uncredited page", p2),
                       ("index", render_index(idx, "o/r", "v1"))):
        errs = wellformed_errors(doc)
        check(f"{label} is well-formed HTML" + (f" ({errs})" if errs else ""), not errs)

    # And prove THAT check can fail, or it is decoration.
    check("the well-formedness check can fail",
          bool(wellformed_errors("<main><div><p>x</p></main>")))

    try:
        render_index({"packs": []}, "o/r", "v1")
        check("empty manifest refuses", False)
    except SystemExit:
        check("empty manifest refuses", True)

    print("self-test: PASS" if ok else "self-test: FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
