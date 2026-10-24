"""The Radarr/Sonarr scan's tag push must not list Jellyfin while the library
scan it just started is running (follow-up to #437).

The scan wrote NFOs (or found new titles), asked Jellyfin for a full library
scan (POST /Library/Refresh, answered at once, the scan runs in the
background) and then listed every film/show to push tags. A recursive
listing of a large library does not answer within the 15 s request timeout
while Jellyfin is scanning: live, a Sonarr webhook scan logged "Triggered
Jellyfin library scan after Sonarr NFO updates" and, exactly 15 s later,
"Series listing stopped early at 0 items" -> "no tags pushed ... 3079 not in
Jellyfin yet". The same listing answers in well under a second when Jellyfin
is idle.

The fake below is the real JellyfinService over a fake HTTP session: a
library-wide /Items listing times out (requests.ReadTimeout, what requests
raises when the read timeout passes) from the moment /Library/Refresh is
posted; single-item GETs, a series' own seasons/episodes and tag writes
still answer. Titles Jellyfin
already lists must get their tags in that scan.
"""
import unittest
from unittest import mock
from urllib.parse import urlsplit

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from hermetic import no_tmdb
from services.jellyfin import JellyfinService
from tmp_dirs import temp_dir

MOVIES = [{"tmdbId": 1000 + i, "title": f"Film {i}", "year": 2000 + i, "hasFile": True,
           "movieFile": {"path": f"/movies/Film {i}/f.mkv"}} for i in range(3)]
SERIES = [{"tmdbId": 2000 + i, "title": f"Show {i}", "year": 2000 + i, "path": f"/tv/Show {i}",
           "statistics": {"episodeFileCount": 1}} for i in range(3)]


class Radarr:
    def __init__(self, *a, **k):
        pass

    def get_all_movies(self):
        return [dict(m) for m in MOVIES]


class Sonarr:
    def __init__(self, *a, **k):
        pass

    def get_all_series(self):
        return [dict(s) for s in SERIES]


class _Response:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._body


class FakeJellyfin:
    """One Jellyfin: its items, whether a library scan is running, and the
    tag writes it received."""

    def __init__(self, items, scan_slows_listings=True):
        self.items = {it["Id"]: it for it in items}
        self.scan_slows_listings = scan_slows_listings
        self.scanning = False
        self.writes = {}             # item id -> Tags posted
        self.listings = []           # ("idle" | "during scan", answered?)

    def get(self, url, params=None, timeout=None, **kw):
        path = urlsplit(url).path
        params = params or {}
        if path == "/Items" and params.get("ParentId"):
            # A series' seasons/episodes (the rating snapshot of a tag push):
            # small, answers during a scan. These fakes have none.
            return _Response(200, {"Items": [], "TotalRecordCount": 0})
        if path == "/Items" and str(params.get("Recursive")).lower() == "true":
            # A library-wide listing: thousands of rows on a real library.
            busy = self.scanning and self.scan_slows_listings
            self.listings.append(("during scan" if busy else "idle", not busy))
            if busy:
                raise requests.ReadTimeout("Read timed out. (read timeout=15)")
            kind = params.get("IncludeItemTypes")
            rows = [dict(it) for it in self.items.values() if it["Type"] == kind]
            start, limit = int(params.get("StartIndex", 0)), int(params.get("Limit", 10 ** 9))
            return _Response(200, {"Items": rows[start:start + limit], "TotalRecordCount": len(rows)})
        item_id = path.rsplit("/", 1)[-1]
        if path.startswith("/Users/") and "/Items/" in path or path.startswith("/Items/"):
            if item_id in self.items:
                return _Response(200, dict(self.items[item_id]))
            return _Response(404)
        return _Response(404)

    def post(self, url, json=None, timeout=None, **kw):
        path = urlsplit(url).path
        if path == "/Library/Refresh":
            self.scanning = True          # runs in the background, longer than this test
            return _Response(204)
        if path.startswith("/Items/") and path.count("/") == 2:
            item_id = path.rsplit("/", 1)[-1]
            if item_id not in self.items:
                return _Response(404)
            tags = list((json or {}).get("Tags") or [])
            self.writes[item_id] = tags
            self.items[item_id]["Tags"] = tags
            return _Response(204)
        return _Response(204)


