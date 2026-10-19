"""Provider errors shown in the dashboard keep the provider login out.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The Xtream API is called as player_api.php?username=U&password=P, and requests
puts that URL into its error text ("Max retries exceeded with url: ...").
Three admin-facing places passed the text on as it was: the provider test
(400 detail), fetch categories (400 detail) and the VOD sync's error pushed
to the sync progress stream. The Live TV provider test already redacted it;
these now do the same, keeping the rest of the message.
"""
import unittest
from unittest import mock

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir
from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
import routers.providers as providers  # noqa: E402
from fastapi import HTTPException  # noqa: E402

ACCOUNT, PASSWORD = "acct-7f3a", "pw-91c2"


def _error(action=""):
    return requests.ConnectionError(
        "HTTPConnectionPool(host='prov.test', port=80): Max retries exceeded with url: "
        f"/player_api.php?username={ACCOUNT}&password={PASSWORD}{action}")


class ProviderErrorsRedacted(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="p", server_url="http://prov.test", username=ACCOUNT, password=PASSWORD)
        self.db.add(p)
        self.db.commit()
        self.pid = p.id

    def assertClean(self, text):
        self.assertNotIn(PASSWORD, text)
        self.assertIn("Max retries exceeded", text)  # the useful part stays

    def test_provider_test(self):
        with mock.patch.object(providers, "test_provider_connection", side_effect=_error()):
            with self.assertRaises(HTTPException) as ctx:
                providers.test_provider(self.pid, self.db)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertClean(ctx.exception.detail)

    def test_fetch_categories(self):
        with mock.patch.object(providers, "fetch_provider_categories",
                               side_effect=_error("&action=get_vod_categories")):
            with self.assertRaises(HTTPException) as ctx:
                providers.fetch_categories(self.pid, self.db)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertTrue(ctx.exception.detail.startswith("Failed to fetch categories: "))
        self.assertClean(ctx.exception.detail)

    def test_vod_sync_error_in_the_progress_stream(self):
        import routers.sync as sync
        with mock.patch("models.database.SessionLocal", self.Session), \
                mock.patch.object(sync, "sync_provider", side_effect=_error("&action=get_vod_streams")):
            sync._run_sync_background(self.pid, "full")
        progress = sync._sync_progress.pop(self.pid)
        self.assertEqual(progress["phase"], "error")
        self.assertClean(progress["stats"]["error"])


if __name__ == "__main__":
    unittest.main()
