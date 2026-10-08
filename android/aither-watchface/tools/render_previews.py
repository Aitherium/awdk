#!/usr/bin/env python3
"""Render previews of the Aither watch face straight from res/raw/watchface.xml.

A small interpreter for the subset of Watch Face Format the face uses (Group, PartDraw
Arc/Ellipse, PartImage, PartText Template, DigitalClock, ComplicationSlot, Condition,
ambient Variant), fed with sample data. It is a preview, not the Wear OS renderer:
fonts and positions match, anti-aliasing and text metrics are approximate.

    python render_previews.py --out DIR      every style x accent, plus ambient
    python render_previews.py --res          also refresh res/drawable-nodpi previews
"""
from __future__ import annotations

import argparse
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent.parent
RES = HERE / "res"
SS = 2  # supersampling
SIZE = 450

SAMPLE = {
    "DAY_OF_WEEK_S": "Thu", "DAY": 8, "MONTH_S": "Oct", "SECOND": 32,
    "WEATHER.IS_AVAILABLE": 1, "WEATHER.TEMPERATURE": 18,
    "WEATHER.TEMPERATURE_HIGH": 21, "WEATHER.TEMPERATURE_LOW": 12,
}
COMPLICATIONS = {  # slotId -> (type, data)
    "0": ("SHORT_TEXT", {"TEXT": "6,240", "icon": "steps"}),
    "1": ("RANGED_VALUE", {"TEXT": "78%", "RANGED_VALUE_VALUE": 78,
                           "RANGED_VALUE_MIN": 0, "RANGED_VALUE_MAX": 100}),
    "2": ("LONG_TEXT", {"TEXT": "2:30 PM  Design review"}),
}


def color(value: str, accent: str) -> tuple[int, int, int, int]:
    if value.startswith("[CONFIGURATION.accent"):
        value = accent
    v = value.lstrip("#")
    if len(v) == 6:
        v = "FF" + v
    a, r, g, b = (int(v[i:i + 2], 16) for i in range(0, 8, 2))
    return (r, g, b, a)


def expr(value: str, data: dict):
    whole = re.fullmatch(r"\[([A-Z0-9_.]+)\]", value.strip())
    if whole:
        return data[whole.group(1)]

    def sub(m):
        return str(data[m.group(1)])
    return eval(re.sub(r"\[([A-Z0-9_.]+)\]", sub, value), {})  # sample data only


