"""Music module, phase 3: applying reconcile verdicts and artist pictures.

Applying goes through a fake Lidarr over real HTTP that behaves like Lidarr
after a pin (it queues a rescan and re-matches files); nothing deletes a file
unless exactly the expected tracks are left over.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from test_music_core import (  # noqa: E402
    ARTIST, RG, FakeLidarr, _Base, lidarr_album, lidarr_release, mb_release,
)


def _files(ids, album_id=1):
    return [{"id": i, "albumId": album_id, "path": f"/data/music/A/Album/{i:02d}.flac", "size": 1000} for i in ids]


class _Apply(_Base):
    """An album pinned to a 17-track edition, 17 files; the original has 12."""

    def setUp(self):
        super().setUp()
        from models.database import set_setting
        self.deluxe = lidarr_release("deluxe", 17, monitored=True)
        self.std = lidarr_release("std", 12)
        FakeLidarr.state["artists"] = [{"id": 1, "foreignArtistId": ARTIST, "artistName": "Big Star"}]
        FakeLidarr.state["albums"] = {1: lidarr_album(1, RG, [self.deluxe, self.std], have=17)}
        FakeLidarr.state["files"] = _files(range(1, 18))
        FakeLidarr.state["mapped"] = {1: set(range(1, 18))}
        FakeLidarr.state["commands"] = [{"id": 40, "name": "RescanFolders", "status": "completed"}]
        self.mb[RG] = [mb_release(f"m{i}", "1972-01-01", 12) for i in range(3)]
        self.rematch = set(range(1, 13))
        self.rescan_status = "completed"

        def on_pin(body):
            st = FakeLidarr.state
            pinned = next(r for r in body["releases"] if r["monitored"])
            st["commands"].append({"id": 41, "name": "RescanFolders", "status": self.rescan_status})
            st["mapped"][1] = set(self.rematch) if pinned["foreignReleaseId"] == "std" else set(range(1, 18))
            st["albums"][1]["statistics"] = {"trackFileCount": len(st["mapped"][1]),
                                             "trackCount": pinned["trackCount"], "sizeOnDisk": 0}
        FakeLidarr.state["on_pin"] = on_pin
        from services.music import jobs, library
        from models.database import MusicAlbum
        library.sync_artist(self.db, library.lidarr_client(self.db), FakeLidarr.state["artists"][0])
        self.row = self.db.query(MusicAlbum).filter_by(mbid=RG).one()
        album = FakeLidarr.state["albums"][1]
        from services.musicbrainz import MusicBrainz
        from services.music.original import Prefs
        self.mbc, self.prefs = MusicBrainz.from_settings(self.db), Prefs()
        library.check_album(self.db, self.row, album, self.mbc, self.prefs)

    def apply(self, **kw):
        from services.music.apply import apply_album
        return apply_album(self.db, self.row, library_client(self.db), self.mbc, self.prefs,
                           sleep=lambda s: None, **kw)

    def deletes(self):
        return [c[1] for c in FakeLidarr.log if c[0] == "DELETE"]


def library_client(db):
    from services.music import library
    return library.lidarr_client(db)


class TestTrim(_Apply):
    def test_re_pin_then_remove_exactly_the_extra_tracks_through_lidarr(self):
        from models.database import DeletionLog
        self.assertEqual(self.row.category, "repin_trim")
        verdict = self.apply(expected="repin_trim")
        self.assertEqual(verdict.category, "right")
        self.assertEqual(sorted(self.deletes()), sorted(f"/api/v1/trackfile/{i}" for i in range(13, 18)))
        self.assertEqual(self.db.query(DeletionLog).filter_by(kind="music-extra-track").count(), 5)

    def test_nothing_is_deleted_when_lidarr_matched_fewer_tracks(self):
        from services.music.apply import ApplyStopped
        self.rematch = set(range(1, 11))  # only 10 of 12 found their file
        with self.assertRaises(ApplyStopped) as e:
            self.apply(expected="repin_trim")
        self.assertIn("matched 10", e.exception.message)
        self.assertEqual(self.deletes(), [])

    def test_nothing_is_deleted_when_the_leftovers_are_not_what_was_expected(self):
        from services.music.apply import ApplyStopped
        FakeLidarr.state["files"] = [f for f in FakeLidarr.state["files"] if f["id"] != 17]  # Lidarr lost one
        with self.assertRaises(ApplyStopped) as e:
            self.apply(expected="repin_trim")
        self.assertIn("Expected 5 extra tracks", e.exception.message)
        self.assertEqual(self.deletes(), [])

    def test_a_failed_rescan_stops_before_anything_is_deleted(self):
        from services.music.apply import ApplyStopped
        self.rescan_status = "failed"
        with self.assertRaises(ApplyStopped):
            self.apply(expected="repin_trim")
        self.assertEqual(self.deletes(), [])

    def test_a_rescan_that_never_finishes_times_out_safely(self):
        from services.music.apply import ApplyStopped, wait_for_rescan
        self.rescan_status = "started"
        ticks = iter(range(0, 10000, 60))
        with self.assertRaises(ApplyStopped):
            from services.music import apply as ap
            with mock.patch.object(ap, "wait_for_rescan",
                                   lambda c, known, sleep, clock: wait_for_rescan(c, known, sleep=lambda s: None,
                                                                                  clock=lambda: next(ticks))):
                self.apply(expected="repin_trim")
        self.assertEqual(self.deletes(), [])

    def test_a_changed_verdict_applies_nothing(self):
        from services.music.apply import ApplyStopped
        with self.assertRaises(ApplyStopped) as e:
            self.apply(expected="repin")
        self.assertIn("changed", e.exception.message)
        self.assertEqual([c for c in FakeLidarr.log if c[0] in ("PUT", "DELETE")], [])

    def test_the_rescan_is_recognised_by_id_not_clock(self):
        from services.music.apply import wait_for_rescan
        FakeLidarr.state["commands"] = [{"id": 5, "name": "RescanFolders", "status": "started"}]  # an older one
        calls = {"n": 0}

        def sleep(_):
            calls["n"] += 1
            if calls["n"] == 2:
                FakeLidarr.state["commands"].append({"id": 6, "name": "RescanFolders", "status": "completed"})
        wait_for_rescan(library_client(self.db), {5}, sleep=sleep)
        self.assertEqual(calls["n"], 2)  # waited for the new one; the old running one didn't count


class TestRepinAndDownload(_Base):
    def test_re_pin_with_files_that_fit(self):
        from services.music import jobs, library
        FakeLidarr.state["artists"] = [{"id": 1, "foreignArtistId": ARTIST, "artistName": "Big Star"}]
        two_lp, cd = lidarr_release("lp", 9, monitored=True, discs=2), lidarr_release("cd", 9)
        FakeLidarr.state["albums"] = {1: lidarr_album(1, RG, [two_lp, cd], have=9)}
        FakeLidarr.state["files"] = _files(range(1, 10))
        FakeLidarr.state["mapped"] = {1: set(range(1, 10))}
        FakeLidarr.state["commands"] = []
        FakeLidarr.state["on_pin"] = lambda body: FakeLidarr.state["commands"].append(
            {"id": 1, "name": "RescanFolders", "status": "completed"})
        self.mb[RG] = [mb_release(f"m{i}", "1985-01-01", 9) for i in range(3)]
        jobs.reconcile("t")(self.db)
        from models.database import MusicAlbum
        self.assertEqual(self.db.query(MusicAlbum).filter_by(mbid=RG).one().category, "repin")
        self.assertEqual(self.calls("PUT"), [])  # the check alone changes nothing
        with mock.patch("services.music.apply.time.sleep"):
            jobs.apply_albums([RG])(self.db)
        pins = [c[2] for c in self.calls("PUT", "/api/v1/album/1")]
        self.assertEqual([r["foreignReleaseId"] for r in pins[0]["releases"] if r["monitored"]], ["cd"])
        self.assertEqual(self.calls("DELETE"), [])
        self.assertEqual(self.calls("POST", "/api/v1/command"), [])

    def test_re_pin_and_download_searches(self):
        from services.music import jobs
        FakeLidarr.state["artists"] = [{"id": 1, "foreignArtistId": ARTIST, "artistName": "Big Star"}]
        FakeLidarr.state["albums"] = {1: lidarr_album(1, RG, [lidarr_release("box", 32, True, discs=4),
                                                              lidarr_release("cd", 5)], have=0)}
        self.mb[RG] = [mb_release(f"m{i}", "1975-01-01", 5) for i in range(3)]
        jobs.reconcile("t")(self.db)
        jobs.apply_albums([RG])(self.db)
        self.assertEqual(self.calls("POST", "/api/v1/command")[0][2], {"name": "AlbumSearch", "albumIds": [1]})

    def test_auto_apply_is_off_by_default_and_on_per_category(self):
        from models.database import set_setting
        from services.music import jobs
        FakeLidarr.state["artists"] = [{"id": 1, "foreignArtistId": ARTIST, "artistName": "Big Star"}]
        FakeLidarr.state["albums"] = {1: lidarr_album(1, RG, [lidarr_release("box", 32, True, discs=4),
                                                              lidarr_release("cd", 5)], have=0)}
        self.mb[RG] = [mb_release(f"m{i}", "1975-01-01", 5) for i in range(3)]
        with mock.patch("services.music.jobs.picture_pass", return_value={}):
            jobs.reconcile("t")(self.db)
            self.assertEqual(self.calls("PUT") + self.calls("POST"), [])
            set_setting(self.db, "music_auto_repin_download", "true")
            jobs.reconcile("t")(self.db)
        self.assertEqual(len(self.calls("PUT", "/api/v1/album/1")), 1)
        self.assertEqual(len(self.calls("POST", "/api/v1/command")), 1)

    def test_locking_right_albums_changes_only_any_release_ok(self):
        from services.music import jobs
        FakeLidarr.state["artists"] = [{"id": 1, "foreignArtistId": ARTIST, "artistName": "Big Star"}]
        FakeLidarr.state["albums"] = {1: lidarr_album(1, RG, [lidarr_release("cd", 12, True), lidarr_release("x", 20)], have=12)}
        self.mb[RG] = [mb_release(f"m{i}", "1974-01-01", 12) for i in range(3)]
        jobs.reconcile("t")(self.db)
        jobs.lock_albums()(self.db)
        put = self.calls("PUT", "/api/v1/album/1")[0][2]
        self.assertFalse(put["anyReleaseOk"])
        self.assertEqual([r["foreignReleaseId"] for r in put["releases"] if r["monitored"]], ["cd"])


# ── Pictures ─────────────────────────────────────────────────────────────

def _png(draw):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (300, 300), (225, 225, 225))
    draw(im, ImageDraw.Draw(im))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def grey_star():
    return _png(lambda im, d: d.polygon([(150, 40), (180, 120), (260, 120), (195, 170), (220, 255),
                                         (150, 205), (80, 255), (105, 170), (40, 120), (120, 120)], fill="white"))


def photo(grayscale=False):
    import random
    rnd = random.Random(7)

    def draw(im, d):
        for _ in range(400):
            x, y = rnd.randrange(300), rnd.randrange(300)
            c = rnd.randrange(256)
            fill = (c, c, c) if grayscale else (c, rnd.randrange(256), rnd.randrange(256))
            d.ellipse((x, y, x + rnd.randrange(10, 60), y + rnd.randrange(10, 60)), fill=fill)
    return _png(draw)


class TestPlaceholders(unittest.TestCase):
    def test_flat_graphics_are_placeholders_and_photos_are_not(self):
        from services.music.pictures import looks_like_placeholder
        self.assertTrue(looks_like_placeholder(grey_star()))
        self.assertTrue(looks_like_placeholder(b"not an image"))
        self.assertFalse(looks_like_placeholder(photo()))
        self.assertFalse(looks_like_placeholder(photo(grayscale=True)))  # black-and-white photos are fine

    def test_placeholder_urls(self):
        from services.music.pictures import is_placeholder_url
        self.assertTrue(is_placeholder_url("https://lastfm.freetls.fastly.net/i/u/300x300/2a96cbd8b46e442fc41c2b86b821562f.png"))
        self.assertTrue(is_placeholder_url("https://e-cdns-images.dzcdn.net/images/artist//1000x1000-000000-80-0-0.jpg"))
        self.assertFalse(is_placeholder_url("https://cdn-images.dzcdn.net/images/artist/d6c4df6ab97ccf7e5b68e8ac494a0bdc/1000x1000.jpg"))


class TestNames(unittest.TestCase):
    def test_clarifying_notes_are_stripped_before_matching(self):
        from services.music.pictures import same_name, strip_note
        self.assertEqual(strip_note("Crowbar (Canadian band)"), "Crowbar")
        self.assertTrue(same_name("Crowbar", "Crowbar (Canadian band)"))
        self.assertTrue(same_name("Blue Öyster Cult", "Blue Oyster Cult"))
        self.assertFalse(same_name("Big Star", "Big Star Tribute Band"))  # partial is not a match

    def test_deezer_needs_exactly_one_exact_match_with_a_real_picture(self):
        from services.music.pictures import choose_deezer
        c = lambda name, pic="https://cdn/x.jpg", fans=0: {"id": 1, "name": name, "picture": pic, "fans": fans}
        self.assertEqual(choose_deezer("Big Star", [c("Big Star"), c("Big Star Tribute")])[0], "https://cdn/x.jpg")
        self.assertIsNone(choose_deezer("Big Star", [c("Big Star Tribute")])[0])
        url, why = choose_deezer("Crowbar", [c("Crowbar", fans=2), c("Crowbar", fans=5)])
        self.assertIsNone(url)
        self.assertIn("2 artists", why)
        self.assertIsNone(choose_deezer("X", [c("X", "https://cdn/images/artist//1000x1000.jpg")])[0])

    def test_deezer_names_keep_their_own_notes(self):
        # Only OUR clarifying note is dropped: "Billy Joel (Karaoke)" is someone else.
        from services.music.pictures import choose_deezer
        cands = [{"id": 1, "name": "Billy Joel", "picture": "https://cdn/bj.jpg", "fans": 860720},
                 {"id": 2, "name": "Billy Joel (Karaoke)", "picture": "https://cdn/k.jpg", "fans": 88}]
        self.assertEqual(choose_deezer("Billy Joel", cands)[0], "https://cdn/bj.jpg")

    def test_a_dominant_duplicate_profile_is_the_artist(self):
        # Real Deezer data, Sept 2026.
        from services.music.pictures import choose_deezer
        dire = [{"id": 1, "name": "Dire Straits", "picture": "https://cdn/ds.jpg", "fans": 2288926},
                {"id": 2, "name": "Dire Straits", "picture": "https://cdn/other.jpg", "fans": 1489}]
        self.assertEqual(choose_deezer("Dire Straits", dire)[0], "https://cdn/ds.jpg")
        close = [{"id": 1, "name": "Crowbar", "picture": "https://cdn/a.jpg", "fans": 21087},
                 {"id": 2, "name": "Crowbar", "picture": "https://cdn/b.jpg", "fans": 9000}]
        self.assertIsNone(choose_deezer("Crowbar", close)[0])  # too close to call

    def test_the_review_picker_offers_real_pictures_of_the_exact_name(self):
        from services.music import pictures
        blank = "https://cdn/images/artist//250x250-000000-80-0-0.jpg"
        data = {"data": [
            {"id": 1, "name": "Crowbar", "picture_xl": blank, "picture_medium": blank},
            {"id": 2, "name": "Crowbar", "picture_xl": "https://cdn/a.jpg", "picture_medium": "https://cdn/a-s.jpg"},
            {"id": 3, "name": "Crowded House", "picture_xl": "https://cdn/c.jpg", "picture_medium": "https://cdn/c-s.jpg"}]}
        resp = mock.Mock(json=lambda: data, raise_for_status=lambda: None)
        with mock.patch.object(pictures.requests, "get", return_value=resp):
            found = pictures.deezer_candidates("Crowbar (Canadian band)")
        self.assertEqual([c["id"] for c in found], [2, 3])  # Deezer's blank picture is dropped
        self.assertEqual([c["id"] for c in pictures.review_candidates("Crowbar (Canadian band)", found)], [2])
        # No exact name on Deezer: offer everything, it may be spelled differently there.
        self.assertEqual(len(pictures.review_candidates("Crowbarr", found)), 2)


class _FakePlayer:
    id, name = "navidrome", "Navidrome"

    def __init__(self, state):
        self.state, self.uploads = state, []

    def artist_image_state(self, mbid, name):
        return self.state

    def set_artist_image(self, mbid, name, data):
        self.uploads.append(data)
        self.state = "ok"


class TestEnsurePicture(_Base):
    def setUp(self):
        super().setUp()
        from models.database import MusicArtist, set_setting
        self.artist = MusicArtist(mbid=ARTIST, name="Big Star", lidarr_artist_id=1, path="/data/music/Big Star")
        self.db.add(self.artist)
        self.db.commit()
        self.deezer = [{"id": 2411, "name": "Big Star", "picture": "https://cdn/bigstar.jpg", "thumb": "t"}]

    def run_with(self, player, **kw):
        from services.music import pictures
        with mock.patch.object(pictures, "_picture_players", return_value=[player]), \
             mock.patch.object(pictures, "deezer_candidates", return_value=self.deezer), \
             mock.patch.object(pictures, "download", return_value=photo()):
            return pictures.ensure_picture(self.db, self.artist, **kw)

    def test_a_player_showing_its_placeholder_gets_the_deezer_picture(self):
        p = _FakePlayer("missing")
        self.assertEqual(self.run_with(p), "set")
        self.assertEqual(len(p.uploads), 1)
        self.assertEqual(self.artist.picture_source, "deezer")

    def test_a_working_picture_is_left_alone(self):
        p = _FakePlayer("ok")
        self.assertEqual(self.run_with(p), "ok")
        self.assertEqual(p.uploads, [])

    def test_an_artist_the_player_has_not_scanned_waits(self):
        self.assertEqual(self.run_with(_FakePlayer("absent")), "waiting")

    def test_a_clarifying_note_goes_to_review_with_the_candidates(self):
        self.artist.disambiguation = "Canadian band"
        self.artist.name = "Crowbar"
        self.deezer = [{"id": 1, "name": "Crowbar", "picture": "https://cdn/c.jpg", "thumb": "t"}]
        p = _FakePlayer("missing")
        self.assertEqual(self.run_with(p), "review")
        self.assertEqual(p.uploads, [])
        self.assertIn("more than one", self.artist.picture_note)
        self.assertEqual(self.artist.picture_candidates[0]["id"], 1)

    def test_a_picture_you_chose_is_set_everywhere_and_not_re_uploaded_daily(self):
        p = _FakePlayer("ok")
        chosen = photo(grayscale=True)
        self.assertEqual(self.run_with(p, data=chosen, source="upload", force=True), "set")
        self.assertEqual(p.uploads, [chosen])
        self.assertEqual(self.run_with(p), "set")  # daily pass: the player shows it; nothing sent again
        self.assertEqual(len(p.uploads), 1)

    def test_a_real_artist_jpg_is_preferred_over_deezer(self):
        import os
        import tempfile
        from models.database import set_setting
        from services.music import pictures
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(f"{base}/Big Star")
            with open(f"{base}/Big Star/artist.jpg", "wb") as f:
                f.write(photo(grayscale=True))
            set_setting(self.db, "music_library_path", base)
            p = _FakePlayer("missing")
            self.assertEqual(self.run_with(p), "set")
            self.assertEqual(self.artist.picture_source, "artist.jpg")
            # a placeholder artist.jpg (an old Last.fm grey star) is ignored
            with open(f"{base}/Big Star/artist.jpg", "wb") as f:
                f.write(grey_star())
            self.assertIsNone(None if pictures.looks_like_placeholder(pictures.local_artist_jpg(self.db, self.artist.path)) else 1)

    def test_artist_folder_mapping_cannot_escape_the_music_folder(self):
        import tempfile
        from models.database import set_setting
        from services.music.pictures import local_artist_jpg
        with tempfile.TemporaryDirectory() as base:
            set_setting(self.db, "music_library_path", base)
            self.assertIsNone(local_artist_jpg(self.db, "/data/music/../../etc"))
            self.assertIsNone(local_artist_jpg(self.db, "/elsewhere/Big Star"))


class _FakeNavidrome(BaseHTTPRequestHandler):
    cover = b""
    uploads = []

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/artist"):
            return self._send(200, [{"id": "nd1", "name": "Big Star", "mbzArtistId": ARTIST}])
        if self.path.startswith("/rest/getCoverArt"):
            return self._send(200, _FakeNavidrome.cover, "image/png")
        self._send(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/auth/login":
            return self._send(200, {"token": "jwt", "isAdmin": True, "subsonicToken": "t", "subsonicSalt": "s"})
        if self.path == "/api/artist/nd1/image":
            _FakeNavidrome.uploads.append((self.headers.get("Content-Type"), raw))
            return self._send(200, {"status": "ok"})
        self._send(404, {})


class TestNavidromePictures(_Base):
    def setUp(self):
        super().setUp()
        from models.database import set_setting
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeNavidrome)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        _FakeNavidrome.uploads = []
        for k, v in {"navidrome_enabled": "true", "navidrome_url": f"http://127.0.0.1:{srv.server_port}",
                     "navidrome_username": "u", "navidrome_password": "p"}.items():
            set_setting(self.db, k, v)

    def test_placeholder_is_missing_photo_is_ok_and_upload_is_multipart_image(self):
        from services.music.players import NavidromePlayer
        p = NavidromePlayer(self.db)
        _FakeNavidrome.cover = grey_star()
        self.assertEqual(p.artist_image_state(ARTIST, "Big Star"), "missing")
        _FakeNavidrome.cover = photo()
        self.assertEqual(p.artist_image_state(ARTIST, "Big Star"), "ok")
        p.set_artist_image(ARTIST, "Big Star", b"JPEGDATA")
        ctype, raw = _FakeNavidrome.uploads[0]
        self.assertIn("multipart/form-data", ctype)
        self.assertIn(b'name="image"', raw)
        self.assertIn(b"JPEGDATA", raw)


class TestJellyfinPicture(unittest.TestCase):
    def test_primary_image_is_posted_base64(self):
        import base64
        from services.music.players import JellyfinPlayer
        p = JellyfinPlayer.__new__(JellyfinPlayer)
        p.url, p.key, p.library_id, p.db = "http://jf", "k", "lib", None
        with mock.patch.object(p, "_get", return_value={"Items": [
                {"Id": "a1", "Name": "Big Star", "ProviderIds": {"MusicBrainzArtist": ARTIST}, "ImageTags": {}}]}):
            self.assertEqual(p.artist_image_state(ARTIST, "Big Star"), "missing")
            with mock.patch("services.music.players.requests.post", return_value=mock.Mock(status_code=204)) as post:
                p.set_artist_image(ARTIST, "Big Star", b"\xff\xd8\xffJPEG")
        self.assertEqual(post.call_args.args[0], "http://jf/Items/a1/Images/Primary")
        self.assertEqual(base64.b64decode(post.call_args.kwargs["data"]), b"\xff\xd8\xffJPEG")
        self.assertEqual(post.call_args.kwargs["headers"]["Content-Type"], "image/jpeg")


class TestReviewEndpoints(_Base):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import routers.music as music
        from models.database import get_db
        app = FastAPI()
        app.include_router(music.router)
        from routers.auth import require_admin
        app.dependency_overrides[get_db] = lambda: self.Session()
        app.dependency_overrides[require_admin] = lambda: self.user  # a signed-in admin
        self.client = TestClient(app)

    def test_review_groups_and_group_apply_rules(self):
        from models.database import MusicAlbum, MusicArtist
        self.db.add_all([MusicAlbum(mbid=RG, title="A", monitored=True, category="repin_trim",
                                    verdict={"target": {"tracks": 12}, "pinned": {"tracks": 17}, "have": 17}),
                         MusicAlbum(mbid="x", title="B", monitored=True, category="right", any_release_ok=True),
                         MusicArtist(mbid=ARTIST, name="Crowbar", picture_status="review", picture_note="n")])
        self.db.commit()
        d = self.client.get("/api/music/review").json()
        self.assertEqual([a["title"] for a in d["groups"]["repin_trim"]], ["A"])
        self.assertEqual((d["unlocked"], [p["name"] for p in d["pictures"]]), (1, ["Crowbar"]))
        self.assertEqual(self.client.post("/api/music/apply", json={"category": "review"}).status_code, 400)
        self.assertEqual(self.client.post("/api/music/apply", json={"category": "repin_trim"}).json(), {"queued": 1})
        self.assertEqual(len(self.jobs), 1)

    def test_the_trim_group_says_whether_lidarr_keeps_deleted_tracks(self):
        import routers.music as music
        from models.database import MusicAlbum
        self.assertIsNone(self.client.get("/api/music/review").json()["recycle_bin"])  # nothing to trim: not asked
        self.db.add(MusicAlbum(mbid=RG, title="A", monitored=True, category="repin_trim", verdict={}))
        self.db.commit()
        for setting in ("", "/data/recycle"):
            music._recycle_bin_cache.update(at=None, value=None)
            FakeLidarr.state["recycle_bin"] = setting
            self.assertEqual(self.client.get("/api/music/review").json()["recycle_bin"], setting)
        music._recycle_bin_cache.update(at=None, value=None)

    def test_an_uploaded_placeholder_is_refused(self):
        from models.database import MusicArtist
        self.db.add(MusicArtist(mbid=ARTIST, name="Crowbar", picture_status="review"))
        self.db.commit()
        r = self.client.post(f"/api/music/artist/{ARTIST}/picture", files={"file": ("a.png", grey_star(), "image/png")})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/api/music/artist/{ARTIST}/picture", files={"file": ("a.png", photo(), "image/png")})
        self.assertEqual(r.json(), {"queued": True})


if __name__ == "__main__":
    unittest.main()
