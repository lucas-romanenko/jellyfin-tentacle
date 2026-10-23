"""Keep Downloaded (and Resolve All) on a show that is only PARTLY downloaded
must not throw away the episodes that exist only as provider streams.

A VOD show becomes a series duplicate as soon as Sonarr holds one real
episode file of it (Download More / Add to Sonarr for one season does that).
`_require_series_download` (routers/duplicates.py) only checks that at
least one real file exists (#255), and `_apply_resolution("keep_radarr")`
then runs `delete_series_files(<VOD folder>)` -- every .strm of the show --
converts the row to a Sonarr row and leaves the keep_radarr tombstone, so the
VOD sync never brings the other episodes back. The Duplicates text says only
"The VOD stream files (.strm and .nfo) will be permanently deleted"; nothing
says that 4 of 5 episodes have no other copy. Resolve All applies the same to
every pending show in one click.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from unittest import mock

from fastapi import HTTPException

import models.database as mdb
from test_issue_649_arr_root_elsewhere import _Base

TMDB = 4242


class PartlyDownloadedShow(_Base):
    def setUp(self):
        super().setUp()
        self.folder = self.tmp / "vod" / "shows" / "Lanterns (2025)"
        season = self.folder / "Season 01"
        season.mkdir(parents=True)
        for n in range(1, 6):
            (season / f"Lanterns S01E0{n}.strm").write_text(f"http://panel.example/series/u/p/{n}.mkv")
        (self.folder / "tvshow.nfo").write_text("<tvshow><title>Lanterns</title></tvshow>")
        self.db.add(mdb.Series(tmdb_id=TMDB, title="Lanterns", year="2025", source="provider_1",
                               strm_path=str(self.folder), sonarr_path=str(self.folder), tags=["Netflix TV"]))
        self.db.add(mdb.Duplicate(tmdb_id=TMDB, media_type="series", resolution="pending",
                                  sources=[{"source": "provider_1", "path": str(self.folder)},
                                           {"source": "sonarr", "path": str(self.folder)}]))
        self.db.commit()
        self.downloaded = []          # episode numbers Sonarr holds a real file of

    def download(self, *episodes):
        for n in episodes:
            (self.folder / "Season 01" / f"Lanterns S01E0{n}.mkv").write_bytes(b"\0" * 16)
            self.downloaded.append(n)

    def sonarr(self):
        folder, downloaded = self.folder, self.downloaded

        class FakeSonarr:
            def __init__(self, *a, **k):
                pass

            def get_series_by_tmdb(self, tmdb_id, raise_errors=False):
                return {"id": 7, "tmdbId": TMDB, "title": "Lanterns", "path": str(folder)}

            def get_episode_files(self, series_id):
                # Sonarr 4 lists Tentacle's .strm files as episode files too.
                files = [{"id": 100 + i, "path": str(folder / "Season 01" / f"Lanterns S01E0{n}.mkv")}
                         for i, n in enumerate(downloaded)]
                files += [{"id": 200 + n, "path": str(p)} for n, p in
                          enumerate(sorted((folder / "Season 01").glob("*.strm")), 1)]
                return files

            def __getattr__(self, name):
                return lambda *a, **k: []

        return mock.patch("services.sonarr.SonarrService", FakeSonarr)

    def streams_left(self):
        return sorted(p.name for p in (self.folder / "Season 01").glob("*.strm")) if self.folder.exists() else []

    def resolve(self, how="single"):
        from routers import duplicates as d
        dup = self.db.query(mdb.Duplicate).one()
        with self.sonarr():
            if how == "single":
                return d.resolve_duplicate(dup.id, d.ResolveRequest(resolution="keep_radarr"), db=self.db)
            return d.resolve_all(d.ResolveAllRequest(resolution="keep_radarr"), db=self.db)

    VOD_ONLY = ["Lanterns S01E02.strm", "Lanterns S01E03.strm", "Lanterns S01E04.strm", "Lanterns S01E05.strm"]

    def test_keep_downloaded_keeps_or_refuses_for_episodes_only_the_vod_copy_has(self):
        self.download(1)
        try:
            self.resolve()
            refused = None
        except HTTPException as e:
            refused = e
        left = self.streams_left()
        if refused is None:
            self.assertTrue(set(self.VOD_ONLY) <= set(left),
                            f"Keep Downloaded deleted the 4 episodes that exist only as streams; left: {left}")
        else:
            self.assertEqual(409, refused.status_code)
            self.assertIn("4", str(refused.detail), "the refusal should say how many episodes exist only as VOD")
            self.assertEqual(5, len(left))

    def test_resolve_all_does_not_delete_a_partly_downloaded_shows_streams(self):
        self.download(1)
        self.resolve("all")
        left = self.streams_left()
        self.assertTrue(set(self.VOD_ONLY) <= set(left),
                        f"Resolve All deleted the 4 episodes that exist only as streams; left: {left}")

    def test_a_show_downloaded_in_full_still_drops_its_streams(self):
        # Control: every VOD episode has a download -> Keep Downloaded does what it says.
        self.download(1, 2, 3, 4, 5)
        self.resolve()
        self.assertEqual([], self.streams_left())
        self.db.expire_all()
        self.assertEqual("sonarr", self.db.query(mdb.Series).filter_by(tmdb_id=TMDB).one().source)

    def test_a_multi_episode_file_covers_each_episode_it_holds(self):
        # Sonarr names a double episode "S01E01-E02": both are downloaded.
        for n in (3, 4, 5):
            self.download(n)
        (self.folder / "Season 01" / "Lanterns S01E01-E02.mkv").write_bytes(b"\0")
        self.downloaded.append("1-E02")
        self.resolve()
        self.assertEqual([], self.streams_left())


if __name__ == "__main__":
    unittest.main()
