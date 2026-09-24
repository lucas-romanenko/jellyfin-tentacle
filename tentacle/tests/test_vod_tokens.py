"""Signed, stable identifiers for provider VOD served through Tentacle.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class Tokens(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

    def test_secret_is_made_once_and_kept(self):
        from services import vod_tokens as vt
        a = vt.token_secret(self.db)
        self.assertEqual(64, len(a))
        self.assertEqual(a, vt.token_secret(self.db))

    def test_same_inputs_same_token_and_url_shape(self):
        from services import vod_tokens as vt
        u1 = vt.url("http://192.168.2.52:8888/", "s", 3, "movie", 2141622, "mkv")
        u2 = vt.url("http://192.168.2.52:8888", "s", 3, "movie", "2141622", ".MKV")
        self.assertEqual(u1, u2, "stable across syncs and spelling")
        self.assertRegex(u1, r"^http://192\.168\.2\.52:8888/api/vod/movie/3\.2141622\.[0-9a-f]{20}\.mkv$")

    def test_parse_and_verify(self):
        from services import vod_tokens as vt
        tail = vt.url("http://t", "sec", 3, "series", 8631, "mp4").rsplit("/", 1)[1]
        p = vt.parse("series", tail)
        self.assertEqual({"provider_id": 3, "stream_id": 8631, "container": "mp4"},
                         {k: p[k] for k in ("provider_id", "stream_id", "container")})
        self.assertTrue(vt.verify("sec", "series", p))
        self.assertFalse(vt.verify("other-secret", "series", p), "a different install's token")
        self.assertFalse(vt.verify("sec", "movie", p), "the kind is signed too")
        forged = dict(p, stream_id=8632)
        self.assertFalse(vt.verify("sec", "series", forged), "changing the id breaks the signature")
        self.assertFalse(vt.verify("sec", "series", dict(p, container="mkv")), "so does the container")

    def test_parse_rejects_other_shapes(self):
        from services import vod_tokens as vt
        for bad in ("2141622.mkv", "3.x.abc.mkv", "3.5.ABC.mkv", "../etc/passwd", ""):
            self.assertIsNone(vt.parse("movie", bad), bad)
        self.assertIsNone(vt.parse("live", "3.5.abcdef.ts"))

    def test_stream_id_is_readable_from_the_url(self):
        from services import vod_tokens as vt
        u = vt.url("http://t:8888", "s", 1, "movie", 42, "mp4")
        self.assertEqual(("movie", 42), vt.stream_id_in_url(u))
        self.assertEqual(("movie", 42), vt.stream_id_in_url(u + "?x=1"))
        self.assertIsNone(vt.stream_id_in_url("http://panel/movie/u/p/42.mp4"))
        self.assertTrue(vt.is_vod_url(u))


if __name__ == "__main__":
    unittest.main()
