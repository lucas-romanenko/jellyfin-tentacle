"""candidate_base_urls() and IPv6 (#77).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Every candidate must be a URL that parses, with the port Tentacle actually
listens on. Covers a bare and a bracketed IPv6 Host / X-Forwarded-Host, and an
IPv6 jellyfin_url — urlparse().hostname drops the brackets, so the "Jellyfin's
host on Tentacle's port" guess, ranked first, came out unparseable.
"""
import tempfile
import unittest
from urllib.parse import urlparse

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _db(jellyfin_url):
    import models.database as mdb
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
    mdb.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    mdb.set_setting(db, "jellyfin_url", jellyfin_url)
    db.commit()
    return db


def _ports(urls):
    return [urlparse(u).port for u in urls]   # raises on an unparseable authority


class CandidateBaseUrlsIPv6(unittest.TestCase):
    def setUp(self):
        from services.youtube import sync
        self.sync = sync

    def test_bare_ipv6_host_does_not_invent_a_port(self):
        db = _db("http://192.168.2.52:8096")
        c = self.sync.candidate_base_urls(db, "fd7a:115c:a1e0:1234:5678:90ab:cdef:1234", "http")
        self.assertIn("http://192.168.2.52:8888", c)
        self.assertNotIn("http://192.168.2.52:1234", c)
        self.assertIn("http://[fd7a:115c:a1e0:1234:5678:90ab:cdef:1234]", c)
        _ports(c)

    def test_loopback_bare_ipv6_is_not_port_1(self):
        db = _db("http://192.168.2.52:8096")
        c = self.sync.candidate_base_urls(db, "::1", "http")
        self.assertNotIn("http://192.168.2.52:1", c)

    def test_bracketed_ipv6_host_keeps_its_port(self):
        db = _db("http://192.168.2.52:8096")
        c = self.sync.candidate_base_urls(db, "[fd7a:115c:a1e0::1]:9999", "http")
        self.assertIn("http://192.168.2.52:9999", c)
        self.assertIn("http://[fd7a:115c:a1e0::1]:9999", c)

    def test_ipv6_jellyfin_url_gives_a_parseable_first_guess(self):
        db = _db("http://[fd7a:115c:a1e0::1]:8096")
        c = self.sync.candidate_base_urls(db, "192.168.2.10:8888", "http")
        self.assertEqual("http://[fd7a:115c:a1e0::1]:8888", c[0])
        self.assertEqual(8888, _ports(c)[0])

    def test_ipv4_behaviour_is_unchanged(self):
        db = _db("http://192.168.2.52:8096")
        self.assertEqual(self.sync.candidate_base_urls(db, "192.168.2.10:9999", "http"),
                         ["http://192.168.2.52:9999", "http://192.168.2.52:8888",
                          "http://192.168.2.10:9999"])


if __name__ == "__main__":
    unittest.main()
