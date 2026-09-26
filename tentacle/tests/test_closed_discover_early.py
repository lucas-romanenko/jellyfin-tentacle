"""Regression cover for the original Discover reports, runnable on the code before the fix.

#2  the image proxy rejected artworks.thetvdb.com when the client handed the url back
    still percent-encoded (plugin: every TVDB artwork refused and logged per image)
#5  a title Jellyfin has but Tentacle's tables don't was shown as addable
#10 a VOD (.strm) title said "In Library" but the detail payload had no id or link

test_discover_library.py and test_discover_image_proxy.py pin these too, but their
fixtures patch caches and helpers that 3f2d866 introduced, so they cannot run on
3f2d866^. These use the same fakes with patches that tolerate the older module, so
they fail on 3f2d866^ and pass from 3f2d866 on.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import hashlib
import re
import socket
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from test_discover_library import (  # noqa: E402  (also runs the web stubs)
    JF_URL, FakeJellyfin, FakeTMDB, movie, Base, Series, Setting, mdb, discover, jellyfin,
)
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

TVDB = "https://artworks.thetvdb.com/banners/posters/262638-1.jpg"
PLUGIN = Path("../tentacle-plugin/Api/DiscoverController.cs")


def _patch_if(obj, name, value):
    """mock.patch.object that also works when the attribute does not exist yet."""
    return mock.patch.object(obj, name, value, create=not hasattr(obj, name))


class _DetailBase(unittest.TestCase):
    user_id = "adminuser"

    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        db = self.Session()
        db.add_all([Setting(key="jellyfin_url", value=JF_URL), Setting(key="jellyfin_api_key", value="k"),
                    Setting(key="jellyfin_user_id", value=self.user_id)])
        db.commit()
        db.close()
        patches = [
            mock.patch.object(mdb, "SessionLocal", self.Session),
            _patch_if(discover, "_arr_ids_cache", {"data": {"movie": set(), "series": set()}, "ts": 1e18}),
            mock.patch.object(discover, "_get_tmdb", lambda db: FakeTMDB()),
            _patch_if(jellyfin.JellyfinService, "get_server_id", lambda svc: "srv1"),
        ]
        if hasattr(discover, "_jf_ids_cache"):
            cache = {k: (dict(v) if isinstance(v, dict) else v) for k, v in discover._jf_ids_cache.items()}
            cache.update(movie=None, series=None, ts={"movie": 0, "series": 0})
            if "ok" in cache:
                cache["ok"] = {"movie": False, "series": False}
            patches.append(mock.patch.object(discover, "_jf_ids_cache", cache))
        if hasattr(discover, "_jf_server_id_cache"):
            c = dict(discover._jf_server_id_cache)
            c.update({k: v for k, v in (("id", None), ("checked", False), ("retry_at", 0.0)) if k in c or k == "id"})
            patches.append(mock.patch.object(discover, "_jf_server_id_cache", c))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def use_jellyfin(self, fake):
        p = mock.patch.object(jellyfin.JellyfinService, "_get",
                              lambda svc, path, params=None: fake.get(svc, path, params))
        p.start()
        self.addCleanup(p.stop)

    def detail(self, media_type, tmdb_id):
        db = self.Session()
        try:
            return discover.get_discover_detail(media_type, tmdb_id, request=None, db=db)
        finally:
            db.close()


class TestIssue5OwnedInJellyfinIsInLibrary(_DetailBase):
    def test_title_only_jellyfin_has_is_in_library(self):
        self.use_jellyfin(FakeJellyfin({"Movie": [movie(603692, "dl1")]}))
        d = self.detail("movie", 603692)
        self.assertTrue(d.get("in_library"), f"#5: a title Jellyfin owns is offered for adding: {d}")


class TestIssue10VodTitleIsReachable(_DetailBase):
    def test_vod_series_gets_an_item_id_and_a_link(self):
        db = self.Session()
        db.add(Series(tmdb_id=95350, title="Lanterns", source="provider_1"))
        db.commit()
        db.close()
        self.use_jellyfin(FakeJellyfin({"Series": [
            {"Id": "891a3459", "Name": "Lanterns", "ProviderIds": {"Tmdb": "95350"}}]}))
        d = self.detail("series", 95350)
        self.assertTrue(d.get("in_library"))
        self.assertEqual(d.get("jellyfin_item_id"), "891a3459", f"#10: no way to open the title: {sorted(d)}")
        self.assertIn("891a3459", d.get("jellyfin_url") or "")


class _FakeAsyncClient:
    fetched = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        _FakeAsyncClient.fetched.append(url)
        return types.SimpleNamespace(status_code=200, content=b"jpeg", headers={"content-type": "image/jpeg"})


class TestIssue2BackendAcceptsEncodedTvdbUrl(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _FakeAsyncClient.fetched = []
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        for p in (mock.patch.object(discover, "TVDB_PROXY_CACHE", Path(tmp.name)),
                  mock.patch.object(discover.httpx, "AsyncClient", _FakeAsyncClient),
                  mock.patch("socket.getaddrinfo", lambda *a, **k: public)):  # no DNS in tests
            p.start()
            self.addCleanup(p.stop)

    def test_double_encoded_tvdb_url_is_fetched(self):
        encoded = quote(TVDB, safe="")
        key = hashlib.md5(TVDB.encode()).hexdigest()
        try:
            asyncio.run(discover.image_proxy(key, url=encoded))
        except discover.HTTPException as e:
            self.fail(f"#2: an encoded artworks.thetvdb.com url was refused ({e.status_code})")
        self.assertEqual(_FakeAsyncClient.fetched, [TVDB])

    def test_non_tvdb_host_is_still_refused_after_decoding(self):
        bad = quote("http://192.0.2.10/x.jpg", safe="")
        with self.assertRaises(discover.HTTPException):
            asyncio.run(discover.image_proxy(hashlib.md5(b"http://192.0.2.10/x.jpg").hexdigest(), url=bad))
        self.assertEqual(_FakeAsyncClient.fetched, [])


class TestIssue2PluginDecodesBeforeTheHostCheck(unittest.TestCase):
    """There is no C# test host in the repo; this reads the controller source."""

    def setUp(self):
        src = PLUGIN.read_text(encoding="utf-8")
        start = src.index("public async Task<ActionResult> ImageProxy(")
        self.body = src[start:src.index("\n    }\n", start)]
        self.src = src

    def test_url_is_decoded_before_the_allowlist_check(self):
        check = self.body.index("IsAllowedImageHost(url)")
        before = self.body[:check]
        m = re.search(r"url\s*=\s*(\w+)\(url\)", before)
        self.assertTrue(m, "#2: ImageProxy validates the raw query value; an encoded TVDB url is refused")
        definition = re.search(r"static\s+string\??\s+" + m.group(1) + r"\(", self.src)
        self.assertTrue(definition, f"no definition of {m.group(1)}")
        self.assertIn("UnescapeDataString", self.src[definition.start():definition.start() + 1500])

    def test_rejections_are_not_logged_once_per_image(self):
        self.assertNotRegex(self.body, r"_logger\.LogWarning\([^;]*disallowed host[^;]*\{Url\}",
                            "#2: every rejected image is logged with its full url")


if __name__ == "__main__":
    unittest.main()
