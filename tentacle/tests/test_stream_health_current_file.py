"""Stream health judges the address the .strm holds now, not the one it held
when it was found dead (follow-up to 95a2b53, #263).

Run from the tentacle/ directory:  python -m unittest discover -s tests

A dead-stream entry records the file's address at the time. The sync can
repoint that file afterwards (#263: a film re-listed under a new stream id;
also new credentials, or VOD through Tentacle switched on or off). Recheck
probed the recorded address, so the entry stayed "dead" for good, "Check now"
answered "alive" but left the entry in place, and Remove deleted the .strm,
the .nfo and the library row of a film that plays.
"""
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Provider, StreamHealth
import services.stream_health as sh
from tmp_dirs import temp_dir

OLD = "http://provider/movie/u/p/102.mp4"
NEW = "http://provider/movie/u/p/1102.mp4"


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.db.add(Provider(id=1, name="P", server_url="http://provider", username="u", password="p",
                             provider_type="xtream"))
        folder = Path(tmp) / "The Matrix (1999)"
        folder.mkdir()
        self.strm = folder / "The Matrix (1999).strm"
        self.strm.write_text(OLD)
        folder.joinpath("The Matrix (1999).nfo").write_text("<movie/>")
        self.db.add(Movie(tmdb_id=603, title="The Matrix", source="provider_1", provider_id=1,
                          strm_path=str(self.strm)))
        self.db.add(StreamHealth(media_type="movie", tmdb_id=603, title="The Matrix", strm_path=str(self.strm),
                                 stream_url=OLD, fail_count=1,
                                 last_checked_at=datetime.utcnow() - timedelta(days=30)))
        self.db.commit()
        self.probed = []
        live = mock.patch.object(sh, "_live_streams_active", lambda: False)
        live.start()
        self.addCleanup(live.stop)

    def verdicts(self, table):
        """check_stream stand-in: `table` maps a stream id to True/False/None."""
        def check(db, media_type, kind, stream_id, url, provider):
            self.probed.append(stream_id)
            return table.get(stream_id)
        return mock.patch.object(sh, "check_stream", check)

    def entry(self):
        self.db.expire_all()
        return self.db.query(StreamHealth).first()

    def remove(self):
        out = sh.remove_dead_stream(self.db, self.entry().id, "admin")
        self.db.expire_all()
        return out


class RecheckProbesTheFile(_Base):
    def test_a_repointed_file_that_plays_is_cleared(self):
        self.strm.write_text(NEW)
        with self.verdicts({102: False, 1102: True}):
            out = sh.recheck_known_bad(self.db)
        self.assertEqual(self.probed, [1102])
        self.assertEqual(out["cleared"], ["The Matrix"])
        self.assertIsNone(self.entry())

    def test_a_blank_file_falls_back_to_the_recorded_address(self):
        self.strm.write_text("")
        with self.verdicts({102: False}):
            sh.recheck_known_bad(self.db)
        self.assertEqual(self.probed, [102])
        self.assertEqual(self.entry().stream_url, OLD)

    def test_dead_at_the_new_address_records_it(self):
        self.strm.write_text(NEW)
        with self.verdicts({1102: False}):
            sh.recheck_known_bad(self.db)
        e = self.entry()
        self.assertEqual((e.stream_url, e.fail_count), (NEW, 2))

    def test_an_inconclusive_answer_records_nothing(self):
        self.strm.write_text(NEW)
        with self.verdicts({1102: None}):
            sh.recheck_known_bad(self.db)
        e = self.entry()
        self.assertEqual((e.stream_url, e.fail_count), (OLD, 1))


class RemoveOnlyWhatWasFoundDead(_Base):
    def test_a_file_changed_since_is_not_deleted(self):
        self.strm.write_text(NEW)
        out = self.remove()
        self.assertFalse(out["ok"])
        self.assertIn("Recheck", out["error"])
        self.assertTrue(self.strm.exists())
        self.assertEqual(self.db.query(Movie).count(), 1)
        self.assertIsNotNone(self.entry())

    def test_the_file_found_dead_is_deleted_as_before(self):
        out = self.remove()
        self.assertTrue(out["ok"])
        self.assertFalse(self.strm.exists())
        self.assertEqual(self.db.query(Movie).count(), 0)

    def test_a_blank_file_is_deleted_as_before(self):
        self.strm.write_text("")
        self.assertTrue(self.remove()["ok"])
        self.assertFalse(self.strm.exists())

    def test_an_entry_without_a_recorded_address_is_deleted_as_before(self):
        self.entry().stream_url = None
        self.db.commit()
        self.strm.write_text(NEW)
        self.assertTrue(self.remove()["ok"])

    def test_after_a_dead_recheck_at_the_new_address_remove_works(self):
        self.strm.write_text(NEW)
        with self.verdicts({1102: False}):
            sh.recheck_known_bad(self.db)
        self.assertTrue(self.remove()["ok"])
        self.assertFalse(self.strm.exists())


class CheckNow(_Base):
    def test_alive_clears_the_entry(self):
        self.strm.write_text(NEW)
        with self.verdicts({1102: True}):
            out = sh.check_title(self.db, "movie", 603)
        self.assertEqual(out["result"], "alive")
        self.assertIsNone(self.entry())

    def test_dead_records_the_address_it_tested(self):
        self.strm.write_text(NEW)
        with self.verdicts({1102: False}):
            out = sh.check_title(self.db, "movie", 603)
        self.assertEqual(out["result"], "dead")
        self.assertEqual(self.entry().stream_url, NEW)

    def test_inconclusive_keeps_the_entry(self):
        self.strm.write_text(NEW)
        with self.verdicts({1102: None}):
            sh.check_title(self.db, "movie", 603)
        self.assertEqual(self.entry().stream_url, OLD)


class AfterTheSyncRepointsIt(unittest.TestCase):
    """End to end with the #263 repoint (tests/test_vod_relisted_streams.py harness)."""

    def test_recheck_clears_and_remove_refuses(self):
        from test_vod_relisted_streams import Relisted, FILLER
        from test_vod_namesakes import stream

        class T(Relisted):
            def runTest(inner):
                cat = lambda *s: {"a": list(s) + [stream(f"{n} (2010)", sid) for n, sid in FILLER]}
                inner.client.movies = cat(stream("The Matrix (1999)", 102, 603))
                inner.night()
                r = inner.row(603)
                inner.db.add(StreamHealth(media_type="movie", tmdb_id=603, title=r.title, strm_path=r.strm_path,
                                          stream_url=Path(r.strm_path).read_text(), fail_count=1,
                                          last_checked_at=datetime.utcnow() - timedelta(days=30)))
                inner.db.commit()
                inner.client.movies = cat(stream("The Matrix (1999)", 1102, 603))
                inner.night()
                self.assertTrue(Path(r.strm_path).read_text().endswith("/1102.mp4"))
                e = inner.db.query(StreamHealth).first()
                out = sh.remove_dead_stream(inner.db, e.id, "admin")
                self.assertFalse(out["ok"])
                self.assertTrue(Path(r.strm_path).exists())
                with mock.patch.object(sh, "check_stream", lambda db, mt, kind, sid, url, prov: sid == 1102), \
                        mock.patch.object(sh, "_live_streams_active", lambda: False):
                    sh.recheck_known_bad(inner.db)
                self.assertEqual(inner.db.query(StreamHealth).count(), 0)

        result = unittest.TestResult()
        T().run(result)
        self.assertEqual(result.failures + result.errors, [])


if __name__ == "__main__":
    unittest.main()
