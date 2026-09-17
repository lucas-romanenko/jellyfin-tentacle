"""Tests for the YouTube source.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from services.youtube import library, playlist
from services.youtube.errors import (
    VideoUnavailable, YouTubeBlocked, YouTubeUnavailable, classify,
)
from services.youtube.indexer import VIDEO_ID_RE, parse_input_url, slugify


class FakeVideo:
    def __init__(self, vid="EXhAoxKXBcE", title="A Video"):
        self.video_id = vid
        self.title = title
        self.description = "Desc & <markup>"
        self.published_at = datetime(2026, 9, 15)
        self.first_seen = datetime(2026, 9, 15)
        self.duration = 484
        self.folder_path = None
        self.strm_path = None


class FakeChannel:
    title = "BBC News"
    slug = "bbc-news"
    extra_tags = ["kids-approved"]
    rating = "TV-PG"


class TestUrlParsing(unittest.TestCase):
    def test_accepted_forms(self):
        self.assertEqual(parse_input_url("https://www.youtube.com/@BBCNews")["handle"], "BBCNews")
        self.assertEqual(parse_input_url("@Bluey")["handle"], "Bluey")
        self.assertEqual(
            parse_input_url("https://www.youtube.com/channel/UC16niRr50-MSBwiO3YDb3RA")["channel_id"],
            "UC16niRr50-MSBwiO3YDb3RA")
        self.assertEqual(parse_input_url("https://www.youtube.com/playlist?list=PLabc")["kind"], "playlist")

    def test_rejects_nonsense(self):
        for bad in ("", "   ", "https://example.com/video"):
            with self.assertRaises(ValueError):
                parse_input_url(bad)

    def test_slug_is_filesystem_safe(self):
        self.assertEqual(slugify("Bluey - Official Channel!"), "bluey-official-channel")
        self.assertEqual(slugify(""), "channel")


class TestErrorClassification(unittest.TestCase):
    """A bot check must never look like "the channel is empty" — that is how
    issue #16 deleted 15,988 titles on the IPTV side."""

    def test_bot_check_and_rate_limit_are_blocked(self):
        for msg in ("ERROR: Sign in to confirm you're not a bot",
                    "HTTP Error 429: Too Many Requests"):
            self.assertIsInstance(classify(Exception(msg)), YouTubeBlocked)

    def test_single_video_problems_are_skippable(self):
        for msg in ("Private video", "This video is unavailable", "members-only content"):
            self.assertIsInstance(classify(Exception(msg)), VideoUnavailable)

    def test_anything_else_is_transient(self):
        self.assertIsInstance(classify(Exception("read timed out")), YouTubeUnavailable)


