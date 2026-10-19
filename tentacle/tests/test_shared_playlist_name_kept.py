"""A playlist that something else still makes survives a rule delete or a switch-off (#382).

Run from the tentacle/ directory:  python -m unittest discover -s tests

get_desired_smartlists() builds one name space per user and skips a rule
whose output tag is already taken by the same user's list on that tag ("one
playlist fed by both", services/tagger.tag_taken_by_another_user) or by a
built-in / source playlist. The fast paths acted by name without asking that:
deleting the rule (routers/tags.delete_rule) or switching the list's playlist
off (smartlists.toggle_auto_playlist_fast) deleted the playlist the other
producer still makes (the next full sync made a new one: new Jellyfin id,
default sort), and saving the rule (sync_single_custom_playlist) rewrote the
list's playlist with the rule's filters. The full sync's orphan pass was
already right: it removes only names no longer made.
"""
import json
import random
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import ListSubscription, TagRule, TentacleUser, AutoPlaylistToggle, set_setting
from tmp_dirs import temp_dir

YEAR = [{"field": "year", "operator": "greater_than", "value": "2000"}]


class FakeJF:
    deleted = []

    def __init__(self, *a, **k):
        pass

    def delete_tentacle_playlist(self, pid, uid=None):
        FakeJF.deleted.append(pid)
        return True


