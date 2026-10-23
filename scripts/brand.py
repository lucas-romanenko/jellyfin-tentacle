#!/usr/bin/env python3
"""Tentacle's brand assets, drawn from one description: the jellyfish mark
(purple dome, glowing multicolour network of tentacles) and the thin,
wide-spaced TENTACLE wordmark, as on the Android TV app's banner
(jellyfin-tentacle-androidtv res/mipmap-xxxhdpi/tentacle_ic_banner.png).

    scripts/brand.py          write the SVGs into tentacle/static/brand/ (and the TV
                              app's one-colour vector, android/ic_tentacle.xml) and
                              copy them to docs/assets/brand/ (the docs site's copy)
    scripts/brand.py --png    also render the PNGs (favicons, app icons, banners,
                              social preview); needs Playwright's Chromium:
                              uv run --with playwright python scripts/brand.py --png

tentacle/static/brand/ is the one source: the dashboard serves it, the plugin
embeds it (Jellyfin.Plugin.Tentacle.csproj links the files), the README points
at it, the docs site gets a copy. tests/test_brand.py fails when a file there
differs from what this script draws, or a copy differs from the source.

Coordinates are those of the 1280x720 TV banner, so the mark and wordmark sit
exactly where they sit there.
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tentacle" / "static" / "brand"
DOCS = ROOT / "docs" / "assets" / "brand"

# ── Colours (also in tokens.css) ──
NAVY = "#181b3c"        # banner background
INK = "#0f0d1a"         # the TV app's and the dashboard's page background
DOME_TOP = "#c39bf0"
DOME = "#8a64e4"
DOME_DEEP = "#6f4bd8"
RIM = "#4a3191"
RIM_LIGHT = "#714dc2"
LAUNCHER = "#402b80"    # app icon background (TV launcher)
ACCENT = "#8b80ff"      # the TV app's accent

# The network: knots (glowing nodes) and where the tentacles end (balls).
NODES = {
    "a": (278, 343, "#e64ce0"), "b": (380, 343, "#f3a8dc"),
    "c": (303, 368, "#f2507f"), "d": (355, 368, "#3fe0e8"),
    "e": (243, 387, "#4fb0f5"), "f": (329, 391, "#e83fd0"),
    "g": (415, 387, "#45d5e8"), "h": (282, 410, "#9ad84a"),
    "i": (376, 410, "#f0566a"), "j": (329, 438, "#f2c23c"),
}
EDGES = ["ae", "ac", "ah", "ce", "ch", "cf", "eh", "hf", "hj", "fj", "fd", "dg", "di", "bd", "bg", "bi", "gi", "ij"]
STRINGS = [(278, "a"), (304, "c"), (329, "f"), (354, "d"), (380, "b")]  # from the dome's rim down to a knot
BALLS = [  # (knot, path of the hanging tentacle, centre, colour light, colour dark)
    ("e", "M243,387 C236,402 238,420 238,436", (238, 448), "#5a8fe0", "#3a6ab8"),
    ("h", "M282,410 C280,430 279,450 279,469", (279, 481), "#5cc27e", "#3b955c"),
    ("j", "M329,438 L329,494", (329, 506), "#f6d25a", "#d39e2e"),
    ("i", "M376,410 C378,430 379,450 379,469", (379, 481), "#e8578f", "#b23a6c"),
    ("g", "M415,387 C422,402 421,420 421,436", (421, 448), "#5fd2dc", "#3aa5b2"),
]
DOME_PATH = "M241,276 C241,222 281,186 329,186 C377,186 417,222 417,276 C417,292 380,303 329,303 C278,303 241,292 241,276 Z"
RIM_ELLIPSE = (329, 296, 61, 10)
MARK_BOX = (222, 182, 214, 338)  # x, y, w, h

# The wordmark: monoline capitals, 83 high, strokes 7 wide, as on the banner.
WORD_X, WORD_Y, CAP, STROKE = 490, 318, 83, 6.5
_h = STROKE / 2
LETTERS = [  # path data in a box whose top-left is (0, 0), x offsets from WORD_X; round ends and joins
    (0, f"M{_h},{_h} H{61 - _h} M30.5,{_h} V{CAP - _h}"),                                  # T
    (78, f"M{48 - _h},{_h} H{_h} V{CAP - _h} H{48 - _h} M{_h},41.5 H{44 - _h}"),           # E
    (144, f"M{_h},{CAP - _h} V{_h} L{62 - _h},{CAP - _h} V{_h}"),                          # N
    (221, f"M{_h},{_h} H{62 - _h} M31,{_h} V{CAP - _h}"),                                  # T
    (285, f"M{_h + 1},{CAP - _h} L34,{_h} L{66 - _h},{CAP - _h} M13.6,56 H54.4"),          # A
    (362, "M63.8,12.6 A38,38 0 1 0 63.8,70.4"),                                            # C
    (444, f"M{_h},{_h} V{CAP - _h} H{48 - _h}"),                                           # L
    (506, f"M{49 - _h},{_h} H{_h} V{CAP - _h} H{49 - _h} M{_h},41.5 H{45 - _h}"),          # E
]
WORD_W = 555


def _glow_defs(prefix):
    out = []
    for k, (_, _, col) in NODES.items():
        out.append(f'<radialGradient id="{prefix}g{k}"><stop offset="0" stop-color="{col}" stop-opacity=".75"/>'
                   f'<stop offset="1" stop-color="{col}" stop-opacity="0"/></radialGradient>')
    for a, b in EDGES:
        (x1, y1, c1), (x2, y2, c2) = NODES[a], NODES[b]
        out.append(f'<linearGradient id="{prefix}e{a}{b}" gradientUnits="userSpaceOnUse" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">'
                   f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient>')
    for k, _, (cx, cy), light, dark in BALLS:
        out.append(f'<linearGradient id="{prefix}b{k}" x1="0" y1="0" x2="1" y2="1">'
                   f'<stop offset="0" stop-color="{light}"/><stop offset="1" stop-color="{dark}"/></linearGradient>')
    return out


def mark(prefix="t", small=False):
    """The jellyfish, in banner coordinates. small: thicker lines and bigger knots for 16-64 px."""
    line, knot, glow, ball = (6, 8, 0, 15) if small else (2.4, 4.2, 11, 12)
    d = [f'<linearGradient id="{prefix}dome" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{DOME_TOP}"/>'
         f'<stop offset=".55" stop-color="{DOME}"/><stop offset="1" stop-color="{DOME_DEEP}"/></linearGradient>',
         f'<linearGradient id="{prefix}rim" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{RIM}"/>'
         f'<stop offset="1" stop-color="{RIM_LIGHT}"/></linearGradient>',
         f'<linearGradient id="{prefix}str" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#b9a4f0" stop-opacity=".9"/>'
         f'<stop offset="1" stop-color="#d6c6ff"/></linearGradient>'] + _glow_defs(prefix)
    g = [f'<path d="{DOME_PATH}" fill="url(#{prefix}dome)"/>']
    cx, cy, rx, ry = RIM_ELLIPSE
    g.append(f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" fill="url(#{prefix}rim)"/>')
    g.append(f'<g fill="none" stroke-linecap="round" stroke-width="{line}">')
    for x, k in STRINGS:
        nx, ny, col = NODES[k]
        g.append(f'<path d="M{x},{cy} L{nx},{ny}" stroke="{col}" stroke-opacity=".6"/>')
    for a, b in EDGES:
        (x1, y1, _), (x2, y2, _) = NODES[a], NODES[b]
        g.append(f'<path d="M{x1},{y1} L{x2},{y2}" stroke="url(#{prefix}e{a}{b})" stroke-opacity=".75"/>')
    for k, path, _, light, _ in BALLS:
        g.append(f'<path d="{path}" stroke="{light}"/>')
    g.append('</g>')
    for k, _, (bx, by), _, _ in BALLS:
        g.append(f'<circle cx="{bx}" cy="{by}" r="{ball}" fill="url(#{prefix}b{k})"/>')
    for k, (x, y, col) in NODES.items():
        if glow:
            g.append(f'<circle cx="{x}" cy="{y}" r="{glow}" fill="url(#{prefix}g{k})"/>')
        g.append(f'<circle cx="{x}" cy="{y}" r="{knot}" fill="{col}"/>')
        if not small:
            g.append(f'<circle cx="{x}" cy="{y}" r="{knot * .5:.1f}" fill="#fff" fill-opacity=".8"/>')
    return d, g


def mark_mono(color="#fff", prefix="m", small=False):
    """One colour: the dome with its rim cut out, the network in lines and dots."""
    line, knot, ball = (6, 8, 15) if small else (2.6, 4.5, 12)
    cx, cy, rx, ry = RIM_ELLIPSE
    x, y, w, h = MARK_BOX
    d = [f'<mask id="{prefix}cut"><rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#fff"/>'
         f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" fill="#000"/></mask>']
    g = [f'<g fill="{color}" stroke="{color}">',
         f'<path d="{DOME_PATH}" stroke="none" mask="url(#{prefix}cut)"/>',
         f'<g fill="none" stroke-linecap="round" stroke-width="{line}">']
    for sx, k in STRINGS:
        nx, ny, _ = NODES[k]
        g.append(f'<path d="M{sx},{cy + ry - 2} L{nx},{ny}"/>')
    for a, b in EDGES:
        (x1, y1, _), (x2, y2, _) = NODES[a], NODES[b]
        g.append(f'<path d="M{x1},{y1} L{x2},{y2}"/>')
    for _, path, _, _, _ in BALLS:
        g.append(f'<path d="{path}"/>')
    g.append('</g><g stroke="none">')
    g += [f'<circle cx="{bx}" cy="{by}" r="{ball}"/>' for _, _, (bx, by), _, _ in BALLS]
    g += [f'<circle cx="{nx}" cy="{ny}" r="{knot}"/>' for nx, ny, _ in NODES.values()]
    g.append('</g></g>')
    return d, g


def wordmark(color="#fff", x=WORD_X, y=WORD_Y, scale=1.0):
    t = f'translate({x:g},{y:g})' + (f' scale({scale:g})' if scale != 1 else '')
    g = [f'<g transform="{t}" fill="none" stroke="{color}" stroke-width="{STROKE}" stroke-linecap="round" stroke-linejoin="round">']
    g += [f'<path transform="translate({dx},0)" d="{p}"/>' for dx, p in LETTERS]
    g.append('</g>')
    return g


def svg(box, defs, body, title, bg=None):
    x, y, w, h = box
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x:g} {y:g} {w:g} {h:g}" '
            f'width="{w:g}" height="{h:g}" role="img" aria-label="{title}">')
    parts = [head, f'<title>{title}</title>']
    if defs:
        parts.append('<defs>' + ''.join(defs) + '</defs>')
    if bg:
        parts.append(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" fill="{bg}"/>')
    parts += body
    parts.append('</svg>')
    return '\n'.join(parts) + '\n'


def lockup_box():
    x, y, w, h = MARK_BOX
    return (x, y, WORD_X + WORD_W + 6 - x, h)


STACK_SCALE = 0.5  # the wordmark under the mark: 278 wide, 41.5 high


def stacked(word_color, mono=None):
    x, y, w, h = MARK_BOX
    ww = WORD_W * STACK_SCALE
    width = max(w, ww) + 16
    left = x + w / 2 - width / 2
    word_y = y + h + 34
    box = (left, y, width, h + 34 + CAP * STACK_SCALE + 8)
    d, g = mark_mono(mono) if mono else mark()
    return box, d, g + wordmark(word_color, x + w / 2 - ww / 2, word_y, STACK_SCALE)


def files():
    """Every SVG in the brand folder: name -> text."""
    out = {}
    d, g = mark()
    out["mark.svg"] = svg(MARK_BOX, d, g, "Tentacle")
    d, g = mark("s", small=True)
    out["mark-small.svg"] = svg(MARK_BOX, d, g, "Tentacle")
    d, g = mark_mono("#fff")
    out["mark-white.svg"] = svg(MARK_BOX, d, g, "Tentacle")
    d, g = mark_mono("#fff", small=True)
    out["mark-white-small.svg"] = svg(MARK_BOX, d, g, "Tentacle")
    word_box = (WORD_X - 4, WORD_Y - 4, WORD_W + 8, CAP + 8)
    out["wordmark.svg"] = svg(word_box, [], wordmark("#fff"), "Tentacle")
    out["wordmark-navy.svg"] = svg(word_box, [], wordmark(NAVY), "Tentacle")
    d, g = mark()
    out["lockup.svg"] = svg(lockup_box(), d, g + wordmark("#fff"), "Tentacle")
    out["lockup-navy.svg"] = svg(lockup_box(), d, g + wordmark(NAVY), "Tentacle")
    d, g = mark_mono("#fff")
    out["lockup-white.svg"] = svg(lockup_box(), d, g + wordmark("#fff"), "Tentacle")
    box, d, g = stacked("#fff")
    out["lockup-stacked.svg"] = svg(box, d, g, "Tentacle")
    box, d, g = stacked("#fff", mono="#fff")
    out["lockup-stacked-white.svg"] = svg(box, d, g, "Tentacle")
    # The app icon: the small mark on the launcher purple, square, safe zone kept (maskable)
    d, g = mark("i", small=True)
    x, y, w, h = MARK_BOX
    side = h / 0.7
    box = (x + w / 2 - side / 2, y + h / 2 - side / 2, side, side)
    out["app-icon.svg"] = svg(box, d, g, "Tentacle", bg=LAUNCHER)
    # The TV banner, as on the TV: 1280x720 navy with the lockup
    d, g = mark("n")
    out["banner.svg"] = svg((0, 0, 1280, 720), d, g + wordmark("#fff"), "Tentacle", bg=NAVY)
    # Social preview (GitHub, links): 1280x640, the lockup a little higher and a line under it
    d, g = mark("p")
    body = ['<g transform="translate(0,-60)">'] + g + wordmark("#fff") + ['</g>',
            '<text x="640" y="560" text-anchor="middle" fill="#b9b5d6" font-family="Inter, DejaVu Sans, Arial, sans-serif" '
            'font-size="30" letter-spacing="1">Live TV, streaming providers and your own media, in one Jellyfin</text>']
    out["social-preview.svg"] = svg((0, 0, 1280, 640), d, body, "Tentacle", bg=NAVY)
    return out


def android_mono():
    """The white mark as an Android vector drawable (no masks there: the rim is cut from the
    dome by even-odd filling), for the TV app's notification and settings icons."""
    x, y, w, h = MARK_BOX
    cx, cy, rx, ry = RIM_ELLIPSE
    rim = f"M{cx - rx},{cy} a{rx},{ry} 0 1,0 {2 * rx},0 a{rx},{ry} 0 1,0 {-2 * rx},0 Z"
    lines = [f"M{sx},{cy + ry - 2} L{NODES[k][0]},{NODES[k][1]}" for sx, k in STRINGS]
    lines += [f"M{NODES[a][0]},{NODES[a][1]} L{NODES[b][0]},{NODES[b][1]}" for a, b in EDGES]
    lines += [path for _, path, _, _, _ in BALLS]
    dots = [(bx, by, 15) for _, _, (bx, by), _, _ in BALLS] + [(nx, ny, 8) for nx, ny, _ in NODES.values()]
    dot = " ".join(f"M{px - r},{py} a{r},{r} 0 1,0 {2 * r},0 a{r},{r} 0 1,0 {-2 * r},0 Z" for px, py, r in dots)
    side = h + 8
    tx = side / 2 - (x + w / 2)
    ty = 4 - y
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!-- Tentacle's jellyfish, one colour (drawn by jellyfin-tentacle scripts/brand.py) -->
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="24dp"
    android:height="24dp"
    android:viewportWidth="{side:g}"
    android:viewportHeight="{side:g}">
    <group android:translateX="{tx:g}" android:translateY="{ty:g}">
        <path android:fillColor="#FFFFFFFF" android:fillType="evenOdd" android:pathData="{DOME_PATH} {rim}" />
        <path android:strokeColor="#FFFFFFFF" android:strokeWidth="6" android:strokeLineCap="round" android:pathData="{' '.join(lines)}" />
        <path android:fillColor="#FFFFFFFF" android:pathData="{dot}" />
    </group>
