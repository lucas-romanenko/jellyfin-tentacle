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
        self.thumbnail_url = None
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
        # These tests are about the .strm and the NFO. Artwork is fetched over
        # the network, which a test must never do — TestArtwork covers it.
        self._real_download = library._download
        library._download = lambda url: b""

    def tearDown(self):
        library._download = self._real_download

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


class TestLiveStatusRefresh(unittest.TestCase):
    """A scheduled stream has to become playable when it actually goes live."""

    def setUp(self):
        import tempfile as _tf
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from models.database import YouTubeChannel, YouTubeVideo
        self.YouTubeVideo = YouTubeVideo
        engine = create_engine(f"sqlite:///{_tf.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.channel = YouTubeChannel(input_url="u", kind="channel",
                                      channel_id="UC" + "z" * 22, title="Ch",
                                      slug="ch", live_enabled=True, enabled=True,
                                      extra_tags=[], include_videos=True)
        self.db.add(self.channel)
        self.db.commit()
        self.db.refresh(self.channel)

    def test_streams_tab_is_polled_whenever_live_tv_is_on(self):
        # /streams is where live and scheduled broadcasts live. Without it a
        # Live TV channel can never find anything to play.
        from services.youtube.indexer import _tab_urls
        self.channel.include_streams = False
        urls = _tab_urls(self.channel)
        self.assertTrue(any(u.endswith("/streams") for u in urls), urls)

    def test_streams_tab_skipped_when_live_tv_is_off(self):
        from services.youtube.indexer import _tab_urls
        self.channel.live_enabled = False
        self.channel.include_streams = False
        self.assertFalse(any(u.endswith("/streams") for u in _tab_urls(self.channel)))

    def test_an_upcoming_stream_is_rechecked_and_becomes_live(self):
        from services.youtube import indexer
        v = self.YouTubeVideo(channel_fk=self.channel.id, video_id="aaaaaaaaaaa",
                              title="Match", live_status="is_upcoming",
                              media_type="livestream", published_at=datetime(2026, 9, 17))
        self.db.add(v)
        self.db.commit()

        with mock.patch.object(indexer.client, "flat_listing",
                               return_value={"entries": [{"id": "aaaaaaaaaaa"}]}), \
             mock.patch.object(indexer.client, "video_details",
                               return_value={"live_status": "is_live", "duration": 3600,
                                             "availability": "public"}):
            indexer.index_channel(self.db, self.channel)

        self.db.refresh(v)
        self.assertEqual(v.live_status, "is_live")

    def test_a_finished_stream_stops_being_live(self):
        from services.youtube import indexer
        v = self.YouTubeVideo(channel_fk=self.channel.id, video_id="bbbbbbbbbbb",
                              title="Match", live_status="is_live",
                              media_type="livestream", published_at=datetime(2026, 9, 17))
        self.db.add(v)
        self.db.commit()

        with mock.patch.object(indexer.client, "flat_listing",
                               return_value={"entries": [{"id": "bbbbbbbbbbb"}]}), \
             mock.patch.object(indexer.client, "video_details",
                               return_value={"live_status": None, "duration": 1800,
                                             "availability": "public"}):
            indexer.index_channel(self.db, self.channel)

        self.db.refresh(v)
        self.assertIsNone(v.live_status)
        # ...and it is now an ordinary library item
        self.assertTrue(indexer.is_library_item(v))


class TestTrackSelection(unittest.TestCase):
    """Picking the video and audio tracks for a live remux.

    Two bugs lived here. Handing ffmpeg the master playlist let it choose, and
    it chose 240p. And audio-only HLS renditions are reported by yt-dlp with
    acodec=None (unknown) rather than a codec name, so filtering on acodec
    dropped every audio track and the stream came out silent.
    """

    # Shape taken from a real visionos extraction of a live stream.
    FORMATS = [
        {"format_id": "233", "vcodec": "none", "acodec": None, "tbr": 64,
         "protocol": "m3u8_native", "url": "https://x/a233.m3u8"},
        {"format_id": "234", "vcodec": "none", "acodec": None, "tbr": 128,
         "protocol": "m3u8_native", "url": "https://x/a234.m3u8"},
        {"format_id": "269", "height": 144, "vcodec": "avc1.4D400C", "acodec": "none",
         "protocol": "m3u8_native", "url": "https://x/v144.m3u8"},
        {"format_id": "311", "height": 720, "vcodec": "avc1.4D4020", "acodec": "none",
         "protocol": "m3u8_native", "url": "https://x/v720.m3u8"},
        {"format_id": "312", "height": 1080, "vcodec": "avc1.4D402A", "acodec": "none",
         "protocol": "m3u8_native", "url": "https://x/v1080.m3u8"},
    ]

    def _pick(self, max_height):
        from services.youtube import resolver
        with mock.patch.object(resolver.client, "extract",
                               return_value={"formats": self.FORMATS}):
            return resolver.pick_tracks("v9LArDyyNxw", max_height)

    def test_picks_the_tallest_within_the_cap(self):
        v, a, _ = self._pick(1080)
        self.assertEqual(v, "https://x/v1080.m3u8")
        v, a, _ = self._pick(720)
        self.assertEqual(v, "https://x/v720.m3u8")

    def test_never_exceeds_the_cap(self):
        v, _, _ = self._pick(360)
        self.assertEqual(v, "https://x/v144.m3u8")

    def test_audio_is_found_despite_acodec_being_none(self):
        # The bug: (acodec or "none") != "none" excluded every rendition, so
        # Live TV played silent video.
        _, a, _ = self._pick(1080)
        self.assertEqual(a, "https://x/a234.m3u8", "audio rendition was dropped")

    def test_highest_bitrate_audio_wins(self):
        _, a, _ = self._pick(1080)
        self.assertEqual(a, "https://x/a234.m3u8")   # tbr 128 over 64

    def test_muxed_stream_needs_no_separate_audio(self):
        from services.youtube import resolver
        muxed = [{"format_id": "18", "height": 360, "vcodec": "avc1", "acodec": "mp4a",
                  "protocol": "m3u8_native", "url": "https://x/muxed.m3u8"}]
        with mock.patch.object(resolver.client, "extract", return_value={"formats": muxed}):
            v, a, _ = resolver.pick_tracks("x", 1080)
        self.assertEqual(v, "https://x/muxed.m3u8")
        self.assertIsNone(a)

    def test_no_usable_tracks_raises(self):
        from services.youtube import resolver
        from services.youtube.errors import YouTubeError
        with mock.patch.object(resolver.client, "extract", return_value={"formats": []}):
            with self.assertRaises(YouTubeError):
                resolver.pick_tracks("x", 1080)


