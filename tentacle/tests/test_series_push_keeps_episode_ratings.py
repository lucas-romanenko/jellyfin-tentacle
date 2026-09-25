"""A tag push to a Series must not overwrite its episodes' own ratings.

Jellyfin 10.11.8 ItemUpdateController.UpdateItem copies a Series' (or
Season's) OfficialRating (unless the child locked it) and CustomRating
(always) onto every season and episode on EVERY update. A tag push therefore
rated a TV-MA episode as the series (TV-14): a TV-14 profile could play it.
On 755ea67 a DisplayOrder mismatch in the body also queued a ReplaceAll
refresh that re-read episode NFOs and hid this for NFO-rated episodes; with
DisplayOrder echoed (7444a25) the flattening stuck. set_item_tags now puts
each child's own ratings back after the push.

The fake below implements the cascade exactly as the 10.11.8 source does.
Run from tentacle/:  python -m unittest discover -s tests -p "test_series_push_keeps_episode_ratings.py"
"""
import unittest


class _FakeJellyfin:
    """Items + the UpdateItem cascade of Jellyfin 10.11.8."""

    def __init__(self):
        self.items = {
            "s1": {"Id": "s1", "Type": "Series", "Name": "Show", "Tags": ["Netflix TV"],
                   "OfficialRating": "TV-14", "CustomRating": "TV-KIDS", "DisplayOrder": "absolute"},
            "se1": {"Id": "se1", "Type": "Season", "ParentId": "s1", "IndexNumber": 1,
                    "OfficialRating": "TV-14", "CustomRating": "TV-KIDS", "Tags": []},
            "e1": {"Id": "e1", "Type": "Episode", "ParentId": "se1", "IndexNumber": 1,
                   "ParentIndexNumber": 1, "OfficialRating": "TV-PG", "CustomRating": "EP1", "Tags": []},
            "e2": {"Id": "e2", "Type": "Episode", "ParentId": "se1", "IndexNumber": 2,
                   "ParentIndexNumber": 1, "OfficialRating": "TV-MA", "CustomRating": None, "Tags": [],
                   "LockedFields": []},
            "e3": {"Id": "e3", "Type": "Episode", "ParentId": "se1", "IndexNumber": 3,
                   "ParentIndexNumber": 1, "OfficialRating": "TV-Y", "CustomRating": None, "Tags": [],
                   "LockedFields": ["OfficialRating"]},
        }
        self.posts = []
        self.gets = []
        self.list_fails = False
        self.down = False
        self.fail_posts = set()
        self.always_fail = set()
        self.before_series_post = None
        self.after_series_post = None

    def children(self, pid, kinds):
        out = []
        for it in self.items.values():
            p = it.get("ParentId")
            while p and p != pid:
                p = self.items[p].get("ParentId")
            if p == pid and it["Type"] in kinds:
                out.append(it)
        return out

    def _dto(self, it):
        d = dict(it)
        if d["Type"] == "Episode":
            d["SeasonId"] = d["ParentId"]
        return d

    def get(self, path, params=None):
        self.gets.append((path, dict(params or {})))
        if path == "/Items" and "Ids" in (params or {}):
            if self.down:
                return None
            it = self.items.get(params["Ids"])
            return {"Items": [self._dto(it)] if it else [], "TotalRecordCount": 1 if it else 0}
        if path == "/Items":
            if self.list_fails == "http":
                import requests
                raise requests.HTTPError("500 Server Error")
            if self.list_fails or self.down:
                return None
            kids = self.children(params["ParentId"], params["IncludeItemTypes"].split(","))
            page = kids[params["StartIndex"]:params["StartIndex"] + params["Limit"]]
            return {"Items": [self._dto(k) for k in page], "TotalRecordCount": len(kids)}
        return dict(self.items[path.rsplit("/", 1)[-1]])

    def post(self, item_id, body):
        if item_id in self.always_fail:
            import requests
            raise requests.HTTPError("500 Server Error")
        if item_id in self.fail_posts:
            self.fail_posts.discard(item_id)
            import requests
            raise requests.HTTPError("500 Server Error")
        if self.before_series_post and self.items[item_id]["Type"] == "Series":
            self.before_series_post()
        self.posts.append(item_id)
        it = self.items[item_id]
        official = (body.get("OfficialRating") or "").strip() or None
        custom = body.get("CustomRating")
        it.update(Tags=body.get("Tags", []), OfficialRating=official, CustomRating=custom)
        if it["Type"] in ("Series", "Season"):
            for child in self.children(item_id, ("Season", "Episode")):
                if "OfficialRating" not in (child.get("LockedFields") or []):
                    child["OfficialRating"] = official
                child["CustomRating"] = custom
        if self.after_series_post and it["Type"] == "Series":
            self.after_series_post()