</vector>
"""


# Rendered from the SVGs above: (svg, png path relative to the repo, width, height)
PNGS = [
    ("app-icon.svg", "tentacle/static/apple-touch-icon.png", 180, 180),
    ("app-icon.svg", "tentacle/static/web-app-manifest-192x192.png", 192, 192),
    ("app-icon.svg", "tentacle/static/web-app-manifest-512x512.png", 512, 512),
    ("mark-small.svg", "tentacle/static/favicon-96x96.png", 96, 96),
    ("mark-small.svg", "tentacle/static/brand/favicon-32.png", 32, 32),
    ("lockup.svg", "tentacle/static/brand/lockup.png", 1660, 676),
    ("banner.svg", "docs/images/banner.png", 1280, 720),
    ("social-preview.svg", "docs/images/social-preview.png", 1280, 640),
    ("lockup.svg", "tentacle-plugin/Inject/tentacle-logo.png", 1660, 676),
]


def write_svgs():
    OUT.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    for name, text in files().items():
        (OUT / name).write_text(text)
    (OUT / "android").mkdir(exist_ok=True)
    (OUT / "android" / "ic_tentacle.xml").write_text(android_mono())
    for f in sorted(OUT.iterdir()):
        if f.suffix in (".svg", ".css"):
            shutil.copyfile(f, DOCS / f.name)


def render_pngs():
    import asyncio
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            b = await p.chromium.launch()
            for name, rel, w, h in PNGS:
                pg = await b.new_page(viewport={"width": w, "height": h})
                src = (OUT / name).read_text().replace("<svg ", '<svg preserveAspectRatio="xMidYMid meet" ', 1)
                await pg.set_content(f'<html><body style="margin:0;background:transparent">'
                                     f'<div style="width:{w}px;height:{h}px">{src.replace(chr(10), "")}</div>'
                                     f'<style>svg{{width:{w}px;height:{h}px;display:block}}</style></body></html>')
                await pg.screenshot(path=str(ROOT / rel), omit_background=True)
                await pg.close()
            await b.close()
    asyncio.run(run())
    # favicon.ico: 16, 32, 48 from the 96 px PNG's source
    from PIL import Image
    Image.open(ROOT / "tentacle/static/favicon-96x96.png").save(
        ROOT / "tentacle/static/favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])


if __name__ == "__main__":
    write_svgs()
    if "--png" in sys.argv[1:]:
        render_pngs()
