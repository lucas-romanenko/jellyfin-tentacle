"""Two different SHOWS that map to one folder get their own files (#155, series side).

FolderCollisions in test_vod_paths covers films only; for films a second
guard (_folder_owned_elsewhere, #185) also catches the clash, so the series
path is the only place _claim_vod_name alone keeps two titles apart.
"""
from pathlib import Path as _RealPath

from nightly_harness import FakeTMDB, NightlyHarness, age_prune_marks


class SeriesFolderCollisions(NightlyHarness):
    def setUp(self):
        super().setUp()
        self.add_category("s1", type_="series")
        # Two different shows whose names sanitize to one folder name.
        self.client.series["s1"] = [("Brothers?", 31), ("Brothers", 32)]
        FakeTMDB.ids.update({"Brothers?": 6001, "Brothers": 6002})
        self.sync_only()

    def _episodes(self, row):
        return sorted(_RealPath(row.strm_path).rglob("*.strm"))

    def test_each_show_gets_its_own_folder(self):
        a, b = self.series_row(6001), self.series_row(6002)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertNotEqual(a.strm_path, b.strm_path)
        self.assertIn("[tmdbid-6002]", b.strm_path)

    def test_each_show_plays_its_own_episodes(self):
        a, b = self.series_row(6001), self.series_row(6002)
        ea, eb = self._episodes(a), self._episodes(b)
        self.assertEqual(1, len(ea), ea)
        self.assertEqual(1, len(eb), eb)
        self.assertTrue(ea[0].read_text().strip().endswith("/311.mp4"), ea[0].read_text())
        self.assertTrue(eb[0].read_text().strip().endswith("/321.mp4"), eb[0].read_text())

    def test_pruning_one_show_keeps_the_others_episodes(self):
        self.client.series["s1"] = [("Brothers?", 31)]   # the provider drops "Brothers"
        self.sync_only()
        age_prune_marks(self.db)           # the next night
        self.sync_only()
        self.assertIsNone(self.series_row(6002))
        a = self.series_row(6001)
        self.assertEqual(1, len(self._episodes(a)), "the other show's episodes were deleted")

    def test_pruning_one_keeps_the_others_episodes_when_the_folder_was_shared(self):
        """Rows written before the fix point at ONE show folder (the films'
        test_pruning_one_keeps_the_others_files_even_when_they_were_shared)."""
        a, b = self.series_row(6001), self.series_row(6002)
        b.strm_path = a.strm_path          # the old, shared layout
        self.db.commit()
        self.client.series["s1"] = [("Brothers?", 31)]
        self.sync_only()
        age_prune_marks(self.db)           # the next night
        self.sync_only()
        self.assertIsNone(self.series_row(6002))
        self.assertEqual(1, len(self._episodes(self.series_row(6001))),
                         "the other show's episodes were deleted")
