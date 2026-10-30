"""Pictures for Live TV recordings (#815): what a recording is, the
pictures in order (each source failing in turn), and that Tentacle only
ever adds, replaces or removes a picture it set itself.

The fake Jellyfin does what 10.11 does with pictures: an upload gives the
item a new image tag; a delete takes it away; /Items lists the recordings
library with ImageTags.

Run from the tentacle/ directory:  python tests/hermetic.py test_recording_art
"""
import io
import random
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta
from unittest import mock

from tmp_dirs import temp_dir
from services.exceptions import TMDBConnectionError
from services.recording_art import cards as C
from services.recording_art import frames as F
from services.recording_art import job as J
from services.recording_art import what as W

NOW = datetime(2026, 10, 24, 12, 0)
LIB = "lib1"


def _db():
    import models.database as mdb
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _jpeg(color=(30, 120, 200), size=(1280, 720), noise=True) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, color)
    if noise:
        d = ImageDraw.Draw(img)
        rnd = random.Random(1)
        for _ in range(300):
            x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
            d.rectangle([x, y, x + 20, y + 20], fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    out = io.BytesIO()
    img.save(out, "JPEG")
    return out.getvalue()


# ── What a recording is ─────────────────────────────────────────────────────

class Titles(unittest.TestCase):
    def test_live_and_new_prefixes(self):
        self.assertEqual(("NHL Hockey", None), W.clean_title("Live: NHL Hockey"))
        self.assertEqual(("The Rookie", None), W.clean_title("New: The Rookie"))

    def test_jellyfins_file_name(self):
        # without an .nfo Jellyfin names the item after the file: ":" was written as two spaces
        self.assertEqual(("NHL Hockey", None), W.clean_title("Live  NHL Hockey 2026_10_23_20_00_00"))

    def test_split_recordings(self):
        self.assertEqual("Jeopardy!", W.clean_title("Jeopardy! 2026_10_23_19_30_00_1")[0])
        self.assertEqual("The Late Show", W.clean_title("The Late Show - Part 2")[0])
        self.assertEqual("The Late Show", W.clean_title("The Late Show (2)")[0])
        self.assertEqual("CBC News", W.clean_title("CBC News - 1")[0])

    def test_date_suffixes(self):
        self.assertEqual("Hockey Night in Canada", W.clean_title("Hockey Night in Canada - Sat, Oct 24")[0])
        self.assertEqual("Le Téléjournal", W.clean_title("Le Téléjournal 2026-10-23")[0])
        self.assertEqual("Match of the Day", W.clean_title("Match of the Day 24.10.2026")[0])
        self.assertEqual("La soirée du hockey", W.clean_title("La soirée du hockey - sam. 24 oct.")[0])

    def test_year_is_kept_apart(self):
        self.assertEqual(("Heat", 1995), W.clean_title("Heat (1995)"))
        # a number that's part of the title stays
        self.assertEqual(("Ocean's 11", None), W.clean_title("Ocean's 11"))
        self.assertEqual(("Apollo 13", None), W.clean_title("New: Apollo 13"))


class Games(unittest.TestCase):
    def game(self, title, desc="", sub="", channel="", category="", booking=None):
        s = W.identify(title, sub, desc, channel, category, booking=booking)
        self.assertEqual("game", s.kind, f"{title!r}: {s.kind} ({s.why})")
        return s

    def names(self, s):
        return [t["name"] for t in s.teams]

    def test_league_colon_home_dash_away(self):
        s = self.game("NHL: Toronto Maple Leafs - Montreal Canadiens")
        self.assertEqual("nhl", s.league["id"])
        self.assertEqual(["Toronto Maple Leafs", "Montreal Canadiens"], self.names(s))
        self.assertFalse(s.at)

    def test_league_dot_home_dash_away(self):
        s = self.game("Premier League. Arsenal - Chelsea")
        self.assertEqual("epl", s.league["id"])
        self.assertEqual(["Arsenal", "Chelsea"], self.names(s))

    def test_accents_and_french_names(self):
        s = self.game("Ligue 1. Paris Saint-Germain - Olympique de Marseille")
        self.assertEqual("ligue1", s.league["id"])
        s = self.game("Live: Fútbol: Atlético de Madrid vs. Sevilla")
        self.assertEqual("laliga", s.league["id"])
        self.assertEqual("Atlético de Madrid", s.teams[0]["name"])
        s = self.game("Hockey LNH : Canadiens de Montréal c. Maple Leafs de Toronto")
        self.assertEqual(["Montreal Canadiens", "Toronto Maple Leafs"], self.names(s))

    def test_at_puts_the_second_at_home(self):
        s = self.game("Canadiens at Blackhawks")
        self.assertEqual(["Montreal Canadiens", "Chicago Blackhawks"], self.names(s))
        self.assertTrue(s.at)
        # an event channel's slot, with its start time after the game
        s = self.game("Penguins@Maple Leafs @7")
        self.assertEqual(["Pittsburgh Penguins", "Toronto Maple Leafs"], self.names(s))
        self.assertTrue(s.at)

    def test_codes_in_the_description(self):
        s = self.game("Live: NHL Hockey", "TOR @ MTL. The Leafs visit the Habs.")
        self.assertEqual(["Toronto Maple Leafs", "Montreal Canadiens"], self.names(s))
        self.assertTrue(s.at)

    def test_arena_says_who_is_at_home(self):
        s = self.game("Molson Habs Hockey: Canadiens vs. Blackhawks", "From the United Center in Chicago.")
        self.assertEqual(["Montreal Canadiens", "Chicago Blackhawks"], self.names(s))
        self.assertTrue(s.at)

    def test_one_team_from_the_arena(self):
        s = self.game("Live: NHL Hockey", "From the United Center in Chicago.")
        self.assertEqual(["Chicago Blackhawks"], self.names(s))

    def test_matchup_in_the_description(self):
        s = self.game("Live: MLB Baseball", "Blue Jays at Yankees.")
        self.assertEqual(["Toronto Blue Jays", "New York Yankees"], self.names(s))

    def test_from_the_booking(self):
        # Lucas's real recording: the rule knew the Habs, the listing the arena
        s = self.game("Live: NHL Hockey", "From the United Center in Chicago.",
                      booking={"kind": "team", "league": "nhl", "team": "mtl",
                               "why": "same time as “Molson Habs Hockey: Canadiens vs. Blackhawks”, "
                                      "from the Blackhawks' arena"})
        self.assertEqual(["Montreal Canadiens", "Chicago Blackhawks"], self.names(s))
        self.assertTrue(s.at)
        self.assertIn("booked", s.why)

    def test_booking_without_an_opponent(self):
        s = self.game("Live: NHL Hockey", "", booking={"kind": "team", "league": "nhl", "team": "tor", "why": ""})
        self.assertEqual(["Toronto Maple Leafs"], self.names(s))

    def test_not_games(self):
        for title, desc in [("AHL Hockey: Toronto Marlies at Belleville Senators", ""),
                            ("Kings of Leon Live", ""), ("Canada vs. Sweden", "Rugby sevens."),
                            ("The Price Is Right", ""), ("Maple Leaf Pro Wrestling", "")]:
            s = W.identify(title, "", desc)
            self.assertNotEqual("game", s.kind, f"{title}: {s.describe()}")

    def test_league_events(self):
        s = W.identify("Formula 1: Mexico City Grand Prix")
        self.assertEqual(("event", "f1", "Mexico City Grand Prix"), (s.kind, s.league["id"], s.title))
        s = W.identify("Live: NHL Hockey")
        self.assertEqual(("event", "nhl"), (s.kind, s.league["id"]))
        s = W.identify("Live: F1 Grand Prix", booking={"kind": "series", "league": "f1"})
        self.assertEqual("event", s.kind)

    def test_shows_and_films(self):
        s = W.identify("New: Heat (1995)", category="Movie")
        self.assertEqual(("show", "Heat", 1995, True), (s.kind, s.title, s.year, s.film))
        s = W.identify("Jeopardy!")
        self.assertEqual(("show", "Jeopardy!", False), (s.kind, s.title, s.film))


# ── The pictures ────────────────────────────────────────────────────────────

def _size(data):
    from PIL import Image
    return Image.open(io.BytesIO(data)).size


class Cards(unittest.TestCase):
    def size(self, data):
        from PIL import Image
        return Image.open(io.BytesIO(data)).size

    def test_every_card_is_1280x720(self):
        s = W.identify("Canadiens at Blackhawks")
        self.assertEqual((1280, 720), self.size(C.matchup_card(s.league, s.teams, s.at, NOW, "TSN 2")))
        self.assertEqual((1280, 720), self.size(C.matchup_card(s.league, s.teams[:1], False, NOW, None)))
        self.assertEqual((1280, 720), self.size(C.league_card(s.league, "NHL Hockey", None, None)))
        self.assertEqual((1280, 720), self.size(C.title_card("A very long title " * 12, NOW, "CBS " * 30)))
        self.assertEqual((1280, 720), self.size(C.title_card("Ω Ζ Αθήνα – Ελλάδα", None, "ERT")))

    def test_every_poster_is_800x1200(self):
        s = W.identify("Canadiens at Blackhawks")
        self.assertEqual((800, 1200), self.size(C.matchup_poster(s.league, s.teams, s.at, NOW, "TSN 2")))
        self.assertEqual((800, 1200), self.size(C.matchup_poster(s.league, s.teams[:1], False, NOW, None)))
        self.assertEqual((800, 1200), self.size(C.league_poster(s.league, "NHL Hockey", None, None)))
        self.assertEqual((800, 1200), self.size(C.title_poster("A very long title " * 12, NOW, "CBS " * 30)))
        self.assertEqual((800, 1200), self.size(C.poster_from_wide(_jpeg(), "Jeopardy! " * 20, NOW, "CBS")))
        self.assertIsNone(C.poster_from_wide(b"not a picture", "Jeopardy!", NOW, None))

    def test_the_footer_stays_out_of_the_progress_bar(self):
        # TV clients draw a progress bar along the bottom of a card: no text there
        self.assertLessEqual(C.SAFE_Y, 0.88 * C.PH)
        self.assertLessEqual(C.WIDE_SAFE_Y, 0.92 * C.H)
        box = C.poster_footer_box()
        self.assertLess(box["rule"], box["when"][0])
        self.assertLessEqual(box["when"][1], box["channel"][0])
        self.assertLessEqual(box["channel"][1], C.SAFE_Y)
        rule, top, bottom = C.footer_box()
        self.assertLess(rule, top)
        self.assertLessEqual(bottom, C.WIDE_SAFE_Y)

    def test_the_matchup_fits_above_the_footer(self):
        foot = C.poster_footer_box()["rule"]
        one = C.matchup_poster_layout(182, foot, [59, 59], 45)
        self.assertEqual(200, one["badge"])          # one-line names: the badges keep their size
        two = C.matchup_poster_layout(182, foot, [126, 126], 45)
        self.assertLess(two["badge"], 200)            # two-line names: smaller badges, still in
        for lay, names in ((one, 59), (two, 126)):
            self.assertLessEqual(lay["bottom"], foot)
            self.assertGreaterEqual(lay["badges"][0] - lay["badge"] // 2, 182)
            self.assertLess(lay["names"][0] + names, lay["mid"])
            self.assertLess(lay["mid"], lay["badges"][1] - lay["badge"] // 2)

    def test_nothing_is_drawn_in_the_safe_area(self):
        # the cards' backgrounds are vertical gradients: a row with nothing on it is one colour
        from PIL import Image
        s = W.identify("Columbus Blue Jackets at Vegas Golden Knights")
        long = "Sportsnet One HD East " * 3
        posters = [C.matchup_poster(s.league, s.teams, s.at, NOW, long),
                   C.matchup_poster(s.league, s.teams[:1], False, NOW, long),
                   C.league_poster(s.league, "NHL Hockey " * 6, NOW, long),
                   C.title_poster("A very long title " * 12, NOW, long)]
        cards = [C.matchup_card(s.league, s.teams, s.at, NOW, long),
                 C.league_card(s.league, "NHL Hockey " * 6, NOW, long),
                 C.title_card("A very long title " * 12, NOW, long)]
        for data, safe in [(p, C.SAFE_Y) for p in posters] + [(c, C.WIDE_SAFE_Y) for c in cards]:
            img = Image.open(io.BytesIO(data)).convert("L")
            w, h = img.size
            px = img.load()
            for y in range(safe, h):
                row = [px[x, y] for x in range(0, w, 4)]
                self.assertLess(max(row) - min(row), 24, f"something drawn at y={y} of {w}x{h}")
            # and the footer itself is drawn just above it
            row = [px[x, y] for y in range(safe - 120, safe) for x in range(0, w, 4)]
            self.assertGreater(max(row) - min(row), 100)

    def test_to_2x3(self):
        self.assertEqual((800, 1200), self.size(C.to_2x3(_jpeg(size=(780, 1170)))))
        self.assertEqual((800, 1200), self.size(C.to_2x3(_jpeg(size=(600, 1000)))))
        self.assertIsNone(C.to_2x3(_jpeg(size=(1280, 720))))      # a wide picture isn't a poster
        self.assertIsNone(C.to_2x3(b"not a picture"))

    def test_to_16x9(self):
        self.assertEqual((1280, 720), self.size(C.to_16x9(_jpeg(size=(1920, 800)))))
        self.assertEqual((1280, 720), self.size(C.to_16x9(_jpeg(size=(1000, 1000)))))
        self.assertIsNone(C.to_16x9(_jpeg(size=(600, 900)), only_if_wide=True))   # a poster
        self.assertIsNotNone(C.to_16x9(_jpeg(size=(1280, 720)), only_if_wide=True))
        self.assertIsNone(C.to_16x9(b"not a picture"))


class FakeJellyfin:
    """Jellyfin's pictures: tags change with every upload."""

    def __init__(self, save_locally=False):
        self.items = {}
        self.images = {}          # (id, kind) -> (tag, bytes, owner)
        self.n = 0
        self.writes = []
        self.timers = []
        self.save_locally = save_locally
        self.reads = []
        self.data = {}
        self.recording = set()    # ids still recording (LiveTv/Recordings?IsInProgress=true)
        self.file_reads = []      # (id, start, length) through /Items/{id}/File
        self.sizes = {}
        self.gets = []

    def add(self, iid, name, created, minutes=60, parent=LIB, folder=False, overview="", container="ts",
            size=10_000_000):
        self.items[iid] = {"Id": iid, "Name": name, "ParentId": parent, "IsFolder": folder,
                           "DateCreated": created.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                           "RunTimeTicks": None if folder else minutes * 60 * 10_000_000,
                           "Overview": overview, "Container": container,
                           "MediaSources": [] if folder else [{"Size": size, "Container": container}]}
        if not folder:
            self.items[iid]["Path"] = f"/config/data/livetv/recordings/{name}/{name}.ts"

    def start(self, iid, name, started, until, overview=""):
        """A recording Jellyfin is making now, as 10.11 has it: an item of
        Type Recording (by id and in LiveTv/Recordings?IsInProgress for a
        user, not in the library's listing, no length yet), its timer runs."""
        self.add(iid, name, started, minutes=0, overview=overview, size=188 * 1000)
        self.items[iid].update(RunTimeTicks=None, Type="Recording")
        self.recording.add(iid)
        self.timers.append({"Name": name, "Status": "InProgress", "StartDate": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "EndDate": until.strftime("%Y-%m-%dT%H:%M:%SZ"), "PrePaddingSeconds": 0,
                            "PostPaddingSeconds": 0, "_id": iid})

    def finish(self, iid, minutes, at, new_id=None):
        """It finished at `at`: Jellyfin makes the file a Movie item of
        another id (`new_id`; None: the same item), created then, with its
        length; the in-progress item and its pictures go."""
        self.recording.discard(iid)
        self.timers = [t for t in self.timers if t.get("_id") != iid]
        item = dict(self.items[iid], Type="Movie", RunTimeTicks=minutes * 60 * 10_000_000,
                    MediaSources=[{"Size": 10_000_000, "Container": "ts"}],
                    DateCreated=at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"))
        if new_id:
            del self.items[iid]
            for k in [k for k in self.images if k[0] == iid]:
                del self.images[k]
            item["Id"] = new_id
        self.items[item["Id"]] = item

    def tags(self, iid):
        return {k: v[0] for (i, k), v in self.images.items() if i == iid}

    def _view(self, iid):
        return dict(self.items[iid], ImageTags=self.tags(iid))

    def put(self, iid, kind, owner, data=b"x"):
        self.n += 1
        self.images[(iid, kind)] = (f"tag{self.n}", data, owner)

    def get(self, path, **p):
        self.gets.append(path)
        if path == "/Users":
            return [{"Id": "u-2", "Name": "kid", "Policy": {"IsAdministrator": False}},
                    {"Id": "u-1", "Name": "admin", "Policy": {"IsAdministrator": True, "BlockedTags": []}}]
        if path == "/LiveTv/Recordings":
            assert p.get("IsInProgress") == "true"
            if p.get("UserId") != "u1":
                return {"Items": []}    # no user: nothing (10.11)
            return {"Items": [self._view(i) for i in sorted(self.recording) if i in self.items]}
        if path == "/System/Configuration/livetv":
            return {"RecordingPath": "", "MediaLocationsCreated": ["/config/data/livetv/recordings"]}
        if path == "/Library/VirtualFolders":
            return [{"Name": "Recordings", "ItemId": LIB, "Locations": ["/config/data/livetv/recordings"],
                     "LibraryOptions": {"SaveLocalMetadata": self.save_locally}}]
        if path == "/LiveTv/Timers":
            return {"Items": self.timers}
        if path == "/Items":
            if p.get("Ids"):
                return {"Items": [self._view(i) for i in p["Ids"].split(",") if i in self.items]}
            folders = p.get("IsFolder") == "true"
            return {"Items": [self._view(i) for i, it in self.items.items() if it["IsFolder"] == folders
                              and it.get("Type") != "Recording"]}
        raise AssertionError(path)

    def upload(self, iid, kind, data):
        self.writes.append(("upload", iid, kind))
        self.put(iid, kind, "tentacle", data)

    def remove(self, iid, kind):
        self.writes.append(("remove", iid, kind))
        self.images.pop((iid, kind), None)

    def image(self, iid, kind="Primary"):
        return self.images[(iid, kind)][1]

    def read(self, iid, start, length):
        self.reads.append((iid, start, length))
        return b"\x47" * 188

    def file_size(self, iid):
        return self.sizes.get(iid, 50_000_000)

    def read_file(self, iid, start, length):
        assert iid in self.recording, "the growing file is read only while it records"
        self.file_reads.append((iid, start, length))
        return b"\x47" * 188


class Base(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict("os.environ", {"DATA_DIR": temp_dir(self)})   # an empty team logos' cache
        env.start()
        self.addCleanup(env.stop)
        self.db = _db()
        self.addCleanup(self.db.close)
        self.jf = FakeJellyfin()
        J._last.update(blocked=None, note=None)
        J._viewer.update(id=None, at=None)

    def run_pass(self, now=NOW, **kw):
        kw.setdefault("grabber", lambda chunk: None)   # no ffmpeg unless a test says so
        return J.run_pass(self.db, self.jf, now=now, **kw)

    def row(self, iid):
        from models.database import RecordingArtwork
        return self.db.query(RecordingArtwork).filter_by(item_id=iid).first()


class Sources(Base):
    """a. matchup / b. TMDB / c. the guide / d. a frame / e. a title card."""

    def show(self, name="Jeopardy!", overview=""):
        self.jf.add("r1", name, NOW - timedelta(hours=3), minutes=60, overview=overview)

    def test_a_game_gets_the_matchup_card(self):
        self.jf.add("r1", "Canadiens at Blackhawks", NOW - timedelta(hours=4), minutes=180)
        self.run_pass()
        row = self.row("r1")
        self.assertEqual(("set", "matchup"), (row.state, row.source))
        self.assertEqual("NHL: Montreal Canadiens @ Chicago Blackhawks", row.detail)
        self.assertEqual({("upload", "r1", "Primary"), ("upload", "r1", "Thumb")}, set(self.jf.writes))
        tags = self.jf.tags("r1")
        self.assertEqual((tags["Primary"], tags["Thumb"]), (row.primary_tag, row.thumb_tag))
        # Primary is the 2:3 poster, Thumb the 16:9 picture
        self.assertEqual((800, 1200), _size(self.jf.image("r1", "Primary")))
        self.assertEqual((1280, 720), _size(self.jf.image("r1", "Thumb")))
        self.assertEqual(J.POSTER, row.primary_shape)

    def test_a_booked_game_names_both_teams(self):
        from models.database import RecordingBooking, RecordingRule
        rule = RecordingRule(name="Montreal Canadiens", kind="team", league="nhl", team="mtl", mode="record")
        self.db.add(rule)
        self.db.commit()
        start = NOW - timedelta(hours=4)
        self.db.add(RecordingBooking(rule_id=rule.id, pool_key="n13", channel_name="TSN 2", start=start,
                                     stop=start + timedelta(hours=2, minutes=30), title="Live: NHL Hockey",
                                     description="From the United Center in Chicago.", state="ended",
                                     pre_padding_seconds=300, post_padding_seconds=3600,
                                     why="same time as “Molson Habs Hockey: Canadiens vs. Blackhawks”"))
        self.db.commit()
        self.jf.add("r1", "Live: NHL Hockey", start - timedelta(minutes=5), minutes=215,
                    overview="From the United Center in Chicago.")
        self.run_pass()
        self.assertEqual(("matchup", "NHL: Montreal Canadiens @ Chicago Blackhawks"),
                         (self.row("r1").source, self.row("r1").detail))

    def tmdb(self, found=None, error=None):
        t = mock.Mock()

        def search(title, year=None, strict=False):
            if error:
                raise error
            return found
        t.search_series.side_effect = search
        t.search_movie.side_effect = search
        return t

    def tmdb_get(self):
        def get(url, timeout=None):
            r = mock.Mock(content=_jpeg(size=(780, 1170) if "/w780/" in url else (1280, 720)), status_code=200)
            r.raise_for_status = lambda: None
            return r
        return get

    def test_b_tmdb_backdrop_on_a_match(self):
        self.show()
        with mock.patch.object(J.requests, "get", side_effect=self.tmdb_get()) as get:
            self.run_pass(tmdb=self.tmdb({"title": "Jeopardy!", "backdrop_path": "/b.jpg", "poster_path": "/p.jpg"}))
        self.assertEqual("tmdb", self.row("r1").source)
        self.assertEqual(["https://image.tmdb.org/t/p/w1280/b.jpg", "https://image.tmdb.org/t/p/w780/p.jpg"],
                         [c.args[0] for c in get.call_args_list])
        self.assertEqual((800, 1200), _size(self.jf.image("r1", "Primary")))
        self.assertEqual((1280, 720), _size(self.jf.image("r1", "Thumb")))

    def test_b_tmdb_without_a_poster_puts_the_backdrop_on_one(self):
        self.show()
        with mock.patch.object(J.requests, "get", side_effect=self.tmdb_get()) as get:
            self.run_pass(tmdb=self.tmdb({"title": "Jeopardy!", "backdrop_path": "/b.jpg"}))
        self.assertEqual(1, get.call_count)
        self.assertEqual(("tmdb", J.POSTER), (self.row("r1").source, self.row("r1").primary_shape))
        self.assertEqual((800, 1200), _size(self.jf.image("r1", "Primary")))

    def test_b_tmdb_down_falls_through(self):
        self.show()
        self.run_pass(tmdb=self.tmdb(error=TMDBConnectionError("Cannot reach TMDB API")))
        self.assertEqual("title", self.row("r1").source)

    def test_b_tmdb_no_match_falls_through(self):
        self.show()
        self.run_pass(tmdb=self.tmdb(None))
        self.assertEqual("title", self.row("r1").source)

    def test_b_tmdb_uses_the_vod_threshold_strictly(self):
        """A weak match is no match: TMDBService's own scorer at the VOD threshold."""
        from services.tmdb import TMDBService
        svc = TMDBService("token", temp_dir(self), match_threshold=0.7)
        results = {"results": [{"id": 7, "name": "Jeopardy! The Greatest of All Time", "first_air_date": "2020-01-07",
                                "popularity": 10}]}
        with mock.patch.object(svc, "_request", return_value=results), \
                mock.patch.object(svc, "get_series_details", return_value={"backdrop_path": "/x.jpg"}):
            self.assertIsNone(J._tmdb_match(W.identify("Jeopardy!"), _Only(svc, "search_series")))

    def guide(self, icon, size=(1280, 720)):
        from models.database import EPGProgram
        self.show()
        self.db.add(EPGProgram(channel_id="cbs.us", title="Jeopardy!", start=NOW - timedelta(hours=3),
                               stop=NOW - timedelta(hours=2), icon_url=icon))
        self.db.commit()
        resp = mock.MagicMock(status_code=200)
        resp.__enter__.return_value = resp
        resp.iter_content.return_value = [_jpeg(size=size)]
        return resp

    def test_c_the_guides_wide_picture(self):
        resp = self.guide("https://img.example.com/j.jpg")
        with mock.patch.object(J.requests, "get", return_value=resp), \
                mock.patch("services.ssrf.is_safe_url", return_value=True):
            self.run_pass(tmdb=self.tmdb(None))
        self.assertEqual("guide", self.row("r1").source)

    def test_c_a_guide_poster_is_not_used(self):
        resp = self.guide("https://img.example.com/j.jpg", size=(600, 900))
        with mock.patch.object(J.requests, "get", return_value=resp), \
                mock.patch("services.ssrf.is_safe_url", return_value=True):
            self.run_pass(tmdb=self.tmdb(None))
        self.assertEqual("title", self.row("r1").source)

    def test_c_a_private_address_is_never_fetched(self):
        self.guide("http://192.168.1.1/j.jpg")
        with mock.patch.object(J.requests, "get") as get:
            self.run_pass(tmdb=self.tmdb(None))
        get.assert_not_called()
        self.assertEqual("title", self.row("r1").source)

    def test_d_the_most_detailed_frame(self):
        self.show()
        frames = iter([(_jpeg(noise=False), {"YAVG": 90, "YLOW": 80, "YHIGH": 95}),     # flat
                       (_jpeg(), {"YAVG": 100, "YLOW": 20, "YHIGH": 200}),
                       (_jpeg((0, 0, 0)), {"YAVG": 16, "YLOW": 16, "YHIGH": 17})])     # black
        self.run_pass(tmdb=self.tmdb(None), grabber=lambda chunk: next(frames))
        row = self.row("r1")
        self.assertEqual("frame", row.source)
        self.assertIn("flat", row.detail)
        self.assertIn("black", row.detail)
        self.assertEqual(3, len(self.jf.reads))
        self.assertTrue(all(n == F.READ_BYTES for _i, _s, n in self.jf.reads))

    def test_d_every_frame_black_gives_the_title_card(self):
        self.show()
        self.run_pass(tmdb=self.tmdb(None),
                      grabber=lambda chunk: (_jpeg((0, 0, 0), noise=False), {"YAVG": 16, "YLOW": 16, "YHIGH": 16}))
        row = self.row("r1")
        self.assertEqual("title", row.source)
        self.assertIn("black", row.detail)

    def test_d_ffmpeg_failing_or_too_slow(self):
        for exc in (FileNotFoundError(), subprocess.TimeoutExpired("ffmpeg", F.FFMPEG_TIMEOUT)):
            with self.subTest(exc=type(exc).__name__):
                self.setUp()
                self.show()
                with mock.patch.object(F.subprocess, "run", side_effect=exc) as run:
                    self.run_pass(tmdb=self.tmdb(None), grabber=F.grab)
                self.assertEqual("title", self.row("r1").source)
                self.assertEqual(F.FFMPEG_TIMEOUT, run.call_args.kwargs["timeout"])
                self.assertIs(F._lowest_priority, run.call_args.kwargs["preexec_fn"])

    def test_d_ffmpeg_finding_no_frame(self):
        self.show()
        with mock.patch.object(F.subprocess, "run", return_value=mock.Mock(returncode=1, stdout=b"", stderr=b"")):
            self.run_pass(tmdb=self.tmdb(None), grabber=F.grab)
        self.assertEqual("title", self.row("r1").source)

    def test_d_never_while_jellyfin_records(self):
        self.show()
        self.jf.timers = [{"Name": "Live: NHL Hockey", "Status": "InProgress", "StartDate": "2026-10-24T11:00:00Z",
                           "EndDate": "2026-10-24T14:00:00Z", "PrePaddingSeconds": 0, "PostPaddingSeconds": 0}]
        grab = mock.Mock(return_value=None)
        out = self.run_pass(tmdb=self.tmdb(None), grabber=grab)
        grab.assert_not_called()
        self.assertEqual([], self.jf.reads)
        self.assertEqual(1, out["waiting"])
        self.assertIsNone(self.row("r1"))
        self.assertEqual([], self.jf.writes)
        # once it has finished, the frames are read
        self.jf.timers = []
        self.run_pass(tmdb=self.tmdb(None), grabber=grab)
        self.assertEqual(3, grab.call_count)
        self.assertEqual("title", self.row("r1").source)

    def test_d_only_mpeg_ts(self):
        self.jf.add("r1", "Jeopardy!", NOW - timedelta(hours=3), container="mkv")
        grab = mock.Mock()
        self.run_pass(tmdb=self.tmdb(None), grabber=grab)
        grab.assert_not_called()
        self.assertEqual("title", self.row("r1").source)


class _Only:
    """A TMDB client with one search: the other finds nothing."""

    def __init__(self, svc, name):
        self.svc, self.name = svc, name

    def __getattr__(self, attr):
        if attr == self.name:
            return getattr(self.svc, attr)
        return lambda *a, **k: None


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class RealFrames(unittest.TestCase):
    """ffmpeg itself on a small MPEG-TS: a picture, then black."""

    def test_black_frames_are_left_out(self):
        d = temp_dir(self)
        path = f"{d}/r.ts"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
                        "-f", "lavfi", "-i", "color=black:size=640x360:rate=25", "-filter_complex",
                        "[0:v]trim=0:4[a];[1:v]trim=0:4,setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1[v]",
                        "-map", "[v]", "-c:v", "libx264", "-g", "25", "-b:v", "2M", "-f", "mpegts", path],
                       check=True, timeout=60)
        data = open(path, "rb").read()
        best, notes = F.best_frame(lambda s, n: data[s:s + 300_000], len(data), 8.0, [1.0, 2.0, 7.5])
        self.assertIsNotNone(best)
        self.assertLess(best.seconds, 4)
        self.assertTrue(any("black" in n for n in notes), notes)


    def test_how_long_a_growing_file_is(self):
        d = temp_dir(self)
        path = f"{d}/r.ts"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
                        "-t", "20", "-c:v", "libx264", "-g", "25", "-f", "mpegts", path], check=True, timeout=60)
        data = open(path, "rb").read()
        part = data[:len(data) // 2 // 188 * 188]       # still being written: half of it
        span = F.written_seconds(lambda s, n: part[s:s + n], len(part))
        self.assertTrue(span and 7 < span < 13, span)


class Timestamps(unittest.TestCase):
    def test_a_wrap_and_nonsense(self):
        read = lambda s, n: b"x" * 188
        self.assertAlmostEqual(600.0, F.written_seconds(read, 10_000, lambda c, last: 10.0 if last else F.PTS_WRAP - 590))
        self.assertIsNone(F.written_seconds(read, 10_000, lambda c, last: 5.0 if last else 5.0))
        self.assertIsNone(F.written_seconds(read, 10_000, lambda c, last: None))
        self.assertIsNone(F.written_seconds(read, 100, lambda c, last: 1.0))


class When(Base):
    def test_waits_until_ten_minutes_after_the_end(self):
        self.jf.add("r1", "Canadiens at Blackhawks", NOW - timedelta(minutes=65), minutes=60)
        self.run_pass()
        self.assertEqual([], self.jf.writes)
        self.run_pass(now=NOW + timedelta(minutes=6))
        self.assertEqual("set", self.row("r1").state)

    def test_skips_a_recording_in_progress(self):
        # Jellyfin has probed the growing file: its length looks over, but the timer still runs
        self.jf.add("r1", "Live: NHL Hockey", NOW - timedelta(hours=2), minutes=20)
        self.jf.timers = [{"Name": "Live: NHL Hockey", "Status": "InProgress", "StartDate": "2026-10-24T10:00:00Z",
                           "EndDate": "2026-10-24T13:00:00Z", "PrePaddingSeconds": 0, "PostPaddingSeconds": 0}]
        self.run_pass()
        self.assertEqual([], self.jf.writes)

    def test_a_few_per_pass_newest_first(self):
        for i in range(7):
            self.jf.add(f"r{i}", f"Show {i}", NOW - timedelta(hours=10 - i), minutes=30)
        self.run_pass()
        done = {w[1] for w in self.jf.writes}
        self.assertEqual({"r6", "r5", "r4", "r3"}, done)
        self.run_pass()
        self.assertEqual(7, len({w[1] for w in self.jf.writes}))


class Safety(Base):
    def test_never_replaces_a_picture_it_didnt_set(self):
        self.jf.add("r1", "Canadiens at Blackhawks", NOW - timedelta(hours=4))
        self.jf.put("r1", "Primary", "jellyfin")       # Jellyfin's own frame grab
        self.run_pass()
        self.assertEqual([], self.jf.writes)
        self.assertEqual("theirs", self.row("r1").state)
        # and not later either, even once that picture is gone
        self.jf.images.clear()
        self.run_pass()
        self.assertEqual([], self.jf.writes)

    def test_never_sets_again_what_a_person_removed(self):
        self.jf.add("r1", "Canadiens at Blackhawks", NOW - timedelta(hours=4))
        self.run_pass()
        self.jf.writes.clear()
        self.jf.images.pop(("r1", "Primary"))
        self.run_pass()
        self.assertEqual("left", self.row("r1").state)
        self.run_pass()
        self.assertEqual([], self.jf.writes)

    def test_off_removes_only_its_own(self):
        from models.database import set_setting
        self.jf.add("r1", "Canadiens at Blackhawks", NOW - timedelta(hours=4))
        self.jf.add("r2", "Penguins at Maple Leafs", NOW - timedelta(hours=5))
        self.jf.add("r3", "Jeopardy!", NOW - timedelta(hours=6))
        self.jf.put("r3", "Primary", "person")
        self.run_pass()
        self.jf.put("r2", "Primary", "person")          # someone chose another picture for r2
        self.jf.writes.clear()
        set_setting(self.db, J.SETTING, "false")
        out = self.run_pass()
        self.assertEqual({("remove", "r1", "Primary"), ("remove", "r1", "Thumb"), ("remove", "r2", "Thumb")},
                         set(self.jf.writes))
        self.assertEqual(3, out["removed"])
        self.assertEqual({"Primary"}, set(self.jf.tags("r2")))
        self.assertEqual({"Primary"}, set(self.jf.tags("r3")))
        # on again: r1 gets its picture back, r2 and r3 stay as people left them
        self.jf.writes.clear()
        set_setting(self.db, J.SETTING, "true")
        self.run_pass()
        self.assertEqual({"r1"}, {w[1] for w in self.jf.writes})

    def test_a_library_that_saves_pictures_next_to_the_media(self):
        self.jf.save_locally = True
        self.jf.add("r1", "Canadiens at Blackhawks", NOW - timedelta(hours=4))
        self.run_pass()
        self.assertEqual([], self.jf.writes)
        st = J.status(self.db)
        self.assertIn("next to the recordings", st["blocked"])
        self.assertIn("Save artwork into media folders", st["blocked"])

    def test_one_of_two_libraries_saving_next_to_the_media(self):
        get = self.jf.get

        def two(path, **p):
            if path == "/Library/VirtualFolders":
                local = dict(get(path)[0], Name="Recordings2", ItemId="lib2", LibraryOptions={"SaveLocalMetadata": True})
                return get(path) + [local]
            return get(path, **p)
        self.jf.get = two
        self.jf.add("r1", "Canadiens at Blackhawks", NOW - timedelta(hours=4))
        self.run_pass()
        self.assertEqual([], self.jf.writes)
        self.assertIn("“Recordings2”", J.status(self.db)["blocked"])

    def test_no_recordings_library(self):
        self.jf.get = lambda path, **p: [] if path == "/Library/VirtualFolders" else {}
        self.run_pass()
        self.assertIn("no library", J.status(self.db)["blocked"])

    def test_two_libraries_with_the_same_folder(self):
        get = self.jf.get

        def two(path, **p):
            if path == "/Library/VirtualFolders":
                return get(path) + [dict(get(path)[0], Name="Recordings2", ItemId="lib2")]
            return get(path, **p)
        self.jf.get = two
        self.jf.add("r1", "Canadiens at Blackhawks", NOW - timedelta(hours=4))
        self.run_pass()
        self.assertEqual([("upload", "r1", "Primary"), ("upload", "r1", "Thumb")], self.jf.writes)

    def test_a_deleted_recording_drops_its_row(self):
        self.jf.add("r1", "Canadiens at Blackhawks", NOW - timedelta(hours=4))
        self.run_pass()
        del self.jf.items["r1"]
        self.run_pass()
        self.assertIsNone(self.row("r1"))

    def test_the_key_is_a_header_never_in_a_url(self):
        jf = J.Jellyfin("http://jf:8096", "SECRETKEY")
        resp = mock.MagicMock(status_code=206, content=b"{}")
        resp.__enter__.return_value = resp
        resp.iter_content.return_value = [b"x" * 10]
        with mock.patch.object(J.requests, "get", return_value=resp) as get, \
                mock.patch.object(J.requests, "post", return_value=resp) as post, \
                mock.patch.object(J.requests, "delete", return_value=resp) as delete:
            jf.get("/Items", Ids="a")
            jf.upload("a", "Primary", b"jpeg")
            jf.remove("a", "Thumb")
            jf.image("a")
            jf.read("a", 100, 10)
            resp.headers = {"Content-Range": "bytes 0-0/5000"}
            self.assertEqual(5000, jf.file_size("a"))
            jf.read_file("a", 200, 10)
        for call in get.call_args_list + post.call_args_list + delete.call_args_list:
            self.assertNotIn("SECRETKEY", repr(call.args) + repr(call.kwargs.get("params")))
            self.assertEqual("SECRETKEY", call.kwargs["headers"]["X-Emby-Token"])
        self.assertEqual(["bytes=100-109", "bytes=0-0", "bytes=200-209"],
                         [c.kwargs["headers"]["Range"] for c in get.call_args_list[-3:]])
        self.assertEqual("http://jf:8096/Items/a/File", get.call_args_list[-1].args[0])


class Posters(Base):
    """Pictures set before posters existed (#827: the 16:9 one as both
    Primary and Thumb) get their poster as Primary, only while both are
    still Tentacle's."""

    def old(self, iid, name, source):
        from models.database import RecordingArtwork
        wide = C.title_card(name, NOW, None)
        self.jf.put(iid, "Primary", "tentacle", wide)
        self.jf.put(iid, "Thumb", "tentacle", wide)
        tags = self.jf.tags(iid)
        self.db.add(RecordingArtwork(item_id=iid, kind="recording", name=name, state="set", source=source,
                                     primary_tag=tags["Primary"], thumb_tag=tags["Thumb"]))
        self.db.commit()
        return wide

    def test_an_old_picture_gets_its_poster(self):
        self.jf.add("r1", "Canadiens at Blackhawks", NOW - timedelta(days=1))
        self.jf.add("r2", "Jeopardy!", NOW - timedelta(days=2))
        self.old("r1", "Canadiens at Blackhawks", "matchup")
        wide = self.old("r2", "Jeopardy!", "frame")
        out = self.run_pass()
        self.assertEqual(2, out["posters"])
        # the matchup card's 16:9 is drawn again too (its footer moved up), the frame's stays
        self.assertEqual({("upload", "r1", "Primary"), ("upload", "r1", "Thumb"), ("upload", "r2", "Primary")},
                         set(self.jf.writes))
        for iid in ("r1", "r2"):
            row = self.row(iid)
            self.assertEqual(("set", J.POSTER), (row.state, row.primary_shape))
            self.assertEqual((800, 1200), _size(self.jf.image(iid, "Primary")))
            self.assertEqual((self.jf.tags(iid)["Primary"], self.jf.tags(iid)["Thumb"]),
                             (row.primary_tag, row.thumb_tag))
        self.assertEqual(wide, self.jf.image("r2", "Thumb"))       # the 16:9 stays
        self.jf.writes.clear()
        self.assertEqual(0, self.run_pass()["posters"])            # once
        self.assertEqual([], self.jf.writes)

    def test_a_picture_a_person_changed_gets_no_poster(self):
        self.jf.add("r1", "Jeopardy!", NOW - timedelta(days=1))
        self.old("r1", "Jeopardy!", "frame")
        self.jf.put("r1", "Thumb", "person")
        self.run_pass()
        self.assertEqual([], self.jf.writes)
        self.assertEqual("left", self.row("r1").state)

    def test_an_older_layouts_pictures_are_drawn_again(self):
        # #839's posters (primary_shape "poster") put the channel under the TV's progress bar
        from models.database import RecordingArtwork
        for iid, name, source in (("r1", "Canadiens at Blackhawks", "matchup"), ("r2", "Jeopardy!", "frame")):
            self.jf.add(iid, name, NOW - timedelta(days=1))
            self.jf.put(iid, "Primary", "tentacle", _jpeg(size=(800, 1200)))
            self.jf.put(iid, "Thumb", "tentacle", _jpeg())
            tags = self.jf.tags(iid)
            self.db.add(RecordingArtwork(item_id=iid, kind="recording", name=name, state="set", source=source,
                                         primary_tag=tags["Primary"], thumb_tag=tags["Thumb"],
                                         primary_shape="poster"))
        self.db.commit()
        frame = self.jf.image("r2", "Thumb")
        self.assertEqual(2, self.run_pass()["posters"])
        # a card: both drawn again; a frame: only its poster (the frame has no text)
        self.assertEqual({("upload", "r1", "Primary"), ("upload", "r1", "Thumb"), ("upload", "r2", "Primary")},
                         set(self.jf.writes))
        self.assertEqual((1280, 720), _size(self.jf.image("r1", "Thumb")))
        self.assertEqual(frame, self.jf.image("r2", "Thumb"))
        for iid in ("r1", "r2"):
            row = self.row(iid)
            self.assertEqual(("set", J.POSTER), (row.state, row.primary_shape))
            self.assertEqual((800, 1200), _size(self.jf.image(iid, "Primary")))
            self.assertEqual((self.jf.tags(iid)["Primary"], self.jf.tags(iid)["Thumb"]),
                             (row.primary_tag, row.thumb_tag))
        self.jf.writes.clear()
        self.assertEqual(0, self.run_pass()["posters"])            # once
        self.assertEqual([], self.jf.writes)

    def test_a_few_per_pass(self):
        for i in range(J.POSTERS_PER_PASS + 3):
            self.jf.add(f"r{i}", "Jeopardy!", NOW - timedelta(days=1 + i))
            self.old(f"r{i}", "Jeopardy!", "title")
        self.assertEqual(J.POSTERS_PER_PASS, self.run_pass()["posters"])
        self.assertEqual(3, self.run_pass()["posters"])


class Folders(Base):
    def test_a_folder_of_games_gets_the_league_card(self):
        self.jf.add("f1", "Live  NHL Hockey", NOW - timedelta(days=3), folder=True)
        self.jf.add("r1", "Canadiens at Blackhawks", NOW - timedelta(days=2), parent="f1")
        self.jf.add("r2", "Penguins at Maple Leafs", NOW - timedelta(days=1), parent="f1")
        self.run_pass()
        row = self.row("f1")
        self.assertEqual(("set", "league", "league:nhl"), (row.state, row.source, row.made_from))

    def test_a_folder_an_older_layout_drew_is_drawn_again(self):
        self.jf.add("f1", "Live  NHL Hockey", NOW - timedelta(days=3), folder=True)
        self.jf.add("r1", "Canadiens at Blackhawks", NOW - timedelta(days=2), parent="f1")
        self.jf.add("r2", "Penguins at Maple Leafs", NOW - timedelta(days=1), parent="f1")
        self.run_pass()
        self.row("f1").primary_shape = "poster"
        self.db.commit()
        self.jf.writes.clear()
        self.run_pass()
        self.assertEqual({("upload", "f1", "Primary"), ("upload", "f1", "Thumb")}, set(self.jf.writes))
        self.assertEqual(J.POSTER, self.row("f1").primary_shape)
        self.jf.writes.clear()
        self.run_pass()
        self.assertEqual([], self.jf.writes)

    def test_the_recordings_folder_itself_gets_nothing(self):
        # Jellyfin lists the recordings folder as a Folder too; a title folder
        # with one recording is shown as that recording, whose parent it is
        self.jf.add("root", "recordings", NOW - timedelta(days=30), folder=True)
        self.jf.items["root"]["Path"] = "/config/data/livetv/recordings"
        self.jf.add("r1", "Jeopardy!", NOW - timedelta(days=2), parent="root")
        self.jf.add("r2", "Canadiens at Blackhawks", NOW - timedelta(days=1), parent="root")
        self.run_pass()
        self.assertEqual({"r1", "r2"}, {w[1] for w in self.jf.writes})

    def test_a_folder_of_shows_follows_the_newest(self):
        self.jf.add("f1", "Jeopardy!", NOW - timedelta(days=3), folder=True)
        self.jf.add("r1", "Jeopardy!", NOW - timedelta(days=2), parent="f1")
        self.run_pass()
        self.assertEqual(C.to_16x9(self.jf.image("r1", "Thumb")), self.jf.image("f1", "Thumb"))
        self.assertEqual(C.to_2x3(self.jf.image("r1", "Primary")), self.jf.image("f1", "Primary"))
        self.jf.add("r2", "Jeopardy!", NOW - timedelta(hours=3), parent="f1")
        self.jf.put("r2", "Primary", "person", _jpeg((200, 30, 30)))   # its own picture: still the newest's
        self.run_pass()
        self.assertEqual(C.to_16x9(self.jf.image("r2")), self.jf.image("f1", "Thumb"))
        self.assertEqual((800, 1200), _size(self.jf.image("f1", "Primary")))   # the wide one on a poster

    def test_a_folder_picture_a_person_changed_stays(self):
        self.jf.add("f1", "Jeopardy!", NOW - timedelta(days=3), folder=True)
        self.jf.add("r1", "Jeopardy!", NOW - timedelta(days=2), parent="f1")
        self.run_pass()
        self.jf.put("f1", "Primary", "person")
        self.jf.add("r2", "Jeopardy!", NOW - timedelta(hours=3), parent="f1")
        self.jf.writes.clear()
        self.run_pass()
        self.assertEqual("left", self.row("f1").state)
        self.assertNotIn("f1", {w[1] for w in self.jf.writes})


class WhileRecording(Base):
    """A recording in progress gets its picture within a minute or two
    (the live pass), a frame from the part written so far once there is
    enough, and its picture moves to the finished item (another id) at
    once; a frame or a title card is made again from the finished file."""

    GOOD = (_jpeg(), {"YAVG": 100, "YLOW": 20, "YHIGH": 200})
    END = NOW + timedelta(minutes=30)

    def live(self, now=NOW, **kw):
        kw.setdefault("grabber", lambda chunk: None)
        kw.setdefault("probe", lambda chunk, last: None)     # no timestamps: the clock since DateCreated
        return J.live_pass(self.db, self.jf, now=now, **kw)

    def show(self, minutes=30):
        self.jf.start("r1", "Jeopardy!", NOW, NOW + timedelta(minutes=minutes))

    def finish(self, new_id="m1", minutes=30):
        self.jf.finish("r1", minutes, NOW + timedelta(minutes=minutes), new_id=new_id)

    def test_a_game_gets_its_matchup_card_at_once_and_never_a_frame(self):
        self.jf.start("r1", "Canadiens at Blackhawks", NOW - timedelta(minutes=1), NOW + timedelta(hours=3))
        grab = mock.Mock(return_value=self.GOOD)
        out = self.live(grabber=grab)
        row = self.row("r1")
        self.assertEqual(("set", "matchup", 1), (row.state, row.source, row.while_recording))
        self.assertEqual(1, out["set"])
        self.assertEqual({("upload", "r1", "Primary"), ("upload", "r1", "Thumb")}, set(self.jf.writes))
        self.assertEqual((800, 1200), _size(self.jf.image("r1", "Primary")))
        self.assertEqual((1280, 720), _size(self.jf.image("r1", "Thumb")))
        for minutes in (10, 30, 60):
            self.live(now=NOW + timedelta(minutes=minutes), grabber=grab)
        grab.assert_not_called()
        self.assertEqual(2, len(self.jf.writes))

    def test_a_show_without_art_gets_the_title_card_then_a_frame(self):
        self.show()
        self.live(now=NOW + timedelta(minutes=1), grabber=lambda chunk: self.GOOD)
        row = self.row("r1")
        self.assertEqual(("title", 1), (row.source, row.while_recording))
        self.assertEqual("/config/data/livetv/recordings/Jeopardy!/Jeopardy!.ts", row.path)
        title_tags = self.jf.tags("r1")
        self.jf.file_reads.clear()
        self.live(now=NOW + timedelta(minutes=5), grabber=lambda chunk: self.GOOD)
        self.assertEqual([], self.jf.file_reads)          # tried again FRAME_RETRY after the title card
        self.live(now=NOW + timedelta(minutes=6), grabber=lambda chunk: self.GOOD)
        row = self.row("r1")
        self.assertEqual(("set", "frame", 1), (row.state, row.source, row.while_recording))
        self.assertNotEqual(title_tags, self.jf.tags("r1"))
        self.assertEqual((row.primary_tag, row.thumb_tag), (self.jf.tags("r1")["Primary"], self.jf.tags("r1")["Thumb"]))
        # 20 s to 4 min 20 s behind what is written (6 min = 50 MB); the two timestamp reads first
        starts = [s for _i, s, n in self.jf.file_reads if n == F.READ_BYTES]
        self.assertEqual(4, len(starts))
        self.assertTrue(all(50_000_000 * 90 // 360 <= s < 50_000_000 for s in starts), starts)
        self.assertEqual([], self.jf.reads)                 # never Jellyfin's stream of a growing file

    def test_how_much_is_written_comes_from_the_timestamps(self):
        """DateCreated of a recording in progress is the NFO's local time
        (4 h off in Toronto): the file's first and last timestamps say 6 min."""
        from models.database import RecordingBooking, RecordingRule
        rule = RecordingRule(name="Montreal Canadiens", kind="team", league="nhl", team="mtl", mode="record")
        self.db.add(rule)
        self.db.commit()
        self.db.add(RecordingBooking(rule_id=rule.id, pool_key="n13", channel_name="TSN 2", start=NOW,
                                     stop=NOW + timedelta(hours=3), title="Live: NHL Hockey", state="recording",
                                     why="same time as “Canadiens vs. Blackhawks”", pre_padding_seconds=0,
                                     post_padding_seconds=0))
        self.db.commit()
        self.jf.start("r1", "Live: NHL Hockey", NOW - timedelta(hours=4), NOW + timedelta(hours=3))
        self.live(now=NOW + timedelta(minutes=6), probe=lambda chunk, last: 7200.0 + (360 if last else 0))
        # the teams come from the booking, matched by when it began (the title has none)
        self.assertEqual(("matchup", "NHL: Montreal Canadiens vs Chicago Blackhawks"),
                         (self.row("r1").source, self.row("r1").detail))

    def test_the_frame_is_taken_again_every_fifteen_minutes(self):
        self.jf.start("r1", "Jeopardy!", NOW, NOW + timedelta(hours=1))
        grab = mock.Mock(return_value=self.GOOD)
        self.live(now=NOW + timedelta(minutes=6), grabber=grab)
        self.assertEqual("frame", self.row("r1").source)
        n = grab.call_count
        self.live(now=NOW + timedelta(minutes=10), grabber=grab)
        self.live(now=NOW + timedelta(minutes=20), grabber=grab)
        self.assertEqual(n, grab.call_count)
        tags = self.jf.tags("r1")
        self.live(now=NOW + timedelta(minutes=21), grabber=grab)
        self.assertEqual(2 * n, grab.call_count)
        self.assertNotEqual(tags, self.jf.tags("r1"))

    def test_no_usable_frame_keeps_the_title_card(self):
        self.jf.start("r1", "Jeopardy!", NOW, NOW + timedelta(hours=1))
        black = (_jpeg((0, 0, 0), noise=False), {"YAVG": 16, "YLOW": 16, "YHIGH": 16})
        self.live(now=NOW + timedelta(minutes=6), grabber=lambda chunk: black)
        row = self.row("r1")
        self.assertEqual("title", row.source)
        self.assertIn("black", row.detail)
        self.assertEqual(2, len(self.jf.writes))

    def test_tmdb_comes_before_a_frame(self):
        self.show()
        t = mock.Mock()
        t.search_series.return_value = {"title": "Jeopardy!", "backdrop_path": "/b.jpg", "poster_path": "/p.jpg"}

        def get(url, timeout=None):
            r = mock.Mock(content=_jpeg(size=(780, 1170) if "/w780/" in url else (1280, 720)), status_code=200)
            r.raise_for_status = lambda: None
            return r
        with mock.patch.object(J.requests, "get", side_effect=get):
            self.live(now=NOW + timedelta(minutes=1), make_tmdb=lambda: t)
        self.assertEqual("tmdb", self.row("r1").source)
        grab = mock.Mock(return_value=self.GOOD)
        self.live(now=NOW + timedelta(minutes=20), grabber=grab)
        grab.assert_not_called()

    def test_tmdb_is_made_only_when_a_picture_is_needed(self):
        make = mock.Mock(return_value=None)
        self.live(make_tmdb=make)
        self.jf.start("r1", "Canadiens at Blackhawks", NOW, NOW + timedelta(hours=3))
        self.live(make_tmdb=make)
        self.live(now=NOW + timedelta(minutes=1), make_tmdb=make)
        self.assertEqual(1, make.call_count)

    def test_nothing_recording_is_one_call(self):
        self.jf.add("r1", "Jeopardy!", NOW - timedelta(hours=3))
        self.live()
        self.jf.gets.clear()
        self.live(now=NOW + timedelta(minutes=1))
        self.assertEqual(["/LiveTv/Recordings"], self.jf.gets)     # the administrator's id is kept
        self.assertIsNone(self.row("r1"))

    def test_only_recordings_in_the_recordings_library(self):
        self.show()
        self.jf.items["r1"]["Path"] = "/media/elsewhere/Jeopardy!.ts"
        self.live(now=NOW + timedelta(minutes=1))
        self.assertEqual([], self.jf.writes)

    def test_a_library_that_saves_pictures_next_to_the_media(self):
        self.jf.save_locally = True
        self.jf.start("r1", "Canadiens at Blackhawks", NOW, NOW + timedelta(hours=3))
        self.live(now=NOW + timedelta(minutes=1))
        self.assertEqual([], self.jf.writes)

    def test_off(self):
        from models.database import set_setting
        set_setting(self.db, J.SETTING, "false")
        self.jf.start("r1", "Canadiens at Blackhawks", NOW, NOW + timedelta(hours=3))
        self.live(now=NOW + timedelta(minutes=1))
        self.assertEqual([], self.jf.writes)
        # and off removes the ones set while recording
        set_setting(self.db, J.SETTING, "true")
        self.live(now=NOW + timedelta(minutes=2))
        set_setting(self.db, J.SETTING, "false")
        self.run_pass(now=NOW + timedelta(minutes=3))
        self.assertEqual({}, self.jf.tags("r1"))
        self.assertIsNone(self.row("r1"))

    def test_a_picture_it_had_at_the_start_is_never_touched(self):
        self.show()
        self.jf.put("r1", "Primary", "jellyfin")
        self.live(now=NOW + timedelta(minutes=1))
        self.assertEqual("theirs", self.row("r1").state)
        self.live(now=NOW + timedelta(minutes=20), grabber=lambda chunk: self.GOOD)
        self.assertEqual([], self.jf.writes)

    def test_a_picture_a_person_set_while_recording_stays(self):
        self.show()
        self.live(now=NOW + timedelta(minutes=1))
        self.jf.writes.clear()
        self.jf.put("r1", "Thumb", "person")
        self.live(now=NOW + timedelta(minutes=7), grabber=lambda chunk: self.GOOD)
        self.assertEqual(("left", None), (self.row("r1").state, self.row("r1").while_recording))
        self.live(now=NOW + timedelta(minutes=25), grabber=lambda chunk: self.GOOD)
        self.assertEqual([], self.jf.writes)
        self.assertEqual("person", self.jf.images[("r1", "Thumb")][2])

    def test_the_pass_for_finished_recordings_leaves_it_while_it_records(self):
        self.show()
        self.live(now=NOW + timedelta(minutes=1))
        self.jf.writes.clear()
        out = self.run_pass(now=NOW + timedelta(minutes=20), grabber=lambda chunk: self.GOOD)
        self.assertEqual([], self.jf.writes)
        self.assertEqual(0, out["finished"])
        self.assertEqual(1, self.row("r1").while_recording)    # not dropped although not in the library listing

    def test_finished_its_picture_moves_to_the_new_item_at_once(self):
        self.jf.start("r1", "Canadiens at Blackhawks", NOW, NOW + timedelta(hours=3))
        self.live(now=NOW + timedelta(minutes=1))
        wide = self.jf.image("r1", "Thumb")
        self.jf.finish("r1", 180, NOW + timedelta(hours=3), new_id="m1")
        self.jf.writes.clear()
        out = self.live(now=NOW + timedelta(hours=3, minutes=1))
        self.assertEqual(1, out["moved"])
        self.assertEqual({("upload", "m1", "Primary"), ("upload", "m1", "Thumb")}, set(self.jf.writes))
        self.assertEqual(wide, self.jf.image("m1", "Thumb"))
        self.assertIsNone(self.row("r1"))
        row = self.row("m1")
        self.assertEqual(("set", "matchup", None), (row.state, row.source, row.while_recording))
        self.assertEqual((row.primary_tag, row.thumb_tag), (self.jf.tags("m1")["Primary"], self.jf.tags("m1")["Thumb"]))
        # final: no more pictures, and the copy kept on disk is gone
        self.jf.writes.clear()
        self.run_pass(now=NOW + timedelta(hours=8))
        self.live(now=NOW + timedelta(hours=8))
        self.assertEqual([], self.jf.writes)
        self.assertEqual((None, None), J._kept(row))

    def test_the_new_item_may_come_later(self):
        self.jf.start("r1", "Canadiens at Blackhawks", NOW, NOW + timedelta(hours=3))
        self.live(now=NOW + timedelta(minutes=1))
        item = self.jf.items.pop("r1")
        self.jf.recording.discard("r1")
        self.live(now=NOW + timedelta(hours=3, minutes=1))
        self.run_pass(now=NOW + timedelta(hours=3, minutes=2))     # not dropped as gone
        self.assertEqual(1, self.row("r1").while_recording)
        self.jf.items["r1"] = item
        self.jf.finish("r1", 180, NOW + timedelta(hours=3), new_id="m1")
        self.live(now=NOW + timedelta(hours=3, minutes=3))
        self.assertEqual("set", self.row("m1").state)

    def test_no_new_item_at_all(self):
        self.show()
        self.live(now=NOW + timedelta(minutes=1))
        del self.jf.items["r1"]
        self.jf.recording.clear()
        for i in range(J.FOLLOW_CHECKS):
            self.live(now=NOW + timedelta(minutes=31 + i))
        self.assertIsNone(self.row("r1"))

    def test_finished_a_frame_is_made_again_from_the_whole_recording(self):
        self.show()
        self.live(now=NOW + timedelta(minutes=6), grabber=lambda chunk: self.GOOD)
        self.assertEqual("frame", self.row("r1").source)
        self.finish()
        self.jf.writes.clear()
        self.live(now=self.END + timedelta(minutes=1))
        row = self.row("m1")
        self.assertEqual(("frame", 2), (row.source, row.while_recording))
        self.assertEqual({("upload", "m1", "Primary"), ("upload", "m1", "Thumb")}, set(self.jf.writes))
        self.jf.writes.clear()
        # the finished pass: when DateCreated (the end) + its length + SETTLE has passed
        self.run_pass(now=self.END + timedelta(minutes=35), grabber=lambda chunk: self.GOOD)
        self.assertEqual([], self.jf.writes)
        out = self.run_pass(now=self.END + timedelta(minutes=41), grabber=lambda chunk: self.GOOD)
        row = self.row("m1")
        self.assertEqual(1, out["finished"])
        self.assertEqual(("set", "frame", None), (row.state, row.source, row.while_recording))
        self.assertEqual(3, len(self.jf.reads))             # 30/50/70 % of the finished file
        self.assertEqual({("upload", "m1", "Primary"), ("upload", "m1", "Thumb")}, set(self.jf.writes))
        self.assertEqual((row.primary_tag, row.thumb_tag), (self.jf.tags("m1")["Primary"], self.jf.tags("m1")["Thumb"]))
        self.jf.writes.clear()
        self.run_pass(now=NOW + timedelta(hours=5), grabber=lambda chunk: self.GOOD)
        self.live(now=NOW + timedelta(hours=5))
        self.assertEqual([], self.jf.writes)

    def test_finished_in_place(self):
        """A Jellyfin that keeps the item: the same, without moving."""
        self.show()
        self.live(now=NOW + timedelta(minutes=1))
        self.finish(new_id=None)
        self.jf.writes.clear()
        self.live(now=self.END + timedelta(minutes=1))
        self.assertEqual([], self.jf.writes)
        self.assertEqual(("title", 2), (self.row("r1").source, self.row("r1").while_recording))
        self.run_pass(now=self.END + timedelta(minutes=41), grabber=lambda chunk: self.GOOD)
        self.assertEqual(("frame", None), (self.row("r1").source, self.row("r1").while_recording))

    def test_finished_while_another_records_the_frame_waits(self):
        self.show()
        self.live(now=NOW + timedelta(minutes=1))
        self.finish()
        self.live(now=self.END + timedelta(minutes=1))
        self.jf.start("r2", "Wheel of Fortune", self.END, self.END + timedelta(hours=1))
        self.jf.writes.clear()
        grab = mock.Mock(return_value=self.GOOD)
        out = self.run_pass(now=self.END + timedelta(minutes=41), grabber=grab)
        self.assertEqual(1, out["waiting"])
        self.assertNotIn("m1", {w[1] for w in self.jf.writes})
        self.assertEqual(("title", 2), (self.row("m1").source, self.row("m1").while_recording))
        self.jf.finish("r2", 60, self.END + timedelta(hours=1), new_id="m2")
        self.run_pass(now=self.END + timedelta(hours=3), grabber=grab)
        self.assertEqual(("frame", None), (self.row("m1").source, self.row("m1").while_recording))

    def test_the_new_item_with_a_picture_of_its_own(self):
        self.show()
        self.live(now=NOW + timedelta(minutes=1))
        self.finish()
        self.jf.put("m1", "Primary", "jellyfin")
        self.jf.writes.clear()
        self.live(now=self.END + timedelta(minutes=1))
        self.run_pass(now=self.END + timedelta(hours=2), grabber=lambda chunk: self.GOOD)
        self.assertEqual([], self.jf.writes)
        self.assertIsNone(self.row("r1"))
        self.assertEqual("theirs", self.row("m1").state)

    def test_a_person_changed_it_after_it_finished(self):
        self.show()
        self.live(now=NOW + timedelta(minutes=1))
        self.finish()
        self.live(now=self.END + timedelta(minutes=1))
        self.jf.put("m1", "Primary", "person")
        self.jf.writes.clear()
        self.run_pass(now=self.END + timedelta(hours=2), grabber=lambda chunk: self.GOOD)
        self.assertEqual([], self.jf.writes)
        self.assertEqual("left", self.row("m1").state)


def _noisy(seed, logo=True, size=(640, 360)) -> bytes:
    """A busy picture (different for every seed), with a station logo in the top-left corner."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, (60, 90, 60))
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    for _ in range(150):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        d.rectangle([x, y, x + 12, y + 12], fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    if logo:
        d.rectangle([24, 14, 120, 54], fill=(250, 250, 250), outline=(0, 0, 0), width=4)
        d.rectangle([44, 24, 100, 44], fill=(10, 10, 160))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()


class AdLooking(unittest.TestCase):
    """The cheap signals of the ad-breaks research: silence and a missing
    station logo put a frame after any that has neither."""

    def test_live_offsets(self):
        self.assertEqual([580.0, 500.0, 420.0, 340.0], F.live_offsets(600))
        self.assertEqual([180.0], F.live_offsets(200, pre_padding=120))    # only inside the programme
        self.assertEqual([], F.live_offsets(40))

    def test_a_frame_without_the_logo_the_others_share(self):
        frames = [F.Frame(_noisy(i, logo=i != 2), 5.0, i) for i in range(4)]
        self.assertEqual([False, False, True, False], F.logo_missing(frames))

    def test_no_logo_anywhere_says_nothing(self):
        frames = [F.Frame(_noisy(i, logo=False), 5.0, i) for i in range(4)]
        self.assertEqual([False] * 4, F.logo_missing(frames))
        self.assertEqual([False, False], F.logo_missing(frames[:2]))   # too few to tell

    def test_an_ad_loses_to_a_less_detailed_frame(self):
        pics = iter([(_noisy(1, logo=False), {"YAVG": 100, "YLOW": 20, "YHIGH": 200}),
                     (_noisy(2), {"YAVG": 100, "YLOW": 20, "YHIGH": 200}),
                     (_noisy(3), {"YAVG": 100, "YLOW": 20, "YHIGH": 200})])
        details = {}
        real = F.detail_of
        with mock.patch.object(F, "detail_of", side_effect=lambda j: details.setdefault(j, real(j) + (50 if j == _noisy(1, logo=False) else 0))):
            best, notes = F.best_frame(lambda s, n: b"x", 1000, 100.0, [10, 50, 90], grabber=lambda c: next(pics))
        self.assertEqual(50, best.seconds)
        self.assertIn("no station logo", notes[0])

    def test_a_silent_frame_comes_after_one_with_sound(self):
        pics = iter([(_jpeg(), {"YAVG": 100, "YLOW": 20, "YHIGH": 200, "VOLUME": -91.0}),
                     (_noisy(2), {"YAVG": 100, "YLOW": 20, "YHIGH": 200, "VOLUME": -22.0})])
        best, notes = F.best_frame(lambda s, n: b"x", 1000, 100.0, [10, 50], grabber=lambda c: next(pics))
        self.assertEqual(50, best.seconds)
        self.assertIn("silent", notes[0])
        # all silent (a programme with no sound): still the most detailed
        pics = iter([(_jpeg(), {"YAVG": 100, "YLOW": 20, "YHIGH": 200, "VOLUME": -91.0})])
        best, _notes = F.best_frame(lambda s, n: b"x", 1000, 100.0, [10], grabber=lambda c: next(pics))
        self.assertIsNotNone(best)

    def test_the_volume_of_a_piece(self):
        for stderr, want in ((b"[Parsed_volumedetect_0] mean_volume: -63.2 dB\n", -63.2),
                             (b"mean_volume: -inf dB", -200.0), (b"", None)):
            with mock.patch.object(F.subprocess, "run", return_value=mock.Mock(returncode=0, stderr=stderr)) as run:
                self.assertEqual(want, F._volume(b"ts", 5))
            self.assertIs(F._lowest_priority, run.call_args.kwargs["preexec_fn"])


class NeverReplaces(unittest.TestCase):
    """Over random sequences of people, Jellyfin and Tentacle acting on the
    same recordings: Tentacle never writes over a picture it didn't set,
    never sets again a picture a person changed or removed, and off removes
    only its own."""

    def test_random_sequences(self):
        from models.database import set_setting
        env = mock.patch.dict("os.environ", {"DATA_DIR": temp_dir(self)})   # the pictures kept while recording
        env.start()
        self.addCleanup(env.stop)
        for seed in range(60):
            rnd = random.Random(seed)
            db = _db()
            jf = FakeJellyfin()
            touched = set()      # items whose picture a person or Jellyfin changed after Tentacle set it
            seen_ours = set()
            ids = []
            now = NOW
            good = (_jpeg(), {"YAVG": 100, "YLOW": 20, "YHIGH": 200})
            for step in range(40):
                op = rnd.choice(["add", "pass", "pass", "person_set", "person_remove", "grab", "off", "on",
                                 "record", "live", "live", "finish"])
                if op in ("add", "record") or not ids:
                    iid = f"r{len(ids)}"
                    name = rnd.choice(["Canadiens at Blackhawks", "Jeopardy!", "Live: NHL Hockey"])
                    if op == "record":
                        jf.start(iid, f"{name} {iid}", now - timedelta(minutes=rnd.randint(0, 30)),
                                 now + timedelta(hours=1))
                    else:
                        jf.add(iid, name, now - timedelta(hours=rnd.randint(2, 30)))
                    ids.append(iid)
                    continue
                if op == "finish":
                    if jf.recording:
                        old = rnd.choice(sorted(jf.recording))
                        new = f"m{old}" if rnd.random() < 0.8 else None
                        jf.finish(old, rnd.randint(20, 90), now, new_id=new)
                        if new:
                            ids.append(new)
                    continue
                iid = rnd.choice(ids)
                kind = rnd.choice(["Primary", "Thumb"])
                if op in ("person_set", "person_remove", "grab"):
                    had = {k: jf.images.get((iid, k)) for k in ("Primary", "Thumb")}
                    if op == "person_set":
                        jf.put(iid, kind, "person")
                    elif op == "person_remove":
                        jf.images.pop((iid, kind), None)
                    elif not jf.tags(iid):
                        jf.put(iid, "Primary", "jellyfin")
                    now_ = {k: jf.images.get((iid, k)) for k in ("Primary", "Thumb")}
                    if now_ != had and any(v and v[2] == "tentacle" for v in had.values()):
                        touched.add(iid)    # a picture Tentacle set was changed or removed
                    continue
                if op in ("off", "on"):
                    set_setting(db, J.SETTING, "false" if op == "off" else "true")
                before = {k: v for k, v in jf.images.items()}
                start = len(jf.writes)
                grabber = (lambda chunk: good) if rnd.random() < 0.5 else (lambda chunk: None)
                if op == "live":
                    J.live_pass(db, jf, now=now, grabber=grabber, probe=lambda chunk, last: None)
                else:
                    J.run_pass(db, jf, now=now, grabber=grabber)
                for action, wid, wkind in jf.writes[start:]:
                    prior = before.get((wid, wkind))
                    if action == "remove":
                        self.assertEqual("tentacle", prior and prior[2], f"seed {seed}: removed {wid} {wkind}")
                    else:
                        others = [v for (i, k), v in before.items() if i == wid and v[2] != "tentacle"]
                        self.assertEqual([], others, f"seed {seed}: wrote over {wid}'s picture")
                        self.assertNotIn(wid, touched, f"seed {seed}: set {wid} again after a person changed it")
                        seen_ours.add(wid)
                now += timedelta(minutes=10)
            db.close()
        self.assertTrue(seen_ours)


if __name__ == "__main__":
    unittest.main()
