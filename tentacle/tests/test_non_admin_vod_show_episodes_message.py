"""#605: a non-admin asking for episodes of a VOD show is not sent to Settings.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Follow-up to #554. A show from the IPTV provider (a .strm series) that gets
downloaded episodes goes through the hybrid path of request_series. Two of its
per-show reasons reached whoever asked as the admin's text:
- the default vod_root layout with no VOD root folder in Sonarr (_NO_VOD_ROOT:
  "... Sonarr → Settings → Media Management ... Tentacle Settings → Integrations"),
- Sonarr with no root folders when they are read only for that show (the
  RequestRefused recorded with e.message, dropping its user_message).
A non-admin is told an admin has to set it up; an admin keeps the precise text.
"""
import unittest
from unittest import mock

from test_non_admin_settings_messages import _Base, _FakeArr  # noqa: E402  (also runs the web stubs)


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
        self.assertEqual([], _FakeArr.posts, "a blocked add still reached Sonarr")
        return resp.get("detail") or ""

    def assert_tells_an_admin(self, text):
        self.assertNotIn("Settings", text, f"a non-admin is sent to Settings: {text!r}")
        self.assertIn("admin", text.lower(), text)

    def test_no_vod_root_folder(self):
        from services.media_requests import _NO_VOD_ROOT
        self.assert_tells_an_admin(self.report(self.nora))
        self.assertEqual(_NO_VOD_ROOT, self.report(self.admin))

    def test_sonarr_without_root_folders_on_the_vod_show_path(self):
        from models.database import set_setting
        from services.sonarr import SonarrService
        # A default root folder is set, so Sonarr's folders are read only for the VOD show.
        set_setting(self.db, "sonarr_root_folder", "/media/tv")
        with mock.patch.object(SonarrService, "get_root_folders", return_value=[]):
            self.assert_tells_an_admin(self.report(self.nora))
            self.assertIn("Sonarr → Settings", self.report(self.admin))

    def test_internal_caller_keeps_the_admin_text(self):
        from services.media_requests import _NO_VOD_ROOT, request_series
        out = request_series(self.db, tmdb_ids=[7], user_id=None, via="test")
        self.assertEqual(_NO_VOD_ROOT, out.report.detail)


if __name__ == "__main__":
    unittest.main()
