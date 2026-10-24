"""Live TV channel logos: the fallback chain and the logo cache (#137).

A provider's group picture (one image for many different channels) or a dead
logo gives way to the same channel elsewhere, the guide's icon or the logo
library; a dead logo Tentacle kept a copy of is served from the copy. The
pick lands on LiveChannel.guide_logo, so every lineup follows.

Run from the tentacle/ directory:  python -m unittest tests.test_channel_logos
"""
import logging
import os
import struct
import unittest
import zlib
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services import channel_logos as CL
from tmp_dirs import temp_dir

GROUP = "http://192.0.2.10/logos/320/13797.png"


def _png(side=64) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * side for _ in range(side))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


LIBRARY = {
    "channels": [
        {"id": "MTV.us", "name": "MTV", "alt_names": [], "country": "US"},
        {"id": "MTVCanada.ca", "name": "MTV", "alt_names": ["MTV Canada"], "country": "CA"},
        {"id": "Crave1.ca", "name": "Crave 1", "alt_names": [], "country": "CA"},
        {"id": "Global.ca", "name": "Global", "alt_names": ["Global TV"], "country": "CA"},
        {"id": "Gone.us", "name": "Gone TV", "alt_names": [], "country": "US", "closed": "2020-01-01"},
    ],
    "logos": [
        {"channel": "MTV.us", "feed": None, "in_use": True, "format": "PNG", "width": 500, "url": "https://img.example/mtv-us.png"},
        {"channel": "MTV.us", "feed": "East", "in_use": True, "format": "PNG", "width": 900, "url": "https://img.example/mtv-east.png"},
        {"channel": "MTVCanada.ca", "feed": None, "in_use": True, "format": "PNG", "width": 400, "url": "https://img.example/mtv-ca.png"},
        {"channel": "Crave1.ca", "feed": None, "in_use": True, "format": "PNG", "width": 400, "url": "https://img.example/crave1.png"},
        {"channel": "Global.ca", "feed": None, "in_use": True, "format": "PNG", "width": 400, "url": "https://img.example/global.png"},
        {"channel": "Gone.us", "feed": None, "in_use": True, "format": "PNG", "width": 400, "url": "https://img.example/gone.png"},
    ],
}


