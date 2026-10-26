"""An M3U provider (playlist link or file) works like an Xtream one.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Most provider playlists are an Xtream panel's get.php: live channels, films
and episodes in one list, every link {server}/{user}/{pass}/{id} or
{server}/{live|movie|series}/{user}/{pass}/{id}.{ext}. Before this:
- the provider test reported no live channels, so the card never offered
  Live TV, and its live groups were offered as film categories;
- the Live TV sync took every film and episode for a channel;
- a .m3u8 channel stayed HLS (the mode that falls apart with several
  channels on a one-connection account) instead of one continuous .ts;
- films played straight from the provider (no resume, not counted);
- the guide the playlist names (url-tvg) was never read;
- a playlist file missing from the container was said only after saving.
"""
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import LiveChannel, LiveChannelGroup, Provider
from services import m3u_parser, m3u_source
from tmp_dirs import temp_dir

S = "http://panel.example:80"
PLAYLIST = f'''#EXTM3U url-tvg="{S}/xmltv.php?username=u%40x&password=p" x-tvg-url="http://guide.example/b.xml.gz"
#EXTINF:-1 tvg-id="cnn.us" tvg-name="US: CNN" tvg-logo="" group-title="US NEWS",US: CNN
{S}/u%40x/p/101
#EXTINF:-1 tvg-id="" tvg-name="CA: TSN 1" group-title="CA SPORTS",CA: TSN 1
{S}/u%40x/p/102
#EXTINF:-1 tvg-id="" tvg-name="No group",No group
{S}/u%40x/p/103
#EXTINF:-1 tvg-name="Heat (1995)" group-title="EN - ACTION",Heat (1995)
{S}/movie/u%40x/p/5001.mkv
#EXTINF:-1 tvg-name="The Expanse S01 E02" group-title="NETFLIX SERIES",The Expanse S01 E02
{S}/series/u%40x/p/7002.mp4
'''


