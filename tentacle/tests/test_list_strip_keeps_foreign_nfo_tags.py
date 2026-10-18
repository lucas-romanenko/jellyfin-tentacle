"""A title leaving a list loses only that list's tag in its NFO, not Jellyfin's own tags.

#165 (6aa6170) made every NFO tag rewrite replace only Tentacle's own tags
(tentacle_owned_tags), so tags Jellyfin's NFO saver wrote (TMDB keywords, tags
added by hand) stay. The strip pass of apply_list_tags_to_library lost that
argument again when PR #221 was merged (a890dc7): when a title leaves a list,
update_nfo_tags(path, tags) without `owned` rewrites EVERY <tag> line, so the
NFO's foreign tags are deleted, and Jellyfin drops them on its next read.

No network. Run from tentacle/:
  python -m unittest discover -s tests -p "test_list_strip_keeps_foreign_nfo_tags.py"
"""
import os
import unittest
from unittest import mock

from test_imdb_partial_list import _fresh_db, _Resp
from tmp_dirs import temp_dir

NFO = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <title>M{n}</title>
  <uniqueid type="tmdb" default="true">{n}</uniqueid>
  <tag>cyberpunk</tag>
  <tag>Watched with Grandma</tag>
  <tag>Watchlist</tag>
</movie>
"""


def _trakt(*tmdb_ids):
    return [{"movie": {"title": f"M{i}", "ids": {"tmdb": i}}} for i in tmdb_ids]


class TestStripKeepsForeignNfoTags(unittest.TestCase):
    TAG = "Watchlist"

    def setUp(self):
        from models.database import ListSubscription, Movie, TentacleUser
        self.dir = temp_dir(self)
        self.db = _fresh_db()
        self.owner = TentacleUser(jellyfin_user_id="u-owner", display_name="owner")
        self.db.add(self.owner)
        self.db.commit()
        self.lst = ListSubscription(user_id=self.owner.id, name="Watchlist", type="trakt",
                                    tag=self.TAG, url="https://trakt.tv/users/someone/lists/w")
        self.db.add(self.lst)
        self.nfo = {}
        for n in (1, 2):
            p = os.path.join(self.dir, f"m{n}.nfo")
            with open(p, "w", encoding="utf-8") as f:
                f.write(NFO.format(n=n))
            self.nfo[n] = p
            self.db.add(Movie(tmdb_id=n, title=f"M{n}", source="provider_1",
                              tags=[self.TAG], nfo_path=p))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _refresh(self, answer):
        from routers import lists
        with mock.patch.object(lists.requests, "get", return_value=_Resp(200, answer)), \
             mock.patch.object(lists, "get_setting", return_value="cid"), \
             mock.patch.object(lists, "_get_tmdb_service", return_value=None), \
             mock.patch("services.smartlists._notify_jellyfin_plugin"):
            lists.fetch_list(self.lst.id, db=self.db, user=self.owner)

    def _tags(self, n):
        import re
        return re.findall(r"<tag>(.*?)</tag>", open(self.nfo[n], encoding="utf-8").read())

    def test_title_leaving_the_list_keeps_its_other_nfo_tags(self):
        self._refresh(_trakt(1))                      # M2 left the list
        self.assertNotIn(self.TAG, self._tags(2))     # the list's tag goes...
        self.assertEqual(sorted(self._tags(2)), ["Watched with Grandma", "cyberpunk"])  # ...nothing else

    def test_title_still_on_the_list_is_untouched(self):
        self._refresh(_trakt(1))
        self.assertEqual(sorted(self._tags(1)), ["Watched with Grandma", "Watchlist", "cyberpunk"])


if __name__ == "__main__":
    unittest.main()
