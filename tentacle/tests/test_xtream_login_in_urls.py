"""An Xtream username or password with a character that means something in
a URL (# & + % ? / a space, ...) reaches the panel as typed, in the
player_api.php query and in every stream path; a login without one gives
exactly the URLs, and .strm files, it always did.

Panels let resellers choose their own passwords, so "Ab#12&cd" is a working
account in other players. Every Xtream URL builder put the raw login in an
f-string: a '#' made the rest a fragment (the panel got password=Ab and no
action: the Test button, the category list, the VOD sync and Live TV all said
the login was refused), '&' split the value, '+' arrived as a space, and in
/movie/<user>/<pass>/<id>.mp4 a '/', '?' or '#' cut the path, so the .strm
files and the live channels did not play.

A fake panel on 127.0.0.1 (tests/hermetic.py allows loopback) reads the query
the way PHP does ('+' is a space, '&' splits, a fragment is never sent) and
the path segment by segment, each segment decoded on its own, and accepts
only the real login. (A panel that decodes the whole path before it splits it
reads an encoded '/' as a separator again, so there a '/' in the login still
does not play from a stream path, as before.)

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import os
import random
import string
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir

# (username, password) pairs a panel accepts and the old URLs broke
LOGINS = [
    ("u", "Ab#12&cd"),
    ("u", "Ab+12"),
    ("john+tv@example.com", "Ab/12?cd"),
    ("u s", "p%41 é'\"x"),
]
STREAM = {"stream_id": 7, "name": "Film (2026)", "category_id": "1", "container_extension": "mp4"}
CATS = [{"category_id": "1", "category_name": "Movies"}]
SAFE = string.ascii_letters + string.digits + "-._~!$()*,;=:@"
SEEDS = int(os.environ.get("XTREAM_LOGIN_PROPERTY_SEEDS", "2000"))


def _db():
    import models.database as mdb
    engine = create_engine(f"sqlite:///{temp_dir()}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class _Panel(BaseHTTPRequestHandler):
    login = ("", "")
    seen = []

    def log_message(self, *a):
        pass

    def _send(self, status, body: bytes, ctype="application/octet-stream"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).seen.append(self.path)
        parts = urlsplit(self.path)
        segs = parts.path.split("/")
        if segs[1:2] in (["live"], ["movie"], ["series"]):
            # /<kind>/<user>/<pass>/<id>.<ext>, each segment decoded on its own
            ok = len(segs) == 5 and (unquote(segs[2]), unquote(segs[3])) == type(self).login
            return self._send(200 if ok else 401, b"VIDEO" if ok else b"")
        q = parse_qs(parts.query, keep_blank_values=True)   # as PHP: '+' is a space
        ok = (q.get("username"), q.get("password")) == ([type(self).login[0]], [type(self).login[1]])
        if parts.path == "/xmltv.php" or parts.path.endswith(".ts"):
            return self._send(200 if ok else 401, b"<tv/>" if ok else b"")
        action = (q.get("action") or [None])[0]
        if not ok:
            out = {"user_info": {"auth": 0}}
        elif action is None:
            out = {"user_info": {"auth": 1, "status": "Active", "max_connections": "1"}, "server_info": {}}
        elif action in ("get_vod_streams", "get_series"):
            out = [STREAM] if action == "get_vod_streams" else []
        elif action in ("get_vod_categories", "get_series_categories", "get_live_categories"):
            out = CATS
        else:
            out = []
        self._send(200, json.dumps(out).encode(), "application/json")


class TheLoginReachesThePanel(unittest.TestCase):
    """Every builder, against a panel that accepts only the real login."""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Panel)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def _use(self, user, password):
        _Panel.login = (user, password)
        _Panel.seen = []

    def _provider(self, user, password, **kw):
        from models.database import Provider
        return Provider(name="P", server_url=self.base, username=user, password=password,
                        provider_type="xtream", **kw)

    def _plays(self, url):
        r = requests.get(url, timeout=5)
        self.assertEqual(200, r.status_code, f"the panel refused {_Panel.seen[-1:]}")

    def test_vod_sync(self):
        import services.sync as sync
        for user, password in LOGINS:
            with self.subTest(login=(user, password)):
                self._use(user, password)
                self.assertEqual([STREAM], sync.XtreamClient(self._provider(user, password)).get_vod_streams("1"))

    def test_the_test_button_and_the_category_list(self):
        from routers import providers
        for user, password in LOGINS:
            with self.subTest(login=(user, password)):
                self._use(user, password)
                p = self._provider(user, password)
                self.assertEqual(1, providers.test_provider_connection(p)["user_info"]["auth"])
                self.assertTrue(providers.fetch_provider_categories(p)[0])

    def test_the_test_route_and_the_sync_preview(self):
        from models.database import ProviderCategory
        from routers import providers
        for user, password in LOGINS:
            with self.subTest(login=(user, password)):
                self._use(user, password)
                db = _db()
                self.addCleanup(db.close)
                p = self._provider(user, password)
                db.add(p)
                db.commit()
                db.add(ProviderCategory(provider_id=p.id, category_id="1", category_name="Movies",
                                        type="movie", whitelisted=True))
                db.commit()
                providers.test_provider(p.id, db)
                self.assertTrue(p.has_vod and p.has_live, "the capability probes were refused")
                self.assertEqual(1, providers.preview_sync(p.id, db)["estimated_movies"])

    def test_live_tv_client_and_guide(self):
        from services.xtream_client import XtreamClient
        for user, password in LOGINS:
            with self.subTest(login=(user, password)):
                self._use(user, password)
                c = XtreamClient(self.base, user, password)
                self.assertEqual(1, c.authenticate()["user_info"]["auth"])
                self.assertEqual(CATS, c.get_live_categories())
                self._plays(c.get_xmltv_url())

    def test_stream_urls_play(self):
        import services.sync as sync
        from services.m3u_parser import xtream_streams_to_m3u
        from services.xtream_client import XtreamClient
        for user, password in LOGINS:
            with self.subTest(login=(user, password)):
                self._use(user, password)
                vod = sync.XtreamClient(self._provider(user, password))
                live = XtreamClient(self.base, user, password)
                m3u = xtream_streams_to_m3u([{"stream_id": 3}], {}, self.base, user, password)
                for url in (vod.movie_stream_url(7, "mp4"), vod.episode_stream_url(8, "mkv"),
                            live.live_stream_url(3, "ts"), live.movie_stream_url(7), live.series_stream_url(8),
                            m3u.splitlines()[-1]):
                    self._plays(url)

    def test_a_provider_edit_writes_the_channel_urls_a_sync_writes(self):
        """#469: saving a new login rewrites the Live TV channels' URLs; they
        must be encoded the same way as a channel sync writes them."""
        from models.database import LiveChannel
        from routers.livetv import _xtream_login, rewrite_xtream_channel_urls
        from services.xtream_client import XtreamClient
        for user, password in LOGINS:
            with self.subTest(login=(user, password)):
                self._use(user, password)
                db = _db()
                self.addCleanup(db.close)
                p = self._provider("old", "old")
                db.add(p)
                db.commit()
                db.add(LiveChannel(provider_id=p.id, name="C", stream_id="3", enabled=True,
                                   stream_url=XtreamClient(self.base, "old", "old").live_stream_url(3, "ts")))
                db.commit()
                before = _xtream_login(p)
                p.username, p.password = user, password
                self.assertEqual(1, rewrite_xtream_channel_urls(p, before, db))
                url = db.query(LiveChannel).one().stream_url
                self.assertEqual(XtreamClient(self.base, user, password).live_stream_url(3, "ts"), url)
                self._plays(url)
                # Saved again unchanged: nothing to rewrite
                self.assertEqual(0, rewrite_xtream_channel_urls(p, ("", "", ""), db))

    def test_health_sweep_and_vod_through_tentacle(self):
        from routers import vod as vod_router
        from services import vod_tokens
        from services.stream_health import _direct_url
        for user, password in LOGINS:
            with self.subTest(login=(user, password)):
                self._use(user, password)
                db = _db()
                self.addCleanup(db.close)
                p = self._provider(user, password)
                db.add(p)
                db.commit()
                secret = vod_tokens.token_secret(db)
                link = vod_tokens.url("http://tentacle.test:8888", secret, p.id, "movie", 7, "mp4")
                self._plays(_direct_url(link, "movie", 7, p))
                with mock.patch.object(vod_router, "lan_origin_guard", lambda server: (lambda url: True)):
                    url, _guard, _ua, _owner = vod_router._resolve(db, "movie", link.rsplit("/", 1)[1])
                self._plays(url)

    def test_provider_migration(self):
        from models.database import Movie, Provider, ProviderCategory
        from services.migration import migrate_provider, preview_migration
        for user, password in LOGINS:
            with self.subTest(login=(user, password)):
                self._use(user, password)
                db = _db()
                self.addCleanup(db.close)
                old = Provider(name="Old", server_url="http://old.test", username="o", password="o")
                new = self._provider(user, password)
                db.add_all([old, new])
                db.commit()
                db.add(ProviderCategory(provider_id=new.id, category_id="1", category_name="Movies",
                                        type="movie", whitelisted=True))
                strm = Path(temp_dir(self)) / "Film (2026).strm"
                strm.write_text("http://old.test/movie/o/o/1.mp4")
                db.add(Movie(tmdb_id=99, title="Film", year="2026", source=f"provider_{old.id}",
                             provider_id=old.id, strm_path=str(strm)))
                db.commit()
                # Migration reads the new provider through its sync client (#460)
                self.assertEqual(1, preview_migration(old, new, db).get("movies_rewritten"))
                self.assertEqual(1, migrate_provider(old.id, new.id, db)["movies_rewritten"])
                self._plays(strm.read_text())


def _old_urls(server, user, password):
    """The URLs every builder made before the login was encoded."""
    return {
        "vod api": f"{server}/player_api.php?username={user}&password={password}",
        "vod movie": f"{server}/movie/{user}/{password}/42.mkv",
        "vod episode": f"{server}/series/{user}/{password}/43.mp4",
        "live api": f"{server}/player_api.php?username={user}&password={password}&action=get_live_streams&category_id=5",
        "guide": f"{server}/xmltv.php?username={user}&password={password}",
        "live": f"{server}/live/{user}/{password}/44.ts",
        "live after a provider edit": f"{server}/live/{user}/{password}/44.ts",
        "live movie": f"{server}/movie/{user}/{password}/42.mkv",
        "live series": f"{server}/series/{user}/{password}/43.mp4",
        "m3u": f"{server}/44.ts?username={user}&password={password}",
    }


def _new_urls(server, user, password):
    import services.sync as sync
    from services.m3u_parser import xtream_streams_to_m3u
    from services.xtream_client import XtreamClient, live_stream_url
    vod = sync.XtreamClient(types.SimpleNamespace(id=1, server_url=server, username=user, password=password))
    live = XtreamClient(server, user, password)
    return {
        "vod api": vod.base,
        "vod movie": vod.movie_stream_url(42, "mkv"),
        "vod episode": vod.episode_stream_url(43, "mp4"),
        "live api": live._api_url("get_live_streams", category_id=5),
        "guide": live.get_xmltv_url(),
        "live": live.live_stream_url(44, "ts"),
        "live after a provider edit": live_stream_url(server, user, password, 44, "ts"),
        "live movie": live.movie_stream_url(42, "mkv"),
        "live series": live.series_stream_url(43, "mp4"),
        "m3u": xtream_streams_to_m3u([{"stream_id": 44}], {}, server, user, password).splitlines()[-1],
    }


def _random_login(rng, alphabet):
    return tuple("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 24))) for _ in range(2))


class AnUpgradeChangesNoWorkingLogin(unittest.TestCase):
    """A login made only of letters, digits and -._~!$()*,;=:@ gives byte for
    byte the URLs it gave before: no .strm, live channel or cached guide is
    rewritten on upgrade."""

    def test_same_urls_property(self):
        for seed in range(SEEDS):
            rng = random.Random(seed)
            user, password = _random_login(rng, SAFE)
            server = rng.choice(["http://panel.test", "http://panel.test:8080", "https://p.test/xc"])
            self.assertEqual(_old_urls(server, user, password), _new_urls(server, user, password),
                             f"seed {seed}")

    def test_typical_logins(self):
        for user, password in [("u", "p"), ("john@example.com", "S3cret!"), ("AB12cd", "x=y;z:(1)*$,")]:
            self.assertEqual(_old_urls("http://panel.test", user, password),
                             _new_urls("http://panel.test", user, password))

    def test_existing_strm_files_are_left_alone(self):
        from services.sync import XtreamClient, _strm_needs_rewrite
        folder = Path(temp_dir(self))
        for seed in range(SEEDS):
            rng = random.Random(seed)
            user, password = _random_login(rng, SAFE)
            client = XtreamClient(types.SimpleNamespace(id=1, server_url="http://panel.test",
                                                        username=user, password=password))
            strm = folder / f"{seed}.strm"
            strm.write_text(f"http://panel.test/movie/{user}/{password}/42.mkv")
            self.assertFalse(_strm_needs_rewrite(strm, client.movie_stream_url(42, "mkv"), client), f"seed {seed}")


class EveryLoginSurvivesTheTrip(unittest.TestCase):
    """For any login, the panel reads back exactly the username and password
    from every URL: the query as PHP parses it, the path segment by segment."""

    ALPHABET = string.printable.replace("\x0b", "").replace("\x0c", "") + "éß€中"

    def test_property(self):
        for seed in range(SEEDS):
            rng = random.Random(seed)
            user, password = _random_login(rng, self.ALPHABET)
            for name, url in _new_urls("http://panel.test:8080", user, password).items():
                parts = urlsplit(url)
                self.assertEqual("", parts.fragment, f"seed {seed} {name}")
                self.assertTrue(url.isascii() and not any(c.isspace() for c in url), f"seed {seed} {name}")
                if parts.query:
                    q = parse_qs(parts.query, keep_blank_values=True)
                    self.assertEqual(([user], [password]), (q.get("username"), q.get("password")),
                                     f"seed {seed} {name}")
                else:
                    segs = parts.path.split("/")
                    self.assertEqual(5, len(segs), f"seed {seed} {name}")
                    self.assertEqual((user, password), (unquote(segs[2]), unquote(segs[3])), f"seed {seed} {name}")


class OldLinksThatCouldNotPlayAreRewritten(unittest.TestCase):
    """A '/' or '?' in the password still let the sync log in, so it wrote
    .strm files that cut the path; a space played but is now written %20.
    The next sync rewrites them once; another stream's file is still left
    alone."""

    def setUp(self):
        self.db = _db()
        self.addCleanup(self.db.close)
        self.dir = Path(temp_dir(self))

    def _client(self, password, links=None):
        from services.sync import XtreamClient
        c = XtreamClient(types.SimpleNamespace(id=3, server_url="http://cf.panel.test",
                                               username="u", password=password))
        c.vod_links = links
        return c

    def test_movie_file_is_rewritten(self):
        from models.database import Movie, Provider
        from services.sync import _repair_movie_strm
        for password, written in (("Ab/12?cd", "Ab%2F12%3Fcd"), ("a b", "a%20b"), ("Ab/12", "Ab%2F12")):
            with self.subTest(password=password):
                p = Provider(name="P", server_url="http://cf.panel.test", username="u", password=password)
                self.db.add(p)
                self.db.commit()
                strm = self.dir / f"{p.id}.strm"
                strm.write_text(f"http://cf.panel.test/movie/u/{password}/42.mkv")
                self.db.add(Movie(tmdb_id=100 + p.id, title="Film", year="2026", source=f"provider_{p.id}",
                                  provider_id=p.id, strm_path=str(strm)))
                self.db.commit()
                client = self._client(password)
                with mock.patch("services.sync.chown_path", lambda path: None):
                    _repair_movie_strm(client, {"stream_id": 42, "container_extension": "mkv"},
                                       100 + p.id, p, self.db)
                self.assertEqual(f"http://cf.panel.test/movie/u/{written}/42.mkv", strm.read_text())

    def test_episode_and_vod_through_tentacle(self):
        from services import vod_tokens
        from services.sync import _strm_needs_rewrite
        strm = self.dir / "S01E01.strm"
        strm.write_text("http://cf.panel.test/series/u/Ab/12?cd/43.mp4")
        client = self._client("Ab/12?cd")
        self.assertTrue(_strm_needs_rewrite(strm, client.episode_stream_url(43, "mp4"), client))
        links = vod_tokens.Links("http://192.168.2.52:8888", "k" * 64, 3)
        client = self._client("Ab/12?cd", links)
        self.assertTrue(_strm_needs_rewrite(strm, client.episode_stream_url(43, "mp4"), client))

    def test_a_link_main_wrote_is_rewritten_once(self):
        # Main logged in with these and wrote the raw link (with a space, a '%'
        # or a non-ASCII letter it played: players encode those themselves).
        # The next sync writes the encoded form once; the one after leaves it.
        from services.sync import _strm_needs_rewrite
        for password in ("a b", "50%off", "péx", "a'b", 'a"b', "a[b]c", "a|b^c", "a\\b", "a<b>", "a`b{c}"):
            with self.subTest(password=password):
                strm = self.dir / "Film.strm"
                strm.write_text(f"http://cf.panel.test/movie/u/{password}/42.mkv", encoding="utf-8")
                client = self._client(password)
                expected = client.movie_stream_url(42, "mkv")
                self.assertTrue(_strm_needs_rewrite(strm, expected, client))
                strm.write_text(expected, encoding="utf-8")
                self.assertFalse(_strm_needs_rewrite(strm, expected, client))

    def test_another_stream_is_left_alone(self):
        from services.sync import _strm_needs_rewrite
        strm = self.dir / "Film.strm"
        strm.write_text("http://cf.panel.test/movie/u/Ab/12?cd/41.mkv")
        client = self._client("Ab/12?cd")
        self.assertFalse(_strm_needs_rewrite(strm, client.movie_stream_url(42, "mkv"), client))


class WhoseLinkIsIt(unittest.TestCase):
    """The checks that tell this provider's files from another provider's
    compare the username as stored, not as it is written in the URL."""

    def test_stream_origin_reads_the_username_back(self):
        from services.sync import XtreamClient, _stream_origin
        c = XtreamClient(types.SimpleNamespace(id=1, server_url="http://panel.test",
                                               username="john+tv@example.com", password="Ab#12&cd"))
        self.assertEqual(("host", "panel.test", "john+tv@example.com"), _stream_origin(c.movie_stream_url(7)))

    def test_another_providers_file_is_recognised(self):
        from services.sync import XtreamClient, _strm_plays_other_provider
        theirs = XtreamClient(types.SimpleNamespace(id=2, server_url="http://panel.test",
                                                    username="john+tv@example.com", password="Ab#12&cd"))
        strm = Path(temp_dir(self)) / "Film.strm"
        strm.write_text(theirs.movie_stream_url(7))
        ours = XtreamClient(types.SimpleNamespace(id=1, server_url="http://other.test", username="me", password="p"))
        ours.other_providers = {"ids": {2}, "accounts": {("panel.test", "john+tv@example.com")}}
        self.assertTrue(_strm_plays_other_provider(strm, ours))


class TheLoginStaysOutOfTheLog(unittest.TestCase):
    def test_every_url_is_redacted(self):
        from services.log_redaction import redact
        user, password = "john+tv@example.com", "Xq/7?Zr#9 Wv'Yk\"Lm&Np+Ts%Ru"
        for name, url in _new_urls("http://panel.test", user, password).items():
            text = redact(f"Stream request: {url} failed")
            for piece in ("john", "Xq", "Zr", "Wv", "Yk", "Lm", "Np", "Ts", "Ru"):
                self.assertNotIn(piece, text, name)


if __name__ == "__main__":
    unittest.main()