def _service(fake):
    from unittest import mock
    from services.jellyfin import JellyfinService
    jf = JellyfinService("http://jf.invalid:8096", "k", "u1")
    jf._get = fake.get
    jf.session = mock.Mock()
    jf.session.post.side_effect = lambda url, json=None, timeout=None: (
        fake.post(url.rsplit("/", 1)[-1], json), mock.Mock(status_code=204, text=""))[1]
    jf._post = lambda path, data=None: fake.post(path.rsplit("/", 1)[-1], data) or True
    return jf


class _PendingDir(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from unittest import mock
        self.data_dir = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"DATA_DIR": self.data_dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pending(self):
        from services.jellyfin import _pending_restores_load
        return _pending_restores_load()


class TestSeriesPush(_PendingDir):
    @staticmethod
    def _effective(fake, iid, field):
        """Jellyfin's *ForComparison: an empty rating inherits the display parent's."""
        while iid:
            v = fake.items[iid].get(field)
            if v:
                return v
            iid = fake.items[iid].get("ParentId")
        return None

    def test_episode_and_season_ratings_survive_a_tag_push(self):
        fake = _FakeJellyfin()
        own = {k: (v["OfficialRating"], v["CustomRating"]) for k, v in fake.items.items()}
        eff = {k: tuple(self._effective(fake, k, f) for f in ("OfficialRating", "CustomRating"))
               for k in fake.items}
        self.assertTrue(_service(fake).set_item_tags("s1", ["Netflix TV", "Watchlist"]))
        for k, (o, c) in own.items():
            with self.subTest(item=k):
                # every rating an item had of its own is kept exactly
                if o:
                    self.assertEqual(fake.items[k]["OfficialRating"], o)
                if c:
                    self.assertEqual(fake.items[k]["CustomRating"], c)
                # and what parental control compares is unchanged for all
                self.assertEqual(tuple(self._effective(fake, k, f)
                                       for f in ("OfficialRating", "CustomRating")), eff[k])
        self.assertEqual(fake.items["s1"]["Tags"], ["Netflix TV", "Watchlist"])

    def test_no_extra_writes_when_nothing_differs(self):
        fake = _FakeJellyfin()
        for k in ("se1", "e1", "e2", "e3"):
            fake.items[k].update(OfficialRating="TV-14", CustomRating="TV-KIDS")
        _service(fake).set_item_tags("s1", ["x"])
        self.assertEqual(fake.posts, ["s1"])

    def test_unrated_children_are_not_rewritten(self):
        """The usual library: episodes carry no rating and inherit the series'."""
        fake = _FakeJellyfin()
        for k in ("se1", "e1", "e2"):
            fake.items[k].update(OfficialRating=None, CustomRating=None)
        _service(fake).set_item_tags("s1", ["x"])
        self.assertEqual(fake.posts, ["s1"])

    def test_an_episode_rated_stricter_than_its_series_keeps_it(self):
        fake = _FakeJellyfin()
        for k in ("se1", "e1"):
            fake.items[k].update(OfficialRating=None, CustomRating=None)
        _service(fake).set_item_tags("s1", ["x"])
        self.assertEqual(fake.items["e2"]["OfficialRating"], "TV-MA")
        self.assertEqual(fake.posts, ["s1", "e2"])

    def test_a_series_whose_children_cannot_be_listed_still_gets_its_tags(self):
        """Blocking it would keep it out of its playlists for good (round-2 review)."""
        for how in (True, "http"):
            with self.subTest(listing_fails=how):
                fake = _FakeJellyfin()
                fake.list_fails = how
                with self.assertLogs("services.jellyfin", level="WARNING"):
                    self.assertTrue(_service(fake).set_item_tags("s1", ["Netflix TV", "x"]))
                self.assertEqual(fake.items["s1"]["Tags"], ["Netflix TV", "x"])

    def test_an_episode_rated_like_the_series_survives_its_seasons_restore(self):
        """Restoring a season cascades its rating to its episodes; an episode whose
        own rating equals the series' (so the series cascade left it alone) must
        not end up with the season's."""
        fake = _FakeJellyfin()
        fake.items["se1"].update(OfficialRating="TV-Y7", CustomRating=None)
        fake.items["e1"].update(OfficialRating="TV-14", CustomRating="TV-KIDS")
        _service(fake).set_item_tags("s1", ["x"])
        self.assertEqual(fake.items["se1"]["OfficialRating"], "TV-Y7")
        self.assertEqual((fake.items["e1"]["OfficialRating"], fake.items["e1"]["CustomRating"]),
                         ("TV-14", "TV-KIDS"))
        self.assertEqual(fake.items["e2"]["OfficialRating"], "TV-MA")

    def test_the_child_listing_is_not_user_scoped(self):
        fake = _FakeJellyfin()
        _service(fake).set_item_tags("s1", ["x"])
        listings = [p for path, p in fake.gets if path == "/Items"]
        self.assertTrue(listings)
        self.assertFalse(any("UserId" in p for p in listings))

    def test_a_movie_push_reads_no_children(self):
        fake = _FakeJellyfin()
        fake.items["m1"] = {"Id": "m1", "Type": "Movie", "Name": "M", "Tags": [], "OfficialRating": "R"}
        fake.list_fails = True           # would make a series push fail
        self.assertTrue(_service(fake).set_item_tags("m1", ["x"]))


class TestAFailedRestoreIsRetried(_PendingDir):
    def _own(self, fake):
        return {k: (v["OfficialRating"], v["CustomRating"]) for k, v in fake.items.items()
                if k in ("se1", "e1", "e2")}

    def test_one_failing_child_does_not_stop_the_others_and_is_retried(self):
        fake = _FakeJellyfin()
        want = self._own(fake)
        fake.fail_posts = {"e1"}                       # a 500 on one episode's restore
        jf = _service(fake)
        with self.assertLogs("services.jellyfin", level="WARNING"):
            self.assertTrue(jf.set_item_tags("s1", ["Netflix TV", "Watchlist"]))
        self.assertEqual(fake.items["e2"]["OfficialRating"], "TV-MA")   # the rest went on
        self.assertIn("e1", self._pending()["s1"]["children"])
        # The tags are right now, so no later push rewrites the series; the
        # retry alone has to put e1 back — from the saved values.
        self.assertEqual(jf.retry_pending_rating_restores(), 0)
        self.assertEqual(self._own(fake), want)
        self.assertEqual(self._pending(), {})

    def test_jellyfin_going_away_after_the_series_update_is_retried(self):
        fake = _FakeJellyfin()
        want = self._own(fake)
        jf = _service(fake)
        real_post = fake.post

        def post_then_down(item_id, body):
            real_post(item_id, body)
            fake.down = True                           # every later request fails
        fake.post = post_then_down
        with self.assertLogs("services.jellyfin", level="WARNING"):
            jf.set_item_tags("s1", ["Netflix TV", "Watchlist"])
        self.assertEqual(fake.items["e2"]["OfficialRating"], "TV-14")  # flattened for now
        fake.post, fake.down = real_post, False
        jf.retry_pending_rating_restores()
        self.assertEqual(self._own(fake), want)

    def test_a_later_push_uses_the_saved_values_not_the_flattened_ones(self):
        fake = _FakeJellyfin()
        fake.fail_posts = {"e2"}
        jf = _service(fake)
        with self.assertLogs("services.jellyfin", level="WARNING"):
            jf.set_item_tags("s1", ["Netflix TV", "Watchlist"])
        self.assertEqual(fake.items["e2"]["OfficialRating"], "TV-14")
        jf.set_item_tags("s1", ["Netflix TV", "Watchlist", "Other"])
        self.assertEqual(fake.items["e2"]["OfficialRating"], "TV-MA")
        self.assertEqual(self._pending(), {})

    def test_the_push_pipeline_retries_pending_restores(self):
        from unittest import mock
        import services.jellyfin as j
        jf = mock.Mock()
        jf.retry_pending_rating_restores.return_value = 0
        j._pending_restores_set("s1", {"e1": ["Episode", "TV-MA", None]})
        j._retry_pending_rating_restores(jf, "test")
        jf.retry_pending_rating_restores.assert_called_once()


def _with_second_season(fake, season_rating, ep_official, ep_custom=None):
    fake.items["se2"] = {"Id": "se2", "Type": "Season", "ParentId": "s1", "IndexNumber": 2,
                         "OfficialRating": season_rating, "CustomRating": None, "Tags": []}
    fake.items["e4"] = {"Id": "e4", "Type": "Episode", "ParentId": "se2", "IndexNumber": 1,
                        "ParentIndexNumber": 2, "OfficialRating": ep_official,
                        "CustomRating": ep_custom, "Tags": []}
    return fake


class TestRound3(_PendingDir):
    """Round-3 review: a failed SEASON restore, the race, the pending file."""

    def test_a_failed_season_restore_is_retried_without_flattening_its_episodes(self):
        for season_rating, ep in (("TV-PG", ("TV-MA", None)), ("TV-MA", ("TV-PG", "TV-MA"))):
            with self.subTest(season=season_rating, episode=ep):
                fake = _with_second_season(_FakeJellyfin(), season_rating, *ep)
                fake.fail_posts = {"se2"}
                jf = _service(fake)
                with self.assertLogs("services.jellyfin", level="WARNING"):
                    jf.set_item_tags("s1", ["Netflix TV", "Watchlist"])
                self.assertIn("e4", self._pending()["s1"]["children"])     # kept while se2 is pending
                self.assertEqual(jf.retry_pending_rating_restores(), 0)
                self.assertEqual(fake.items["se2"]["OfficialRating"], season_rating)
                self.assertEqual((fake.items["e4"]["OfficialRating"], fake.items["e4"]["CustomRating"]), ep)
                self.assertEqual(self._pending(), {})

    def test_a_rating_changed_while_pending_is_not_overwritten(self):
        fake = _FakeJellyfin()
        fake.fail_posts = {"e2"}
        jf = _service(fake)
        with self.assertLogs("services.jellyfin", level="WARNING"):
            jf.set_item_tags("s1", ["Netflix TV", "Watchlist"])
        fake.items["e2"]["OfficialRating"] = "TV-Y"                    # someone set it by hand
        jf.retry_pending_rating_restores()
        self.assertEqual(fake.items["e2"]["OfficialRating"], "TV-Y")
        self.assertEqual(self._pending(), {})

    def test_retries_are_capped(self):
        import services.jellyfin as j
        fake = _FakeJellyfin()
        fake.always_fail = {"e2"}
        jf = _service(fake)
        with self.assertLogs("services.jellyfin", level="WARNING"):
            jf.set_item_tags("s1", ["Netflix TV", "Watchlist"])
        with self.assertLogs("services.jellyfin", level="WARNING") as logs:
            for _ in range(j.PENDING_MAX_ATTEMPTS):
                jf.retry_pending_rating_restores()
        errors = [r for r in logs.records if r.levelname == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Giving up", errors[0].getMessage())
        self.assertEqual(self._pending(), {})

    def test_a_corrupt_pending_file_is_kept_aside_not_dropped(self):
        import os
        import services.jellyfin as j
        path = j._pending_restores_path()
        path.write_text('{"s1": {"children": {"e2": {"type": "Episode", "offic', encoding="utf-8")
        with self.assertLogs("services.jellyfin", level="WARNING"):
            self.assertEqual(j._pending_restores_load(), {})
        self.assertTrue(any(n.startswith(path.name + ".corrupt-") for n in os.listdir(self.data_dir)))

    def test_a_wrong_shaped_file_does_not_break_pushes(self):
        import services.jellyfin as j
        path = j._pending_restores_path()
        for bad in ('[1, 2]', '"text"', '{"s1": 5}', '{"s1": {"children": {"e1": 7}}}'):
            with self.subTest(content=bad):
                path.write_text(bad, encoding="utf-8")
                fake = _FakeJellyfin()
                with self.assertLogs("services.jellyfin", level="WARNING"):
                    self.assertTrue(_service(fake).set_item_tags("s1", ["Netflix TV", "x"]))
                self.assertEqual(fake.items["e2"]["OfficialRating"], "TV-MA")

    def test_two_pushes_of_one_series_at_once_do_not_flatten_it(self):
        """Refresh Tags beside the nightly push, forced into the reviewer's order:
        B reads the pending file before A saves it; A saves, updates the series;
        B lists the children now (it sees A's copies, so it needs nothing); A
        restores; B updates the series — flattening everything with nothing
        pending. With the lock B runs wholly before or after A."""
        import threading
        import services.jellyfin as j
        fake = _FakeJellyfin()
        b_read, a_updated, b_listed, a_done = (threading.Event() for _ in range(4))
        real_load, real_get = j._pending_restores_load, fake.get

        def load():
            out = real_load()
            if threading.current_thread().name == "B":
                b_read.set()
            return out

        def get(path, params=None):
            if path == "/Items" and "ParentId" in (params or {}) and threading.current_thread().name == "B":
                a_updated.wait(1.0)
                out = real_get(path, params)
                b_listed.set()
                return out
            return real_get(path, params)
        fake.get = get

        def after():
            if threading.current_thread().name == "A":
                a_updated.set()
                b_listed.wait(1.0)
        fake.after_series_post = after

        def before():
            if threading.current_thread().name == "B":
                a_done.wait(1.0)
        fake.before_series_post = before

        jf_a, jf_b = _service(fake), _service(fake)

        def run_a():
            b_read.wait(1.0)
            jf_a.set_item_tags("s1", ["Netflix TV", "A"])
            a_done.set()
        from unittest import mock
        with mock.patch.object(j, "_pending_restores_load", load):
            tb = threading.Thread(target=jf_b.set_item_tags, args=("s1", ["Netflix TV", "A", "B"]), name="B")
            ta = threading.Thread(target=run_a, name="A")
            tb.start()
            ta.start()
            ta.join(10)
            tb.join(10)
        self.assertEqual(fake.items["e2"]["OfficialRating"], "TV-MA")
        self.assertEqual(fake.items["e1"]["OfficialRating"], "TV-PG")
        self.assertEqual(self._pending(), {})


class TestEpisodeNumberingIsEchoed(unittest.TestCase):
    def test_restoring_an_episode_keeps_its_numbering(self):
        from services.jellyfin import _item_update_payload
        body = _item_update_payload({"Id": "e1", "IndexNumber": 3, "ParentIndexNumber": 1,
                                     "AirsBeforeSeasonNumber": 2, "AirsBeforeEpisodeNumber": 1,
                                     "AirsAfterSeasonNumber": 1, "Album": "A"})
        for f in ("IndexNumber", "ParentIndexNumber", "AirsBeforeSeasonNumber",
                  "AirsBeforeEpisodeNumber", "AirsAfterSeasonNumber", "Album"):
            self.assertIn(f, body)


if __name__ == "__main__":
    unittest.main()
