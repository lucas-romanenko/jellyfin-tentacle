"""Music module, phase 2: requests, reconcile, webhook, status, pages.

Lidarr is a fake over real HTTP (so the client's guard rails run); MusicBrainz
is stubbed at the cached-lookup layer.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import copy
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from tmp_dirs import temp_dir  # noqa: E402

RG = "11111111-1111-1111-1111-111111111111"
RG2 = "22222222-2222-2222-2222-222222222222"
ARTIST = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def mb_release(rid, date, tracks, fmt="CD", discs=1):
    per = tracks // discs
    return {"id": rid, "date": date, "status": "Official", "country": "US", "title": "Album",
            "media": [{"format": fmt, "track-count": per} for _ in range(discs)]}


def lidarr_release(rid, tracks, monitored=False, discs=1, fmt="CD"):
    return {"id": abs(hash(rid)) % 10000, "foreignReleaseId": rid, "title": "Album", "trackCount": tracks,
            "mediumCount": discs, "format": fmt, "status": "Official", "monitored": monitored, "country": ["United States"]}


def lidarr_album(aid, rgid, releases, monitored=True, have=0, artist_id=1, title="Album"):
    pinned = next((r for r in releases if r["monitored"]), None)
    return {"id": aid, "foreignAlbumId": rgid, "title": title, "artistId": artist_id, "monitored": monitored,
            "anyReleaseOk": True, "albumType": "Album", "secondaryTypes": [], "releaseDate": "1974-01-01T00:00:00Z",
            "releases": releases, "images": [],
            "statistics": {"trackFileCount": have, "trackCount": pinned["trackCount"] if pinned else 0, "sizeOnDisk": 0},
            "artist": {"id": artist_id, "foreignArtistId": ARTIST, "artistName": "Big Star"}}


class FakeLidarr(BaseHTTPRequestHandler):
    state = {}
    log = []

    def log_message(self, *a):
        pass

    def _send(self, status, body):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        return json.loads(raw or b"null")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        FakeLidarr.log.append(("GET", u.path, q))
        st = FakeLidarr.state
        albums = st.setdefault("albums", {})
        if u.path == "/api/v1/artist":
            return self._send(200, st.get("artists", []))
        if u.path.startswith("/api/v1/artist/"):
            aid = int(u.path.rsplit("/", 1)[1])
            return self._send(200, next(a for a in st.get("artists", []) if a["id"] == aid))
        if u.path == "/api/v1/album":
            if "foreignAlbumId" in q:
                return self._send(200, [a for a in albums.values() if a["foreignAlbumId"] == q["foreignAlbumId"]])
            if "artistId" in q:
                return self._send(200, [a for a in albums.values() if a["artistId"] == int(q["artistId"])])
        if u.path.startswith("/api/v1/album/") and u.path != "/api/v1/album/lookup":
            return self._send(200, albums[int(u.path.rsplit("/", 1)[1])])
        if u.path == "/api/v1/album/lookup":
            return self._send(200, st.get("lookup", []))
        if u.path == "/api/v1/queue":
            return self._send(200, {"records": st.get("queue", []), "totalRecords": len(st.get("queue", []))})
        if u.path == "/api/v1/trackfile":
            multi = parse_qs(u.query)
            if "trackFileIds" in multi:
                ids = {int(i) for i in multi["trackFileIds"]}
                return self._send(200, [f for f in st.get("files", []) if f["id"] in ids])
            aid = int(q["albumId"])
            return self._send(200, [f for f in st.get("files", []) if f["id"] in st.get("mapped", {}).get(aid, set())])
        if u.path == "/api/v1/command":
            return self._send(200, st.get("commands", []))
        if u.path == "/api/v1/config/mediamanagement":
            return self._send(200, {"recycleBin": st.get("recycle_bin", "")})
        self._send(404, {"message": "no route"})

    def do_DELETE(self):
        u = urlparse(self.path)
        FakeLidarr.log.append(("DELETE", u.path, None))
        st = FakeLidarr.state
        if u.path.startswith("/api/v1/trackfile/"):
            fid = int(u.path.rsplit("/", 1)[1])
            st["files"] = [f for f in st.get("files", []) if f["id"] != fid]
            return self._send(200, None)
        self._send(404, {})

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        FakeLidarr.log.append(("POST", u.path, body))
        st = FakeLidarr.state
        if u.path == "/api/v1/album":
            added = copy.deepcopy(st["after_add"])
            st.setdefault("albums", {})[added["id"]] = added
            if st.get("add_answer_delay"):   # Lidarr keeps the album, the answer comes late
                import time
                time.sleep(st["add_answer_delay"])
            return self._send(201, added)
        if u.path == "/api/v1/command":
            return self._send(201, {"id": 9, "name": body.get("name")})
        self._send(404, {})

    def do_PUT(self):
        u = urlparse(self.path)
        body = self._body()
        FakeLidarr.log.append(("PUT", u.path, body))
        st = FakeLidarr.state
        if u.path == "/api/v1/album/monitor":
            for aid in body["albumIds"]:
                st["albums"][aid]["monitored"] = body["monitored"]
            return self._send(202, [])
        if u.path.startswith("/api/v1/album/"):
            st["albums"][body["id"]] = body
            if st.get("on_pin"):
                st["on_pin"](body)  # what Lidarr does after a pin: rescan, re-match files
            return self._send(202, body)
        self._send(404, {})


class _Base(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        FakeLidarr.state, FakeLidarr.log = {}, []
        srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLidarr)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = create_engine(f"sqlite:///{tmp.name}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.user = mdb.TentacleUser(id=1, jellyfin_user_id="u1", display_name="u", is_admin=True)
        self.db.add(self.user)
        self.db.commit()
        for k, v in {"music_enabled": "true", "lidarr_url": f"http://127.0.0.1:{srv.server_port}",
                     "lidarr_api_key": "k", "lidarr_root_folder": "/data/music",
                     "lidarr_quality_profile_id": "3", "lidarr_metadata_profile_id": "1",
                     "musicbrainz_contact": "me@example.com", "data_dir": tmp.name,
                     "music_webhook_secret": "the-secret"}.items():
            mdb.set_setting(self.db, k, v)
        self.mb = {}  # rgid -> releases
        # autospec: the first argument is the MusicBrainz instance
        def lookup(_mb, rgid):
            value = self.mb[rgid]
            if isinstance(value, Exception):
                raise value
            return value
        p = mock.patch("services.musicbrainz.MusicBrainz.release_group_releases", autospec=True,
                       side_effect=lookup)
        p.start()
        self.addCleanup(p.stop)
        # Run worker jobs inline, in submission order, on this thread.
        import services.music.worker as worker
        self.jobs = []
        p2 = mock.patch.object(worker, "submit", side_effect=lambda fn, priority=1, label="": self.jobs.append(fn))
        p2.start()
        self.addCleanup(p2.stop)
        import services.music.library as library
        library._queue_cache["at"] = 0.0

    def run_jobs(self):
        while self.jobs:
            self.jobs.pop(0)(self.db)

    def calls(self, method, path_prefix=""):
        return [c for c in FakeLidarr.log if c[0] == method and c[1].startswith(path_prefix)]


class TestRequestAlbum(_Base):
    def _new_album_in_lidarr(self, releases):
        FakeLidarr.state["lookup"] = [{"foreignAlbumId": RG, "title": "Radio City",
                                       "artist": {"foreignArtistId": ARTIST, "artistName": "Big Star"}}]
        FakeLidarr.state["after_add"] = lidarr_album(10, RG, releases, title="Radio City")

    def test_a_new_album_adds_the_artist_monitoring_only_it_then_pins_and_searches(self):
        from services.media_requests import request_album
        self._new_album_in_lidarr([lidarr_release("r13", 13, monitored=True), lidarr_release("r12", 12)])
        self.mb[RG] = [mb_release("m1", "1974-01-01", 12, '12" Vinyl'), mb_release("m2", "1974-01-01", 13, "Digital Media")]
        out = request_album(self.db, RG, user_id=1, via="test")
        self.assertEqual(out["status"], "requested")

        post = self.calls("POST", "/api/v1/album")[0][2]
        self.assertTrue(post["monitored"])
        self.assertFalse(post["addOptions"]["searchForNewAlbum"])
        artist = post["artist"]
        self.assertEqual((artist["qualityProfileId"], artist["metadataProfileId"], artist["rootFolderPath"]),
                         (3, 1, "/data/music"))
        self.assertEqual(artist["monitorNewItems"], "none")
        # "none" alone would unmonitor this album on Lidarr's first scan of the artist.
        self.assertEqual(artist["addOptions"], {"monitor": "none", "albumsToMonitor": [RG],
                                                "searchForMissingAlbums": False})
        self.assertEqual(self.calls("POST", "/api/v1/command"), [])  # nothing searched before the pin

        self.run_jobs()
        put = self.calls("PUT", "/api/v1/album/10")[0][2]
        self.assertFalse(put["anyReleaseOk"])
        self.assertEqual([r["foreignReleaseId"] for r in put["releases"] if r["monitored"]], ["r12"])
        self.assertEqual(self.calls("POST", "/api/v1/command")[0][2], {"name": "AlbumSearch", "albumIds": [10]})
        from models.database import MusicAlbum
        row = self.db.query(MusicAlbum).filter_by(mbid=RG).one()
        self.assertEqual((row.requested_by, row.category), (1, "right"))

    def test_an_album_lidarr_has_unmonitored_is_monitored_not_re_added(self):
        from services.media_requests import request_album
        FakeLidarr.state["albums"] = {7: lidarr_album(7, RG, [lidarr_release("r12", 12)], monitored=False)}
        self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]
        request_album(self.db, RG, user_id=1, via="test")
        self.assertEqual(self.calls("POST", "/api/v1/album"), [])
        self.assertEqual(self.calls("PUT", "/api/v1/album/monitor")[0][2], {"albumIds": [7], "monitored": True})
        self.run_jobs()
        self.assertEqual(len(self.calls("POST", "/api/v1/command")), 1)

    def test_an_ambiguous_original_is_not_pinned_or_searched(self):
        from services.media_requests import request_album
        from models.database import MusicAlbum
        FakeLidarr.state["albums"] = {7: lidarr_album(7, RG, [lidarr_release("r9", 9), lidarr_release("r10", 10)])}
        self.mb[RG] = [mb_release(f"a{i}", "1977-09-29", 9) for i in range(3)] + \
                      [mb_release(f"b{i}", "1977-09-29", 10) for i in range(3)]
        request_album(self.db, RG, user_id=1, via="test")
        self.run_jobs()
        self.assertEqual(self.calls("PUT", "/api/v1/album/7"), [])
        self.assertEqual(self.calls("POST", "/api/v1/command"), [])
        row = self.db.query(MusicAlbum).filter_by(mbid=RG).one()
        self.assertEqual((row.category, row.verdict["reason"]), ("review", "tie"))
        self.assertEqual([o["tracks"] for o in row.verdict["options"]], [9, 10])

    def test_an_explicit_choice_is_pinned(self):
        from services.media_requests import request_album
        FakeLidarr.state["albums"] = {7: lidarr_album(7, RG, [lidarr_release("r9", 9), lidarr_release("r10", 10)])}
        self.mb[RG] = [mb_release(f"a{i}", "1977-09-29", 9) for i in range(3)] + \
                      [mb_release(f"b{i}", "1977-09-29", 10) for i in range(3)]
        request_album(self.db, RG, user_id=1, via="test", choice={"tracks": 10})
        self.run_jobs()
        put = self.calls("PUT", "/api/v1/album/7")[0][2]
        self.assertEqual([r["foreignReleaseId"] for r in put["releases"] if r["monitored"]], ["r10"])

    def test_refused_without_the_lidarr_defaults_or_with_the_module_off(self):
        from models.database import set_setting
        from services.media_requests import RequestRefused, request_album
        set_setting(self.db, "lidarr_metadata_profile_id", "")
        with self.assertRaises(RequestRefused) as e:
            request_album(self.db, RG, user_id=1, via="test")
        self.assertIn("metadata profile", e.exception.message)
        set_setting(self.db, "music_enabled", "false")
        with self.assertRaises(RequestRefused):
            request_album(self.db, RG, user_id=1, via="test")
        self.assertEqual(FakeLidarr.log, [])


class TestReconcile(_Base):
    def setUp(self):
        super().setUp()
        FakeLidarr.state["artists"] = [{"id": 1, "foreignArtistId": ARTIST, "artistName": "Big Star", "path": "/data/music/Big Star"}]
        FakeLidarr.state["albums"] = {
            1: lidarr_album(1, RG, [lidarr_release("r12", 12, monitored=True)], have=12),
            2: lidarr_album(2, RG2, [lidarr_release("box", 32, monitored=True, discs=4), lidarr_release("cd", 5)], have=0),
            3: lidarr_album(3, "33333333-3333-3333-3333-333333333333", [lidarr_release("x", 9, True)], monitored=False),
        }
        self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]
        self.mb[RG2] = [mb_release(f"w{i}", "1975-09-12", 5, '12" Vinyl') for i in range(3)]

    def test_reconcile_checks_monitored_albums_and_changes_nothing_in_lidarr(self):
        from models.database import MusicAlbum, get_setting
        from services.music import jobs
        jobs.reconcile("manual")(self.db)
        self.assertEqual(self.calls("PUT") + self.calls("POST"), [])
        cats = {r.mbid: r.category for r in self.db.query(MusicAlbum).all()}
        self.assertEqual(cats[RG], "right")
        self.assertEqual(cats[RG2], "repin_download")
        self.assertEqual(cats["33333333-3333-3333-3333-333333333333"], "")  # unmonitored: not checked
        summary = json.loads(get_setting(self.db, "music_last_reconcile"))
        self.assertEqual((summary["albums_checked"], summary["counts"]), (2, {"right": 1, "repin_download": 1}))
        self.assertFalse(jobs.progress["running"])

    def test_reads_lidarr_one_artist_at_a_time_without_include_flags(self):
        from services.music import jobs
        jobs.reconcile("manual")(self.db)
        album_reads = [q for m, p, q in FakeLidarr.log if p == "/api/v1/album"]
        self.assertTrue(album_reads and all(set(q) == {"artistId"} for q in album_reads), album_reads)

    def test_an_artist_removed_from_lidarr_leaves_the_snapshot(self):
        from models.database import MusicAlbum, MusicArtist
        from services.music import jobs
        jobs.reconcile("manual")(self.db)
        FakeLidarr.state["artists"] = []
        jobs.reconcile("manual")(self.db)
        self.assertEqual((self.db.query(MusicArtist).count(), self.db.query(MusicAlbum).count()), (0, 0))

    def test_a_failed_musicbrainz_lookup_is_counted_and_keeps_going(self):
        from services.music import jobs
        from services.musicbrainz import MusicBrainzError
        from models.database import get_setting
        self.mb[RG] = MusicBrainzError("rate limited")
        jobs.reconcile("manual")(self.db)
        summary = json.loads(get_setting(self.db, "music_last_reconcile"))
        self.assertEqual((summary["errors"], summary["albums_checked"]), (1, 1))


class TestWebhook(_Base):
    def setUp(self):
        super().setUp()
        FakeLidarr.state["artists"] = [{"id": 1, "foreignArtistId": ARTIST, "artistName": "Big Star"}]
        self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]
        self.mb[RG2] = [mb_release("m2", "1972-01-01", 12)]

    def test_artist_added_pins_only_albums_with_nothing_downloaded(self):
        from services.music import jobs
        FakeLidarr.state["albums"] = {
            1: lidarr_album(1, RG, [lidarr_release("deluxe", 20, True), lidarr_release("std", 12)], have=0),
            2: lidarr_album(2, RG2, [lidarr_release("deluxe2", 20, True), lidarr_release("std2", 12)], have=20),
        }
        job, event = jobs.handle_webhook({"eventType": "ArtistAdd", "artist": {"id": 1}})
        job(self.db)
        pinned = {c[1]: [r["foreignReleaseId"] for r in c[2]["releases"] if r["monitored"]] for c in self.calls("PUT", "/api/v1/album/")}
        self.assertEqual(pinned, {"/api/v1/album/1": ["std"]})  # album 2 has files: left for review
        self.assertEqual(self.calls("POST", "/api/v1/command")[0][2]["albumIds"], [1])

    def test_album_imported_is_checked_and_players_rescanned(self):
        from services.music import jobs
        FakeLidarr.state["albums"] = {1: lidarr_album(1, RG, [lidarr_release("std", 12, True)], have=12)}
        with mock.patch("services.music.players.rescan_all") as rescan:
            job, _ = jobs.handle_webhook({"eventType": "Download", "album": {"id": 1}, "artist": {"id": 1}})
            job(self.db)
        rescan.assert_called_once()
        self.assertEqual(self.calls("PUT"), [])
        from models.database import MusicAlbum
        self.assertEqual(self.db.query(MusicAlbum).filter_by(mbid=RG).one().category, "right")


class TestSongsAndSearch(unittest.TestCase):
    def _mb(self, recordings, relations=()):
        mb = mock.Mock()
        mb.artist.return_value = {"relations": list(relations)}
        mb.recordings_by.return_value = recordings
        return mb

    @staticmethod
    def _rec(rid, title, releases):
        return {"id": rid, "title": title, "releases": releases}

    @staticmethod
    def _rel(rgid, date, primary="Album", secondary=None, status="Official", title="X"):
        return {"status": status, "date": date, "title": title,
                "release-group": {"id": rgid, "primary-type": primary, "secondary-types": secondary or [], "title": title}}

    def test_the_earliest_studio_album_wins_over_compilations_live_and_singles(self):
        from services.music.browse import original_album_for_song
        mb = self._mb([
            self._rec("rec1", "Purple Haze", [self._rel("single", "1967-03-17", "Single"),
                                              self._rel("aye", "1967-08-23", title="Are You Experienced"),
                                              self._rel("comp", "1966-01-01", "Album", ["Compilation"])]),
            self._rec("rec2", "Purple Haze (live)", [self._rel("live", "1965-01-01", "Album", ["Live"])]),
            self._rec("rec3", "Purple Haze", [self._rel("boot", "1960-01-01", status="Bootleg")]),
        ], relations=[{"type": "member of band", "artist": {"id": "band-id"}}])
        found = original_album_for_song(mb, "Purple Haze", "jimi-id")
        self.assertEqual(found["album"]["id"], "aye")
        self.assertEqual(found["recordings"], ["rec1", "rec3"])
        # The band a solo artist was in is searched too (The Jimi Hendrix Experience).
        self.assertEqual(mb.recordings_by.call_args.args[1], ["jimi-id", "band-id"])

    def test_a_song_only_on_singles_says_so(self):
        from services.music.browse import original_album_for_song
        mb = self._mb([self._rec("r", "Non-Album Song", [self._rel("s1", "1970-01-01", "Single"),
                                                         self._rel("e1", "1971-01-01", "EP")])])
        found = original_album_for_song(mb, "Non-Album Song", "a")
        self.assertIsNone(found["album"])
        self.assertEqual([s["id"] for s in found["singles"]], ["s1", "e1"])

    def test_titles_match_regardless_of_accents_and_apostrophes(self):
        from services.music.browse import normalize
        self.assertEqual(normalize("Don’t Stop Believin'"), normalize("dont stop believin"))
        self.assertEqual(normalize("Déjà vu"), "deja vu")

    def test_bootleg_only_albums_are_left_out(self):
        from services.music.browse import _has_official
        from services.musicbrainz import MusicBrainz
        self.assertFalse(_has_official({"releases": [{"status": "Bootleg"}]}))
        self.assertTrue(_has_official({"releases": [{"status": "Bootleg"}, {"status": "Official"}]}))
        self.assertTrue(_has_official({}))  # unknown: keep
        mb = MusicBrainz("me@example.com", temp_dir(self))
        with mock.patch.object(mb, "_browse_all", return_value=[]) as browse:
            mb.artist_release_groups("x")
        self.assertEqual(browse.call_args.args[1]["release-group-status"], "website-default")

    def test_search_input_cannot_change_the_query(self):
        from services.musicbrainz import lucene_phrase, lucene_quote
        self.assertEqual(lucene_phrase('title:"x" AND arid:y'), 'title\\:\\"x\\" AND arid\\:y')
        self.assertEqual(lucene_quote('a"b'), '"a\\"b"')


class TestEndpoints(_Base):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import routers.music as music
        from models.database import get_db
        from routers.auth import get_user_from_request
        app = FastAPI()
        app.include_router(music.router)
        app.include_router(music.webhook_router)
        app.dependency_overrides[get_db] = lambda: self.Session()
        app.dependency_overrides[get_user_from_request] = lambda: self.user
        self.client = TestClient(app)

    def test_everything_is_404_while_the_module_is_off(self):
        from models.database import set_setting
        set_setting(self.db, "music_enabled", "false")
        for url in (f"/api/music/search?q=big+star", f"/api/music/album/{RG}", f"/api/music/artist/{ARTIST}",
                    "/api/music/library"):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(self.client.post("/api/music/request", json={"mbid": RG}).status_code, 404)
        self.assertEqual(self.client.get("/api/music/config").json()["enabled"], False)

    def test_inside_jellyfin_music_needs_the_jellyfin_music_integration(self):
        cfg = self.client.get("/api/music/config?api_key=tok").json()
        self.assertEqual((cfg["enabled"], cfg["show"]), (True, False))
        self.assertEqual(self.client.get("/api/music/library?api_key=tok").status_code, 404)
        self.assertEqual(self.client.get("/api/music/library").status_code, 200)

    def test_status_is_cheap_and_needs_a_secret_or_an_admin(self):
        from models.database import MusicAlbum
        self.db.add_all([MusicAlbum(mbid=RG, title="A", monitored=True, category="right"),
                         MusicAlbum(mbid=RG2, title="B", monitored=True, category="review"),
                         MusicAlbum(mbid="x", title="C", monitored=True, category="")])
        self.db.commit()
        FakeLidarr.log.clear()
        with mock.patch("services.music.status.integrations", return_value={"lidarr": {"ok": True}}):
            ok = self.client.get("/api/music/status?secret=the-secret")
        self.assertEqual(ok.status_code, 200)
        body = ok.json()
        self.assertEqual((body["counts"]["right"], body["needs_review"], body["counts"]["unchecked"]), (1, 1, 1))
        self.assertEqual(FakeLidarr.log, [])  # no Lidarr call to answer it
        self.assertIn(self.client.get("/api/music/status?secret=wrong").status_code, (401, 403))

    def test_search_filters_weak_matches_bootlegs_and_duplicate_songs(self):
        from models.database import MusicAlbum
        self.db.add(MusicAlbum(mbid=RG, title="Radio City", monitored=True, lidarr_album_id=1,
                               track_count=12, track_file_count=12, category="right"))
        self.db.commit()
        artists = [{"id": ARTIST, "name": "Big Star", "score": 100},
                   {"id": "b", "name": "Radio Star", "score": 87}]
        groups = [{"id": RG, "title": "Radio City", "score": 100, "primary-type": "Album",
                   "releases": [{"status": "Official"}], "artist-credit": [{"name": "Big Star", "artist": {"id": ARTIST}}]},
                  {"id": RG2, "title": "Radio City (bootleg)", "score": 90, "primary-type": "Album",
                   "releases": [{"status": "Bootleg"}]}]
        rec = lambda rid: {"id": rid, "title": "September Gurls", "artist-credit": [{"name": "Big Star", "artist": {"id": ARTIST}}]}
        with mock.patch("services.musicbrainz.MusicBrainz.search_artists", return_value=artists), \
             mock.patch("services.musicbrainz.MusicBrainz.search_release_groups", return_value=groups), \
             mock.patch("services.musicbrainz.MusicBrainz.search_recordings", return_value=[rec("r1"), rec("r2")]):
            data = self.client.get("/api/music/search?q=big+star+radio+city").json()
        self.assertEqual([a["name"] for a in data["artists"]], ["Big Star"])
        self.assertEqual([(a["title"], a["status"]) for a in data["albums"]], [("Radio City", "in_library")])
        self.assertEqual([s["recording_mbid"] for s in data["songs"]], ["r1"])

    def test_library_groups_by_artist_with_statuses(self):
        from models.database import MusicAlbum
        self.db.add_all([
            MusicAlbum(mbid=RG, title="Radio City", artist_name="Big Star", monitored=True, lidarr_album_id=1,
                       track_count=12, track_file_count=12, category="right", release_date="1974-01-01"),
            MusicAlbum(mbid=RG2, title="#1 Record", artist_name="Big Star", monitored=True, lidarr_album_id=2,
                       track_count=12, track_file_count=0, category="right", release_date="1972-01-01"),
            MusicAlbum(mbid="33333333-3333-3333-3333-333333333333", title="Third", artist_name="Big Star",
                       monitored=True, lidarr_album_id=3, track_count=14, track_file_count=0, category="review"),
        ])
        self.db.commit()
        FakeLidarr.state["queue"] = [{"albumId": 2, "size": 100, "sizeleft": 25}]
        data = self.client.get("/api/music/library").json()
        albums = {a["title"]: a for a in data["artists"][0]["albums"]}
        self.assertEqual(albums["Radio City"]["status"], "in_library")
        self.assertEqual((albums["#1 Record"]["status"], albums["#1 Record"]["progress"]), ("downloading", 75))
        self.assertEqual(albums["Third"]["status"], "needs_review")
        self.assertEqual(data["counts"]["needs_review"], 1)
        only = self.client.get("/api/music/library?status=needs_review").json()
        self.assertEqual([a["title"] for a in only["artists"][0]["albums"]], ["Third"])


class TestLidarrQualityCheck(unittest.TestCase):
    def test_lossless_only_profile_is_flagged(self):
        from services.service_checks import allowed_qualities, LOSSLESS
        flac_only = {"items": [{"quality": {"name": "FLAC"}, "allowed": True},
                               {"quality": {"name": "MP3-320"}, "allowed": False}]}
        mixed = {"items": [{"name": "Lossless", "allowed": True, "items": [{"quality": {"name": "FLAC"}}]},
                           {"quality": {"name": "MP3-320"}, "allowed": True}]}
        self.assertTrue(all(q.lower() in LOSSLESS for q in allowed_qualities(flac_only)))
        self.assertFalse(all(q.lower() in LOSSLESS for q in allowed_qualities(mixed)))


if __name__ == "__main__":
    unittest.main()