class TestFinishedStreamsStayOutOfTheLibrary(unittest.TestCase):
    """A Live TV channel's back catalogue must not become library items.

    /streams is polled for any Live TV channel so we can tell what is on air.
    Letting finished broadcasts (live_status="was_live") through filled the
    library with old streams instead of the channel's actual uploads, even with
    "Past live streams" unchecked.
    """

    def setUp(self):
        import tempfile as _tf
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from models.database import YouTubeChannel, YouTubeVideo
        self.YouTubeVideo = YouTubeVideo
        engine = create_engine(f"sqlite:///{_tf.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.channel = YouTubeChannel(
            input_url="u", kind="channel", channel_id="UC" + "q" * 22,
            title="TraderTV Live", slug="tradertv-live", enabled=True,
            live_enabled=True, include_videos=True, include_streams=False,
            min_duration=60, extra_tags=[])
        self.db.add(self.channel)
        self.db.commit()
        self.db.refresh(self.channel)

    def _should(self, live_status, duration=600):
        from services.youtube.indexer import _should_index
        return _should_index({"live_status": live_status, "duration": duration,
                              "availability": "public"}, self.channel)

    def test_a_real_upload_is_indexed(self):
        keep, _ = self._should(None)
        self.assertTrue(keep)

    def test_a_finished_stream_is_not(self):
        keep, reason = self._should("was_live")
        self.assertFalse(keep, reason)

    def test_unless_past_live_streams_is_ticked(self):
        self.channel.include_streams = True
        keep, _ = self._should("was_live")
        self.assertTrue(keep)

    def test_a_live_stream_is_still_kept_for_the_guide(self):
        keep, _ = self._should("is_live", duration=None)
        self.assertTrue(keep)

    def test_already_indexed_finished_streams_are_cleaned_up(self):
        from services.youtube import indexer
        for vid, status in (("aaaaaaaaaaa", "was_live"), ("bbbbbbbbbbb", None)):
            self.db.add(self.YouTubeVideo(
                channel_fk=self.channel.id, video_id=vid, title=vid,
                live_status=status, duration=600))
        self.db.commit()

        removed = indexer._apply_stream_preference(self.db, self.channel)
        self.assertEqual(removed, 1)
        survivors = self.db.query(self.YouTubeVideo).filter(
            self.YouTubeVideo.removed_at.is_(None)).all()
        self.assertEqual([v.video_id for v in survivors], ["bbbbbbbbbbb"])

    def test_cleanup_leaves_them_alone_when_the_user_wants_them(self):
        from services.youtube import indexer
        self.channel.include_streams = True
        self.db.add(self.YouTubeVideo(channel_fk=self.channel.id, video_id="aaaaaaaaaaa",
                                      title="old stream", live_status="was_live"))
        self.db.commit()
        self.assertEqual(indexer._apply_stream_preference(self.db, self.channel), 0)


