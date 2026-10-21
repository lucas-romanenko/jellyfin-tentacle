"""People deleted in Jellyfin are forgotten by Tentacle too (services/gone_people.py).

Jellyfin's DELETE /Users/{id} removes the account, its sign-ins and the
playlists it owns (10.11.8, Jellyfin.Api/Controllers/UserController.cs);
Tentacle's own rows and files for that person are Tentacle's to remove.

Run from the tentacle/ directory:  python -m unittest tests.test_gone_people
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import models.database as mdb
from services import family as F, gone_people, smartlists as S
from tests.test_family import ADMIN, ANN, BEN, Case


class Gone(Case):
    def setUp(self):
        super().setUp()
        self.lists = Path(self.tmp) / "smartlists"
        self.homes = Path(self.tmp) / "home-configs"
        mdb.set_setting(self.db, "smartlists_path", str(self.lists))
        p = mock.patch.object(gone_people, "HOME_CONFIG_DIR", self.homes)
        p.start()
        self.addCleanup(p.stop)
        for uid in (ADMIN, ANN, BEN):
            (self.lists / uid / "netflix").mkdir(parents=True)
            (self.lists / uid / "netflix" / "config.json").write_text("{}")
            (self.homes / "backups").mkdir(parents=True, exist_ok=True)
            (self.homes / f"{uid}.json").write_text("{}")
            (self.homes / "backups" / f"{uid}-20261021T120000000000.json").write_text("{}")
        for u in (self.admin, self.ann, self.ben):
            sub = mdb.ListSubscription(user_id=u.id, name="IMDb", type="imdb_rss", url="http://x", tag="IMDb")
            sub.items.append(mdb.ListItem(tmdb_id=1))
            self.db.add_all([
                sub,
                mdb.TagRule(user_id=u.id, name="Docs", output_tag="Docs"),
                mdb.HomeRowOrder(user_id=u.id, playlist_id="p1", display_order=1),
                mdb.AutoPlaylistToggle(user_id=u.id, key="source:Netflix:movies", enabled=True),
                mdb.Notification(user_id=u.id, tmdb_id=1, title="Dune", message="Ready"),
                mdb.DownloadRequest(user_id=u.id, tmdb_id=u.id),
                mdb.MediaRequest(user_id=u.id, kind="movie", tmdb_id=1, title="Dune"),
                mdb.MusicImport(user_id=u.id, name="Mix"),
                mdb.LiveUserLineup(jellyfin_user_id=u.jellyfin_user_id, own_list=True),
                mdb.LiveUserChannel(jellyfin_user_id=u.jellyfin_user_id, channel_key="c1"),
            ])
        self.db.commit()
        self.ann_id = self.ann.id

    def _counts(self, user_id, jf_id):
        per = [m for m in gone_people.PER_PERSON] + [mdb.ListSubscription]
        rows = sum(self.db.query(m).filter(m.user_id == user_id).count() for m in per)
        live = sum(self.db.query(m).filter(m.jellyfin_user_id == jf_id).count()
                   for m in (mdb.LiveUserLineup, mdb.LiveUserChannel))
        return rows, live

    def test_a_deleted_account_is_forgotten_everyone_else_kept(self):
        del self.jf.users[ANN]
        self.assertEqual(["Ann"], gone_people.forget_gone(self.db, self.jf.get("/Users")))
        self.assertIsNone(self.db.query(mdb.TentacleUser).filter_by(jellyfin_user_id=ANN).first())
        self.assertEqual((0, 0), self._counts(self.ann_id, ANN))
        self.assertEqual(2, self.db.query(mdb.ListItem).count())     # only Ann's list item went
        self.assertFalse((self.lists / ANN).exists())
        self.assertFalse((self.homes / f"{ANN}.json").exists())
        self.assertEqual([], list((self.homes / "backups").glob(f"{ANN}-*")))
        for u in (self.admin, self.ben):
            self.assertEqual((8, 2), self._counts(u.id, u.jellyfin_user_id))
            self.assertTrue((self.lists / u.jellyfin_user_id / "netflix" / "config.json").exists())
            self.assertTrue((self.homes / f"{u.jellyfin_user_id}.json").exists())
        log = self.db.query(mdb.ActivityLog).filter_by(event="person_removed").one()
        self.assertIn("Ann", log.message)

    def test_jellyfin_ids_with_dashes_count_as_present(self):
        self.jf.users[ANN]["Id"] = F.dashed(ANN)
        self.assertEqual([], gone_people.forget_gone(self.db, self.jf.get("/Users")))

    def test_an_empty_or_unreadable_list_forgets_nobody(self):
        for users in ([], None, [{"Name": "no id"}]):
            self.assertEqual([], gone_people.forget_gone(self.db, users))
        self.assertEqual(3, self.db.query(mdb.TentacleUser).count())

    def test_the_owner_and_the_account_tentacle_reads_as_are_never_forgotten(self):
        mdb.set_setting(self.db, "jellyfin_user_id", BEN)
        self.assertEqual([], gone_people.forget_gone(self.db, [{"Id": ANN}]))
        self.assertEqual(3, self.db.query(mdb.TentacleUser).count())

    def test_defaults_that_named_them_go_back_to_none(self):
        F.save_defaults(self.db, self.ann.id, self.ben.id)
        del self.jf.users[ANN]
        gone_people.forget_gone(self.db, self.jf.get("/Users"))
        self.assertEqual({"home_from": None, "channels_from": self.ben.id}, F.defaults(self.db))

    def test_caches_forget_them(self):
        from routers import auth
        auth._token_profiles[ANN] = {"name": "Ann", "is_admin": False}
        self.addCleanup(auth._token_profiles.pop, ANN, None)
        S.bump_home_version(self.ann_id)
        del self.jf.users[ANN]
        gone_people.forget_gone(self.db, self.jf.get("/Users"))
        self.assertNotIn(ANN, auth._token_profiles)
        self.assertNotIn(self.ann_id, S._home_versions)

    def test_people_page_forgets_whoever_jellyfin_no_longer_has(self):
        from routers import family
        from routers.auth import require_admin
        del self.jf.users[BEN]
        app = FastAPI()
        app.include_router(family.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[require_admin] = lambda: self.admin
        with mock.patch("services.family.start_new_person"), \
                mock.patch("services.tv_app.latest", return_value=None):
            d = TestClient(app).get("/api/family/people").json()
        self.assertEqual(["Ann", "Lucas"], [p["name"] for p in d["people"]])
        self.assertIsNone(self.db.query(mdb.TentacleUser).filter_by(jellyfin_user_id=BEN).first())


if __name__ == "__main__":
    unittest.main()
