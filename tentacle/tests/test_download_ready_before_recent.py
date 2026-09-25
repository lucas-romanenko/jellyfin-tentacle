"""A download joins "Recently downloaded" only when it is ready to watch, and a
new download is moved to the front of its playlists even right after the add.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Seen live 2026-09-24 (Welcome to the Family, 4 episodes via Sonarr):
- Activity listed the show under "Recently downloaded" the moment Sonarr
  imported it; the "ready to watch" notification came ~2 minutes later, after
  Tentacle had found it in Jellyfin, tagged it and filled the playlists.
- The show was added to Downloaded TV / lucas's Downloads / Recently Added TV
  but left at the END (entry 655 of 659), so the home rows (first 20-30) never
  showed it: the plugin answered 404 "Playlist entry not found" for the
  just-added entry (#114), and move_playlist_item fell back to Jellyfin's own
  move endpoint, which always answers 400 to an API key.
"""
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

TMDB = 333058


def _maker():
    import models.database as mdb
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    db = maker()
    for k, v in (("jellyfin_url", "http://jf"), ("jellyfin_api_key", "k"), ("jellyfin_user_id", "u1")):
        mdb.set_setting(db, k, v)
    db.commit()
    db.close()
    return maker


def _request():
    return Request({"type": "http", "method": "POST", "headers": [], "path": "/api/sonarr/webhook",
                    "query_string": b"", "client": ("10.0.0.2", 1)})


class _SyncThread:
    """threading.Thread that runs its target inline, so the webhook's
    background work finishes before the route returns."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._run = lambda: target(*args, **(kwargs or {}))

    def start(self):
        self._run()


class _FakeJellyfin:
    found = True

    def __init__(self, *a, **k):
        pass

    def search_by_tmdb_id(self, tmdb_id, kind, title=None, year=None):
        return {"Id": "jf-item", "Tags": []} if self.found else None

    def get_item_by_id(self, item_id):
        return {"Id": item_id, "Tags": []}

    def set_item_tags(self, item_id, tags):
        return True

    def refresh_item_metadata(self, item_id):
        return False

    def trigger_library_scan(self):
        pass


class Readiness(unittest.TestCase):
    def setUp(self):
        from services import download_readiness
        self.r = download_readiness
        self.r.reset()
        self.addCleanup(self.r.reset)

    def test_held_back_while_in_flight_then_ready(self):
        t = self.r.begin("series", TMDB, "Show", "S01E01", "sonarr")
        self.assertTrue(self.r.held_back("series", TMDB))
        self.assertEqual(["S01E01"], [e["episode"] for e in self.r.in_flight()])
        self.r.end(t, ready=True)
        self.assertFalse(self.r.held_back("series", TMDB))
        self.assertEqual([], self.r.in_flight())

    def test_a_later_episode_does_not_hide_a_title_that_was_ready(self):
        self.r.end(self.r.begin("series", TMDB, "Show", "S01E01"), ready=True)
        self.r.begin("series", TMDB, "Show", "S01E02")
        self.assertFalse(self.r.held_back("series", TMDB), "no flicker out of Recently downloaded")

    def test_not_found_in_jellyfin_is_not_ready_but_no_longer_in_flight(self):
        self.r.end(self.r.begin("movie", 5, "Film"), ready=False)
        self.assertFalse(self.r.held_back("movie", 5))
        self.assertEqual([], self.r.in_flight())

    def test_a_dead_webhook_thread_does_not_hide_a_title_for_ever(self):
        self.r.begin("movie", 5, "Film")
        with mock.patch.object(self.r.time, "monotonic", return_value=self.r.time.monotonic() + self.r.STALE_SECONDS + 1):
            self.assertFalse(self.r.held_back("movie", 5))
            self.assertEqual([], self.r.in_flight())


class ActivityLists(unittest.TestCase):
    def setUp(self):
        from services import download_readiness
        self.r = download_readiness
        self.r.reset()
        self.addCleanup(self.r.reset)
        self.maker = _maker()
        self.db = self.maker()
        self.addCleanup(self.db.close)

    def _series(self, jellyfin_item_id):
        from models.database import Series
        self.db.add(Series(tmdb_id=TMDB, title="Welcome to the Family", source="sonarr",
                           date_added=datetime.utcnow(), jellyfin_item_id=jellyfin_item_id,
                           last_downloaded_episode="S01E01"))
        self.db.commit()

    def _recent(self):
        from routers.activity import _get_recently_downloaded
        return [r["tmdb_id"] for r in _get_recently_downloaded(self.db)]

    def test_not_in_jellyfin_yet_is_not_recently_downloaded(self):
        self._series(None)
        self.assertEqual([], self._recent())

    def test_held_back_while_being_got_ready_then_listed(self):
        self._series("jf-item")
        t = self.r.begin("series", TMDB, "Welcome to the Family", "S01E01", "sonarr")
        self.assertEqual([], self._recent(), "listed before the ready-to-watch notification")
        self.r.end(t, ready=True)
        self.assertEqual([TMDB], self._recent())

    def test_getting_ready_shows_as_importing_and_is_not_doubled_with_the_queue(self):
        from routers.activity import _getting_ready
        self.r.begin("series", TMDB, "Welcome to the Family", "S01E01", "sonarr")
        cards = _getting_ready([])
        self.assertEqual(1, len(cards))
        c = cards[0]
        self.assertEqual(("importing", 100.0, "S01E01", None), (c["status"], c["progress"], c["episode"], c["queue_id"]))
        queued = [{"media_type": "series", "tmdb_id": TMDB, "episode": "S01E01", "status": "importing"}]
        self.assertEqual([], _getting_ready(queued), "the queue still lists it: one card, not two")


class SonarrWebhookEndToEnd(unittest.TestCase):
    """The real webhook route, its background work run inline."""

    def setUp(self):
        from services import download_readiness
        self.r = download_readiness
        self.r.reset()
        self.addCleanup(self.r.reset)
        self.maker = _maker()
        db = self.maker()
        from models.database import Series
        db.add(Series(tmdb_id=TMDB, title="Welcome to the Family", source="sonarr",
                      date_added=datetime.utcnow(), tags=["Downloaded TV"]))
        db.commit()
        db.close()
        self.seen_during_playlists = None

    def _run(self, found=True):
        import routers.sonarr as sonarr
        from routers.activity import _get_recently_downloaded, _getting_ready

        def add_to_playlists(db, item_id, tags, media_type, jf_item=None):
            probe = self.maker()
            try:
                self.seen_during_playlists = {
                    "held_back": self.r.held_back("series", TMDB),
                    "recent": [x["tmdb_id"] for x in _get_recently_downloaded(probe)],
                    "importing": [(c["status"], c["episode"]) for c in _getting_ready([])],
                }
            finally:
                probe.close()
            return {"added_to": 3}

        fake = type("FJ", (_FakeJellyfin,), {"found": found})
        payload = {"eventType": "Download", "series": {"tmdbId": TMDB, "title": "Welcome to the Family"},
                   "episodes": [{"seasonNumber": 1, "episodeNumber": 1, "title": "The Perfect Couple"}]}
        db = self.maker()
        try:
            with mock.patch("models.database.SessionLocal", self.maker), \
                    mock.patch.object(sonarr, "scan_sonarr_library", lambda db: None), \
                    mock.patch.object(sonarr.threading, "Thread", _SyncThread), \
                    mock.patch("services.jellyfin.JellyfinService", fake), \
                    mock.patch("services.smartlists.add_item_to_matching_playlists", add_to_playlists), \
                    mock.patch("services.smartlists._notify_jellyfin_plugin", lambda db: None), \
                    mock.patch("time.sleep", lambda s: None):
                sonarr.sonarr_webhook(payload, _request(), db)
        finally:
            db.close()
        probe = self.maker()
        try:
            return [x["tmdb_id"] for x in _get_recently_downloaded(probe)]
        finally:
            probe.close()

    def test_importing_until_ready_then_recently_downloaded(self):
        recent_after = self._run(found=True)
        self.assertEqual({"held_back": True, "recent": [], "importing": [("importing", "S01E01")]},
                         self.seen_during_playlists,
                         "while Tentacle is still getting it ready it is 'importing', not 'recently downloaded'")
        self.assertEqual([TMDB], recent_after, "ready once the webhook is done")
        self.assertEqual([], self.r.in_flight())

    def test_not_found_in_jellyfin_is_never_listed_as_ready(self):
        recent_after = self._run(found=False)
        self.assertIsNone(self.seen_during_playlists, "no playlist work without a Jellyfin item")
        self.assertEqual([], recent_after)
        self.assertEqual([], self.r.in_flight(), "and it does not sit in 'importing' for ever")


class _FakePlugin:
    """Jellyfin + plugin on loopback; the plugin's move answers are scripted."""

    def __init__(self, answers):
        self.answers = list(answers)       # (status, body) per plugin call
        self.calls = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                kind = "plugin" if self.path.startswith("/Tentacle/") else "native"
                fake.calls.append(kind)
                status, body = (fake.answers.pop(0) if kind == "plugin" and fake.answers
                                else (400, b"") if kind == "native" else (204, b""))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


