"""Stream-parsing XMLTV must not hold every programme it has read (#178).

Run from the tentacle/ directory:  python -m unittest discover -s tests

iterparse + elem.clear() empties each programme, but a cleared element is
still a child of <tv>, so the tree kept one element per programme read (and
every <channel> whole). Peak memory grew with the feed, and MAX_XMLTV_BYTES
allows 2 GiB. Ten times the programmes must not cost anywhere near ten times
the memory.
"""
import logging
import os
import shutil
import tracemalloc
import unittest
from unittest import mock

import services.xmltv as xmltv
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _write_feed(path, programmes):
    with open(path, "w", encoding="utf-8") as f:
        f.write("<tv>")
        for c in range(50):
            f.write(f'<channel id="c{c}"><display-name>Channel {c}</display-name>'
                    f'<icon src="http://img.example/{c}.png"/></channel>')
        for i in range(programmes):
            f.write(f'<programme start="20260924{(i // 60) % 24:02d}{i % 60:02d}00 +0000" '
                    f'stop="20260924{(i // 60) % 24:02d}{i % 60:02d}30 +0000" channel="c{i % 50}">'
                    f'<title>Programme {i}</title><desc>Something happens in episode {i}.</desc>'
                    f'<category>Drama</category></programme>')
        f.write("</tv>")


class ParseMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(xmltv, "XMLTV_CACHE_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _peak(self, programmes):
        url = f"http://192.0.2.10/feed-{programmes}.xml"
        _write_feed(xmltv._get_cache_path(url), programmes)
        tracemalloc.start()
        try:
            kept = xmltv.stream_parse_xmltv(url, {"c1"})  # one channel of fifty is kept
            cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(programmes // 50, len(kept))
        # What the parse held on the way, not the programmes it returns (still
        # alive in `cur`, and rightly ten times as many): a tree that keeps every
        # element read is freed by the end, so it shows here.
        return peak - cur

    def test_memory_does_not_grow_with_the_feed(self):
        small = self._peak(4_000)
        large = self._peak(40_000)
        self.assertLess(large, small * 3,
                        f"peak {large / 1e6:.1f} MB for 40k programmes vs {small / 1e6:.1f} MB for 4k")


if __name__ == "__main__":
    unittest.main()
