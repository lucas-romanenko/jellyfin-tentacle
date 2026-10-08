"""A playlist rule "from a provider, plus filters" fills its playlist with
what the rule matches, not with the provider's whole catalogue (#540).

The Create Playlist dialog offers "Content from: <provider>" plus genres,
rating and year. The playlist built for such a rule queried only the
provider's source tag ("<Provider> Movies" / "<Provider> TV") and dropped
every other condition (get_desired_smartlists, and
sync_single_custom_playlist when the rule is saved), so "Netflix, Comedy,
rating over 7" became a playlist of every Netflix film.

The playlist's query keeps every condition: the provider's tag(s) plus
Jellyfin's own fields for genre, rating and year, so it fills as soon as the
rule is saved. A provider with conditions Jellyfin can't query (list,
runtime, downloaded) queries the rule's own tag, which the tagger gives only
to titles that pass every condition. Saving the rule and the full sync
build the same query.

Run from tentacle/:  python tests/hermetic.py discover -s tests -p "test_rule_playlist_keeps_its_filters.py"
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import TagRule, TentacleUser, set_setting
from tmp_dirs import temp_dir

NETFLIX = {"field": "source", "operator": "equals", "value": "Netflix"}
COMEDY = {"field": "genre", "operator": "contains", "value": "Comedy", "genre_logic": "and"}
RATING = {"field": "rating", "operator": "greater_than", "value": "7"}
YEAR = {"field": "year", "operator": "greater_than", "value": "2015"}
RUNTIME = {"field": "runtime", "operator": "greater_than", "value": "90"}
ON_LIST = {"field": "list", "operator": "equals", "value": "Trending"}


def _query(entry_or_config) -> list:
    """What a playlist's query asks for (a desired entry or a config.json),
    as sorted (field, operator, value) triples."""
    if "ExpressionSets" in entry_or_config:
        exprs = entry_or_config["ExpressionSets"][0]["Expressions"]
    else:
        exprs = entry_or_config.get("expressions") or [
            {"MemberName": "Tags", "Operator": "Contains", "TargetValue": entry_or_config["tag"]}]
    return sorted((e["MemberName"], e["Operator"], e["TargetValue"]) for e in exprs)


def _tags(*values):
    return [("Tags", "Contains", v) for v in values]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.user = TentacleUser(jellyfin_user_id="u1", display_name="u", is_admin=True)
        self.db.add(self.user)
        self.db.commit()

    def rule(self, tag, conditions, apply_to="movies"):
        self.db.add(TagRule(name=tag, output_tag=tag, user_id=self.user.id, active=True,
                            apply_to=apply_to, conditions=conditions))
        self.db.commit()

    def desired(self, name):
        from services.smartlists import get_desired_smartlists
        return next(s for s in get_desired_smartlists(self.db, user_id=self.user.id) if s["name"] == name)


class TestFullSync(_Base):
    def test_provider_plus_genre_keeps_the_genre(self):
        self.rule("Netflix comedies", [NETFLIX, COMEDY])
        self.assertEqual(_query(self.desired("Netflix comedies")),
                         sorted(_tags("Netflix Movies") + [("Genres", "Contains", "Comedy")]))

    def test_provider_plus_rating_and_year_for_movies_and_shows_keeps_both(self):
        self.rule("Good Netflix", [NETFLIX, RATING, YEAR], apply_to="both")
        self.assertEqual(_query(self.desired("Good Netflix")),
                         sorted(_tags("Netflix Movies", "Netflix TV")
                                + [("CommunityRating", "GreaterThan", "7"),
                                   ("ProductionYear", "GreaterThan", "2015")]))

    def test_provider_plus_a_condition_jellyfin_cant_query_uses_the_rules_own_tag(self):
        for name, cond in (("Long Netflix", RUNTIME), ("Trending Netflix", ON_LIST)):
            with self.subTest(rule=name):
                self.rule(name, [NETFLIX, COMEDY, cond])
                self.assertEqual(_query(self.desired(name)), _tags(name))

    # Unchanged: a rule that is only "this provider" is the provider's catalogue.
    def test_provider_only_rule_still_queries_the_source_tag(self):
        self.rule("All Netflix", [NETFLIX])
        self.assertEqual(_query(self.desired("All Netflix")), _tags("Netflix Movies"))

    def test_provider_only_rule_for_both_types_still_queries_both_source_tags(self):
        self.rule("All Netflix", [NETFLIX], apply_to="both")
        self.assertEqual(_query(self.desired("All Netflix")), _tags("Netflix Movies", "Netflix TV"))

    def test_native_rule_is_unchanged(self):
        self.rule("Comedies", [COMEDY, RATING])
        self.assertEqual(_query(self.desired("Comedies")),
                         [("CommunityRating", "GreaterThan", "7"), ("Genres", "Contains", "Comedy")])


class TestSavingTheRule(_Base):
    """sync_single_custom_playlist(): the fast path when a rule is saved."""

    def setUp(self):
        super().setUp()
        set_setting(self.db, "smartlists_path", f"{self.tmp}/smartlists")
        set_setting(self.db, "jellyfin_url", "http://jf.invalid")
        set_setting(self.db, "jellyfin_api_key", "k")
        import services.smartlists as sl
        for name, value in (("write_home_config", {}), ("_notify_jellyfin_plugin", {}),
                            ("bump_playlist_version", None), ("_create_jellyfin_playlist", "PL1")):
            p = mock.patch.object(sl, name, lambda *a, _v=value, **k: _v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("services.jellyfin.JellyfinService", mock.MagicMock())
        p.start()
        self.addCleanup(p.stop)

    def save(self, name, conditions, apply_to):
        import services.smartlists as sl
        self.rule(name, conditions, apply_to)
        filled = []
        with mock.patch.object(sl, "_process_single_playlist",
                               lambda jf, folder, config, *a, **k: filled.append(json.loads(json.dumps(config)))), \
             mock.patch("routers.collections.sync_playlist_artwork", lambda *a, **k: {}):
            sl.sync_single_custom_playlist(self.db, self.user.id, name, conditions, apply_to, name)
        on_disk = [json.loads(p.read_text()) for p in Path(self.tmp, "smartlists").rglob("config.json")]
        self.assertEqual(len(on_disk), 1)
        return filled[0], on_disk[0]

    def test_provider_plus_genre_fills_with_the_genre(self):
        filled, on_disk = self.save("Netflix comedies", [NETFLIX, COMEDY], "movies")
        want = sorted(_tags("Netflix Movies") + [("Genres", "Contains", "Comedy")])
        self.assertEqual(_query(filled), want)
        self.assertEqual(_query(on_disk), want)

    def test_saving_and_the_full_sync_agree(self):
        for name, conds, apply_to in (("A", [NETFLIX], "movies"), ("B", [NETFLIX], "both"),
                                      ("C", [NETFLIX, COMEDY], "both"),
                                      ("D", [NETFLIX, RATING, RUNTIME], "series"),
                                      ("E", [COMEDY, YEAR], "both")):
            with self.subTest(rule=name):
                for p in Path(self.tmp, "smartlists").rglob("config.json"):
                    p.unlink()
                filled, _ = self.save(name, conds, apply_to)
                self.assertEqual(_query(filled), _query(self.desired(name)))


class TestPreviewCount(_Base):
    """The rule builder's "N items match" asks Jellyfin what the playlist will."""

    def setUp(self):
        super().setUp()
        import main
        from routers import auth
        set_setting(self.db, "jellyfin_url", "http://jf.invalid")
        set_setting(self.db, "jellyfin_api_key", "k")
        self.db.refresh(self.user)
        self.db.expunge(self.user)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Session = sessionmaker(bind=engine)

        def get_db():
            s = Session()
            try:
                yield s
            finally:
                s.close()
        self.app = main.app
        self.app.dependency_overrides[mdb.get_db] = get_db
        self.app.dependency_overrides[auth.get_user_from_request] = lambda: self.user
        self.addCleanup(self.app.dependency_overrides.clear)
        self.asked = []
        p = mock.patch("services.jellyfin.JellyfinService.query_items",
                       lambda svc, **kw: self.asked.append(kw) or [{"Id": "1"}, {"Id": "2"}])
        p.start()
        self.addCleanup(p.stop)

    def count(self, conditions, apply_to="movies"):
        from fastapi.testclient import TestClient
        r = TestClient(self.app).post("/api/smartlists/preview-count",
                                      json={"apply_to": apply_to, "conditions": conditions})
        self.assertEqual(200, r.status_code)
        return r.json()["count"]

    def test_provider_plus_genre_is_counted_with_both(self):
        self.assertEqual(2, self.count([NETFLIX, COMEDY]))
        self.assertEqual(["Netflix Movies"], self.asked[0]["tags"])
        self.assertEqual(["Comedy"], self.asked[0]["genres"])

    def test_a_condition_only_the_tagger_knows_is_not_previewed(self):
        self.assertEqual(-1, self.count([NETFLIX, ON_LIST]))
        self.assertEqual([], self.asked)


if __name__ == "__main__":
    unittest.main()
