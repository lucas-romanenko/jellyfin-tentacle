"""Backup & restore of the setup (#870, services/backup.py): a file of the
parts an admin picks, matched by name on restore, a preview before anything
is written, a provider of another name mapped or skipped, never a secret in
the file.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tmp_dirs import temp_dir

SECRET_PW = "hunter2-provider-pass"
SECRET_KEY = "radarr-key-0123456789abcdef"


def _db():
    import models.database as mdb
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{temp_dir()}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


ANN = "a" * 32
BEN = "b" * 32


def _install(db, provider="Acme", ids=0, setup=True):
    """An install: a provider with categories, groups and channels (provider
    ids shifted by `ids`, as after a renumbering), two people; with `setup`
    the admin's choices on top."""
    from models.database import (
        AutoPlaylistToggle, ListSubscription, LiveChannel, LiveChannelGroup, Provider, ProviderCategory,
        RecordingRule, RecordingRuleChannel, RecordingRuleTerm, TagRule, TentacleUser, set_setting,
    )
    p = Provider(name=provider, server_url="http://iptv.example:8080", username="acmeuser",
                 password=SECRET_PW, provider_type="xtream")
    ann = TentacleUser(jellyfin_user_id=ANN, display_name="Ann", is_admin=True)
    ben = TentacleUser(jellyfin_user_id=BEN, display_name="Ben")
    db.add_all([p, ann, ben])
    db.flush()
    for n, (name, typ) in enumerate([("Movies | 4K", "movie"), ("Netflix Movies", "movie"),
                                     ("Kids Series", "series"), ("Old Stuff", "movie")]):
        db.add(ProviderCategory(provider_id=p.id, category_id=str(100 + n + ids), category_name=name, type=typ,
                                whitelisted=setup and name != "Old Stuff",
                                source_tag="Netflix" if setup and name == "Netflix Movies" else None))
    for g in ("CA| Sports", "UK| News", "Music"):
        db.add(LiveChannelGroup(provider_id=p.id, name=g, enabled=setup and g != "Music"))
    chans = [("TSN 1", "CA| Sports"), ("TSN 2", "CA| Sports"), ("Sportsnet", "CA| Sports"),
             ("BBC News", "UK| News"), ("MTV", "Music")]
    for n, (name, group) in enumerate(chans):
        on = setup and group != "Music" and name != "TSN 2"
        db.add(LiveChannel(provider_id=p.id, name=name, group_title=group, stream_id=str(500 + n + ids),
                           stream_url=f"http://iptv.example/live/{n}", enabled=on,
                           custom_name="TSN" if setup and name == "TSN 1" else None,
                           channel_number=7 if setup and name == "BBC News" else None))
    set_setting(db, "radarr_api_key", SECRET_KEY)
    db.commit()
    if not setup:
        return p, ann, ben
    db.add_all([
        AutoPlaylistToggle(user_id=ann.id, key="source:Netflix:movies", enabled=True),
        AutoPlaylistToggle(user_id=ben.id, key="builtin:recently_added_movies", enabled=True),
        TagRule(user_id=ann.id, name="Short comedies", output_tag="Short comedies",
                conditions=[{"field": "genre", "operator": "contains", "value": "Comedy"}], apply_to="movies"),
        ListSubscription(user_id=ann.id, name="IMDb Top", type="imdb_rss", tag="IMDb Top",
                         url="https://www.imdb.com/chart/top/", playlist_enabled=True),
        ListSubscription(user_id=ben.id, name="Keyed", type="trakt", tag="Keyed",
                         url=f"https://trakt.tv/lists/1?api_key={SECRET_KEY}"),
    ])
    tsn = db.query(LiveChannel).filter(LiveChannel.name == "TSN 1").one()
    r = RecordingRule(name="Toronto Maple Leafs", kind="team", league="nhl", team="tor", mode="record",
                      owner_user_id=ben.id, keep_last=5)
    k = RecordingRule(name="Grand Prix", kind="keywords", mode="preview", owner_user_id=ann.id)
    db.add_all([r, k])
    db.flush()
    db.add_all([RecordingRuleChannel(rule_id=r.id, pool_key=f"c{tsn.id}"),
                RecordingRuleTerm(rule_id=k.id, term="Grand Prix"),
                RecordingRuleTerm(rule_id=k.id, term="Qualifying", exclude=True)])
    db.commit()
    return p, ann, ben


