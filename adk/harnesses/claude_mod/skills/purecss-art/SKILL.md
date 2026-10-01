---
name: purecss-art
allowed-tools: Read, Write, Edit, Bash
description: Generate realistic illustrations hand-coded entirely in HTML + CSS (the Diana Smith / "cyanharlow" technique — absolute-positioned divs, gradients, layered box-shadow, clip-path, transforms; no images, no SVG, no scripts). Brief → self-contained index.html + style.css → headless-Chromium render → vision-critique refine loop. The live HTML/CSS source is the deliverable; a PNG preview is rendered alongside. Load before bundling any exemplar artwork as a few-shot or training source — most of the well-known pieces are NOT openly licensed.
argument-hint: "[what to draw] [optional style hint]"

---

# PureCSS art → live HTML/CSS + PNG preview

Turns a description into a **single self-contained `index.html`** (with `style.css`) drawn
entirely from styled `<div>`s — gradients for shading, layered `box-shadow` for volume,
`border-radius`/`clip-path` for form, `transform` for perspective. The artwork is
resolution-independent and animatable because the art *is* the code.

This complements diffusion image generation rather than replacing it: diffusion for
photographic images, **PureCSS for hand-coded vector-like art** you can diff, animate and
restyle with a variable change.

## Inputs

- **description** — what to draw (e.g. "a renaissance portrait of a woman in warm light").
- **style** — optional medium/style hint (e.g. "oil painting", "art deco"). Default: none.
- **refine_rounds** — max corrective re-generations after the first attempt (default 2).
  `0` = single-shot.

## The loop

1. **Generate.** Prompt the model for a complete `index.html` + `style.css`, with the purity
   constraint stated up front (see below) — it is far cheaper to constrain generation than to
   repair a violation afterwards.
2. **Validate purity** before rendering. A violation list is more useful than a render.
3. **Render** headless (Chromium/Playwright) to PNG at a fixed viewport.
4. **Critique** the render against the brief with a vision model, and if it falls short and
   rounds remain, re-prompt with *both* the purity violations and the visual feedback.
   Feeding back only the text brief tends to reproduce the same mistake.
5. **Deliver.** The primary deliverable is the live `index.html` (+ `style.css`) — it opens in
   any browser and scales infinitely. The PNG is a flat preview for chat and thumbnails.

## Guardrails

### Licensing — read this before bundling exemplars or training

This is the part that actually bites, because the best-known examples of the technique are
not uniformly licensed:

- **The technique itself is free.** Methods and ideas are not copyrightable — generating CSS
  art "in the style of" hand-coded illustration is fine.
- **`cyanharlow/purecss-character` is MIT** — free to use, derive from, and train on, keeping
  attribution.
- **Diana Smith's other artworks** (francine / gaze / lace / pinup / pink) have **no open
  licence.** Forking a repository does **not** grant rights; the upstream licence governs.
  Using these as bundled few-shot exemplars **or** as training data requires her explicit
  permission.
- **Your own accepted outputs are yours** — journal them with an explicit
  `license: self-generated` tag so a later training run can tell them apart from ingested
  third-party work. Do that at write time; reconstructing provenance afterwards is guesswork.

Gate this in config rather than in prose: keep a sources file where each entry carries a
licence and a `rights_confirmed` flag, and have the ingester refuse any non-permissive source
until the flag is set. A licensing rule that lives only in a document is a suggestion.

### Output integrity

- **Purity is enforced, not requested.** Reject `<img>`, `<script>`, `<svg>`, `<canvas>`,
  media and `<link>` tags, non-`data:` `url()`, `@import`, external `src=`, and remote
  `@font-face`. Never ship an artwork with violations as "PureCSS" — refine until clean.
- **Self-contained.** The `index.html` must render with no network: no CDNs, no web fonts, no
  external images. System font stacks only. This is what makes the deliverable durable.
- **Render cost is per round.** Each refine round is an extra model call plus a headless
  render (budget ~30s). Use `refine_rounds=0` for quick drafts and raise it for hero pieces.
