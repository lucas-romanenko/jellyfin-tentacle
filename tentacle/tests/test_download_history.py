"""Everything each person has downloaded, for the admins (Lucas, 2026-11-09:
"even though I clicked to allow Roman to download without having to put a
request through me, I feel like there should still be a section somewhere in
Tentacle for admins to see everything that each person has downloaded").

Every download sent for a person is kept (straight away, approved, by a
list), with the date asked; its status (downloading / ready / removed) comes
from the library, and requests not sent (waiting, declined, failed) are listed
with them. Removing the title (Radarr's delete drops the DownloadRequest) never
loses the record.

Run from the tentacle/ directory:  python -m unittest tests.test_download_history
"""
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from routers import family as family_router
from routers.auth import require_admin
from services import download_history as H
from services import gone_people
from services.media_requests import record_download_request
from tmp_dirs import temp_dir


class DownloadHistory(unittest.TestCase):
    def setUp(self):
        tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.db.add_all([
            mdb.TentacleUser(id=1, jellyfin_user_id="jf-lucas", display_name="Lucas", is_admin=True),
            mdb.TentacleUser(id=2, jellyfin_user_id="jf-roman", display_name="Roman", request_mode="direct"),
            mdb.TentacleUser(id=3, jellyfin_user_id="jf-mia", display_name="Mia", kids_level="kids"),
        ])
        self.db.commit()

    def items(self, user_id=None):
        return H.listing(self.db, user_id=user_id)["items"]

    def one(self, **match):
        got = [i for i in self.items() if all(i.get(k) == v for k, v in match.items())]
        self.assertEqual(len(got), 1, f"{match}: {self.items()}")
        return got[0]

    def test_a_straight_away_download_goes_downloading_ready_removed_and_stays(self):
        record_download_request(self.db, 603, "movie", 2, via="the Jellyfin plugin (web or Android TV)")
        i = self.one(tmdb_id=603)
        self.assertEqual((i["person"], i["how"], i["status"]), ("Roman", "direct", "downloading"))

        self.db.add(mdb.Movie(tmdb_id=603, title="The Matrix", year="1999", poster_path="/m.jpg", source="radarr"))
        self.db.commit()
        i = self.one(tmdb_id=603)
        self.assertEqual((i["status"], i["title"], i["year"], i["poster_path"]), ("ready", "The Matrix", "1999", "/m.jpg"))

        # Radarr's delete (routers/radarr.py) drops the row and the request
        self.db.query(mdb.Movie).delete()
        self.db.query(mdb.DownloadRequest).delete()
        self.db.add(mdb.DeletionLog(kind="download-delete", name="The Matrix", media_type="movie",
                                    created_at=datetime.utcnow() + timedelta(seconds=1)))
        self.db.commit()
        i = self.one(tmdb_id=603)
        self.assertEqual((i["status"], i["title"]), ("removed", "The Matrix"), "the record and its name stay")
        self.assertTrue(i["status_at"])

    def test_a_vod_title_downloaded_too_is_ready(self):
        record_download_request(self.db, 77, "movie", 2)
        self.db.add(mdb.Movie(tmdb_id=77, title="VOD film", source="provider_1", radarr_path="/movies/VOD film"))
        self.db.commit()
        self.assertEqual(self.one(tmdb_id=77)["status"], "ready")

    def test_a_show_is_ready_once_sonarr_has_files(self):
        record_download_request(self.db, 1399, "series", 2)
        self.assertEqual(self.one(tmdb_id=1399)["status"], "downloading")
        self.db.add(mdb.Series(tmdb_id=1399, title="Game of Thrones", source="sonarr"))
        self.db.commit()
        self.assertEqual(self.one(tmdb_id=1399)["status"], "ready")

    def test_an_approved_request_keeps_the_day_asked_and_who_said_yes(self):
        asked = datetime.utcnow() - timedelta(days=3)
        record_download_request(self.db, 11, "movie", 3, via="approved by Lucas", asked_at=asked)
        self.db.add(mdb.MediaRequest(user_id=3, kind="movie", tmdb_id=11, title="Star Wars", status="approved",
                                     decided_by="Lucas", created_at=asked, decided_at=datetime.utcnow()))
        self.db.commit()
        i = self.one(tmdb_id=11)   # once: the approved request is the download
        self.assertEqual((i["how"], i["via"], i["person"]), ("approved", "approved by Lucas", "Mia"))
        self.assertEqual(i["asked_at"], asked.isoformat() + "Z")

    def test_a_list_download_says_so(self):
        record_download_request(self.db, 12, "movie", 2, via="list 'IMDb Top' auto-add")
        self.assertEqual(self.one(tmdb_id=12)["how"], "list")

    def test_requests_not_sent_are_listed_with_their_answer(self):
        self.db.add_all([
            mdb.MediaRequest(user_id=3, kind="movie", tmdb_id=21, title="Waiting one", status="pending"),
            mdb.MediaRequest(user_id=3, kind="series", tmdb_id=22, title="Declined one", status="declined",
                             decided_by="Lucas", note="Too scary", decided_at=datetime.utcnow()),
            mdb.MediaRequest(user_id=3, kind="movie", tmdb_id=23, title="Failed one", status="failed",
                             decided_by="Lucas", note="Radarr is down", decided_at=datetime.utcnow()),
            mdb.MediaRequest(user_id=3, kind="album", title="An album", status="pending"),
        ])
        self.db.commit()
        got = {i["title"]: (i["status"], i.get("note", "")) for i in self.items()}
        self.assertEqual(got, {"Waiting one": ("waiting", ""), "Declined one": ("declined", "Too scary"),
                               "Failed one": ("failed", "Radarr is down")}, "music isn't a download here")

    def test_two_people_downloading_the_same_title_both_keep_it(self):
        """DownloadRequest holds one requester per title; the history holds both."""
        record_download_request(self.db, 603, "movie", 2)
        record_download_request(self.db, 603, "movie", 1)
        self.assertEqual(self.db.query(mdb.DownloadRequest).count(), 1)
        self.assertEqual(sorted(i["person"] for i in self.items()), ["Lucas", "Roman"])

    def test_filtered_by_person_newest_asked_first(self):
        now = datetime.utcnow()
        record_download_request(self.db, 1, "movie", 2, asked_at=now - timedelta(days=2))
        record_download_request(self.db, 2, "movie", 2, asked_at=now)
        record_download_request(self.db, 3, "movie", 1, asked_at=now - timedelta(days=1))
        self.assertEqual([i["tmdb_id"] for i in self.items(user_id=2)], [2, 1])
        self.assertEqual([i["tmdb_id"] for i in self.items()], [2, 3, 1])
        self.assertEqual(H.counts_by_person(self.db), {2: 2, 1: 1})

    def test_earlier_downloads_are_added_once(self):
        old = datetime(2026, 10, 1)
        self.db.add_all([
            mdb.DownloadRequest(tmdb_id=5, media_type="movie", user_id=2, created_at=old),
            mdb.DownloadRequest(tmdb_id=6, media_type="series", user_id=3, created_at=old),
            mdb.MediaRequest(user_id=3, kind="series", tmdb_id=6, title="Bluey", status="approved",
                             decided_by="Lucas", created_at=old - timedelta(days=1), decided_at=old),
            mdb.MediaRequest(user_id=3, kind="movie", tmdb_id=7, title="Gone now", poster_path="/g.jpg",
                             status="approved", decided_by="Lucas", created_at=old, decided_at=old),
        ])
        self.db.commit()
        self.assertEqual(H.backfill_once(self.db), 3)
        self.assertEqual(H.backfill_once(self.db), 0)
        got = {i["tmdb_id"]: (i["how"], i["status"], i["asked_at"][:10]) for i in self.items()}
        self.assertEqual(got, {5: ("direct", "downloading", "2026-10-01"),
                               6: ("approved", "downloading", "2026-09-30"),
                               7: ("approved", "removed", "2026-10-01")})
        self.assertEqual(self.one(tmdb_id=7)["title"], "Gone now")

    def test_a_person_gone_from_jellyfin_takes_their_history(self):
        self.assertIn(mdb.DownloadHistory, gone_people.PER_PERSON)

    def test_only_admins_can_read_it(self):
        route = next(r for r in family_router.router.routes if getattr(r, "path", "") == "/api/requests/downloads")
        self.assertIn(require_admin, [d.dependency for d in route.dependencies])


if __name__ == "__main__":
    unittest.main()