class SharedPlaylistNameKept(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.user = TentacleUser(jellyfin_user_id="u1", display_name="u", is_admin=True)
        self.other = TentacleUser(jellyfin_user_id="u2", display_name="v")
        self.db.add_all([self.user, self.other])
        self.db.commit()
        set_setting(self.db, "smartlists_path", f"{self.tmp}/smartlists")
        set_setting(self.db, "jellyfin_url", "http://jf.invalid")
        set_setting(self.db, "jellyfin_api_key", "k")
        FakeJF.deleted = []
        import services.smartlists as sl
        for target, value in ((sl, "write_home_config"), (sl, "_notify_jellyfin_plugin"),
                              (sl, "bump_playlist_version")):
            p = mock.patch.object(target, value, lambda *a, **k: {})
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("services.jellyfin.JellyfinService", FakeJF)
        p.start()
        self.addCleanup(p.stop)

    def folder(self, name, pid, user="u1", exprs=None):
        d = Path(self.tmp) / "smartlists" / user / f"folder-{pid}"
        d.mkdir(parents=True)
        cfg = {"Name": name, "Id": f"folder-{pid}", "UserPlaylists": [{"UserId": user, "JellyfinPlaylistId": pid}]}
        if exprs is not None:
            cfg["ExpressionSets"] = [{"Expressions": exprs}]
        (d / "config.json").write_text(json.dumps(cfg))
        return d

    def a_list(self, tag="Picks", on=True, user=None):
        lst = ListSubscription(name=tag, type="trakt", url="http://trakt.tv/x", tag=tag, active=True,
                               playlist_enabled=on, user_id=(user or self.user).id)
        self.db.add(lst)
        self.db.commit()
        return lst

    def a_rule(self, tag="Picks", user=None):
        rule = TagRule(name=tag, output_tag=tag, user_id=(user or self.user).id, active=True, conditions=YEAR)
        self.db.add(rule)
        self.db.commit()
        return rule

    def delete_rule(self, rule, user=None):
        import routers.tags as tags
        with mock.patch.object(tags, "JellyfinService", FakeJF, create=True):
            tags.delete_rule(rule.id, db=self.db, user=user or self.user)

    def switch_off(self, key, user=None):
        import services.smartlists as sl
        return sl.toggle_auto_playlist_fast(self.db, (user or self.user).id, key, False)

    def made(self, user=None):
        import services.smartlists as sl
        uid = (user or self.user).id
        if hasattr(sl, "playlist_names_still_made"):
            return sl.playlist_names_still_made(self.db, uid)
        # (before #382) the same definition, from the orphan sweep's parts
        return {s["name"] for s in sl.get_desired_smartlists(self.db, user_id=uid)} | sl._enabled_toggle_names(self.db, uid)

    # ── deleting a rule ───────────────────────────────────────────────────
    def test_deleting_a_rule_keeps_the_lists_playlist_on_the_same_tag(self):
        self.a_list()
        rule = self.a_rule()
        folder = self.folder("Picks", "PL-LIST")
        self.delete_rule(rule)
        self.assertIn("Picks", self.made())
        self.assertEqual(FakeJF.deleted, [], "the list's playlist was deleted with the rule")
        self.assertTrue(folder.exists(), "the list's SmartList folder (sort, ids) was removed")

    def test_deleting_a_rule_named_like_a_builtin_keeps_the_builtin(self):
        self.db.add(AutoPlaylistToggle(user_id=self.user.id, key="builtin:recently_added_movies", enabled=True))
        self.db.commit()
        rule = self.a_rule("Recently Added Movies")
        folder = self.folder("Recently Added Movies", "PL-BUILTIN")
        self.delete_rule(rule)
        self.assertEqual(FakeJF.deleted, [])
        self.assertTrue(folder.exists())

    def test_deleting_the_only_producer_still_deletes_its_playlist(self):
        rule = self.a_rule()
        folder = self.folder("Picks", "PL-RULE")
        self.delete_rule(rule)
        self.assertEqual(FakeJF.deleted, ["PL-RULE"])
        self.assertFalse(folder.exists())

    def test_another_users_list_does_not_keep_this_users_playlist_and_is_untouched(self):
        self.a_list(user=self.other)
        theirs = self.folder("Picks", "PL-THEIRS", user="u2")
        rule = self.a_rule()
        mine = self.folder("Picks", "PL-MINE")
        self.delete_rule(rule)
        self.assertEqual(FakeJF.deleted, ["PL-MINE"])
        self.assertFalse(mine.exists())
        self.assertTrue(theirs.exists())

    # ── switching a list's (or built-in's) playlist off ──────────────────
    def test_switching_the_list_off_keeps_the_playlist_the_rule_still_makes(self):
        lst = self.a_list(on=False)          # the route commits the switch first
        self.a_rule()
        folder = self.folder("Picks", "PL-SHARED")
        self.assertTrue(self.switch_off(f"list:{lst.id}")["success"])
        self.assertIn("Picks", self.made())
        self.assertEqual(FakeJF.deleted, [], "the rule's playlist was deleted with the list's switch")
        self.assertTrue(folder.exists())

    def test_switching_a_builtin_off_keeps_it_when_a_rule_has_its_name(self):
        self.db.add(AutoPlaylistToggle(user_id=self.user.id, key="builtin:downloaded_movies", enabled=False))
        self.db.commit()
        self.a_rule("Downloaded Movies")
        folder = self.folder("Downloaded Movies", "PL-DL")
        self.switch_off("builtin:downloaded_movies")
        self.assertEqual(FakeJF.deleted, [])
        self.assertTrue(folder.exists())

    def test_switching_the_only_producer_off_still_deletes(self):
        lst = self.a_list(on=False)
        folder = self.folder("Picks", "PL-LIST")
        self.switch_off(f"list:{lst.id}")
        self.assertEqual(FakeJF.deleted, ["PL-LIST"])
        self.assertFalse(folder.exists())
        self.db.add(AutoPlaylistToggle(user_id=self.user.id, key="builtin:recently_added_tv", enabled=False))
        self.db.commit()
        folder = self.folder("Recently Added TV", "PL-RATV")
        self.switch_off("builtin:recently_added_tv")
        self.assertEqual(FakeJF.deleted, ["PL-LIST", "PL-RATV"])

    # ── saving a rule ─────────────────────────────────────────────────────
    def _sync_one(self, name):
        import services.smartlists as sl
        filled = []
        with mock.patch.object(sl, "_process_single_playlist",
                               lambda jf, folder, config, *a, **k: filled.append(json.loads(json.dumps(config)))), \
             mock.patch("routers.collections.sync_playlist_artwork", lambda *a, **k: {}):
            r = sl.sync_single_custom_playlist(self.db, self.user.id, name, YEAR, "both", name)
        return r, filled

    def test_saving_a_rule_leaves_the_lists_playlist_definition_alone(self):
        self.a_list()
        self.a_rule()
        list_exprs = [{"MemberName": "Tags", "Operator": "Contains", "TargetValue": "Picks"}]
        folder = self.folder("Picks", "PL-LIST", exprs=list_exprs)
        before = (folder / "config.json").read_text()
        r, filled = self._sync_one("Picks")
        self.assertTrue(r["success"])
        self.assertEqual((folder / "config.json").read_text(), before,
                         "the list's playlist now holds the rule's year filter")
        self.assertEqual(filled[0]["ExpressionSets"][0]["Expressions"], list_exprs, "refilled as the list's")

    def test_saving_the_only_producer_writes_its_filters(self):
        self.a_rule()
        folder = self.folder("Picks", "PL-RULE",
                             exprs=[{"MemberName": "Tags", "Operator": "Contains", "TargetValue": "Old"}])
        r, filled = self._sync_one("Picks")
        cfg = json.loads((folder / "config.json").read_text())
        self.assertEqual(cfg["UserPlaylists"][0]["JellyfinPlaylistId"], "PL-RULE")
        self.assertNotIn("Old", json.dumps(cfg["ExpressionSets"]))
        self.assertEqual(len(filled), 1)

    # ── property ──────────────────────────────────────────────────────────
    def test_property_a_playlist_goes_only_when_nothing_of_the_user_makes_it(self):
        """1,000 seeds: two users, random lists / rules / built-in switches on
        a few shared names, existing SmartList folders for every name each
        user makes, then a random rule delete or switch-off. Invariants: a
        folder (and its Jellyfin playlist) is removed only for the acting user,
        only for the name acted on, and only when nothing of that user's still
        makes the name afterwards; then it is removed."""
        import shutil
        names = ["Picks", "Recently Added Movies", "Downloaded Movies", "Weekend"]
        builtin_key = {"Recently Added Movies": "builtin:recently_added_movies",
                       "Downloaded Movies": "builtin:downloaded_movies"}
        for seed in range(1000):
            rng = random.Random(seed)
            for model in (ListSubscription, TagRule, AutoPlaylistToggle):
                self.db.query(model).delete()
            self.db.commit()
            shutil.rmtree(Path(self.tmp) / "smartlists", ignore_errors=True)
            FakeJF.deleted = []
            users = (self.user, self.other)
            for u in users:
                for name in names:
                    if name in builtin_key and rng.random() < 0.5:
                        self.db.add(AutoPlaylistToggle(user_id=u.id, key=builtin_key[name], enabled=True))
                    if rng.random() < 0.4:
                        self.db.add(ListSubscription(name=name, type="trakt", url="http://trakt.tv/x", tag=name,
                                                     active=True, playlist_enabled=rng.random() < 0.7, user_id=u.id))
                    if rng.random() < 0.5:
                        self.db.add(TagRule(name=name, output_tag=name, user_id=u.id, active=True, conditions=YEAR))
            self.db.commit()
            folders = {}
            for u in users:
                for name in self.made(u):
                    folders[(u.jellyfin_user_id, name)] = self.folder(name, f"PL-{u.jellyfin_user_id}-{name}",
                                                                     user=u.jellyfin_user_id)
            actor = rng.choice(users)
            rules = self.db.query(TagRule).filter_by(user_id=actor.id).all()
            lists = self.db.query(ListSubscription).filter_by(user_id=actor.id, playlist_enabled=True).all()
            toggles = self.db.query(AutoPlaylistToggle).filter_by(user_id=actor.id, enabled=True).all()
            choices = [("rule", r) for r in rules] + [("list", l) for l in lists] + [("builtin", t) for t in toggles]
            if not choices:
                continue
            kind, obj = rng.choice(choices)
            if kind == "rule":
                name = obj.output_tag
                self.delete_rule(obj, user=actor)
            elif kind == "list":
                name = obj.tag
                obj.playlist_enabled = False
                self.db.commit()
                self.switch_off(f"list:{obj.id}", user=actor)
            else:
                name = next(n for n, k in builtin_key.items() if k == obj.key)
                obj.enabled = False
                self.db.commit()
                self.switch_off(obj.key, user=actor)
            still = self.made(actor)
            msg = f"seed {seed}: {actor.jellyfin_user_id} {kind} {name}"
            for (uid, n), d in folders.items():
                gone = (uid == actor.jellyfin_user_id and n == name and n not in still)
                self.assertEqual(not d.exists(), gone, f"{msg}: folder {uid}/{n}")
                self.assertEqual(f"PL-{uid}-{n}" in FakeJF.deleted, gone, f"{msg}: playlist {uid}/{n}")


if __name__ == "__main__":
    unittest.main()
