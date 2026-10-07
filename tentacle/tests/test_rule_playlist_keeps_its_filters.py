"""A playlist rule "from a provider, plus filters" fills its playlist with
what the rule matches, not with the provider's whole catalogue.

The Create Playlist dialog offers "Content from: <provider>" plus genres,
rating and year. The tagger gives the rule's own tag only to titles that
pass every condition (services/tagger.apply_tag_rules). But the playlist
built for the rule queried the provider's source tag instead whenever the
rule had a provider condition: "<Provider> Movies" / "<Provider> TV"
(get_desired_smartlists, and sync_single_custom_playlist when the rule is
saved). So "Netflix, Comedy, rating over 7" became a playlist of every
Netflix film. Only a rule whose ONLY condition is the provider means
"the whole provider".

Run from tentacle/:  python -m unittest discover -s tests -p "test_rule_playlist_keeps_its_filters.py"
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


def _tags_queried(entry_or_config) -> list:
    """The tag values a playlist's query asks for (desired entry or config.json)."""
    if "ExpressionSets" in entry_or_config:
        exprs = entry_or_config["ExpressionSets"][0]["Expressions"]
    else:
        exprs = entry_or_config.get("expressions") or [
            {"MemberName": "Tags", "Operator": "Contains", "TargetValue": entry_or_config["tag"]}]
    return sorted(e["TargetValue"] for e in exprs if e.get("MemberName") == "Tags")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
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
    def test_provider_plus_genre_queries_the_rules_own_tag(self):
        self.rule("Netflix comedies", [NETFLIX, COMEDY])
        self.assertEqual(_tags_queried(self.desired("Netflix comedies")), ["Netflix comedies"])

    def test_provider_plus_rating_for_movies_and_shows_queries_the_rules_own_tag(self):
        self.rule("Good Netflix", [NETFLIX, RATING], apply_to="both")
        self.assertEqual(_tags_queried(self.desired("Good Netflix")), ["Good Netflix"])

    # Unchanged: a rule that is only "this provider" is the provider's catalogue.
    def test_provider_only_rule_still_queries_the_source_tag(self):
        self.rule("All Netflix", [NETFLIX])
        self.assertEqual(_tags_queried(self.desired("All Netflix")), ["Netflix Movies"])

    def test_provider_only_rule_for_both_types_still_queries_both_source_tags(self):
        self.rule("All Netflix", [NETFLIX], apply_to="both")
        self.assertEqual(_tags_queried(self.desired("All Netflix")), ["Netflix Movies", "Netflix TV"])

    def test_native_rule_is_unchanged(self):
        self.rule("Comedies", [COMEDY, RATING])
        entry = self.desired("Comedies")
        self.assertEqual(sorted(e["MemberName"] for e in entry["expressions"]),
                         ["CommunityRating", "Genres"])


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

    def test_provider_plus_genre_fills_with_the_rules_own_tag(self):
        filled, on_disk = self.save("Netflix comedies", [NETFLIX, COMEDY], "movies")
        self.assertEqual(_tags_queried(filled), ["Netflix comedies"])
        self.assertEqual(_tags_queried(on_disk), ["Netflix comedies"])

    def test_saving_and_the_full_sync_agree(self):
        for name, conds, apply_to in (("A", [NETFLIX], "movies"), ("B", [NETFLIX], "both"),
                                      ("C", [NETFLIX, COMEDY], "both")):
            with self.subTest(rule=name):
                filled, _ = self.save(name, conds, apply_to)
                self.assertEqual(_tags_queried(filled), _tags_queried(self.desired(name)))
                for p in Path(self.tmp, "smartlists").rglob("config.json"):
                    p.unlink()


if __name__ == "__main__":
    unittest.main()