def jellyfin_class(fake):
    class Jellyfin(JellyfinService):
        def __init__(self, url, api_key, user_id=""):
            super().__init__(url, api_key, user_id)
            self.session = fake
    return Jellyfin


def jf_items(kind, prefix, tmdb_base, year_base, indexes):
    return [{"Id": f"i{i}", "Type": kind, "Name": f"{prefix} {i}", "ProductionYear": year_base + i,
             "ProviderIds": {"Tmdb": str(tmdb_base + i)}, "Tags": ["keyword"],
             "ImageTags": {"Primary": "p"}} for i in indexes]


class RadarrScanTagPush(unittest.TestCase):
    logger = "services.radarr"
    kind, prefix, tmdb_base = "Movie", "Film", 1000

    def setUp(self):
        no_tmdb(self)
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr:7878"), ("radarr_api_key", "k"),
                     ("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"), ("data_dir", tmp),
                     ("jellyfin_url", "http://jellyfin.test"), ("jellyfin_api_key", "k"),
                     ("jellyfin_user_id", "u1")):
            mdb.set_setting(self.db, k, v)
        self.db.commit()

    def scan(self, fake):
        import services.radarr as radarr
        with mock.patch.object(radarr, "RadarrService", Radarr), \
                mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None), \
                mock.patch("services.jellyfin.JellyfinService", jellyfin_class(fake)), \
                self.assertLogs(self.logger, level="INFO") as logs:
            stats = radarr.scan_radarr_library(self.db)
        return stats, "\n".join(logs.output)

    def fake(self, **kw):
        # Titles 0 and 1 are in Jellyfin already (untagged by Tentacle);
        # title 2 is new: Jellyfin has not scanned it in yet.
        return FakeJellyfin(jf_items(self.kind, self.prefix, self.tmdb_base, 2000, (0, 1)), **kw)

    def test_titles_jellyfin_lists_get_their_tags_although_the_scan_started_a_library_scan(self):
        fake = self.fake()
        stats, log = self.scan(fake)
        self.assertTrue(fake.scanning, "the scan asked Jellyfin for a library scan (new titles)")
        self.assertNotIn("listing came back empty", log)
        self.assertEqual({"i0", "i1"}, set(fake.writes), f"tag writes: {fake.writes}; listings: {fake.listings}")
        for item_id, tags in fake.writes.items():
            self.assertIn("keyword", tags, "a tag Tentacle doesn't own stays")
            self.assertGreater(len(tags), 1, "Tentacle's tags were added")
        self.assertEqual((2, 0, 1), (stats["jf_tags_pushed"], stats["jf_tags_failed"],
                                     stats["jf_tags_not_found"]))

    def test_control_jellyfin_that_answers_during_a_scan(self):
        # Proves the fake: with a Jellyfin that lists during a scan, main pushes.
        fake = self.fake(scan_slows_listings=False)
        stats, log = self.scan(fake)
        self.assertEqual({"i0", "i1"}, set(fake.writes))
        self.assertEqual((2, 0, 1), (stats["jf_tags_pushed"], stats["jf_tags_failed"],
                                     stats["jf_tags_not_found"]))


class SonarrScanTagPush(RadarrScanTagPush):
    logger = "services.sonarr"
    kind, prefix, tmdb_base = "Series", "Show", 2000

    def scan(self, fake):
        import services.sonarr as sonarr
        with mock.patch.object(sonarr, "SonarrService", Sonarr), \
                mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None), \
                mock.patch("services.jellyfin.JellyfinService", jellyfin_class(fake)), \
                self.assertLogs(self.logger, level="INFO") as logs:
            stats = sonarr.scan_sonarr_library(self.db)
        return stats, "\n".join(logs.output)


if __name__ == "__main__":
    unittest.main()