class TestPostLiveStatus(unittest.TestCase):
    """yt-dlp reports a just-ended broadcast as post_live, not was_live.

    Handling only was_live let recently finished streams into the library even
    with "Past live streams" unchecked — and a channel that streams constantly
    always has one in that state.
    """

    def setUp(self):
        import tempfile as _tf
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from models.database import YouTubeChannel, YouTubeVideo
        self.YouTubeVideo = YouTubeVideo
        engine = create_engine(f"sqlite:///{_tf.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.channel = YouTubeChannel(
            input_url="u", kind="channel", channel_id="UC" + "p" * 22,
            title="Ch", slug="ch", enabled=True, live_enabled=True,
            include_videos=True, include_streams=False, min_duration=60,
            extra_tags=[])
        self.db.add(self.channel)
        self.db.commit()
        self.db.refresh(self.channel)

    def _should(self, status):
        from services.youtube.indexer import _should_index
        return _should_index({"live_status": status, "duration": 600,
                              "availability": "public"}, self.channel)[0]

    def test_both_finished_states_are_excluded(self):
        self.assertFalse(self._should("was_live"))
        self.assertFalse(self._should("post_live"))

    def test_both_are_included_when_asked_for(self):
        self.channel.include_streams = True
        self.assertTrue(self._should("was_live"))
        self.assertTrue(self._should("post_live"))

    def test_cleanup_covers_post_live_too(self):
        from services.youtube import indexer
        for vid, st in (("aaaaaaaaaaa", "was_live"), ("bbbbbbbbbbb", "post_live"),
                        ("ccccccccccc", None)):
            self.db.add(self.YouTubeVideo(channel_fk=self.channel.id, video_id=vid,
                                          title=vid, live_status=st, duration=600))
        self.db.commit()
        self.assertEqual(indexer._apply_stream_preference(self.db, self.channel), 2)
        left = self.db.query(self.YouTubeVideo).filter(
            self.YouTubeVideo.removed_at.is_(None)).all()
        self.assertEqual([v.video_id for v in left], ["ccccccccccc"])

    def test_a_finished_broadcast_is_a_library_item_not_a_guide_entry(self):
        from services.youtube.indexer import is_library_item
        v = self.YouTubeVideo(channel_fk=self.channel.id, video_id="aaaaaaaaaaa",
                              title="x", live_status="post_live")
        self.assertTrue(is_library_item(v))


