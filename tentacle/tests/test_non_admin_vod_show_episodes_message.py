"""#554 follow-up: a non-admin asking for episodes of a VOD show is not sent
to Settings either.

Run from the tentacle/ directory:  python -m unittest discover -s tests

A show that came from the IPTV provider (.strm) gets its downloaded episodes
in the same Jellyfin series (hybrid_series_layout). With the default layout
(vod_root) that needs the VOD folder as a root folder in Sonarr; without it
the add is refused, per show, in the add's report. That report text was the
admin's ("Add the folder ... as a Root Folder in Sonarr → Settings → Media
Management ... or switch to the shared-library layout in Tentacle Settings →
Integrations"), shown to whoever asked, and so was the text for a Sonarr with
no root folders at all on that path. A non-admin can open neither.
"""
import unittest
from unittest import mock

from tests.test_non_admin_settings_messages import _Base, _FakeArr


class VodShowEpisodes(_Base):
    def setUp(self):
        super().setUp()
        from models.database import Series, set_setting
        set_setting(self.db, "sonarr_quality_profile_id", "4")
        # The fake Sonarr's only root folder is /media: no VOD root folder.
        self.db.add(Series(tmdb_id=7, title="Lanterns", source="provider_1",
                           strm_path="/media/vod/shows/Lanterns (2020)"))
        self.db.commit()

    def report(self, user):
        import routers.lists as lists
        resp = lists.add_to_sonarr(lists.AddMissingBody(tmdb_ids=[7]), self.db, user)
        self.assertEqual(1, resp["failed"], resp)
        self.assertEqual([], _FakeArr.posts, "a refused add still reached Sonarr")
        return resp.get("detail") or ""

    def assert_tells_an_admin(self, text):
        self.assertNotIn("Settings", text, f"a non-admin is sent to Settings: {text!r}")
        self.assertIn("admin", text.lower(), f"a non-admin isn't told an admin has to set it: {text!r}")
        self.assertIn("Sonarr", text, text)

    def test_no_vod_root_folder_non_admin(self):
        self.assert_tells_an_admin(self.report(self.nora))

    def test_no_vod_root_folder_admin_keeps_the_precise_text(self):
        from services.media_requests import _NO_VOD_ROOT
        self.assertIn(_NO_VOD_ROOT, self.report(self.admin))

    def test_sonarr_without_root_folders_on_the_vod_show_path(self):
        # A default root folder is set, so the folders are read only for the
        # VOD show, and Sonarr now has none.
        from models.database import set_setting
        from services.sonarr import SonarrService
        set_setting(self.db, "sonarr_root_folder", "/media/tv")
        with mock.patch.object(SonarrService, "get_root_folders", return_value=[]):
            self.assert_tells_an_admin(self.report(self.nora))
            self.assertIn("Sonarr → Settings", self.report(self.admin))


if __name__ == "__main__":
    unittest.main()
