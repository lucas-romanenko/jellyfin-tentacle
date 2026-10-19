"""#682: Sonarr 4 counts Tentacle's own .strm files as episode files, so in
the merged layout (Sonarr's root folder is the VOD shows folder) a VOD show
added to Sonarr has episodeFileCount > 0 without a single download. #255
stopped listing such shows as duplicates (series_has_real_download), but the
scan still treated them as downloads: "Downloaded TV" and "Recently Added TV"
replaced the VOD tags, downloaded_at took Sonarr's added date, and the scan
wrote those tags into the show's tvshow.nfo.

A provider row whose Sonarr show holds only .strm files is no download: the
scan leaves its tags, date and NFO alone, and takes the download tags and
date off a row an older scan gave them. Sonarr not answering (None) keeps
today's behaviour; a real file is still a download. ("Recently Added TV"
follows the VOD row's own date, as the nightly recency pass has it.)
"""
import unittest
from datetime import datetime
from unittest import mock

import models.database as mdb
from test_issue_649_arr_root_elsewhere import _Base


class StrmOnlyShowInSonarr(_Base):
    def _scan(self, shows, files):
        import services.sonarr as sonarr

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_series(self, raise_errors=False):
                return [dict(s) for s in shows]

            def get_episode_files(self, series_id):
                if files is None:
                    raise ConnectionError("Sonarr is busy")
                return files

            def __getattr__(self, name):
                return lambda *a, **k: []

        with mock.patch.object(sonarr, "SonarrService", Fake), \
             mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None):
            return sonarr.scan_sonarr_library(self.db)

    def _vod_show(self, tags, downloaded_at=None):
        folder = self.shows / "Inventing Anna (2022)"
        (folder / "Season 01").mkdir(parents=True)
        (folder / "Season 01" / "Inventing Anna S01E01.strm").write_text("http://panel.example/series/u/p/1.mkv")
        nfo = folder / "tvshow.nfo"
        nfo.write_text('<?xml version="1.0"?>\n<tvshow>\n  <title>Inventing Anna</title>\n'
                       + "".join(f"  <tag>{t}</tag>\n" for t in tags) + "</tvshow>\n")
        self.db.add(mdb.Series(tmdb_id=9001, title="Inventing Anna", year="2022", source="provider_1",
                               strm_path=str(folder), nfo_path=str(nfo), tags=list(tags),
                               source_tag="Netflix TV", downloaded_at=downloaded_at,
                               # Old: "Recently Added TV" isn't the VOD show's due either.
                               date_added=datetime(2025, 1, 1),
                               sonarr_path="/data/media/tv-shows/Inventing Anna (2022)" if downloaded_at else None))
        self.db.commit()
        show = {"tmdbId": 9001, "tvdbId": 1, "id": 7, "title": "Inventing Anna", "year": 2022,
                "path": "/data/media/tv-shows/Inventing Anna (2022)", "added": "2026-09-01T00:00:00Z",
                "monitorNewItems": "none", "statistics": {"episodeFileCount": 1}}
        return nfo, show

    def _row(self):
        self.db.expire_all()
        return self.db.query(mdb.Series).filter_by(tmdb_id=9001).one()

    STRM = [{"path": "/data/media/tv-shows/Inventing Anna (2022)/Season 01/Inventing Anna S01E01.strm"}]
    MKV = [{"path": "/data/media/tv-shows/Inventing Anna (2022)/Season 01/Inventing Anna S01E01.mkv"}]

    def test_a_strm_only_show_is_not_a_download(self):
        nfo, show = self._vod_show(["Netflix TV"])
        self._scan([show], self.STRM)
        row = self._row()
        self.assertNotIn("Downloaded TV", row.tags or [])
        self.assertNotIn("Recently Added TV", row.tags or [])
        self.assertIn("Netflix TV", row.tags or [])
        self.assertIsNone(row.downloaded_at)
        self.assertNotIn("<tag>Downloaded TV</tag>", nfo.read_text())
        self.assertNotIn("<tag>Recently Added TV</tag>", nfo.read_text())
        # Still an intentional add: Sonarr holds it.
        self.assertEqual(row.sonarr_path, show["path"])

    def test_an_older_scans_download_tags_come_off(self):
        nfo, show = self._vod_show(["Downloaded TV", "Recently Added TV", "Netflix TV"],
                                   downloaded_at=datetime(2026, 9, 1))
        self._scan([show], self.STRM)
        row = self._row()
        self.assertNotIn("Downloaded TV", row.tags or [])
        self.assertNotIn("Recently Added TV", row.tags or [])
        self.assertIn("Netflix TV", row.tags or [])
        self.assertIsNone(row.downloaded_at)
        self.assertNotIn("<tag>Downloaded TV</tag>", nfo.read_text())
        self.assertNotIn("<tag>Recently Added TV</tag>", nfo.read_text())
        self.assertIn("<tag>Netflix TV</tag>", nfo.read_text())
        self.assertEqual(row.sonarr_path, show["path"])

    def test_a_real_file_is_still_a_download(self):
        _, show = self._vod_show(["Netflix TV"])
        self._scan([show], self.MKV)
        row = self._row()
        self.assertIn("Downloaded TV", row.tags or [])
        self.assertIsNotNone(row.downloaded_at)

    def test_sonarr_not_answering_keeps_todays_behaviour(self):
        _, show = self._vod_show(["Netflix TV"])
        self._scan([show], None)
        row = self._row()
        self.assertIn("Downloaded TV", row.tags or [])
        self.assertIsNotNone(row.downloaded_at)


if __name__ == "__main__":
    unittest.main()