class MoveRetriesAJustAddedEntry(unittest.TestCase):
    ENTRY_404 = (404, b'"Playlist entry not found"')

    def _move(self, answers):
        import services.jellyfin as jellyfin
        fake = _FakePlugin(answers)
        self.addCleanup(fake.close)
        slept = []
        with mock.patch.object(jellyfin.time, "sleep", lambda s: slept.append(s)):
            ok = jellyfin.JellyfinService(fake.url, "k", "u1").move_playlist_item("p1", "e1", 0)
        return ok, fake.calls, slept

    def test_entry_not_visible_yet_is_asked_again_not_sent_to_the_native_endpoint(self):
        ok, calls, slept = self._move([self.ENTRY_404, self.ENTRY_404, (204, b"")])
        self.assertTrue(ok)
        self.assertEqual(["plugin", "plugin", "plugin"], calls, "the native endpoint always 400s an API key")
        self.assertEqual([2.0, 4.0], slept)

    def test_gives_up_after_the_retries_without_the_native_call(self):
        ok, calls, slept = self._move([self.ENTRY_404] * 4)
        self.assertFalse(ok)
        self.assertEqual(["plugin"] * 4, calls)

    def test_a_missing_route_still_falls_back_to_the_native_endpoint(self):
        ok, calls, _ = self._move([(404, b"")])
        self.assertEqual(["plugin", "native"], calls, "no plugin (or too old) = the old path, unchanged")
        self.assertFalse(ok)

    def test_playlist_not_found_is_the_plugins_answer_not_a_missing_route(self):
        ok, calls, slept = self._move([(404, b'"Playlist not found"')])
        self.assertEqual((False, ["plugin"], []), (ok, calls, slept))


if __name__ == "__main__":
    unittest.main()