class TestPlaylistIsUploadsOnly(unittest.TestCase):
    """A channel's playlist holds its uploads — never its live streams.

    This is the whole point of the feature: the home row shows what the channel
    posted, and live broadcasts belong to Live TV.
    """

    def setUp(self):
        import tempfile as _tf
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from models.database import (TentacleUser, YouTubeChannel,
                                     YouTubeRowSubscription, YouTubeVideo)
        self.mdb = mdb
        self.YouTubeVideo, self.YouTubeRowSubscription = YouTubeVideo, YouTubeRowSubscription
        engine = create_engine(f"sqlite:///{_tf.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(TentacleUser(jellyfin_user_id="u1", display_name="lucas", is_admin=True))
        self.channel = YouTubeChannel(
            input_url="u", kind="channel", channel_id="UC" + "a" * 22,
            title="TraderTV Live", slug="tradertv-live", enabled=True,
            include_videos=True, include_streams=False, extra_tags=[])
        self.db.add(self.channel)
        self.db.commit()
        self.db.refresh(self.channel)
        self.user = self.db.query(TentacleUser).first()
        # A realistic mix, including a NULL status
        for vid, st in (("aaaaaaaaaaa", None), ("bbbbbbbbbbb", "not_live"),
                        ("ccccccccccc", "is_live"), ("ddddddddddd", "is_upcoming"),
                        ("eeeeeeeeeee", "was_live")):
            self.db.add(self.YouTubeVideo(channel_fk=self.channel.id, video_id=vid,
                                          title=vid, live_status=st, duration=300))
        self.db.commit()

    def test_counts_exclude_pending_broadcasts_but_include_null_status(self):
        # NULL NOT IN (...) is NULL in SQL, so a bare NOT IN silently dropped
        # every video whose status was never set.
        from routers.smartlists import _compute_auto_playlists
        row = next(r for r in _compute_auto_playlists(self.db, user_id=self.user.id)
                   if r["category"] == "youtube")
        self.assertEqual(row["item_count"], 3)   # NULL + not_live + was_live

    def test_the_channel_appears_on_the_playlists_page(self):
        from routers.smartlists import _compute_auto_playlists
        rows = [r for r in _compute_auto_playlists(self.db, user_id=self.user.id)
                if r["category"] == "youtube"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "TraderTV Live")
        self.assertEqual(rows[0]["tag"], "yt:tradertv-live")
        self.assertEqual(rows[0]["origin"], "YouTube channel")

    def test_enabled_state_follows_the_row_subscription(self):
        from routers.smartlists import _compute_auto_playlists
        def enabled():
            return next(r for r in _compute_auto_playlists(self.db, user_id=self.user.id)
                        if r["category"] == "youtube")["enabled"]
        self.assertFalse(enabled())
        self.db.add(self.YouTubeRowSubscription(channel_fk=self.channel.id,
                                               user_id=self.user.id, max_items=30))
        self.db.commit()
        self.assertTrue(enabled())

    def test_the_desired_playlist_is_movies_sorted_newest_first(self):
        from services.smartlists import get_desired_smartlists
        self.db.add(self.YouTubeRowSubscription(channel_fk=self.channel.id,
                                               user_id=self.user.id, max_items=30))
        self.db.commit()
        sl = next(s for s in get_desired_smartlists(self.db, user_id=self.user.id)
                  if s["name"] == "TraderTV Live")
        self.assertEqual(sl["tag"], "yt:tradertv-live")
        self.assertEqual(sl["media_type"], ["Movie"])
        self.assertEqual(sl["default_sort"], "ReleaseDate")

    def test_skip_reasons_are_recorded_for_the_user_to_see(self):
        from services.youtube import indexer
        with mock.patch.object(indexer.client, "flat_listing",
                               return_value={"entries": [{"id": "fffffffffff"}]}), \
             mock.patch.object(indexer.client, "video_details",
                               return_value={"live_status": None, "duration": 20,
                                             "availability": "public"}):
            self.channel.min_duration = 600
            r = indexer.index_channel(self.db, self.channel)
        self.assertEqual(r["new"], 0)
        self.assertTrue(r["skips"], "a silent skip is why this looked like nothing happened")
        self.assertTrue(any("minimum" in k for k in r["skips"]), r["skips"])
        self.assertEqual(self.channel.last_skips, r["skips"])


class TestStreamPreferenceIsReversible(unittest.TestCase):
    """Turning "Past live streams" back on has to bring them back.

    A video is detailed once, on the run that first sees it; everything already
    in the table is skipped as "known". So a finished broadcast removed when the
    preference was off was never reconsidered when it went back on, and the
    setting was quietly one-way — the user turns it on, presses Refresh, and
    nothing happens.
    """

    def setUp(self):
        import tempfile as _tf
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from models.database import YouTubeChannel, YouTubeVideo
        self.YouTubeVideo = YouTubeVideo
        engine = create_engine(f"sqlite:///{_tf.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.channel = YouTubeChannel(
            input_url="u", kind="channel", channel_id="UC" + "r" * 22,
            title="Ch", slug="ch", enabled=True, live_enabled=True,
            include_videos=True, include_streams=False, min_duration=60,
            extra_tags=[])
        self.db.add(self.channel)
        self.db.commit()
        self.db.refresh(self.channel)
        for vid, st in (("aaaaaaaaaaa", "was_live"), ("bbbbbbbbbbb", "post_live"),
                        ("ccccccccccc", None)):
            self.db.add(YouTubeVideo(channel_fk=self.channel.id, video_id=vid,
                                     title=vid, live_status=st, duration=600))
        self.db.commit()

    def _live(self):
        return sorted(v.video_id for v in self.db.query(self.YouTubeVideo).filter(
            self.YouTubeVideo.removed_at.is_(None)).all())

    def test_turning_the_preference_off_then_on_restores_them(self):
        from services.youtube import indexer
        self.assertEqual(indexer._apply_stream_preference(self.db, self.channel), 2)
        self.assertEqual(self._live(), ["ccccccccccc"])

        self.channel.include_streams = True
        # Negative = restored, so the caller can tell the two directions apart.
        self.assertEqual(indexer._apply_stream_preference(self.db, self.channel), -2)
        self.assertEqual(self._live(), ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"])

    def test_restoring_is_idempotent(self):
        from services.youtube import indexer
        self.channel.include_streams = True
        self.assertEqual(indexer._apply_stream_preference(self.db, self.channel), 0)
        self.assertEqual(len(self._live()), 3)

    def test_an_ordinary_video_is_never_restored_by_it(self):
        # Retention removes videos the channel no longer lists. That is not a
        # settings decision and must not be undone here.
        from services.youtube import indexer
        gone = self.db.query(self.YouTubeVideo).filter(
            self.YouTubeVideo.video_id == "ccccccccccc").first()
        gone.removed_at = datetime.utcnow()
        self.db.commit()
        self.channel.include_streams = True
        indexer._apply_stream_preference(self.db, self.channel)
        self.assertEqual(self._live(), ["aaaaaaaaaaa", "bbbbbbbbbbb"])


class TestColumnDefaultsSurviveUpgrade(unittest.TestCase):
    """ALTER TABLE ADD COLUMN fills existing rows with NULL.

    A model default is applied by SQLAlchemy at INSERT time, so it never reaches
    rows that already exist. For a boolean defaulting to True that reads as
    False on every upgraded row — `include_videos` going False means a channel's
    uploads are never even looked at, with nothing in the logs to say why.
    """

    def setUp(self):
        import sqlite3
        import tempfile as _tf
        self.sqlite3 = sqlite3
        self.path = _tf.mkdtemp() + "/t.db"
        self.conn = sqlite3.connect(self.path)
        self.cursor = self.conn.cursor()
        self.cursor.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        self.cursor.execute("INSERT INTO t (id) VALUES (1)")
        self.conn.commit()

    def _add(self, name, sql_type, column):
        import models.database as mdb
        self.cursor.execute(f"ALTER TABLE t ADD COLUMN {name} {sql_type}")
        self.conn.commit()
        mdb._backfill_default(self.cursor, self.conn, "t", column)
        self.cursor.execute(f"SELECT {name} FROM t WHERE id = 1")
        return self.cursor.fetchone()[0]

    def test_a_true_default_reaches_existing_rows(self):
        from sqlalchemy import Boolean, Column
        col = Column("include_videos", Boolean, default=True)
        col.name = "include_videos"
        self.assertEqual(self._add("include_videos", "BOOLEAN", col), 1)

    def test_a_false_default_reaches_existing_rows(self):
        from sqlalchemy import Boolean, Column
        col = Column("include_streams", Boolean, default=False)
        col.name = "include_streams"
        self.assertEqual(self._add("include_streams", "BOOLEAN", col), 0)

    def test_a_numeric_default_reaches_existing_rows(self):
        from sqlalchemy import Column, Integer
        col = Column("min_duration", Integer, default=60)
        col.name = "min_duration"
        self.assertEqual(self._add("min_duration", "INTEGER", col), 60)

    def test_a_column_with_no_default_is_left_null(self):
        from sqlalchemy import Column, Integer
        col = Column("whatever", Integer)
        col.name = "whatever"
        self.assertIsNone(self._add("whatever", "INTEGER", col))

    def test_a_callable_default_is_left_to_the_application(self):
        # datetime.utcnow / dict / list — these are not stable scalars and
        # writing one value into every row would be wrong.
        from sqlalchemy import Column, JSON
        col = Column("last_skips", JSON, default=dict)
        col.name = "last_skips"
        self.assertIsNone(self._add("last_skips", "JSON", col))


class TestSkippedVideosAreRemembered(unittest.TestCase):
    """A skipped video has to leave a trace, or it is re-detailed forever.

    Details are fetched one every few seconds to stay under YouTube's rate
    limit. A skipped video used to be dropped on the floor, so it was "new"
    again on the next run — a channel whose back catalogue is mostly excluded
    paid the full detail cost on every single refresh and never settled.
    """

    def setUp(self):
        import tempfile as _tf
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from models.database import YouTubeChannel, YouTubeVideo
        from services.youtube import client, indexer
        self.YouTubeVideo = YouTubeVideo
        self.indexer = indexer
        engine = create_engine(f"sqlite:///{_tf.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.channel = YouTubeChannel(
            input_url="u", kind="channel", channel_id="UC" + "s" * 22,
            title="Ch", slug="ch", enabled=True, include_videos=True,
            include_streams=False, include_shorts=False, min_duration=60,
            backfill=10, extra_tags=[])
        self.db.add(self.channel)
        self.db.commit()
        self.db.refresh(self.channel)

        self.entries = [{"id": "a" * 11, "title": "upload"},
                        {"id": "b" * 11, "title": "old broadcast"},
                        {"id": "c" * 11, "title": "clip"}]
        self.details = {
            "a" * 11: {"title": "upload", "duration": 600, "availability": "public"},
            "b" * 11: {"title": "old broadcast", "duration": 3600,
                       "live_status": "was_live", "availability": "public"},
            "c" * 11: {"title": "clip", "duration": 20, "availability": "public"},
        }
        self.detail_calls = []
        self._real_listing, self._real_details = client.flat_listing, client.video_details
        self._real_sleep = indexer.time.sleep
        client.flat_listing = lambda url, limit: {"entries": list(self.entries)}
        def _details(vid):
            self.detail_calls.append(vid)
            return dict(self.details[vid])
        client.video_details = _details
        indexer.time.sleep = lambda s: None

    def tearDown(self):
        from services.youtube import client
        client.flat_listing, client.video_details = self._real_listing, self._real_details
        self.indexer.time.sleep = self._real_sleep

    def _index(self):
        return self.indexer.index_channel(self.db, self.channel)

    def test_a_second_run_re_reads_nothing(self):
        first = self._index()
        self.assertEqual(first["new"], 1)
        self.assertEqual(first["filtered"], 2)
        self.assertEqual(len(self.detail_calls), 3)

        self.detail_calls.clear()
        second = self._index()
        self.assertEqual(self.detail_calls, [], "skipped videos were fetched again")
        self.assertEqual(second["new"], 0)

    def test_the_skipped_rows_stay_out_of_the_library(self):
        self._index()
        live = self.db.query(self.YouTubeVideo).filter(
            self.YouTubeVideo.removed_at.is_(None)).all()
        self.assertEqual([v.video_id for v in live], ["a" * 11])

    def test_each_row_records_why_it_was_passed_over(self):
        self._index()
        reasons = {v.video_id: v.skip_reason for v in
                   self.db.query(self.YouTubeVideo).filter(
                       self.YouTubeVideo.skip_reason.isnot(None)).all()}
        self.assertEqual(reasons["b" * 11], self.indexer.STREAM_PREFERENCE_REASON)
        self.assertIn("under minimum", reasons["c" * 11])

    def test_turning_past_live_streams_on_brings_back_only_that_one(self):
        self._index()
        self.channel.include_streams = True
        self.db.commit()
        self.detail_calls.clear()
        self._index()
        live = sorted(v.video_id for v in self.db.query(self.YouTubeVideo).filter(
            self.YouTubeVideo.removed_at.is_(None)).all())
        self.assertEqual(live, ["a" * 11, "b" * 11])
        # The short clip was excluded by the minimum length, which this
        # preference has no business overriding.
        self.assertEqual(self.detail_calls, [])

    def test_what_each_tab_returned_is_recorded(self):
        # "No videos" has two opposite causes — YouTube listed nothing, or it
        # listed plenty that the settings excluded. Only this tells them apart.
        result = self._index()
        self.assertEqual(result["listing"], {"videos": 3})
        self.assertEqual(self.channel.last_listing, {"videos": 3})


class TestMasterOffersOneVariant(unittest.TestCase):
    """A master must expose one video track and one audio track.

    YouTube's master offers the whole ladder in two codecs, each variant
    pointing at an audio group holding the original soundtrack plus every
    machine-generated dub. Passed through, ffmpeg opens all of them at once —
    400 streams for a five-minute video — and reports corrupt packets, which
    Jellyfin shows as a fatal playback error.
    """

    MASTER = "\n".join([
        "#EXTM3U",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        '#EXT-X-MEDIA:URI="https://y/es.m3u8",TYPE=AUDIO,GROUP-ID="234",'
        'LANGUAGE="es",NAME="es - dubbed-auto",'
        'YT-EXT-XTAGS="ChQKBWFjb250EgtkdWJiZWQtYXV0bwoKCgRsYW5nEgJlcw",DEFAULT=NO',
        '#EXT-X-MEDIA:URI="https://y/en.m3u8",TYPE=AUDIO,GROUP-ID="234",'
        'LANGUAGE="en-US",NAME="American English - original",'
        'YT-EXT-XTAGS="ChEKBWFjb250EghvcmlnaW5hbAoNCgRsYW5nEgVlbi1VUw",DEFAULT=NO',
        '#EXT-X-STREAM-INF:BANDWIDTH=300000,CODECS="avc1.4D4015,mp4a.40.2",'
        'RESOLUTION=426x240,AUDIO="234"',
        "https://y/240.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=3657065,CODECS="avc1.64002A,mp4a.40.2",'
        'RESOLUTION=1920x1080,AUDIO="234"',
        "https://y/1080.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=2100863,CODECS="vp09.00.41.08,mp4a.40.2",'
        'RESOLUTION=1920x1080,AUDIO="234"',
        "https://y/1080vp9.m3u8",
    ]) + "\n"

    def _rewrite(self, max_height=None, text=None):
        from services.youtube import playlist
        return playlist.rewrite(text if text is not None else self.MASTER,
                                "https://y/master.m3u8", "/p", max_height=max_height)

    def test_only_one_video_variant_survives(self):
        out = self._rewrite()
        self.assertEqual(out.count("#EXT-X-STREAM-INF"), 1)

    def test_only_one_audio_rendition_survives(self):
        out = self._rewrite()
        self.assertEqual(out.count("#EXT-X-MEDIA"), 1)

    def test_the_surviving_audio_is_the_original_not_a_dub(self):
        out = self._rewrite()
        self.assertIn("American English - original", out)
        self.assertNotIn("dubbed-auto", out)

    def test_audio_is_kept_at_all(self):
        # The CODECS attribute advertises muxed video+audio, but the variant is
        # served video-only — dropping the group entirely plays it silent.
        out = self._rewrite()
        self.assertIn("TYPE=AUDIO", out)
        self.assertIn('AUDIO="234"', out)

    def test_h264_wins_over_vp9_at_the_same_height(self):
        out = self._rewrite()
        self.assertIn("avc1.64002A", out)
        self.assertNotIn("vp09", out)

    def test_the_height_cap_is_honoured(self):
        out = self._rewrite(max_height=480)
        self.assertIn("RESOLUTION=426x240", out)
        self.assertNotIn("1920x1080", out)

    def test_a_cap_below_everything_still_plays_something(self):
        # Returning an empty master would be a playback error, which is worse
        # than exceeding the cap the user asked for.
        out = self._rewrite(max_height=100)
        self.assertEqual(out.count("#EXT-X-STREAM-INF"), 1)
        self.assertIn("426x240", out)

    def test_a_media_playlist_is_untouched_by_variant_selection(self):
        media = "\n".join([
            "#EXTM3U", "#EXT-X-TARGETDURATION:5",
            "#EXTINF:5.0,", "https://y/seg1.ts",
            "#EXTINF:5.0,", "https://y/seg2.ts",
            "#EXT-X-ENDLIST",
        ]) + "\n"
        out = self._rewrite(text=media)
        self.assertEqual(out.count("/p/r/"), 2)
        self.assertIn("#EXT-X-ENDLIST", out)
        # ffmpeg checks every segment URL against allowed_segment_extensions.
        self.assertEqual(out.count(".ts"), 2)

    def test_a_master_with_no_audio_group_still_plays(self):
        text = "\n".join([
            "#EXTM3U",
            '#EXT-X-STREAM-INF:BANDWIDTH=300000,CODECS="avc1.4D4015",RESOLUTION=426x240',
            "https://y/240.m3u8",
        ]) + "\n"
        out = self._rewrite(text=text)
        self.assertEqual(out.count("#EXT-X-STREAM-INF"), 1)
        self.assertNotIn("AUDIO=", out)

    def test_the_variant_url_is_proxied(self):
        out = self._rewrite()
        self.assertNotIn("https://y/1080.m3u8", out)
        self.assertIn("/p/r/", out)
        self.assertTrue(any(l.startswith("/p/r/") and l.endswith(".m3u8")
                            for l in out.splitlines()))


class TestArtwork(unittest.TestCase):
    """Videos arrived in Jellyfin with no images at all."""

    class _Video:
        video_id = "kQA2wNKxy_8"
        thumbnail_url = None
        title = "A video"
        description = "d"
        published_at = None
        first_seen = None
        duration = 390
        folder_path = None
        strm_path = None

    class _Channel:
        title = "Ch"
        slug = "ch"
        extra_tags = []
        rating = None

    def setUp(self):
        import tempfile as _tf
        from pathlib import Path
        from services.youtube import library
        self.library = library
        self.root = Path(_tf.mkdtemp())
        self.video, self.channel = self._Video(), self._Channel()
        self.fetched = []

        def _fake(url):
            self.fetched.append(url)
            return b"\xff\xd8\xff" + b"j" * 2000
        self._real = library._download
        library._download = _fake

    def tearDown(self):
        library._download = self._real

    def test_a_poster_and_a_backdrop_are_written(self):
        from pathlib import Path
        info = self.library.write_video(self.video, self.channel, "http://t", root=self.root)
        folder = Path(info["folder"])
        self.assertTrue((folder / "poster.jpg").exists())
        self.assertTrue((folder / "fanart.jpg").exists())
        self.assertEqual(info["artwork"], 2)

    def test_youtubes_own_jpeg_is_tried_first(self):
        self.library.write_video(self.video, self.channel, "http://t", root=self.root)
        self.assertEqual(self.fetched,
                         ["https://i.ytimg.com/vi/kQA2wNKxy_8/maxresdefault.jpg"])

    def test_it_falls_back_when_maxres_does_not_exist(self):
        # maxresdefault is absent for plenty of uploads; hqdefault always is.
        misses = {"https://i.ytimg.com/vi/kQA2wNKxy_8/maxresdefault.jpg"}

        def _fake(url):
            self.fetched.append(url)
            return b"" if url in misses else b"\xff\xd8\xff" + b"j" * 2000
        library._download = _fake
        info = self.library.write_video(self.video, self.channel, "http://t", root=self.root)
        self.assertEqual(self.fetched[-1], "https://i.ytimg.com/vi/kQA2wNKxy_8/hqdefault.jpg")
        self.assertEqual(info["artwork"], 2)

    def test_yt_dlps_choice_is_the_last_resort_not_the_first(self):
        # yt-dlp reports a WebP for most videos; a plain JPEG is handled
        # without question by every client, so YouTube's own path wins.
        self.video.thumbnail_url = "https://i.ytimg.com/vi_webp/x/maxresdefault.webp"
        self.library.write_video(self.video, self.channel, "http://t", root=self.root)
        self.assertEqual(self.fetched[0],
                         "https://i.ytimg.com/vi/kQA2wNKxy_8/maxresdefault.jpg")
        self.assertIn("https://i.ytimg.com/vi_webp/x/maxresdefault.webp",
                      self.library.artwork_candidates(self.video))

    def test_a_webp_is_not_written_into_a_file_called_jpg(self):
        from pathlib import Path
        webp = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"w" * 2000
        library._download = lambda url: webp
        info = self.library.write_video(self.video, self.channel, "http://t", root=self.root)
        folder = Path(info["folder"])
        self.assertTrue((folder / "poster.webp").exists())
        self.assertFalse((folder / "poster.jpg").exists())

    def test_something_that_is_not_an_image_is_not_saved(self):
        from pathlib import Path
        library._download = lambda url: b"<html>404 not found</html>" * 100
        info = self.library.write_video(self.video, self.channel, "http://t", root=self.root)
        self.assertEqual(info["artwork"], 0)
        self.assertEqual(
            [f.name for f in Path(info["folder"]).iterdir() if f.suffix in (".jpg", ".webp")],
            [])

    def test_it_is_not_downloaded_again_on_every_sync(self):
        from pathlib import Path
        info = self.library.write_video(self.video, self.channel, "http://t", root=self.root)
        self.fetched.clear()
        again = self.library.fetch_artwork(self.video, Path(info["folder"]))
        self.assertEqual((again, self.fetched), (0, []))

    def test_an_existing_webp_poster_also_counts_as_done(self):
        from pathlib import Path
        webp = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"w" * 2000
        library._download = lambda url: webp
        info = self.library.write_video(self.video, self.channel, "http://t", root=self.root)
        self.fetched.clear()
        self.assertEqual(self.library.fetch_artwork(self.video, Path(info["folder"])), 0)

    def test_the_nfo_names_the_image_too(self):
        # So Jellyfin still has artwork if the local fetch failed.
        nfo = self.library.build_nfo(self.video, self.channel, "http://t")
        self.assertIn("<thumb aspect=\"poster\">", nfo)
        self.assertIn("<fanart>", nfo)

    def test_a_failed_download_does_not_fail_the_write(self):
        from pathlib import Path
        library._download = lambda url: b""
        info = self.library.write_video(self.video, self.channel, "http://t", root=self.root)
        folder = Path(info["folder"])
        self.assertEqual(info["artwork"], 0)
        self.assertTrue(any(f.suffix == ".strm" for f in folder.iterdir()))
        self.assertTrue((folder / "movie.nfo").exists())


class TestReprobe(unittest.TestCase):
    """Jellyfin probes a .strm once and reuses the answer.

    A later scan skips any file whose size and modified time are unchanged, so
    a change in what the resolver serves is invisible to an item Jellyfin has
    already seen — it keeps building playback around streams that are no longer
    there, which surfaces as a playback error with nothing wrong on either side.
    """

    def setUp(self):
        import tempfile as _tf
        from pathlib import Path
        self.root = Path(_tf.mkdtemp())
        self.strm = self.root / "v.strm"
        self.strm.write_text("http://t/api/youtube/v/x/master.m3u8")

    def test_touching_changes_the_modified_time(self):
        import os
        old = 1_600_000_000
        os.utime(self.strm, (old, old))

        class V:
            strm_path = str(self.strm)
        self.assertTrue(library.touch_strm(V()))
        self.assertGreater(self.strm.stat().st_mtime, old)

    def test_the_url_inside_is_left_exactly_as_it_was(self):
        # Only the timestamp may change. Rewriting the file would be pointless
        # churn, and the .strm's contents are a stable Tentacle URL.
        before = self.strm.read_text()

        class V:
            strm_path = str(self.strm)
        library.touch_strm(V())
        self.assertEqual(self.strm.read_text(), before)

    def test_a_video_with_no_file_is_skipped(self):
        class V:
            strm_path = None
        self.assertFalse(library.touch_strm(V()))

    def test_a_missing_file_is_not_created(self):
        from pathlib import Path

        class V:
            strm_path = str(Path(self.root) / "gone" / "v.strm")
        self.assertFalse(library.touch_strm(V()))
        self.assertFalse(Path(V.strm_path).exists())


class TestBaseUrlIsChecked(unittest.TestCase):
    """The address a .strm carries is fetched by ffmpeg, which cannot log in.

    An address behind Cloudflare Access, a reverse proxy asking for a login, or
    simply the wrong host answers a media request with an HTML login page.
    ffmpeg reports that as "Invalid data found when processing input" and
    nothing anywhere points at the address — the setting page only ever said it
    had to be reachable, and never checked.
    """

    def setUp(self):
        from services.youtube import sync as ysync
        self.sync = ysync
        self._real = ysync._probe

    def tearDown(self):
        self.sync._probe = self._real

    def _answer(self, status=200, body=None, headers=None, raises=None):
        class _R:
            status_code = status

            def __init__(self_):
                self_.headers = headers or {}

            def json(self_):
                if body is None:
                    raise ValueError("not json")
                return body

        def _probe(url):
            if raises:
                raise raises
            return _R()
        self.sync._probe = _probe

    def test_a_working_address_passes(self):
        self._answer(body={"yt_dlp_available": True})
        r = self.sync.check_base_url("http://192.168.1.10:8888")
        self.assertTrue(r["ok"])

    def test_cloudflare_access_is_named_specifically(self):
        self._answer(status=302, headers={
            "location": "https://x.cloudflareaccess.com/cdn-cgi/access/login/t"})
        r = self.sync.check_base_url("https://t.example.com")
        self.assertFalse(r["ok"])
        self.assertIn("Cloudflare Access", r["detail"])

    def test_any_other_redirect_is_rejected_too(self):
        self._answer(status=302, headers={"location": "https://elsewhere/login"})
        r = self.sync.check_base_url("https://t.example.com")
        self.assertFalse(r["ok"])
        self.assertIn("elsewhere", r["detail"])

    def test_an_address_asking_for_a_login_is_rejected(self):
        self._answer(status=401)
        self.assertFalse(self.sync.check_base_url("https://t.example.com")["ok"])

    def test_html_instead_of_json_is_rejected(self):
        # This is exactly what ffmpeg chokes on, reported as invalid data.
        self._answer(status=200, body=None)
        r = self.sync.check_base_url("https://t.example.com")
        self.assertFalse(r["ok"])

    def test_some_other_service_answering_is_rejected(self):
        self._answer(status=200, body={"hello": "i am not tentacle"})
        r = self.sync.check_base_url("http://192.168.1.10:9999")
        self.assertFalse(r["ok"])
        self.assertIn("not Tentacle", r["detail"])

    def test_an_unreachable_address_is_reported_not_raised(self):
        self._answer(raises=OSError("connection refused"))
        r = self.sync.check_base_url("http://192.168.1.99:8888")
        self.assertFalse(r["ok"])
        self.assertIn("Could not reach", r["detail"])


class TestStrmFollowsTheAddress(unittest.TestCase):
    """Changing the address has to repoint the videos that carry it.

    Every existing .strm has the old address written inside it, so changing the
    setting alone appears to do nothing: the videos keep pointing somewhere that
    no longer serves them and playback keeps failing for the reason the setting
    page claims to have fixed.
    """

    def setUp(self):
        import tempfile as _tf
        from pathlib import Path
        self.root = Path(_tf.mkdtemp())
        self.strm = self.root / "v.strm"
        self.strm.write_text("https://old.example.com/api/youtube/v/kQA2wNKxy_8/master.m3u8")

        class V:
            video_id = "kQA2wNKxy_8"
        self.video = V()
        self.video.strm_path = str(self.strm)

    def test_the_address_is_replaced(self):
        self.assertTrue(library.rewrite_strm(self.video, "http://192.168.1.10:8888"))
        self.assertEqual(self.strm.read_text(),
                         "http://192.168.1.10:8888/api/youtube/v/kQA2wNKxy_8/master.m3u8")

    def test_the_video_id_is_preserved(self):
        library.rewrite_strm(self.video, "http://192.168.1.10:8888")
        self.assertIn("kQA2wNKxy_8", self.strm.read_text())

    def test_rewriting_to_the_same_address_is_a_no_op(self):
        # Rewriting makes Jellyfin discard what it has probed, so it must
        # happen only when the address actually changed.
        library.rewrite_strm(self.video, "http://192.168.1.10:8888")
        self.assertFalse(library.rewrite_strm(self.video, "http://192.168.1.10:8888"))
        self.assertFalse(library.rewrite_strm(self.video, "http://192.168.1.10:8888/"))

    def test_a_video_with_no_file_is_skipped(self):
        self.video.strm_path = None
        self.assertFalse(library.rewrite_strm(self.video, "http://192.168.1.10:8888"))

    def test_an_empty_address_never_blanks_a_file(self):
        before = self.strm.read_text()
        self.assertFalse(library.rewrite_strm(self.video, ""))
        self.assertEqual(self.strm.read_text(), before)