class TestLibraryWriter(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_strm_points_at_tentacle_never_at_google(self):
        # Google's URLs expire in ~6h and are signed to the extracting IP, so a
        # client handed one directly gets a 403.
        url = library.strm_url("http://192.168.2.75:8888/", "EXhAoxKXBcE")
        self.assertEqual(url, "http://192.168.2.75:8888/api/youtube/v/EXhAoxKXBcE/master.m3u8")
        self.assertNotIn("googlevideo", url)

    def test_writes_strm_and_nfo(self):
        v, c = FakeVideo(), FakeChannel()
        info = library.write_video(v, c, "http://t:8888", self.root)
        folder = Path(info["folder"])
        self.assertTrue((folder / f"{folder.name}.strm").exists())
        nfo = (folder / "movie.nfo").read_text()
        self.assertIn("<tag>youtube</tag>", nfo)
        self.assertIn("<tag>yt:bbc-news</tag>", nfo)
        self.assertIn("<mpaa>TV-PG</mpaa>", nfo)
        self.assertIn("Desc &amp; &lt;markup&gt;", nfo)   # escaped, not raw
        self.assertIn("<uniqueid type=\"youtube\" default=\"true\">EXhAoxKXBcE</uniqueid>", nfo)

    def test_strm_is_not_rewritten_on_a_second_pass(self):
        # Rewriting resets the file's mtime, which wipes Jellyfin's scanned
        # media segments for the item.
        v, c = FakeVideo(), FakeChannel()
        self.assertTrue(library.write_video(v, c, "http://t:8888", self.root)["strm_written"])
        self.assertFalse(library.write_video(v, c, "http://t:8888", self.root)["strm_written"])

    def test_dateadded_uses_the_upload_date(self):
        # Otherwise backfilling a channel floods "Recently Added" with old uploads.
        nfo = library.build_nfo(FakeVideo(), FakeChannel(), "http://t")
        self.assertIn("<dateadded>2026-09-15", nfo)

    def test_removal_touches_only_that_videos_folder(self):
        v, c = FakeVideo(), FakeChannel()
        library.write_video(v, c, "http://t:8888", self.root)
        sibling = Path(v.folder_path).parent / "Another Video [zzzzzzzzzzz]"
        sibling.mkdir(parents=True)
        (sibling / "keep.strm").write_text("x")
        library.remove_video(v)
        self.assertFalse(Path(v.folder_path).exists())
        self.assertTrue((sibling / "keep.strm").exists())

    def test_unsafe_title_characters_are_stripped(self):
        v = FakeVideo(title='Bad/Name: "quoted" <x>')
        folder = library.video_folder("Chan", v, self.root)
        self.assertNotIn("/", folder.name)
        self.assertNotIn(":", folder.name)
        self.assertIn("[EXhAoxKXBcE]", folder.name)


MASTER = """#EXTM3U
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MEDIA:URI="https://r1.googlevideo.com/audio.m3u8",TYPE=AUDIO,GROUP-ID="233",NAME="Default"
#EXT-X-STREAM-INF:BANDWIDTH=352312,CODECS="avc1.4D4015",RESOLUTION=426x240,AUDIO="233"
https://r1.googlevideo.com/v240.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5200000,CODECS="avc1.640028",RESOLUTION=1920x1080,AUDIO="233"
https://r1.googlevideo.com/v1080.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-PLAYLIST-TYPE:VOD
#EXTINF:5.0,
https://r1.googlevideo.com/seg0
#EXTINF:5.0,
https://r1.googlevideo.com/seg1
#EXT-X-ENDLIST
"""


class TestPlaylistRewrite(unittest.TestCase):
    def test_no_upstream_url_survives(self):
        out = playlist.rewrite(MASTER, "https://r1.googlevideo.com/m.m3u8", "/p")
        self.assertNotIn("googlevideo", out)

    def test_segment_urls_carry_an_extension(self):
        # ffmpeg's HLS demuxer validates every segment URL against
        # allowed_segment_extensions and refuses extensionless ones outright
        # ("not in allowed_segment_extensions"), which made the whole playlist
        # unplayable. Caught only by running real ffmpeg against it.
        out = playlist.rewrite(MEDIA, "https://r1.googlevideo.com/m.m3u8", "/p")
        urls = [l for l in out.splitlines() if l.startswith("/p/")]
        self.assertTrue(urls)
        for u in urls:
            self.assertTrue(u.endswith(".ts"), u)

    def test_variant_urls_are_playlists(self):
        out = playlist.rewrite(MASTER, "https://r1.googlevideo.com/m.m3u8", "/p")
        urls = [l for l in out.splitlines() if l.startswith("/p/")]
        for u in urls:
            self.assertTrue(u.endswith(".m3u8"), u)
        self.assertIn('URI="/p/', out)      # audio rendition rewritten too
        self.assertIn('.m3u8"', out)

    def test_height_cap_drops_taller_variants(self):
        out = playlist.rewrite(MASTER, "https://r1.googlevideo.com/m.m3u8", "/p", max_height=720)
        self.assertIn("426x240", out)
        self.assertNotIn("1920x1080", out)
        # ...and drops the URL line that followed it, not just the tag. One
        # standalone variant line is left (the audio rendition's URL lives in a
        # URI= attribute, not on its own line).
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("/p/")]), 1)

    def test_tokens_resolve_back_to_the_upstream_url(self):
        playlist.rewrite(MEDIA, "https://r1.googlevideo.com/m.m3u8", "/p")
        token = playlist.register("https://r1.googlevideo.com/seg0")
        self.assertEqual(playlist.lookup(token), "https://r1.googlevideo.com/seg0")

    def test_unknown_token_is_not_resolvable(self):
        # The endpoint must never be usable as a proxy for an arbitrary host.
        self.assertIsNone(playlist.lookup("not-a-real-token"))

    def test_relative_urls_resolve_against_the_playlist(self):
        rel = "#EXTM3U\n#EXTINF:5.0,\nseg0.ts\n"
        playlist.rewrite(rel, "https://r1.googlevideo.com/dir/m.m3u8", "/p")
        self.assertEqual(playlist.lookup(playlist.register("https://r1.googlevideo.com/dir/seg0.ts")),
                         "https://r1.googlevideo.com/dir/seg0.ts")

    def test_master_detection(self):
        self.assertTrue(playlist.is_master(MASTER))
        self.assertFalse(playlist.is_master(MEDIA))


class TestVideoIdValidation(unittest.TestCase):
    def test_only_real_ids_accepted(self):
        self.assertTrue(VIDEO_ID_RE.match("EXhAoxKXBcE"))
        for bad in ("../../etc/passwd", "short", "way-too-long-for-an-id", ""):
            self.assertIsNone(VIDEO_ID_RE.match(bad), bad)


if __name__ == "__main__":
    unittest.main()