class Renderer:
    def __init__(self, accent: str, ambient: bool):
        self.accent, self.ambient = accent, ambient

    def font(self, family: str, size: float) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(RES / "font" / f"{family}.ttf"), int(size * SS))

    def alpha_of(self, el: ET.Element) -> int:
        a = int(el.get("alpha", "255"))
        if self.ambient:
            for v in el.findall("Variant"):
                if v.get("mode") == "AMBIENT" and v.get("target") == "alpha":
                    a = int(v.get("value"))
        return a

    def layer(self) -> Image.Image:
        return Image.new("RGBA", (SIZE * SS, SIZE * SS), (0, 0, 0, 0))

    def composite(self, base, layer, alpha, angle=0.0, pivot=None):
        if angle:
            layer = layer.rotate(-angle, resample=Image.BICUBIC,
                                 center=tuple(p * SS for p in pivot))
        if alpha < 255:
            layer.putalpha(layer.getchannel("A").point(lambda v: v * alpha // 255))
        base.alpha_composite(layer)

    # --- elements ------------------------------------------------------------------
    def walk(self, base, el, ox, oy, data):
        for child in el:
            tag = child.tag
            if tag == "Group":
                a = self.alpha_of(child)
                if a == 0:
                    continue
                lay = self.layer()
                self.walk(lay, child, ox + float(child.get("x")), oy + float(child.get("y")), data)
                self.composite(base, lay, a)
            elif tag == "PartDraw":
                self.part_draw(base, child, ox, oy, data)
            elif tag == "PartImage":
                self.part_image(base, child, ox, oy, data)
            elif tag == "PartText":
                self.part_text(base, child, ox, oy, data)
            elif tag == "DigitalClock":
                self.clock(base, child, ox, oy)
            elif tag == "Condition":
                exprs = {e.get("name"): e.text for e in child.find("Expressions")}
                for cmp in child.findall("Compare"):
                    src = exprs[cmp.get("expression")]
                    if expr(src, data):
                        self.walk(base, cmp, ox, oy, data)
                        break
                else:
                    d = child.find("Default")
                    if d is not None:
                        self.walk(base, d, ox, oy, data)
            elif tag == "ComplicationSlot":
                self.slot(base, child, data)

    def part_draw(self, base, el, ox, oy, data):
        a = self.alpha_of(el)
        if a == 0:
            return
        x, y = ox + float(el.get("x")), oy + float(el.get("y"))
        w, h = float(el.get("width")), float(el.get("height"))
        lay = self.layer()
        d = ImageDraw.Draw(lay)
        for shape in el:
            if shape.tag not in ("Arc", "Ellipse"):
                continue
            stroke, fill = shape.find("Stroke"), shape.find("Fill")
            if shape.tag == "Ellipse":
                bx, by = x + float(shape.get("x")), y + float(shape.get("y"))
                bw, bh = float(shape.get("width")), float(shape.get("height"))
                box = [bx * SS, by * SS, (bx + bw) * SS, (by + bh) * SS]
                if fill is not None:
                    d.ellipse(box, fill=color(fill.get("color"), self.accent))
                if stroke is not None:
                    t = float(stroke.get("thickness"))
                    box = [box[0] - t * SS / 2, box[1] - t * SS / 2, box[2] + t * SS / 2, box[3] + t * SS / 2]
                    d.ellipse(box, outline=color(stroke.get("color"), self.accent), width=int(t * SS))
            else:
                cx, cy = x + float(shape.get("centerX")), y + float(shape.get("centerY"))
                aw, ah = float(shape.get("width")), float(shape.get("height"))
                start, end = float(shape.get("startAngle")), float(shape.get("endAngle"))
                for tr in shape.findall("Transform"):
                    if tr.get("target") == "endAngle":
                        end = expr(tr.get("value"), data)
                if stroke is None or end <= start:
                    continue
                t = float(stroke.get("thickness"))
                c = color(stroke.get("color"), self.accent)
                box = [(cx - aw / 2 - t / 2) * SS, (cy - ah / 2 - t / 2) * SS,
                       (cx + aw / 2 + t / 2) * SS, (cy + ah / 2 + t / 2) * SS]
                if end - start >= 360:
                    d.ellipse(box, outline=c, width=int(t * SS))
                else:
                    d.arc(box, start - 90, end - 90, fill=c, width=int(t * SS))
                    if stroke.get("cap") == "ROUND":
                        for ang in (start, end):
                            r = math.radians(ang - 90)
                            px, py = cx + aw / 2 * math.cos(r), cy + ah / 2 * math.sin(r)
                            d.ellipse([(px - t / 2) * SS, (py - t / 2) * SS,
                                       (px + t / 2) * SS, (py + t / 2) * SS], fill=c)
        angle = float(el.get("angle", "0"))
        self.composite(base, lay, a, angle, (x + w / 2, y + h / 2))

    def part_image(self, base, el, ox, oy, data):
        a = self.alpha_of(el)
        if a == 0:
            return
        x, y = ox + float(el.get("x")), oy + float(el.get("y"))
        w, h = float(el.get("width")), float(el.get("height"))
        res = el.find("Image").get("resource")
        lay = self.layer()
        if res.startswith("[COMPLICATION"):
            icon = data.get("icon")
            if not icon:
                return
            c = color(el.get("tintColor", "#FFFFFFFF"), self.accent)
            d = ImageDraw.Draw(lay)
            # a stand-in step icon: two footprints
            for fx, fy in ((0.28, 0.2), (0.58, 0.45)):
                d.ellipse([(x + w * fx) * SS, (y + h * fy) * SS,
                           (x + w * (fx + 0.22)) * SS, (y + h * (fy + 0.42)) * SS], fill=c)
        else:
            img = Image.open(RES / "drawable-nodpi" / f"{res}.png").convert("RGBA")
            lay.paste(img.resize((int(w * SS), int(h * SS)), Image.LANCZOS), (int(x * SS), int(y * SS)))
        self.composite(base, lay, a)

    def draw_text(self, base, text, family, size, col, box, spacing=0.0, alpha=255):
        x, y, w, h = box
        f = self.font(family, size)
        lay = self.layer()
        d = ImageDraw.Draw(lay)
        track = spacing * size * SS
        widths = [d.textlength(ch, font=f) for ch in text]
        total = sum(widths) + track * max(len(text) - 1, 0)
        asc, desc = f.getmetrics()
        cx = x * SS + (w * SS - total) / 2
        top = y * SS + (h * SS - (asc + desc)) / 2
        for ch, cw in zip(text, widths):
            d.text((cx, top), ch, font=f, fill=col)
            cx += cw + track
        self.composite(base, lay, alpha)

    def part_text(self, base, el, ox, oy, data):
        a = self.alpha_of(el)
        if a == 0:
            return
        x, y = ox + float(el.get("x")), oy + float(el.get("y"))
        w, h = float(el.get("width")), float(el.get("height"))
        font = el.find("Text/Font")
        tmpl = font.find(".//Template")
        if tmpl is not None:
            params = [expr(p.get("expression"), data) for p in tmpl.findall("Parameter")]
            text = (tmpl.text or "") % tuple(params)
        else:
            text = font.text or ""
        if font.find("Upper") is not None:
            text = text.upper()
        self.draw_text(base, text, font.get("family"), float(font.get("size")),
                       color(font.get("color", "#FFFFFFFF"), self.accent), (x, y, w, h),
                       float(font.get("letterSpacing", "0")), a)

    def clock(self, base, el, ox, oy):
        x, y = ox + float(el.get("x")), oy + float(el.get("y"))
        tt = el.find("TimeText")
        f = tt.find("Font")
        self.draw_text(base, "10:08", f.get("family"), float(f.get("size")),
                       color(f.get("color", "#FFFFFFFF"), self.accent),
                       (x, y, float(tt.get("width")), float(tt.get("height"))), 0, self.alpha_of(el))

    def slot(self, base, el, data):
        a = self.alpha_of(el)
        if a == 0 or el.get("slotId") not in COMPLICATIONS:
            return
        ctype, cdata = COMPLICATIONS[el.get("slotId")]
        comp = next(c for c in el.findall("Complication") if c.get("type") == ctype)
        lay = self.layer()
        merged = dict(data)
        merged.update({f"COMPLICATION.{k}": v for k, v in cdata.items()})
        merged.update(cdata)
        self.walk(lay, comp, float(el.get("x")), float(el.get("y")), merged)
        self.composite(base, lay, a)


def render(style: str, accent: str, ambient: bool) -> Image.Image:
    root = ET.parse(RES / "raw" / "watchface.xml").getroot()
    scene = root.find("Scene")
    r = Renderer(accent, ambient)
    base = Image.new("RGBA", (SIZE * SS, SIZE * SS), color(scene.get("backgroundColor"), accent))
    for child in scene:
        if child.tag == "ListConfiguration":
            opt = next(o for o in child if o.get("id") == style)
            r.walk(base, opt, 0, 0, SAMPLE)
        else:
            holder = ET.Element("holder")
            holder.append(child)
            r.walk(base, holder, 0, 0, SAMPLE)
    img = base.resize((SIZE, SIZE), Image.LANCZOS)
    mask = Image.new("L", (SIZE * 4, SIZE * 4), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, SIZE * 4 - 1, SIZE * 4 - 1], fill=255)
    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask.resize((SIZE, SIZE), Image.LANCZOS)))
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "out" / "previews"))
    ap.add_argument("--res", action="store_true", help="refresh res/drawable-nodpi previews")
    a = ap.parse_args()
    root = ET.parse(RES / "raw" / "watchface.xml").getroot()
    styles = [o.get("id") for o in root.find("UserConfigurations/ListConfiguration")]
    accents = {o.get("id"): o.get("colors")
               for o in root.find("UserConfigurations/ColorConfiguration")}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tiles = []
    for s in styles:
        for acc_id, acc in accents.items():
            img = render(s, acc, False)
            img.save(out / f"{s}-{acc_id}.png")
            tiles.append(img)
        amb = render(s, next(iter(accents.values())), True)
        amb.save(out / f"{s}-ambient.png")
        tiles.append(amb)
        if a.res:
            render(s, next(iter(accents.values())), False).resize((225, 225), Image.LANCZOS) \
                .save(RES / "drawable-nodpi" / f"style_{s}.png", optimize=True)
    if a.res:
        render(styles[0], next(iter(accents.values())), False) \
            .save(RES / "drawable-nodpi" / "preview.png", optimize=True)
    cols = len(accents) + 1
    sheet = Image.new("RGBA", (cols * 230, len(styles) * 230), (40, 40, 48, 255))
    for i, t in enumerate(tiles):
        sheet.alpha_composite(t.resize((225, 225), Image.LANCZOS), ((i % cols) * 230 + 2, (i // cols) * 230 + 2))
    sheet.save(out / "contact-sheet.png")
    print(f"{len(tiles)} previews + contact sheet -> {out}")


if __name__ == "__main__":
    main()