def _session(test):
    engine = create_engine(f"sqlite:///{temp_dir(test)}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    test.addCleanup(db.close)
    return db


class Classify(unittest.TestCase):
    def kind(self, url, name="X", tvg_type=None):
        return m3u_parser.classify({"stream_url": url, "name": name, "tvg_type": tvg_type})

    def test_panel_links(self):
        self.assertEqual("live", self.kind(f"{S}/u/p/1"))
        self.assertEqual("live", self.kind(f"{S}/u/p/1.ts"))
        self.assertEqual("live", self.kind(f"{S}/live/u/p/1.m3u8"))
        self.assertEqual("movie", self.kind(f"{S}/movie/u/p/1.mkv", "Show S01E01"))
        self.assertEqual("series", self.kind(f"{S}/series/u/p/1.mp4"))

    def test_other_links(self):
        self.assertEqual("live", self.kind("https://cdn.example/news/master.m3u8"))
        self.assertEqual("live", self.kind("http://158.69.124.9:1935/a/a/playlist.m3u8"))
        self.assertEqual("live", self.kind("https://cdn.example/movie/channel/index.m3u8"))
        self.assertEqual("movie", self.kind("https://files.example/heat.mp4", "Heat"))
        self.assertEqual("series", self.kind("https://files.example/e.mp4", "Show S02E03"))
        self.assertEqual("movie", self.kind("https://files.example/movie/42"))

    def test_tvg_type_wins(self):
        self.assertEqual("live", self.kind("https://files.example/loop.mp4", tvg_type="live"))
        self.assertEqual("movie", self.kind("https://cdn.example/x.m3u8", tvg_type="movie"))


class PanelLinks(unittest.TestCase):
    def test_shapes(self):
        link = m3u_parser.panel_link(f"{S}/live/u/p/7.m3u8")
        self.assertEqual(("live", S, "u", "p", "7", "m3u8"), (link.kind, link.base, link.user, link.pw,
                                                                link.stream_id, link.ext))
        self.assertEqual(f"{S}/live/u/p/7.ts", link.live_url("ts"))
        self.assertIsNone(m3u_parser.panel_link(f"{S}/u/p/7?token=1"))
        self.assertIsNone(m3u_parser.panel_link("https://cdn.example/hls/channel/7.m3u8"))
        self.assertIsNone(m3u_parser.panel_link(f"{S}/movie/u/p/7"))

    def test_login_is_the_one_most_links_share_decoded(self):
        entries = m3u_parser.parse_m3u(PLAYLIST)
        self.assertEqual((S, "u@x", "p"), m3u_parser.panel_login(entries))

    def test_a_few_panel_like_links_are_not_a_panel(self):
        entries = [{"stream_url": f"https://cdn.example/x/{i}.m3u8"} for i in range(5)]
        entries.append({"stream_url": f"{S}/u/p/1"})
        self.assertIsNone(m3u_parser.panel_login(entries))

    def test_guide_urls_from_the_header(self):
        self.assertEqual([f"{S}/xmltv.php?username=u%40x&password=p", "http://guide.example/b.xml.gz"],
                         m3u_parser.guide_urls(PLAYLIST))
        self.assertEqual(["http://a.example/1.xml", "http://a.example/2.xml"],
                         m3u_parser.guide_urls('#EXTM3U x-tvg-url="http://a.example/1.xml, http://a.example/2.xml"\n'))
        self.assertEqual([], m3u_parser.guide_urls("#EXTINF:-1,x\nhttp://a/1\n"))


def _m3u_provider(db, ptype="m3u_url", m3u_url="http://lists.example/get.php?username=u&password=p"):
    p = Provider(name="P", provider_type=ptype, server_url="", username="", password="", m3u_url=m3u_url)
    db.add(p)
    db.commit()
    return p


def _read(text):
    return mock.patch.object(m3u_parser, "read_m3u_text", return_value=text)


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.db = _session(self)
        self.p = _m3u_provider(self.db)

    def _test(self, player_api=None):
        from routers import providers
        answer = mock.Mock(status_code=200)
        answer.json.return_value = player_api or {}
        with _read(PLAYLIST), mock.patch("requests.get", return_value=answer) as get, \
                mock.patch("services.provider_activity.refuse_while_recording"):
            r = providers.test_provider(self.p.id, self.db)
        self.db.expire_all()
        return r, get

    def test_finds_live_films_and_series_and_switches_live_tv_on(self):
        r, _ = self._test()
        self.assertEqual((True, True, True), (r["has_live"], r["has_vod"], r["has_series"]))
        p = self.db.get(Provider, self.p.id)
        self.assertTrue(p.live_tv_enabled)
        self.assertEqual((S, "u@x", "p"), (p.server_url, p.username, p.password))

    def test_a_retest_leaves_the_live_switch_alone(self):
        self._test()
        p = self.db.get(Provider, self.p.id)
        p.live_tv_enabled = False
        self.db.commit()
        self._test()
        self.assertFalse(self.db.get(Provider, self.p.id).live_tv_enabled)

    def test_the_panels_account_details(self):
        r, get = self._test({"user_info": {"auth": 1, "status": "Active", "exp_date": "1893456000",
                                           "max_connections": "2", "active_cons": "0",
                                           "allowed_output_formats": ["m3u8", "ts"]}})
        self.assertEqual(2, r["max_connections"])
        p = self.db.get(Provider, self.p.id)
        self.assertEqual(2, p.max_connections)
        self.assertIsNotNone(p.expiry)
        from services.xtream_client import quote_cred
        self.assertIn(f"/player_api.php?username={quote_cred('u@x')}&password=p", get.call_args[0][0])

    def test_no_account_details_is_fine(self):
        r, _ = self._test({"user_info": {"auth": 0}})
        self.assertIsNone(r["max_connections"])
        self.assertIsNone(self.db.get(Provider, self.p.id).max_connections)

    def test_a_channels_only_playlist_starts_with_movies_off(self):
        from routers import providers
        self.p.active = True
        self.db.commit()
        text = '#EXTM3U\n#EXTINF:-1 group-title="News",News 1\nhttps://cdn.example/n1/master.m3u8\n'
        with _read(text), mock.patch("services.provider_activity.refuse_while_recording"):
            providers.test_provider(self.p.id, self.db)
        p = self.db.get(Provider, self.p.id)
        self.assertEqual((False, True), (p.active, p.live_tv_enabled))
        p.active = True                      # the admin's choice stands at the next test
        self.db.commit()
        with _read(text), mock.patch("services.provider_activity.refuse_while_recording"):
            providers.test_provider(self.p.id, self.db)
        self.assertTrue(self.db.get(Provider, self.p.id).active)

    def test_the_playlists_guide_is_kept(self):
        self._test()
        self.assertEqual(f"{S}/xmltv.php?username=u%40x&password=p", m3u_source.guide_url(self.db, self.p.id))


class VodImport(unittest.TestCase):
    def setUp(self):
        self.db = _session(self)
        self.p = _m3u_provider(self.db)

    def _client(self, links=True):
        from services.sync import M3UClient
        from services import vod_tokens
        c = M3UClient(self.p)
        c.db = self.db
        if links:
            c.vod_links = vod_tokens.Links("http://tentacle.example:8888", "s" * 64, self.p.id)
        with _read(PLAYLIST):
            c._load()
        return c

    def test_channels_are_not_films(self):
        c = self._client()
        self.assertEqual(3, c.live_count)
        films = [m["name"] for g in c._movies.values() for m in g]
        self.assertEqual(["Heat (1995)"], films)
        self.assertEqual(["NETFLIX SERIES"], list(c._series))

    def test_panel_films_play_through_tentacle(self):
        c = self._client()
        film = c._movies["EN - ACTION"][0]
        url = c.movie_stream_url(film["stream_id"], film["container_extension"])
        self.assertTrue(url.startswith(f"http://tentacle.example:8888/api/vod/movie/{self.p.id}.5001."), url)
        self.assertTrue(url.endswith(".mkv"))
        ep = c._series["NETFLIX SERIES"]["The Expanse"]["1"][0]
        self.assertIn(f"/api/vod/series/{self.p.id}.7002.", c.episode_stream_url(ep["id"]))

    def test_without_the_route_the_playlist_url(self):
        c = self._client(links=False)
        film = c._movies["EN - ACTION"][0]
        self.assertEqual(f"{S}/movie/u%40x/p/5001.mkv", c.movie_stream_url(film["stream_id"]))

    def test_a_film_of_another_login_plays_as_listed(self):
        from services.sync import M3UClient
        from services import vod_tokens
        other = PLAYLIST + '#EXTINF:-1 group-title="X",Elsewhere (2001)\nhttp://other.example/movie/a/b/9.mp4\n'
        c = M3UClient(self.p)
        c.vod_links = vod_tokens.Links("http://tentacle.example:8888", "s" * 64, self.p.id)
        with _read(other):
            c._load()
        film = c._movies["X"][0]
        self.assertEqual("http://other.example/movie/a/b/9.mp4", c.movie_stream_url(film["stream_id"]))

    def test_the_relay_rebuilds_the_panels_url(self):
        from routers import vod
        from services import vod_tokens
        c = self._client()
        self.db.commit()
        secret = vod_tokens.token_secret(self.db)
        token = vod_tokens.mint(secret, self.p.id, "movie", 5001, "mkv")
        with mock.patch.object(vod, "lan_origin_guard", return_value=lambda u: True):
            url, _guard, _ua, owner = vod._resolve(self.db, "movie", f"{token}.mkv")
        from services.xtream_client import quote_cred
        self.assertEqual(f"{S}/movie/{quote_cred('u@x')}/p/5001.mkv", url)   # the same film, the login as Xtream writes it
        self.assertEqual(f"vod:movie:{self.p.id}:5001", owner)

    def test_existing_direct_strm_moves_to_the_route(self):
        from services.sync import _strm_needs_rewrite
        c = self._client()
        strm = Path(temp_dir(self)) / "Heat.strm"
        film = c._movies["EN - ACTION"][0]
        expected = c.movie_stream_url(film["stream_id"], "mkv")
        strm.write_text(f"{S}/movie/u%40x/p/5001.mkv")
        self.assertTrue(_strm_needs_rewrite(strm, expected, c))
        strm.write_text(f"{S}/movie/u%40x/p/5002.mkv")       # another film: never
        self.assertFalse(_strm_needs_rewrite(strm, expected, c))
        strm.write_text("http://my-proxy.example/heat.mkv")   # somebody's own: never
        self.assertFalse(_strm_needs_rewrite(strm, expected, c))


class LiveSync(unittest.TestCase):
    def setUp(self):
        self.db = _session(self)
        self.p = _m3u_provider(self.db)
        self.p.live_tv_enabled = True
        self.db.commit()

    def _sync(self, text=PLAYLIST):
        import routers.livetv as livetv
        from routers.livetv import _snapshot_provider, _sync_from_m3u
        with _read(text), mock.patch.object(livetv, "_set_sync_status"):
            return _sync_from_m3u(_snapshot_provider(self.p, "m3u_url"), self.db, "")

    def test_only_channels_and_every_one_in_a_group(self):
        self._sync()
        rows = self.db.query(LiveChannel).filter_by(provider_id=self.p.id).all()
        self.assertEqual({"US: CNN", "CA: TSN 1", "No group"}, {r.name for r in rows})
        groups = {g.name for g in self.db.query(LiveChannelGroup).filter_by(provider_id=self.p.id)}
        self.assertEqual({"US NEWS", "CA SPORTS", "Uncategorized"}, groups)

    def test_films_an_older_sync_took_for_channels_go(self):
        # What the sync before this did: every line a channel
        for i, (name, url, group) in enumerate([("Heat (1995)", f"{S}/movie/u%40x/p/5001.mkv", "EN - ACTION"),
                                                ("Old S01 E01", f"{S}/series/u%40x/p/7001.mp4", "NETFLIX SERIES"),
                                                ("US: CNN", f"{S}/u%40x/p/101", "US NEWS")]):
            self.db.add(LiveChannel(provider_id=self.p.id, name=name, stream_id=f"x{i}", m3u_key=f"k{i}",
                                    stream_url=url, group_title=group))
            self.db.add(LiveChannelGroup(provider_id=self.p.id, name=group, enabled=False))
        self.db.commit()
        stats = self._sync()
        names = {r.name for r in self.db.query(LiveChannel).filter_by(provider_id=self.p.id)}
        self.assertEqual({"US: CNN", "CA: TSN 1", "No group"}, names)
        self.assertEqual(0, stats["removals_refused"])
        groups = {g.name for g in self.db.query(LiveChannelGroup).filter_by(provider_id=self.p.id)}
        self.assertNotIn("EN - ACTION", groups)
        self.assertNotIn("NETFLIX SERIES", groups)

    def test_guide_from_the_playlist_then_the_panel(self):
        import routers.livetv as livetv
        self._sync()
        self.db.expire_all()
        p = self.db.get(Provider, self.p.id)
        data = livetv._epg_provider_data(p, [])
        self.assertEqual(f"{S}/xmltv.php?username=u%40x&password=p", livetv._epg_feed_url(data))
        data["m3u_guide_url"] = ""
        with mock.patch("services.xtream_client.XtreamClient.get_xmltv_url", return_value="panel-guide"):
            self.assertEqual("panel-guide", livetv._epg_feed_url(data))
        data["epg_url"] = "http://mine.example/guide.xml"
        self.assertEqual("http://mine.example/guide.xml", livetv._epg_feed_url(data))

    def test_a_playlist_with_no_guide_and_no_panel_has_none(self):
        import routers.livetv as livetv
        self._sync('#EXTM3U\n#EXTINF:-1 group-title="News",News 1\nhttps://cdn.example/n1/master.m3u8\n')
        self.db.expire_all()
        p = self.db.get(Provider, self.p.id)
        self.assertEqual("", p.server_url)
        self.assertIsNone(livetv._epg_feed_url(livetv._epg_provider_data(p, [])))


class LiveOpen(unittest.TestCase):
    """Like Xtream: HLS re-read from the channel URL, with one continuous
    .ts as the fallback."""

    def setUp(self):
        import routers.livetv as livetv
        self.livetv = livetv
        self.db = _session(self)
        self.p = _m3u_provider(self.db)
        self.addCleanup(livetv._ts_refused_channels.clear)
        self.addCleanup(livetv._hls_failed_channels.clear)

    def _open(self, url, cid=1):
        ch = LiveChannel(id=cid, provider_id=self.p.id, name="c", stream_id="abc", stream_url=url)
        return self.livetv._live_open_urls(self.db, self.p, ch)

    def test_m3u8_link_opens_as_hls_with_ts_behind_it(self):
        self.assertEqual((f"{S}/live/u/p/7.m3u8", f"{S}/live/u/p/7.ts"), self._open(f"{S}/live/u/p/7.m3u8"))

    def test_ts_link_keeps_its_form_as_the_fallback(self):
        self.assertEqual((f"{S}/live/u/p/7.m3u8", f"{S}/u/p/7"), self._open(f"{S}/u/p/7"))

    def test_a_channel_that_refused_ts_goes_straight_to_m3u8(self):
        self.livetv._ts_refused_channels.add(5)
        self.assertEqual((f"{S}/live/u/p/7.m3u8", None), self._open(f"{S}/live/u/p/7.m3u8", cid=5))

    def test_the_format_setting_applies(self):
        mdb.set_setting(self.db, "livetv_stream_format", "m3u8")
        self.assertEqual((f"{S}/live/u/p/7.m3u8", None), self._open(f"{S}/u/p/7"))

    def test_an_account_without_ts(self):
        mdb.set_setting(self.db, f"livetv_output_formats_{self.p.id}", '["m3u8"]')
        self.assertEqual((f"{S}/live/u/p/7.m3u8", None), self._open(f"{S}/live/u/p/7.m3u8"))

    def test_other_links_open_as_listed(self):
        url = "https://cdn.example/news/master.m3u8?token=1"
        self.assertEqual((url, None), self._open(url))


class LanOrigin(unittest.TestCase):
    def test_the_address_the_admin_entered(self):
        link = Provider(provider_type="m3u_url", m3u_url="http://192.168.1.5:8901/get.php?username=a&password=b",
                        server_url="http://10.0.0.9")
        self.assertEqual("http://192.168.1.5:8901/get.php?username=a&password=b", m3u_source.guard_origin(link))
        f = Provider(provider_type="m3u_file", m3u_url="/data/x.m3u", server_url="http://192.168.1.5:8901")
        self.assertEqual("http://192.168.1.5:8901", m3u_source.guard_origin(f))
        x = Provider(provider_type="xtream", server_url="http://192.168.1.5:8901")
        self.assertEqual("http://192.168.1.5:8901", m3u_source.guard_origin(x))


class Categories(unittest.TestCase):
    """An older build offered a playlist's live groups as film categories."""

    def setUp(self):
        self.db = _session(self)
        self.p = _m3u_provider(self.db)
        for name, typ, chosen in (("US NEWS", "movie", False), ("CA SPORTS", "movie", True),
                                  ("EN - ACTION", "movie", False)):
            self.db.add(mdb.ProviderCategory(provider_id=self.p.id, category_id=name, category_name=name,
                                             type=typ, whitelisted=chosen, title_count=1))
        self.db.commit()

    def _fetch(self, text):
        from routers import providers
        with _read(text), mock.patch("services.provider_activity.refuse_while_recording"):
            return providers.fetch_categories(self.p.id, self.db)

    def names(self):
        return {(c.category_name, c.type) for c in self.db.query(mdb.ProviderCategory).filter_by(provider_id=self.p.id)}

    def test_live_groups_leave_the_film_categories_unless_chosen(self):
        r = self._fetch(PLAYLIST)
        self.assertEqual(1, r["removed_categories"])
        self.assertEqual({("CA SPORTS", "movie"), ("EN - ACTION", "movie"), ("NETFLIX SERIES", "series")},
                         self.names())

    def test_a_live_only_playlist_too(self):
        self._fetch('#EXTM3U\n#EXTINF:-1 group-title="News",News 1\nhttps://cdn.example/n1/master.m3u8\n')
        self.assertEqual({("CA SPORTS", "movie")}, self.names())

    def test_an_empty_answer_removes_nothing(self):
        self._fetch("#EXTM3U\n")
        self.assertEqual(3, len(self.names()))


class PlaylistFile(unittest.TestCase):
    def setUp(self):
        self.db = _session(self)

    def test_said_when_saved(self):
        from fastapi import HTTPException
        from routers import providers
        with self.assertRaises(HTTPException) as e:
            providers.create_provider(providers.ProviderCreate(
                name="F", provider_type="m3u_file", m3u_url="/nowhere/list.m3u"), self.db)
        self.assertIn("Tentacle's data folder (mounted at /data)", e.exception.detail)
        with self.assertRaises(HTTPException) as e:
            providers.create_provider(providers.ProviderCreate(
                name="F", provider_type="m3u_file", m3u_url="list.m3u"), self.db)
        self.assertIn("/data/list.m3u", e.exception.detail)
        with self.assertRaises(HTTPException) as e:
            providers.create_provider(providers.ProviderCreate(
                name="F", provider_type="m3u_url", m3u_url="/data/list.m3u"), self.db)
        self.assertIn("Playlist file on the server", e.exception.detail)
        self.assertEqual(0, self.db.query(Provider).count())

    def test_a_readable_file_is_taken(self):
        from routers import providers
        f = Path(temp_dir(self)) / "list.m3u"
        f.write_text(PLAYLIST)
        with mock.patch("services.provider_addresses.ensure_current"), \
                mock.patch("routers.providers._follow_stream_limit"):
            r = providers.create_provider(providers.ProviderCreate(
                name="F", provider_type="m3u_file", m3u_url=str(f)), self.db)
        self.assertTrue(r["success"])
        p = self.db.get(Provider, r["id"])
        playlist = m3u_source.read(p)
        self.assertEqual({"live": 3, "movie": 1, "series": 1}, playlist.counts())


if __name__ == "__main__":
    unittest.main()
