"""The Activity feed is stored and served: provider credentials that reach a
message through an exception's text (httpx puts the URL in it) are redacted,
like the log. Run from tentacle/: python -m unittest discover -s tests"""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir


class ActivityMessagesAreRedacted(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.mdb = mdb

    def test_an_epg_failure_with_the_guide_url_keeps_no_credentials(self):
        msg = ("EPG sync failed: All connection attempts failed for url "
               "'http://panel.test:8080/xmltv.php?username=acct-7f3a&password=pw-91c2'")
        self.mdb.log_activity(self.db, "livetv", msg, {"url": "http://panel.test/live/acct-7f3a/pw-91c2/1.ts", "n": 3})
        row = self.db.query(self.mdb.ActivityLog).one()
        self.assertNotIn("pw-91c2", row.message)
        self.assertNotIn("acct-7f3a&", row.message)
        self.assertNotIn("pw-91c2", str(row.detail))
        self.assertEqual(3, row.detail["n"])

    def test_ordinary_messages_are_unchanged(self):
        self.mdb.log_activity(self.db, "sync", "Synced 12 movies (3 new)")
        self.assertEqual("Synced 12 movies (3 new)", self.db.query(self.mdb.ActivityLog).one().message)


if __name__ == "__main__":
    unittest.main()
