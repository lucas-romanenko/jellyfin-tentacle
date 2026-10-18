"""The media bar's title/overview box sits just above the home rows on every screen size.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The media bar is a fixed full-width layer; the home rows scroll over its lower
part (they start at #tentacle-home's margin-top). Its text box is placed with
`bottom:` inside the bar, so it must be the part the rows cover (bar height
minus the rows' margin-top) plus a gap. On desktop that holds (100dvh bar,
rows at 85dvh, bottom 15dvh + 40px). The tablet (<= 1024 px) and phone
(<= 768 px) rules used 40dvh and 50dvh instead of 10dvh: the box landed at
the top of the screen, under the navbar and partly above the screen's edge
(measured in Chromium: at 390x844 the box spanned y = -34..64 under a navbar
at 4..74; at 768x1024 -16..82; at 1024x768 93..200 under a navbar ending at 94).
"""
import re
import unittest
from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject" / "tentacle-mediabar.css"


def _blocks(css: str):
    """(media query or '', body) for the top level and each @media block."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, top, i = [], [], 0
    for m in re.finditer(r"@media([^{]+)\{", css):
        if m.start() < i:
            continue
        top.append(css[i:m.start()])
        depth, j = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        out.append((m.group(1).strip(), css[m.end():j - 1]))
        i = j
    top.append(css[i:])
    return [("", "".join(top))] + out


def _prop(body: str, selector: str, prop: str):
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", body):
        sels = [s.strip() for s in m.group(1).split(",")]
        if selector in sels:
            p = re.search(r"(?:^|;)\s*%s\s*:\s*([^;]+)" % re.escape(prop), m.group(2))
            if p:
                return p.group(1).strip()
    return None


def _dvh(v: str) -> float:
    return float(re.search(r"([\d.]+)dvh", v).group(1))


class TextBoxAboveRows(unittest.TestCase):
    def test_every_size_puts_the_text_box_above_the_rows(self):
        blocks = _blocks(CSS.read_text(encoding="utf-8"))
        base = blocks[0][1]
        height = _prop(base, ".moonfin-mediabar", "height")
        rows = _prop(base, "body.moonfin-mediabar-active #tentacle-home", "margin-top")
        bottom = _prop(base, ".moonfin-mediabar-content", "bottom")
        checked = []
        for media, body in blocks:
            h = _prop(body, ".moonfin-mediabar", "height") or height
            r = _prop(body, "body.moonfin-mediabar-active #tentacle-home", "margin-top") or rows
            b = _prop(body, ".moonfin-mediabar-content", "bottom")
            if b is None and media:
                continue
            b = b or bottom
            covered = _dvh(h) - _dvh(r)
            self.assertAlmostEqual(covered, _dvh(b), msg="%s: text box bottom %s, but the rows cover the bar's last %sdvh"
                                   % (media or "default", b, covered))
            self.assertRegex(b, r"\+\s*\d+px", "%s: no gap above the rows" % (media or "default"))
            checked.append(media or "default")
        self.assertGreaterEqual(len(checked), 3, checked)


if __name__ == "__main__":
    unittest.main()
