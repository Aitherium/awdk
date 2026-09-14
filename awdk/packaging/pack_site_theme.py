"""The visual identity for pack pages.

Kept apart from the generator so the design can be judged as a design, and so a
change to it cannot quietly become a change to what the page CLAIMS.

THE IDEA: the page exists to get one person to paste one command. So the
terminal is the hero — the largest, most deliberate element — rather than a code
block bolted under a headline. Everything above it is identity, everything below
is evidence.

CONSTRAINT THAT SHAPED THE TYPE: these pages ship to other people's communities
and must be self-contained, so no webfont. Personality therefore comes from
TREATMENT rather than from an exotic family — display set large with tight
negative tracking, eyebrows tiny and wide, and the monospace given real presence
because it is the subject's own instrument.

Neutrals are mixed toward the upstream's accent rather than being pure grey. A
mid-grey reads as unconsidered; a grey that leans the project's own hue reads as
chosen, and it costs nothing.
"""

from __future__ import annotations


def css(accent: str, accent_dark: str) -> str:
    """Theme-aware stylesheet.

    Light lives on bare `:root`; dark redefines ONLY the tokens, guarded so an
    explicit light choice still beats a dark OS, and again under
    `[data-theme="dark"]` so the toggle wins the other way. Nothing is styled
    inside a media block — a colour defined only there never applies in the
    un-stamped default state, which is what most viewers see.
    """
    return f"""
:root {{
  --accent: {accent};
  --ground: #fbfaf9;
  --panel: #ffffff;
  --ink: #17161a;
  --muted: #6b6772;
  --rule: #e6e3e6;
  /* The terminal is a screen in BOTH themes: it should read as an inset
     instrument, not as a light-mode code block that goes dark later. */
  --screen: #16151a;
  --screen-ink: #e9e6ef;
  --screen-dim: #6f6a7d;
  --shadow: 0 1px 2px rgba(20,18,24,.05), 0 12px 32px -12px rgba(20,18,24,.18);
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --accent: {accent_dark};
    --ground: #0e0d11;
    --panel: #151419;
    --ink: #ece9f0;
    --muted: #9a94a5;
    --rule: #262430;
    --screen: #08070a;
    --screen-ink: #e9e6ef;
    --screen-dim: #6f6a7d;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 18px 40px -16px rgba(0,0,0,.7);
  }}
}}
:root[data-theme="dark"] {{
  --accent: {accent_dark};
  --ground: #0e0d11;
  --panel: #151419;
  --ink: #ece9f0;
  --muted: #9a94a5;
  --rule: #262430;
  --screen: #08070a;
  --screen-ink: #e9e6ef;
  --screen-dim: #6f6a7d;
  --shadow: 0 1px 2px rgba(0,0,0,.4), 0 18px 40px -16px rgba(0,0,0,.7);
}}

*, *::before, *::after {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font: 400 16px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-synthesis-weight: none;
}}
.shell {{ max-width: 940px; margin: 0 auto; padding: 0 28px; }}

/* ── identity ─────────────────────────────────────────────────────────── */
.mast {{ padding: 76px 0 0; }}
.eyebrow {{
  margin: 0 0 18px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .18em;
  text-transform: uppercase;
  color: var(--muted);
}}
.eyebrow b {{ color: var(--accent); font-weight: 600; }}
h1 {{
  margin: 0;
  font-size: clamp(40px, 7vw, 66px);
  line-height: .98;
  letter-spacing: -.04em;
  font-weight: 680;
  text-wrap: balance;
}}
.lede {{
  margin: 20px 0 0;
  max-width: 34em;
  font-size: 19px;
  line-height: 1.5;
  color: var(--muted);
  text-wrap: pretty;
}}
.jump {{ display: flex; flex-wrap: wrap; gap: 22px; margin: 26px 0 0; }}
.jump a {{
  color: var(--ink);
  text-decoration: none;
  font-size: 14px;
  font-weight: 550;
  border-bottom: 2px solid var(--accent);
  padding-bottom: 2px;
}}
.jump a:hover {{ color: var(--accent); }}

/* ── two shells, equal weight ─────────────────────────────────────────── */
/* Side by side above 900px, stacked below. Neither lane is a footnote: the
   pack's own upstream is a Windows application, so demoting Windows to a
   parenthetical would be demoting the majority of the readership. */
.lanes {{
  display: grid;
  grid-template-columns: 1fr;
  gap: 22px;
  margin: 44px 0 0;
}}
@media (min-width: 900px) {{
  .lanes {{ grid-template-columns: 1fr 1fr; gap: 18px; }}
}}
.lane {{ display: flex; flex-direction: column; gap: 9px; min-width: 0; }}
.lane-tag {{
  font: 600 11px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  letter-spacing: .09em;
  text-transform: uppercase;
  color: var(--muted);
}}
/* The terminal carries the lane's top margin when it stands alone; inside a
   lane the grid gap already provides it, and both would double up. */
.lane .term {{ margin: 0; }}

/* ── the terminal: the hero ───────────────────────────────────────────── */
.term {{
  margin: 44px 0 0;
  background: var(--screen);
  border-radius: 14px;
  box-shadow: var(--shadow);
  overflow: hidden;
}}
.term-bar {{
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 13px 18px;
  border-bottom: 1px solid rgba(255,255,255,.07);
}}
.dot {{
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--screen-dim); opacity: .5;
}}
.dot.live {{ background: var(--accent); opacity: 1; }}
.term-name {{
  margin-left: 6px;
  font: 500 12px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  letter-spacing: .04em;
  color: var(--screen-dim);
}}
.term-body {{
  margin: 0;
  padding: 22px 20px 26px;
  overflow-x: auto;
  font: 400 13.5px/1.95 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: var(--screen-ink);
  white-space: pre;
  tab-size: 2;
}}
.term-body .p {{ color: var(--accent); user-select: none; }}
.term-body .c {{ color: var(--screen-dim); }}
.term-body .o {{ color: var(--screen-dim); }}
.caret {{
  display: inline-block;
  width: 8px;
  height: 1em;
  vertical-align: -2px;
  background: var(--accent);
  animation: blink 1.15s step-end infinite;
}}
@keyframes blink {{ 50% {{ opacity: 0; }} }}
@media (prefers-reduced-motion: reduce) {{ .caret {{ animation: none; }} }}

/* ── spec strip: data, so tabular ─────────────────────────────────────── */
.spec {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 1px;
  margin: 1px 0 0;
  background: var(--rule);
  border-radius: 0 0 14px 14px;
  overflow: hidden;
}}
.spec div {{ background: var(--panel); padding: 15px 18px; }}
.spec dt {{
  margin: 0 0 5px;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: .14em;
  text-transform: uppercase;
  color: var(--muted);
}}
.spec dd {{
  margin: 0;
  font: 500 14px/1.3 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}}

/* ── sections ─────────────────────────────────────────────────────────── */
section {{ padding: 60px 0 0; }}
h2 {{
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: .16em;
  text-transform: uppercase;
  color: var(--accent);
}}
.sub {{ margin: 0 0 26px; color: var(--muted); font-size: 15px; max-width: 40em; }}
p {{ margin: 0 0 16px; }}

dl.feat {{ margin: 0; display: grid; gap: 0; }}
dl.feat > div {{
  display: grid;
  grid-template-columns: 210px 1fr;
  gap: 26px;
  padding: 18px 0;
  border-top: 1px solid var(--rule);
}}
dl.feat dt {{ font-weight: 600; font-size: 15px; }}
dl.feat dd {{ margin: 0; color: var(--muted); font-size: 15px; }}
@media (max-width: 640px) {{
  dl.feat > div {{ grid-template-columns: 1fr; gap: 6px; }}
}}

.prose {{ max-width: 62ch; color: var(--muted); }}
.prose strong {{ color: var(--ink); }}

/* ── colophon: whose work this is ─────────────────────────────────────── */
.colophon {{
  margin: 64px 0 0;
  padding: 26px 28px;
  background: var(--panel);
  border: 1px solid var(--rule);
  border-left: 3px solid var(--accent);
  border-radius: 3px 12px 12px 3px;
}}
.colophon p {{ margin: 0; max-width: 60ch; color: var(--muted); font-size: 15px; }}
.colophon strong {{ color: var(--ink); }}
.colophon a {{ color: var(--accent); }}

footer {{
  margin: 72px 0 0;
  padding: 26px 0 80px;
  border-top: 1px solid var(--rule);
  color: var(--muted);
  font-size: 13.5px;
}}
footer a {{ color: var(--accent); }}
a:focus-visible, .jump a:focus-visible {{
  outline: 2px solid var(--accent);
  outline-offset: 3px;
  border-radius: 2px;
}}

/* ── index ────────────────────────────────────────────────────────────── */
.packs {{ display: grid; gap: 0; margin: 8px 0 0; }}
.packs a {{
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 18px;
  align-items: baseline;
  padding: 20px 4px;
  border-top: 1px solid var(--rule);
  text-decoration: none;
  color: inherit;
}}
.packs a:hover {{ background: var(--panel); }}
.packs .nm {{ font-size: 19px; font-weight: 600; letter-spacing: -.01em; }}
.packs .by {{ font-size: 13.5px; color: var(--muted); }}
.packs .ds {{ grid-column: 1 / -1; color: var(--muted); font-size: 14.5px; max-width: 62ch; }}
.packs .vs {{
  font: 500 12.5px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}}
"""
