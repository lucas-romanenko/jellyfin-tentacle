"""Live TV changes reach an open guide without a reload (Lucas, 2026-10-21:
he removed TLC from his favourites in Tentacle while Jellyfin web showed
Live TV → Favorites, and TLC stayed).

Run from the tentacle/ directory:  python -m unittest discover -s tests

- The web guide (tentacle-plugin/Inject/tentacle-livetv.js) listens on
  Jellyfin web's websocket: UserDataChanged (a favourite changed anywhere)
  updates the channel's star and the Favorites rows; LibraryChanged re-reads
  the channel list and redraws only what differs. Without a socket it polls.
- Tentacle tells the clients after every Live TV setup run
  (services/livetv_tuners.tell_clients → POST /Tentacle/Refresh?scope=livetv),
  and the plugin answers that scope with a LibraryChanged to everyone and no
  cache clearing.
"""
import re
import unittest
from pathlib import Path
from unittest import mock

from inject_js import HAVE_NODE
from test_livetv_guide import JS, run

PLUGIN = Path(__file__).resolve().parents[2] / "tentacle-plugin"


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class GuideFollowsFavouritesLive(unittest.TestCase):
    SETUP = """
      var G = T.G;
      G.userId = 'u1';
      G.channels = [{id: 'a1', name: 'TLC', fav: true}, {id: 'b2', name: 'CNN', fav: true},
                    {id: 'c3', name: 'BBC', fav: false}];
    """

    def test_unstarred_elsewhere_leaves_favorites(self):
        got = run(self.SETUP + """
          T.onUserData({UserId: 'u1', UserDataList: [{ItemId: 'a1', IsFavorite: false}]});
          return G.channels.filter(function (c) { return c.fav; }).map(function (c) { return c.name; });
        """)
        self.assertEqual(["CNN"], got)

    def test_starred_elsewhere_joins_favorites(self):
        got = run(self.SETUP + """
          T.onUserData({UserId: 'u1', UserDataList: [{ItemId: 'c3', IsFavorite: true}]});
          return G.channels.filter(function (c) { return c.fav; }).length;
        """)
        self.assertEqual(3, got)

    def test_ids_with_dashes_match(self):
        got = run("""
          var G = T.G; G.userId = '0f4728d068804be09a8ca85a86b95931';
          G.channels = [{id: '1f3cae9b274cb6c06ee67b6eed633e12', name: 'TLC', fav: true}];
          T.onUserData({UserId: '0f4728d0-6880-4be0-9a8c-a85a86b95931',
                        UserDataList: [{ItemId: '1f3cae9b-274c-b6c0-6ee6-7b6eed633e12', IsFavorite: false}]});
          return G.channels[0].fav;
        """)
        self.assertFalse(got)

    def test_someone_elses_favourites_change_nothing(self):
        got = run(self.SETUP + """
          T.onUserData({UserId: 'u2', UserDataList: [{ItemId: 'a1', IsFavorite: false}]});
          return G.channels[0].fav;
        """)
        self.assertTrue(got)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class ChannelListDiff(unittest.TestCase):
    def diff(self, old, new):
        return run("return T.diffChannels(%s, %s);" % (old, new))

    A = "{id: 'a', name: 'A', number: '1', logo: 'x', fav: false}"
    B = "{id: 'b', name: 'B', number: '2', logo: 'y', fav: true}"

    def test_same_list_is_no_change(self):
        d = self.diff(f"[{self.A}, {self.B}]", f"[{self.A}, {self.B}]")
        self.assertEqual({"moved": False, "favs": False, "changed": []}, d)

    def test_channel_gone_is_moved(self):
        self.assertTrue(self.diff(f"[{self.A}, {self.B}]", f"[{self.A}]")["moved"])

    def test_channel_added_is_moved(self):
        self.assertTrue(self.diff(f"[{self.A}]", f"[{self.A}, {self.B}]")["moved"])

    def test_reorder_is_moved(self):
        self.assertTrue(self.diff(f"[{self.A}, {self.B}]", f"[{self.B}, {self.A}]")["moved"])

    def test_renamed_or_starred_redraws_only_that_row(self):
        d = self.diff(f"[{self.A}, {self.B}]",
                      "[{id: 'a', name: 'A2', number: '1', logo: 'x', fav: false}, "
                      "{id: 'b', name: 'B', number: '2', logo: 'y', fav: false}]")
        self.assertFalse(d["moved"])
        self.assertTrue(d["favs"])
        self.assertEqual(["a", "b"], d["changed"])


