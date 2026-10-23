"""Tentacle's brand comes from one place.

scripts/brand.py draws the jellyfish, the wordmark and the lockups; its SVGs
live in static/brand/, which the dashboard serves and the plugin embeds
(Jellyfin.Plugin.Tentacle.csproj links them); the docs site gets a copy.
These tests fail when a file there was edited by hand (or the script changed
without re-running it), when the docs' copy drifts, when a page points at a
brand file that doesn't exist, or when the colours lose their contrast.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BRAND = ROOT / "tentacle" / "static" / "brand"


def _brand_script():
    spec = importlib.util.spec_from_file_location("brand_script", ROOT / "scripts" / "brand.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _luminance(hex_):
    c = [int(hex_.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class BrandFilesTest(unittest.TestCase):
    def test_svgs_are_what_the_script_draws(self):
        brand = _brand_script()
        for name, text in brand.files().items():
            with self.subTest(name=name):
                self.assertEqual((BRAND / name).read_text(), text, f"run scripts/brand.py: {name} differs")
        self.assertEqual((BRAND / "android" / "ic_tentacle.xml").read_text(), brand.android_mono())

    def test_no_stray_svgs(self):
        drawn = set(_brand_script().files())
        on_disk = {f.name for f in BRAND.glob("*.svg")}
        self.assertEqual(on_disk, drawn)

    def test_docs_copy_matches(self):
        docs = ROOT / "docs" / "assets" / "brand"
        for f in sorted(BRAND.iterdir()):
            if f.suffix in (".svg", ".css"):
                with self.subTest(name=f.name):
                    self.assertEqual((docs / f.name).read_bytes(), f.read_bytes(), "run scripts/brand.py")

    def test_plugin_embeds_the_brand_folder(self):
        csproj = (ROOT / "tentacle-plugin" / "Jellyfin.Plugin.Tentacle.csproj").read_text()
        self.assertIn('Include="../tentacle/static/brand/*.svg"', csproj)
        self.assertIn("Jellyfin.Plugin.Tentacle.Assets.brand-%(Filename)%(Extension)", csproj)

    def test_every_brand_url_exists(self):
        pages = [ROOT / "tentacle" / "static" / "index.html", ROOT / "tentacle" / "static" / "sw.js",
                 *sorted((ROOT / "tentacle" / "static" / "js").glob("*.js"))]
        for p in pages:
            for name in re.findall(r"/static/brand/([\w.-]+)", p.read_text()):
                with self.subTest(page=p.name, name=name):
                    self.assertTrue((BRAND / name).exists(), name)
        inject = ROOT / "tentacle-plugin" / "Inject"
        for p in sorted(inject.glob("tentacle-*.*")):
            if p.suffix not in (".js", ".css"):
                continue
            for name in re.findall(r"/Tentacle/Assets/brand-([\w.-]+)", p.read_text()):
                with self.subTest(page=p.name, name=name):
                    self.assertTrue((BRAND / name).exists(), name)

    def test_pngs_exist(self):
        for _, rel, w, h in _brand_script().PNGS:
            with self.subTest(png=rel):
                self.assertTrue((ROOT / rel).exists(), f"run scripts/brand.py --png: {rel}")


class BrandColoursTest(unittest.TestCase):
    """WCAG AA (4.5:1 for text) for the pairs the pages use."""

    @classmethod
    def setUpClass(cls):
        css = (BRAND / "tokens.css").read_text()
        cls.t = dict(re.findall(r"--tentacle-([\w-]+):\s*(#[0-9a-fA-F]{6})", css))

    def test_white_on_accent_fill(self):
        self.assertGreaterEqual(_contrast("#ffffff", self.t["accent-fill"]), 4.5)

    def test_accent_text_on_backgrounds(self):
        for bg in ("ink", "ink-1", "ink-2", "navy"):
            with self.subTest(bg=bg):
                self.assertGreaterEqual(_contrast(self.t["accent"], self.t[bg]), 4.5)

    def test_muted_text_on_backgrounds(self):
        for bg in ("ink", "ink-1", "ink-2", "ink-3", "navy"):
            with self.subTest(bg=bg):
                self.assertGreaterEqual(_contrast(self.t["text-muted"], self.t[bg]), 4.5)

    def test_dashboard_uses_the_brand_values(self):
        html = (ROOT / "tentacle" / "static" / "index.html").read_text()
        root = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", html.split("</style>")[0]))
        pairs = {"bg0": "ink", "bg1": "ink-1", "bg2": "ink-2", "bg3": "ink-3", "text2": "text-muted",
                 "accent": "accent-fill", "accent-text": "accent"}
        for dash, brand in pairs.items():
            with self.subTest(token=dash):
                self.assertEqual(root[dash].lower(), self.t[brand].lower())

    def test_dashboard_text3_on_its_backgrounds(self):
        html = (ROOT / "tentacle" / "static" / "index.html").read_text()
        text3 = re.search(r"--text3:\s*(#[0-9a-fA-F]{6})", html).group(1)
        for bg in ("ink-2", "ink-3"):
            with self.subTest(bg=bg):
                self.assertGreaterEqual(_contrast(text3, self.t[bg]), 4.5)


if __name__ == "__main__":
    unittest.main()