class LogoCase(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = temp_dir(self)
        env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        CL._library.update({"data": None, "mtime": 0.0, "tried": 0.0})
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p", live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.pid = p.id
        self.n = 0
        os.makedirs(CL.cache_dir(), exist_ok=True)
        with open(CL._library_path(), "w") as f:
            import json
            json.dump(CL.build_library(LIBRARY["channels"], LIBRARY["logos"]), f)
        no_guide = mock.patch.object(CL, "guide_icons", return_value={})
        self.icons = no_guide.start()
        self.addCleanup(no_guide.stop)

    def ch(self, name, logo=None, enabled=True, gid=None):
        self.n += 1
        c = mdb.LiveChannel(id=self.n, provider_id=self.pid, name=name, stream_id=str(self.n),
                            stream_url=f"http://192.0.2.10/live/{self.n}.ts", logo_url=logo,
                            enabled=enabled, epg_channel_id=gid)
        self.db.add(c)
        self.db.commit()
        return c

    def run_pass(self, answers=None):
        """A pass where each logo address answers from `answers` (url -> result
        of fetch_logo); anything else is a 64 px PNG."""
        answers = answers or {}
        fetched = []

        def fake(url):
            fetched.append(url)
            return answers.get(url) or ("ok", _png(), "png")
        with mock.patch.object(CL, "fetch_logo", side_effect=fake), mock.patch.object(CL.time, "sleep"):
            done = CL.run_pass(self.db, fetch=True)
        self.fetched = fetched
        return done


class GroupPictures(unittest.TestCase):
    def test_a_picture_of_many_brands_is_a_group_picture_a_brand_logo_is_not(self):
        pairs = [("CA: ABC NEWS", GROUP), ("CA: GLOBAL", GROUP + "?1"), ("TV: ZENLIFE", GROUP),
                 ("TV: YAHOO FINANCE", GROUP),
                 ("US: FOX 10 PHOENIX", "http://x/fox.png"), ("US: FOX 11 TUCSON", "http://x/fox.png"),
                 ("US: FOX 5 ATLANTA", "http://x/fox.png"),
                 ("ESPN+ PPV 1", "http://x/ppv.png"), ("ESPN+ PPV 2", "http://x/ppv.png"), ("ESPN+ PPV 3", "http://x/ppv.png"),
                 ("CA: TSN 1", "http://x/tsn.png"), ("CA: TSN 2", "http://x/tsn.png")]
        self.assertEqual({GROUP.lower()}, CL.group_logos(pairs))

    def test_two_channels_never_make_a_group(self):
        self.assertEqual(set(), CL.group_logos([("A ONE", GROUP), ("B TWO", GROUP)]))


class Chain(LogoCase):
    def test_a_good_provider_logo_is_left_alone(self):
        c = self.ch("US: TBS HD", "http://192.0.2.10/logos/tbs.png?5")
        done = self.run_pass()
        self.assertIsNone(c.auto_logo_url)
        self.assertEqual("http://192.0.2.10/logos/tbs.png?5", c.guide_logo)
        self.assertEqual({"provider": 1}, done["sources"])
        row = self.db.get(mdb.LiveLogo, "http://192.0.2.10/logos/tbs.png")
        self.assertEqual("ok", row.state)
        self.assertTrue(os.path.isfile(os.path.join(CL.cache_dir(), row.file)))

    def test_a_group_picture_gives_way_to_the_same_channel_elsewhere(self):
        mtv = self.ch("TV: MTV ᴿᴬᵂ", GROUP)
        self.ch("CA: GLOBAL", GROUP)
        self.ch("TV: ZENLIFE", GROUP)
        self.ch("CA EN: MTV HD", "http://192.0.2.10/logos/mtv.png", enabled=False)
        self.run_pass()
        self.assertEqual(("same channel", "http://192.0.2.10/logos/mtv.png"), (mtv.auto_logo_source, mtv.auto_logo_url))
        self.assertEqual("http://192.0.2.10/logos/mtv.png", mtv.guide_logo)

    def test_then_the_guide_icon_then_the_library_by_guide_id_then_by_name_and_country(self):
        crave = self.ch("CA EN: CRAVE1 HD", GROUP, gid="crave1.ca")
        glob = self.ch("CA: GLOBAL", GROUP)
        zen = self.ch("TV: ZENLIFE", GROUP, gid="Zen.us")
        mtv = self.ch("CA EN: MTV", GROUP)
        self.icons.return_value = {"zen.us": "http://guide.example/zen.png"}
        self.run_pass()
        self.assertEqual(("library", "https://img.example/crave1.png"), (crave.auto_logo_source, crave.auto_logo_url))
        self.assertEqual(("library", "https://img.example/global.png"), (glob.auto_logo_source, glob.auto_logo_url))
        self.assertEqual(("guide", "http://guide.example/zen.png"), (zen.auto_logo_source, zen.auto_logo_url))
        # "MTV" is in the library twice: the channel's "CA" prefix picks Canada's.
        self.assertEqual("https://img.example/mtv-ca.png", mtv.auto_logo_url)

    def test_nothing_better_keeps_the_group_picture(self):
        a = self.ch("TV: SOMETHING ONE", GROUP)
        self.ch("TV: OTHER TWO", GROUP)
        self.ch("TV: THIRD THREE", GROUP)
        self.run_pass()
        self.assertIsNone(a.auto_logo_url)
        self.assertEqual(GROUP, a.guide_logo)

    def test_the_admins_logo_still_wins(self):
        c = self.ch("CA: GLOBAL", GROUP)
        self.ch("TV: ZENLIFE", GROUP)
        self.ch("TV: ABC NEWS", GROUP)
        c.custom_logo_url = "https://mine.example/global.png"
        self.db.commit()
        self.run_pass()
        self.assertEqual("https://img.example/global.png", c.auto_logo_url)
        self.assertEqual("https://mine.example/global.png", c.guide_logo)


class Checks(LogoCase):
    def test_a_dead_logo_falls_to_its_copy_and_only_after_two_failures(self):
        url = "http://192.0.2.10/logos/tsn.png"
        c = self.ch("CA: TSN 1", url)
        mdb.set_setting(self.db, "youtube_base_url", "http://tentacle.lan:8888")
        self.run_pass()
        file = self.db.get(mdb.LiveLogo, url).file
        later = datetime.utcnow() - timedelta(days=8)
        for _ in range(2):
            self.db.get(mdb.LiveLogo, url).checked_at = later
            self.db.commit()
            self.run_pass({url: ("dead", None, "HTTP 404")})
            if _ == 0:
                self.assertIsNone(c.auto_logo_url, "one failure is not dead yet")
        self.assertEqual("dead", self.db.get(mdb.LiveLogo, url).state)
        self.assertEqual(("cache", f"http://tentacle.lan:8888/api/live/logo/{file}"), (c.auto_logo_source, c.auto_logo_url))
        path, kind = CL.cached_file(file)
        self.assertEqual("image/png", kind)

    def test_a_logo_that_is_no_picture_is_skipped(self):
        bad = "http://192.0.2.10/logos/err.png"
        c = self.ch("CA EN: MTV", bad)
        self.ch("US: MTV", "http://192.0.2.10/logos/mtv.png")
        self.run_pass({bad: ("bad", None, "not a picture")})
        self.assertEqual(("same channel", "http://192.0.2.10/logos/mtv.png"), (c.auto_logo_source, c.auto_logo_url))

    def test_checks_stay_within_the_budget_and_are_not_repeated(self):
        for i in range(5):
            self.ch(f"CHANNEL {i}", f"http://192.0.2.10/logos/{i}.png")
        with mock.patch.object(CL, "fetch_logo", return_value=("ok", _png(), "png")) as f, \
                mock.patch.object(CL.time, "sleep"):
            CL.run_pass(self.db, budget=3)
            self.assertEqual(3, f.call_count)
            CL.run_pass(self.db, budget=3)
            self.assertEqual(5, f.call_count)
            CL.run_pass(self.db, budget=3)
            self.assertEqual(5, f.call_count, "checked within a week: not again")

    def test_sniff_and_placeholders(self):
        self.assertEqual("png", CL.sniff(_png()))
        self.assertEqual("svg", CL.sniff(b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'))
        self.assertIsNone(CL.sniff(b"<html>Not found</html>"))
        self.assertTrue(CL._too_small(_png(1), "png"))
        self.assertFalse(CL._too_small(_png(64), "png"))

    def test_the_copy_route_only_serves_copies(self):
        self.assertIsNone(CL.cached_file("../t.db"))
        self.assertIsNone(CL.cached_file("0123456789abcdef0123.png"))


class Library(unittest.TestCase):
    def test_closed_channels_and_feed_logos_are_left_out(self):
        lib = CL.build_library(LIBRARY["channels"], LIBRARY["logos"])
        self.assertNotIn("gone.us", lib["ids"])
        self.assertEqual("https://img.example/mtv-us.png", lib["ids"]["mtv.us"][2])
        self.assertEqual(["mtv.us", "mtvcanada.ca"], sorted(lib["names"]["mtv"]))

    def test_a_name_in_several_countries_needs_a_country(self):
        lib = CL.build_library(LIBRARY["channels"], LIBRARY["logos"])
        self.assertIsNone(CL.library_match(lib, None, ["TV: MTV"], []))
        self.assertEqual("https://img.example/mtv-us.png", CL.library_match(lib, None, ["TV: MTV"], ["US"]))
        self.assertEqual("https://img.example/global.png", CL.library_match(lib, None, ["Global TV"], []))


class NetworkVote(unittest.TestCase):
    def test_a_folded_network_votes_with_each_versions_logo_after_the_chain(self):
        from types import SimpleNamespace
        from services.livetv_catalog import network_logo

        def v(own, auto=None):
            return SimpleNamespace(logo_url=own, auto_logo_url=auto, custom_logo_url=None,
                                   automatic_logo=auto or own)
        net = SimpleNamespace(custom_logo_url=None)
        a, b, c = v(GROUP, "https://img.example/mtv-us.png"), v(GROUP, "https://img.example/mtv-us.png"), v("http://x/mtv.png")
        self.assertEqual("https://img.example/mtv-us.png", network_logo(net, a, [a, b, c]))


if __name__ == "__main__":
    unittest.main()