HOME_ANN = {"hero": {"enabled": True, "playlist_id": "p-net", "display_name": "Netflix Movies", "sort_by": "random"},
            "rows": [{"type": "builtin", "section_id": "resumevideo", "display_name": "Continue Watching",
                      "order": 1, "shape": "wide"},
                     {"type": "playlist", "playlist_id": "p-net", "display_name": "Netflix Movies", "order": 2},
                     {"type": "playlist", "playlist_id": "p-imdb", "display_name": "IMDb Top", "order": 3,
                      "unresolved_since": "2026-10-01T00:00:00"}],
            "merge_continue_watching": True}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(temp_dir(self))
        env = mock.patch.dict(os.environ, {"DATA_DIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.homes = {}

        def home_path(db, user_id=None):
            from models.database import TentacleUser
            u = db.get(TentacleUser, user_id)
            d = self.root / f"homes-{id(db)}"
            d.mkdir(exist_ok=True)
            return d / f"{u.jellyfin_user_id}.json"
        self.follow_ups = []
        self.pool_changes = []
        for target, value in (
                ("services.smartlists._user_home_config_path", home_path),
                ("services.backup._follow_up_soon", self.follow_ups.append),
                ("services.backup._pool_names", lambda db: {}),
                ("routers.livetv._pool_changed", self.pool_changes.append),
                ("routers.recordings._changed", lambda db: None)):
            pt = mock.patch(target, value)
            pt.start()
            self.addCleanup(pt.stop)

    def write_home(self, db, user, home):
        from services.smartlists import _user_home_config_path
        _user_home_config_path(db, user.id).write_text(json.dumps(home))

    def read_home(self, db, user):
        from services.smartlists import _user_home_config_path
        path = _user_home_config_path(db, user.id)
        return json.loads(path.read_text()) if path.exists() else None

    def source(self):
        db = _db()
        self.addCleanup(db.close)
        p, ann, ben = _install(db)
        self.write_home(db, ann, HOME_ANN)
        self.write_home(db, ben, {"follows_default": True, "rows": []})
        return db


def comparable(data):
    """The setup a file describes, without what differs between installs
    (ids, dates)."""
    out = json.loads(json.dumps(data))
    out.pop("made_at", None)
    for p in out.get("providers") or []:
        p.pop("id", None)
        for k in ("vod_categories", "live_groups", "live_channels"):
            for row in p.get(k) or []:
                row.pop("id", None)
    for r in out.get("recording_rules") or []:
        for c in r.get("channels") or []:
            c.pop("key", None)
    return out


class Export(Base):
    def test_file_has_each_part_by_name_and_no_secret(self):
        from services import backup as B
        db = self.source()
        data = B.export(db)
        text = json.dumps(data)
        for secret in (SECRET_PW, SECRET_KEY, "acmeuser", "iptv.example"):
            self.assertNotIn(secret, text)
        self.assertEqual(["list Keyed (its address carries a key)"], data["left_out"])
        acme = data["providers"][0]
        self.assertEqual("Acme", acme["name"])
        self.assertEqual({"Movies | 4K", "Netflix Movies", "Kids Series"},
                         {c["name"] for c in acme["vod_categories"]})
        self.assertEqual({"CA| Sports", "UK| News"}, {g["name"] for g in acme["live_groups"]})
        # TSN 2 is off inside a group that is on: kept, so a restore keeps it off
        self.assertEqual({("TSN 1", True), ("TSN 2", False), ("Sportsnet", True), ("BBC News", True)},
                         {(c["name"], c["enabled"]) for c in acme["live_channels"]})
        self.assertEqual("TSN", next(c for c in acme["live_channels"] if c["name"] == "TSN 1")["custom_name"])
        homes = {h["name"]: h for h in data["home_rows"]["people"]}
        self.assertTrue(homes["Ben"]["follows_default"])
        self.assertNotIn("unresolved_since", json.dumps(homes["Ann"]))
        rules = {r["name"]: r for r in data["recording_rules"]}
        self.assertEqual("Ben", rules["Toronto Maple Leafs"]["owner"]["name"])
        self.assertEqual("TSN", rules["Toronto Maple Leafs"]["channels"][0]["name"])
        self.assertEqual((False, 5), (rules["Toronto Maple Leafs"]["keep_forever"], rules["Toronto Maple Leafs"]["keep_last"]))
        self.assertEqual((["Grand Prix"], ["Qualifying"]), (rules["Grand Prix"]["terms"], rules["Grand Prix"]["excludes"]))

    def test_only_the_parts_and_people_picked(self):
        from services import backup as B
        db = self.source()
        data = B.export(db, parts=["home_rows", "lists"], people=["Ann"])
        self.assertEqual(["home_rows", "lists"], data["parts"])
        self.assertNotIn("providers", data)
        self.assertNotIn("recording_rules", data)
        self.assertEqual(["Ann"], [h["name"] for h in data["home_rows"]["people"]])
        self.assertEqual(["Ann"], [p["name"] for p in data["lists"]["people"]])

    def test_snapshots_keep_the_last_14(self):
        from services import backup as B
        db = self.source()
        os.makedirs(B.backup_dir())
        for n in range(16):
            Path(B.backup_dir(), f"tentacle-setup-202610{n + 1:02d}-023000-daily.json").write_text(
                json.dumps(B.export(db, parts=["lists"])))
        Path(B.backup_dir(), "notes.txt").write_text("mine")
        made = B.snapshot(db, "now")
        names = [s["name"] for s in B.snapshots()]
        self.assertEqual(14, len(names))
        self.assertEqual(made["name"], names[0])
        self.assertTrue(made["name"].endswith("-now.json"))
        self.assertTrue(Path(B.backup_dir(), "notes.txt").exists())
        self.assertNotIn(SECRET_PW, Path(B.backup_dir(), made["name"]).read_text())
        with self.assertRaises(FileNotFoundError):
            B.read_snapshot("../t.db")


class Restore(Base):
    def target(self, provider="Acme", ids=1000):
        db = _db()
        self.addCleanup(db.close)
        _install(db, provider=provider, ids=ids, setup=False)
        return db

    def test_round_trip_into_an_empty_install(self):
        from services import backup as B
        src = self.source()
        data = B.export(src)
        dst = self.target()
        before = B.export(dst)
        # the preview writes nothing
        pv = B.plan(dst, data)
        self.assertEqual(comparable(before), comparable(B.export(dst)))
        self.assertEqual(3, pv["parts"]["vod_categories"]["added"])
        self.assertEqual([], pv["parts"]["live_channels"]["not_found"])
        self.assertEqual(2, pv["parts"]["recording_rules"]["added"])

        done = B.plan(dst, data, apply=True)
        self.assertTrue(done["snapshot"].endswith("-before-restore.json"))
        # the preview said what the restore did
        counts = ("added", "changed", "unchanged", "removed", "not_found")
        self.assertEqual({p: [pv["parts"][p][k] for k in counts] for p in B.PART_IDS},
                         {p: [done["parts"][p][k] for k in counts] for p in B.PART_IDS})
        self.assertEqual(3, done["parts"]["live_channels"]["added"])   # TSN 1, Sportsnet, BBC News
        after = B.export(dst)
        self.assertEqual(comparable(data)["providers"], comparable(after)["providers"])
        self.assertEqual(comparable(data)["playlists"], comparable(after)["playlists"])
        self.assertEqual(comparable(data)["lists"], comparable(after)["lists"])
        self.assertEqual(comparable(data)["recording_rules"], comparable(after)["recording_rules"])
        from models.database import TentacleUser
        ann = dst.query(TentacleUser).filter_by(display_name="Ann").one()
        self.assertEqual(["Continue Watching", "Netflix Movies", "IMDb Top"],
                         [r["display_name"] for r in self.read_home(dst, ann)["rows"]])
        self.assertEqual("Netflix Movies", self.read_home(dst, ann)["hero"]["display_name"])
        # the rest follows in the background: playlists built, homes written
        self.assertEqual(1, len(self.follow_ups))
        self.assertIn(ann.id, self.follow_ups[0]["users"])
        self.assertEqual(1, len(self.follow_ups[0]["to_default"]))
        self.assertEqual(["setup restored"], self.pool_changes)
        # again: nothing left to do
        again = B.plan(dst, data)
        self.assertEqual([0] * 6, [again["parts"][p]["added"] + again["parts"][p]["changed"] for p in B.PART_IDS])

    def test_only_the_parts_picked(self):
        from models.database import ProviderCategory, RecordingRule
        from services import backup as B
        data = B.export(self.source())
        dst = self.target()
        out = B.plan(dst, data, parts=["recording_rules"], apply=True)
        self.assertEqual(["recording_rules"], list(out["parts"]))
        self.assertEqual(2, dst.query(RecordingRule).count())
        self.assertEqual(0, dst.query(ProviderCategory).filter_by(whitelisted=True).count())
        self.assertEqual([], self.pool_changes)

    def test_two_playlists_of_one_name_restore_as_two(self):
        from models.database import TagRule, TentacleUser
        from services import backup as B
        src = self.source()
        ann = src.query(TentacleUser).filter_by(display_name="Ann").one()
        src.add(TagRule(user_id=ann.id, name="short comedies", output_tag="Short 2", conditions=[]))
        src.commit()
        data = B.export(src)
        dst = self.target()
        B.plan(dst, data, parts=["playlists"], apply=True)
        self.assertEqual(["Short 2", "Short comedies"], sorted(r.output_tag for r in dst.query(TagRule)))
        again = B.plan(dst, data, parts=["playlists"])["parts"]["playlists"]
        self.assertEqual((0, 0), (again["added"], again["changed"]))

    def test_one_persons_rows(self):
        from models.database import TentacleUser
        from services import backup as B
        data = B.export(self.source())
        dst = self.target()
        B.plan(dst, data, parts=["home_rows"], people=["Ann"], apply=True)
        ann = dst.query(TentacleUser).filter_by(display_name="Ann").one()
        ben = dst.query(TentacleUser).filter_by(display_name="Ben").one()
        self.assertIsNotNone(self.read_home(dst, ann))
        self.assertIsNone(self.read_home(dst, ben))

    def test_a_provider_of_another_name_is_mapped_by_the_admin(self):
        from models.database import LiveChannel, Provider, ProviderCategory
        from services import backup as B
        data = B.export(self.source())
        dst = self.target(provider="Zeta TV")
        zeta = dst.query(Provider).one()
        pv = B.plan(dst, data)
        acme = pv["providers"][0]
        self.assertIsNone(acme["target"])                 # not by itself
        cand = acme["candidates"][0]
        self.assertEqual((3, 3, 2, 2, 3, 3), (cand["vod_matched"], cand["vod_total"], cand["groups_matched"],
                                              cand["groups_total"], cand["channels_matched"], cand["channels_total"]))
        out = B.plan(dst, data, providers={"Acme": zeta.id}, apply=True)
        self.assertEqual(3, out["parts"]["vod_categories"]["added"])
        self.assertEqual(3, dst.query(ProviderCategory).filter_by(whitelisted=True).count())
        self.assertEqual("TSN", dst.query(LiveChannel).filter_by(name="TSN 1").one().custom_name)

    def test_a_skipped_provider_leaves_its_parts_and_the_rest_restores(self):
        from models.database import LiveChannel, ProviderCategory, RecordingRule, TagRule
        from services import backup as B
        data = B.export(self.source())
        dst = self.target(provider="Zeta TV")
        out = B.plan(dst, data, providers={"Acme": None}, apply=True)
        self.assertIn("Acme: skipped (3 categories)", out["parts"]["vod_categories"]["notes"])
        self.assertEqual(0, dst.query(ProviderCategory).filter_by(whitelisted=True).count())
        self.assertEqual(0, dst.query(LiveChannel).filter_by(enabled=True).count())
        self.assertEqual(1, dst.query(TagRule).count())
        # the team's channel isn't here: it waits in preview instead of
        # recording on every channel
        leafs = dst.query(RecordingRule).filter_by(kind="team").one()
        self.assertEqual("preview", leafs.mode)

    def test_merge_adds_and_replace_also_removes(self):
        from models.database import ProviderCategory, TagRule, TentacleUser
        from services import backup as B
        data = B.export(self.source())
        dst = self.target()
        old = dst.query(ProviderCategory).filter_by(category_name="Old Stuff").one()
        old.whitelisted = True
        ann = dst.query(TentacleUser).filter_by(display_name="Ann").one()
        dst.add(TagRule(user_id=ann.id, name="Mine", output_tag="Mine", conditions=[]))
        dst.commit()
        B.plan(dst, data, apply=True)
        self.assertTrue(dst.query(ProviderCategory).filter_by(category_name="Old Stuff").one().whitelisted)
        self.assertEqual(2, dst.query(TagRule).count())
        pv = B.plan(dst, data, replace=["vod_categories", "playlists"])
        self.assertEqual((1, 1), (pv["parts"]["vod_categories"]["removed"], pv["parts"]["playlists"]["removed"]))
        B.plan(dst, data, replace=["vod_categories", "playlists"], apply=True)
        self.assertFalse(dst.query(ProviderCategory).filter_by(category_name="Old Stuff").one().whitelisted)
        self.assertEqual(["Short comedies"], [r.name for r in dst.query(TagRule)])

    def test_merge_keeps_someones_own_home_and_adds_the_missing_rows(self):
        from models.database import TentacleUser
        from services import backup as B
        data = B.export(self.source())
        dst = self.target()
        ann = dst.query(TentacleUser).filter_by(display_name="Ann").one()
        mine = {"hero": {"enabled": False}, "rows": [
            {"type": "playlist", "playlist_id": "x", "display_name": "My Own", "order": 1},
            {"type": "playlist", "playlist_id": "y", "display_name": "Netflix Movies", "order": 2}]}
        self.write_home(dst, ann, mine)
        B.plan(dst, data, parts=["home_rows"], apply=True)
        self.assertEqual(["My Own", "Netflix Movies", "Continue Watching", "IMDb Top"],
                         [r["display_name"] for r in self.read_home(dst, ann)["rows"]])
        B.plan(dst, data, parts=["home_rows"], replace=["home_rows"], apply=True)
        self.assertEqual(["Continue Watching", "Netflix Movies", "IMDb Top"],
                         [r["display_name"] for r in self.read_home(dst, ann)["rows"]])

    def test_someone_not_here_is_reported(self):
        from models.database import TentacleUser
        from services import backup as B
        data = B.export(self.source())
        dst = self.target()
        dst.query(TentacleUser).filter_by(display_name="Ben").delete()
        dst.commit()
        pv = B.plan(dst, data)
        self.assertEqual(["Ben"], pv["parts"]["home_rows"]["not_found"])
        self.assertIn("Toronto Maple Leafs: Ben isn't here; the rule is the household's",
                      pv["parts"]["recording_rules"]["notes"])

    def test_not_a_backup(self):
        from services import backup as B
        with self.assertRaises(ValueError):
            B.check_file({"rows": []})
        with self.assertRaises(ValueError):
            B.check_file({"format": B.FORMAT, "version": B.VERSION + 1})


class Api(Base):
    def test_routes_are_admin_only(self):
        from routers import backup as R
        from routers.auth import require_admin
        self.assertIn(require_admin, [d.dependency for d in R.router.dependencies])

    def test_export_download_and_restore_from_a_snapshot(self):
        from routers import backup as R
        from services import backup as B
        db = self.source()
        resp = R.export_file(parts="lists,recording_rules", people=None, providers=None, db=db)
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertEqual(["lists", "recording_rules"], json.loads(resp.body)["parts"])
        snap = R.back_up_now(db=db)
        pv = R.preview(R.RestoreBody(snapshot=snap["name"]), db=db)
        self.assertFalse(pv["applied"])
        self.assertEqual(set(B.PART_IDS), set(pv["parts"]))
        with self.assertRaises(R.HTTPException) as e:
            R.preview(R.RestoreBody(backup={"hello": 1}), db=db)
        self.assertEqual(400, e.exception.status_code)


if __name__ == "__main__":
    unittest.main()