class TestLiveTv(unittest.TestCase):
    """YouTube live streams surfaced as a Live TV channel."""

    def setUp(self):
        import tempfile as _tf
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from models.database import EPGProgram, YouTubeChannel, YouTubeVideo
        self.mdb, self.EPGProgram = mdb, EPGProgram
        self.YouTubeChannel, self.YouTubeVideo = YouTubeChannel, YouTubeVideo
        engine = create_engine(f"sqlite:///{_tf.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.channel = YouTubeChannel(
            input_url="u", kind="channel", channel_id="UC" + "x" * 22,
            title="Sports Channel", slug="sports-channel",
            live_enabled=True, enabled=True, extra_tags=[])
        self.db.add(self.channel)
        self.db.commit()
        self.db.refresh(self.channel)

    def _video(self, vid, status, start, duration=None):
        v = self.YouTubeVideo(channel_fk=self.channel.id, video_id=vid, title=f"Game {vid}",
                              live_status=status, published_at=start, duration=duration,
                              media_type="livestream")
        self.db.add(v)
        self.db.commit()
        return v

    def test_only_opted_in_channels_appear(self):
        from services.youtube import livetv
        self.assertEqual(len(livetv.live_channels(self.db)), 1)
        self.channel.live_enabled = False
        self.db.commit()
        self.assertEqual(livetv.live_channels(self.db), [])

    def test_guide_numbers_do_not_collide_with_iptv_stream_ids(self):
        from services.youtube import livetv
        self.assertGreaterEqual(int(livetv.guide_number(self.channel)), livetv.GUIDE_NUMBER_BASE)

    def test_live_and_upcoming_streams_become_guide_entries(self):
        from services.youtube import livetv
        self._video("aaaaaaaaaaa", "is_live", datetime(2026, 9, 17, 18, 0), 7200)
        self._video("bbbbbbbbbbb", "is_upcoming", datetime(2026, 9, 18, 20, 0))
        self.assertEqual(livetv.refresh_guide(self.db, self.channel), 2)
        rows = self.db.query(self.EPGProgram).filter(
            self.EPGProgram.channel_id == "yt.sports-channel").all()
        self.assertEqual(len(rows), 2)

    def test_start_times_are_never_moved(self):
        # A programme's identity is channel + start, so moving a start orphans
        # any DVR timer set against it.
        from services.youtube import livetv
        start = datetime(2026, 9, 17, 18, 0)
        self._video("aaaaaaaaaaa", "is_upcoming", start, 3600)
        livetv.refresh_guide(self.db, self.channel)
        first = self.db.query(self.EPGProgram).filter_by(channel_id="yt.sports-channel").one()
        original_start = first.start
        livetv.refresh_guide(self.db, self.channel)
        again = self.db.query(self.EPGProgram).filter_by(channel_id="yt.sports-channel").one()
        self.assertEqual(again.start, original_start)

    def test_a_live_stream_extends_rather_than_duplicating(self):
        from services.youtube import livetv
        self._video("aaaaaaaaaaa", "is_live", datetime(2026, 9, 17, 18, 0), 60)
        livetv.refresh_guide(self.db, self.channel)
        livetv.refresh_guide(self.db, self.channel)
        rows = self.db.query(self.EPGProgram).filter_by(channel_id="yt.sports-channel").all()
        self.assertEqual(len(rows), 1)
        # Still running, so its end is pushed out past now
        self.assertGreater(rows[0].stop, datetime.utcnow())

    def test_no_filler_programmes_for_an_idle_channel(self):
        # Inventing "nothing on" entries floods Jellyfin's "On Now" row.
        from services.youtube import livetv
        self.assertEqual(livetv.refresh_guide(self.db, self.channel), 0)
        self.assertEqual(self.db.query(self.EPGProgram).count(), 0)

    def test_current_live_video_ignores_upcoming_ones(self):
        from services.youtube import livetv
        self._video("bbbbbbbbbbb", "is_upcoming", datetime(2026, 9, 18, 20, 0))
        self.assertIsNone(livetv.current_live_video(self.db, self.channel.id))
        self._video("aaaaaaaaaaa", "is_live", datetime(2026, 9, 17, 18, 0))
        live = livetv.current_live_video(self.db, self.channel.id)
        self.assertEqual(live.video_id, "aaaaaaaaaaa")

    def test_live_streams_are_not_written_as_library_items(self):
        # A stream has no duration yet; Jellyfin would file it as a 0-length movie.
        from services.youtube.indexer import is_library_item
        live = self._video("aaaaaaaaaaa", "is_live", datetime(2026, 9, 17, 18, 0))
        done = self._video("ccccccccccc", None, datetime(2026, 9, 16, 18, 0), 600)
        self.assertFalse(is_library_item(live))
        self.assertTrue(is_library_item(done))
