"""#252: a Discover → Music list added (or removed) while the lists are being
built must not be recorded as built. The build stamps the ids it actually
built, so the next ensure_fresh sees the change and builds again, instead of
waiting for the 30-day refresh.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import test_music_discover as base  # noqa: E402

R1, R2 = base.R1, base.R2


class TestListsChangedMidBuild(base._DiscoverBase):
    def _build_while(self, before, during):
        from models.database import set_setting
        from services.music import discover
        set_setting(self.db, "music_lists", before)

        def build_list(mb, sid):
            set_setting(self.db, "music_lists", during)   # the admin changes the lists now
            return {"id": sid, "name": sid, "built": 0, "albums": []}
        with mock.patch.object(discover, "build_list", side_effect=build_list), \
                mock.patch.object(discover.worker, "run_urgent_jobs"):
            return discover.build_lists(self.db, self.fmb)

    def test_a_list_added_during_the_build_is_built_next(self):
        from services.music import discover
        result = self._build_while(R1, f"{R1},{R2}")
        self.assertEqual(result["ids"], [R1])
        self.assertEqual([l["id"] for l in result["lists"]], [R1])
        discover._save(self.db, lists=result)
        discover.jobs["lists"] = False
        self.jobs.clear()
        discover.ensure_fresh(self.db)
        self.assertTrue(discover.jobs["lists"], "the new list must be queued for building")

    def test_a_list_removed_during_the_build_is_rebuilt_without_it(self):
        from services.music import discover
        result = self._build_while(f"{R1},{R2}", R2)
        self.assertEqual(result["ids"], [R1, R2])
        discover._save(self.db, lists=result)
        discover.jobs["lists"] = False
        discover.ensure_fresh(self.db)
        self.assertTrue(discover.jobs["lists"])

    def tearDown(self):
        from services.music import discover
        discover.jobs["lists"] = False


if __name__ == "__main__":
    unittest.main()
