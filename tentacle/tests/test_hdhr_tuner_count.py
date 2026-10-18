"""discover.json's TunerCount follows the stream ceiling.

Jellyfin takes TunerCount when it discovers the device and refuses a stream
beyond it ("HDHomeRun simultaneous stream limit has been reached"). Every
install was seeded with 3 while Tentacle carries 6 by default: five
continuous-TS channels at once held on a 1-connection account, and Jellyfin
would have refused the fourth.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tmp_dirs import temp_dir


class TunerCount(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

    def _set(self, key, value):
        from models.database import set_setting
        set_setting(self.db, key, value)
        self.db.commit()

    def _count(self):
        from routers import livetv
        return livetv._hdhr_tuner_count(self.db)

    def test_the_seeded_three_reports_the_default_ceiling(self):
        from routers import livetv
        self._set("hdhr_tuner_count", "3")
        self.assertEqual(livetv._DEFAULT_MAX_CONCURRENT_STREAMS, self._count())

    def test_a_raised_ceiling_raises_the_count(self):
        self._set("hdhr_tuner_count", "3")
        self._set("livetv_max_concurrent_streams", "10")
        self.assertEqual(10, self._count())

    def test_a_higher_tuner_count_wins_and_no_ceiling_keeps_it(self):
        self._set("hdhr_tuner_count", "8")
        self.assertEqual(8, self._count())
        self._set("hdhr_tuner_count", "4")
        self._set("livetv_max_concurrent_streams", "0")
        self.assertEqual(4, self._count())

    def test_garbage_keeps_a_working_count(self):
        self._set("hdhr_tuner_count", "lots")
        self._set("livetv_max_concurrent_streams", "0")
        self.assertEqual(3, self._count())

    def test_discover_json_reports_it(self):
        from types import SimpleNamespace
        from routers import livetv
        self._set("hdhr_base_url", "http://tentacle.test:8888")
        req = SimpleNamespace(headers={}, base_url="http://x/")
        self.assertEqual(livetv._DEFAULT_MAX_CONCURRENT_STREAMS,
                         livetv.hdhr_discover(req, self.db)["TunerCount"])


if __name__ == "__main__":
    unittest.main()
