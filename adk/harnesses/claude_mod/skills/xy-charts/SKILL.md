---
name: xy-charts
allowed-tools: Read, Write, Bash
description: Render data charts (line/scatter/bar/heatmap/pie/histogram/density) to PNG/SVG/JPEG/WebP/PDF or self-contained interactive HTML via the xy engine — for metrics, benchmarks, report figures, dashboard panels, and any "turn these numbers into a picture" ask. Use INSTEAD of writing matplotlib code, ASCII tables, or reaching for mermaid/diffusion when the subject is data. Covers direct library use in scripts and the xy_render_chart / xy_chart_spec_help tools where they are available.
argument-hint: [what to chart, or a path to the data]

---

# XY Charts — data → picture

`xy` (reflex-dev, Apache-2.0, PyPI `xy`, pinned 0.0.5) is a data-charting engine with a
Rust core and native browser-free export, scaling from 5 points to 100M-point density
surfaces.

## Which tool for which job

| Ask | Tool |
|---|---|
| Metrics, benchmarks, distributions, comparisons, report figures | **this pack** (`xy_render_chart`) |
| Architecture / flow / sequence diagrams | a mermaid renderer |
| Character/figure art, images from prompts | image generation |
| Interactive chart to hand someone, no server | `xy_render_chart` with `format="html"` |

## Tool usage (preferred where available)

1. `xy_chart_spec_help()` — returns kinds, per-series options, worked examples.
2. `xy_render_chart(spec_json, format="png", width=1280, height=720)` — returns
   `{path, format, size_bytes, cached}`. Output is content-hash cached, so the same
   spec returns instantly.

Minimal spec:

```json
{"kind": "line", "title": "Revenue",
 "series": [{"x": [1,2,3], "y": [10,25,18], "label": "web"}],
 "x_label": "day", "y_label": "$", "legend": true}
```

Kinds: `line scatter bar area step stem histogram heatmap hexbin pie ecdf`.
Large scatter: add `"density": true` to the series (GPU-style density surface).
Theme: `"theme": {"background": "#0b0b0e", "text_color": "#e6e6e1", ...}`.

## Direct library use (scripts, notebooks, media pipelines)

```python
import xy
chart = xy.scatter_chart(
    xy.scatter(xs, ys, color=vals, colormap="magma_r", density=True),
    xy.x_axis(label="x"), xy.legend(),
    title="100M points",
)
chart.to_png("out.png")      # also to_svg / to_html / write_images for jpeg/webp/pdf
```

matplotlib-style code ports with one import swap: `import xy.pyplot as plt`
(subset — see the upstream compat guide before assuming a pyplot API exists).

## Traps (measured, not theoretical)

- **Windows hosts: App Control blocks xy's unsigned native DLL** (WinError 4551,
  measured 2026-08-01). `import xy` succeeds (lazy load) and the first chart build
  dies. Run xy in a Linux container; host-side Windows use needs a policy exemption
  first.
- **Python ≥3.11 floor.** Older venvs can't install it.
- **SVG is not fully vector** for density/heatmap layers (raster tiles embedded,
  upstream limitation). Use PNG at 2x scale for print if that matters.
- **Alpha upstream.** Pin `xy==0.0.5`; bumping the pin means re-running your tool
  module's container test.
- The chart tool takes a JSON spec, **never Python source** — that's deliberate
  (a code-string surface is arbitrary execution in the tool host). Full pyplot
  freedom belongs in a script run through your normal script-execution path.
