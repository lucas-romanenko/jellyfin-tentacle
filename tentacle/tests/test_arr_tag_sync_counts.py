"""#437: a tagged title Jellyfin hasn't listed is not a failed tag push.

The Radarr/Sonarr scan's tag push counted a title missing from Jellyfin's
listing in the same counter as a tag write Jellyfin refused. A listing that
timed out (nothing in it) logged "0 pushed, 17421 failed" with nothing
written and nothing refused. "failed" now counts refused writes only; titles
Jellyfin hasn't listed get their own count, and an empty listing says so.
"""
import unittest
from unittest import mock

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


class Jellyfin(JellyfinService):
    """The real listing code over a fake /Items answer."""
    items, refused = [], set()

    def __init__(self, *a, **k):
        self.url, self.api_key, self.user_id = "http://jellyfin.test", "k", ""

    def _get(self, path, params=None):
        items = type(self).items
        return {"Items": items, "TotalRecordCount": len(items)} if items is not None else None

    def trigger_library_scan(self):
        return True

    def set_item_tags(self, item_id, tags):
        return item_id not in type(self).refused


def jf_items(prefix, tmdb_base, year_base, indexes):
    return [{"Id": f"i{i}", "Name": f"{prefix} {i}", "ProductionYear": year_base + i,
             "ProviderIds": {"Tmdb": str(tmdb_base + i)}, "Tags": [],
             "ImageTags": {"Primary": "p"}} for i in indexes]


class _Base(unittest.TestCase):
    def setUp(self):
        no_tmdb(self)
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr:7878"), ("radarr_api_key", "k"),
                     ("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"), ("data_dir", tmp),
                     ("jellyfin_url", "http://jellyfin.test"), ("jellyfin_api_key", "k")):
            mdb.set_setting(self.db, k, v)
        self.db.commit()
        Jellyfin.items, Jellyfin.refused = [], set()


class RadarrTagSyncCounts(_Base):
    logger = "services.radarr"

    def scan(self):
        import services.radarr as radarr
        with mock.patch.object(radarr, "RadarrService", Radarr), \
                mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None), \
                mock.patch("services.jellyfin.JellyfinService", Jellyfin), \
                self.assertLogs(self.logger, level="INFO") as logs:
            stats = radarr.scan_radarr_library(self.db)
        return stats, "\n".join(logs.output)

    def listed(self, indexes):
        return jf_items("Film", 1000, 2000, indexes)

    def test_one_refused_write_and_one_title_jellyfin_has_not_listed(self):
        # 0 is in Jellyfin; 1 is too, but Jellyfin refuses its tag write;
        # 2 is not in Jellyfin yet.
        Jellyfin.items = self.listed((0, 1))
        Jellyfin.refused = {"i1"}
        stats, log = self.scan()
        self.assertEqual(1, stats["jf_tags_pushed"])
        self.assertEqual(1, stats["jf_tags_failed"])
        self.assertEqual(1, stats["jf_tags_not_found"])
        self.assertIn("1 pushed, 1 failed, 1 not in Jellyfin yet", log)

    def test_listing_that_timed_out_fails_nothing(self):
        Jellyfin.items = None                             # what _get answers on a timeout
        stats, log = self.scan()
        self.assertEqual(0, stats["jf_tags_pushed"])
        self.assertEqual(0, stats["jf_tags_failed"])
        self.assertEqual(3, stats["jf_tags_not_found"])
        self.assertIn("listing came back empty", log)

    def test_complete_listing_logs_no_empty_warning(self):
        Jellyfin.items = self.listed((0, 1, 2))
        stats, log = self.scan()
        self.assertEqual((3, 0, 0), (stats["jf_tags_pushed"], stats["jf_tags_failed"],
                                     stats["jf_tags_not_found"]))
        self.assertNotIn("listing came back empty", log)


class SonarrTagSyncCounts(RadarrTagSyncCounts):
    logger = "services.sonarr"

    def scan(self):
        import services.sonarr as sonarr
        with mock.patch.object(sonarr, "SonarrService", Sonarr), \
                mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None), \
                mock.patch("services.jellyfin.JellyfinService", Jellyfin), \
                self.assertLogs(self.logger, level="INFO") as logs:
            stats = sonarr.scan_sonarr_library(self.db)
        return stats, "\n".join(logs.output)

    def listed(self, indexes):
        return jf_items("Show", 2000, 2000, indexes)


if __name__ == "__main__":
    unittest.main()