class GuideListensOnTheSocket(unittest.TestCase):
    def test_messages_it_reacts_to(self):
        self.assertIn("ApiClient._webSocket", JS)
        self.assertIn("'UserDataChanged'", JS)
        self.assertIn("'LibraryChanged'", JS)

    def test_tabs_pass_messages_on(self):
        # Jellyfin talks to one socket per browser (the newest tab's): the tab
        # that gets a message relays it, and every tab listens, guide or not.
        self.assertIn("new BroadcastChannel('tentacle-livetv')", JS)
        self.assertIn("relay.postMessage(", JS)
        self.assertIn("setInterval(watchSocket", JS)

    def test_polls_without_a_socket_and_rereads_on_return(self):
        self.assertRegex(JS, r"POLL_MS = 10000")
        self.assertIn("visibilitychange", JS)

    def test_redraws_in_place_keeping_the_scroll(self):
        body = JS[JS.index("function refreshRows("):JS.index("document.addEventListener('visibilitychange'")]
        self.assertIn("drawRows()", body)
        self.assertIn("scrollTop = top", body)

    def test_timers_stop_when_leaving_the_guide(self):
        body = JS[JS.index("function deactivate("):JS.index("// ── Container")]
        self.assertIn("clearInterval(G.live)", body)


class PluginLiveTvScope(unittest.TestCase):
    SRC = re.sub(r"//[^\n]*", "", (PLUGIN / "Api" / "TentacleController.cs").read_text(encoding="utf-8"))

    def refresh(self):
        start = self.SRC.index('[HttpPost("Refresh")]')
        return self.SRC[start:self.SRC.index("const int playlistCount", start)]

    def test_livetv_scope_broadcasts_without_clearing_caches(self):
        body = self.refresh()
        self.assertIn('"livetv"', body)
        head = body[body.index('"livetv"'):body.index("var homeOnly")]
        self.assertIn("BroadcastLibraryChanged(null", head)
        self.assertIn("LiveTvVersionMarker", head)
        self.assertNotIn("ClearCache", head)
        self.assertIn("return Ok(", head)

    def test_marker_format(self):
        self.assertIn('$"tentacle-livetv:{', self.SRC)


class TentacleTellsTheClients(unittest.TestCase):
    def test_tell_clients_posts_the_livetv_scope(self):
        from services import livetv_tuners as T
        jf = mock.Mock()
        with mock.patch.object(T, "jellyfin_for", return_value=jf):
            self.assertTrue(T.tell_clients(object()))
        path = jf.post.call_args.args[0]
        self.assertEqual("/Tentacle/Refresh", path)
        self.assertEqual("livetv", jf.post.call_args.kwargs["scope"])
        self.assertGreater(jf.post.call_args.kwargs["homeVersion"], 0)

    def test_tell_clients_never_raises(self):
        import requests
        from services import livetv_tuners as T
        jf = mock.Mock()
        jf.post.side_effect = requests.ConnectionError("down")
        with mock.patch.object(T, "jellyfin_for", return_value=jf):
            self.assertFalse(T.tell_clients(object()))

    def _run(self, done):
        from services import livetv_tuners as T
        with mock.patch.object(T, "SessionLocal"), \
                mock.patch.object(T, "sync", return_value=done), \
                mock.patch.object(T, "log_activity"), \
                mock.patch.object(T, "set_setting"), \
                mock.patch.object(T, "tell_clients") as tell:
            T.run_sync("test")
        return tell

    def test_every_setup_run_tells_the_clients(self):
        tell = self._run({"added": 0, "removed": 0, "favourites": 0, "schedules": 0, "policies": 1})
        tell.assert_called_once()

    def test_a_skipped_run_does_not(self):
        tell = self._run({"added": 0, "removed": 0, "favourites": 0, "schedules": 0, "skipped": "no Live TV channels"})
        tell.assert_not_called()


if __name__ == "__main__":
    unittest.main()
