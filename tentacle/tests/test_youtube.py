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
        from models.database import TentacleUser, YouTubeChannel, YouTubeVideo
        self.mdb = mdb
        self.YouTubeVideo = YouTubeVideo
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
        self.assertTrue(rows[0]["origin"].startswith("YouTube channel"))

    def test_it_is_always_on_and_says_so(self):
        # Adding the channel is the decision. A switch here that could turn the
        # playlist off while the channel stayed was one more thing to explain.
        from routers.smartlists import _compute_auto_playlists, toggle_auto_playlist, AutoPlaylistToggleRequest
        row = next(r for r in _compute_auto_playlists(self.db, user_id=self.user.id)
                   if r["category"] == "youtube")
        self.assertTrue(row["enabled"])
        self.assertTrue(row["locked"])
        r = toggle_auto_playlist(AutoPlaylistToggleRequest(key=row["key"], enabled=False),
                                 db=self.db, user=self.user)
        self.assertFalse(r["success"])
        self.assertIn("remove the channel", r["message"])

    def test_every_user_gets_the_playlist_not_just_whoever_added_it(self):
        from models.database import TentacleUser
        from services.smartlists import get_desired_smartlists
        other = TentacleUser(jellyfin_user_id="u2", display_name="guest")
        self.db.add(other)
        self.db.commit()
        for u in (self.user, other):
            names = [x["name"] for x in get_desired_smartlists(self.db, user_id=u.id)]
            self.assertIn("TraderTV Live", names, u.display_name)

    def test_a_disabled_channel_has_no_playlist(self):
        from services.smartlists import get_desired_smartlists
        self.channel.enabled = False
        self.db.commit()
        names = [x["name"] for x in get_desired_smartlists(self.db, user_id=self.user.id)]
        self.assertNotIn("TraderTV Live", names)

    def test_the_desired_playlist_is_movies_sorted_newest_first(self):
        from services.smartlists import get_desired_smartlists
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
            keep_count=10, extra_tags=[])
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

    def test_every_image_type_a_row_might_ask_for_is_written(self):
        # poster -> Primary, fanart -> Backdrop, landscape -> Thumb. A wide row
        # draws from Thumb; without it the client falls back to the portrait
        # Primary and crops a strip out of the middle.
        from pathlib import Path
        info = self.library.write_video(self.video, self.channel, "http://t", root=self.root)
        folder = Path(info["folder"])
        for name in ("poster.jpg", "fanart.jpg", "landscape.jpg"):
            self.assertTrue((folder / name).exists(), name)
        self.assertEqual(info["artwork"], 3)

    def test_a_folder_written_before_thumbs_existed_gets_one(self):
        from pathlib import Path
        info = self.library.write_video(self.video, self.channel, "http://t", root=self.root)
        folder = Path(info["folder"])
        (folder / "landscape.jpg").unlink()
        self.fetched.clear()
        self.assertEqual(self.library.fetch_artwork(self.video, folder), 1)
        self.assertTrue((folder / "landscape.jpg").exists())

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
        self.assertEqual(info["artwork"], 3)

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
        self._answer(body={"tentacle": True, "youtube": True})
        r = self.sync.check_base_url("http://192.168.1.10:8888")
        self.assertTrue(r["ok"])

    def test_it_probes_an_endpoint_that_needs_no_login(self):
        # A .strm is fetched with no session, so the probe has to be too.
        # Probing an admin route reported every correctly configured instance
        # as needing a login, ffmpeg having no session either.
        seen = []

        def _probe(url):
            seen.append(url)

            class _R:
                status_code = 200
                headers = {}
                def json(self_): return {"tentacle": True}
            return _R()
        self.sync._probe = _probe
        self.sync.check_base_url("http://192.168.1.10:8888")
        self.assertEqual(seen, ["http://192.168.1.10:8888/api/youtube/ping"])

        from routers import youtube
        route = next(r for r in youtube.router.routes
                     if getattr(r, "path", "") == "/api/youtube/ping")
        self.assertEqual(list(route.dependencies), [])

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


class TestAddingAChannel(unittest.TestCase):
    """Add is the only step. Everything after it happens on its own.

    The form is a URL, how many of the newest videos to keep, and two
    checkboxes. Adding starts the index straight away; the playlist exists for
    every user; putting it on a home screen is done on the Home Screen tab like
    any other row.
    """

    def setUp(self):
        import tempfile as _tf
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from models.database import TentacleUser, YouTubeChannel
        from routers import youtube
        self.youtube, self.YouTubeChannel = youtube, YouTubeChannel
        engine = create_engine(f"sqlite:///{_tf.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(TentacleUser(jellyfin_user_id="a" * 32, display_name="u", is_admin=True))
        self.db.commit()

        self.info = {
            "kind": "channel", "channel_id": "UC" + "a" * 22, "handle": None,
            "playlist_id": None, "title": "A Channel", "avatar_url": None,
            "banner_url": None, "canonical": "u", "has_uploads": True,
        }
        self.started = []
        self._real = (youtube.client.available, youtube.indexer.resolve_channel,
                      youtube._start_refresh)
        youtube.client.available = lambda: True
        youtube.indexer.resolve_channel = lambda url: dict(self.info)
        youtube._start_refresh = lambda **kw: self.started.append(kw) or True

    def tearDown(self):
        (self.youtube.client.available, self.youtube.indexer.resolve_channel,
         self.youtube._start_refresh) = self._real

    def _add(self, **fields):
        body = self.youtube.ChannelCreate(url="https://youtube.com/@x", **fields)
        return self.youtube.add_channel(body, db=self.db)

    def _channel(self):
        return self.db.query(self.YouTubeChannel).first()

    def test_indexing_starts_on_its_own(self):
        r = self._add()
        self.assertTrue(r["indexing"])
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0]["channel_ids"], [r["id"]])

    def test_keep_newest_is_the_one_setting(self):
        self._add(keep_count=25)
        ch = self._channel()
        self.assertEqual(ch.keep_count, 25)
        # Derived, not asked for: no length filter, uploads always on.
        self.assertEqual(ch.min_duration, 0)
        self.assertTrue(ch.include_videos)

    def test_keep_newest_is_clamped_to_something_sane(self):
        self._add(keep_count=5000)
        self.assertEqual(self._channel().keep_count, self.youtube.indexer.MAX_KEEP)
        self.db.delete(self._channel())
        self.db.commit()
        self.info["channel_id"] = "UC" + "b" * 22
        self.info["title"] = "B"
        self._add(keep_count=0)
        self.assertEqual(self._channel().keep_count, 1)

    def test_live_tv_is_chosen_at_add(self):
        self._add(live=True)
        self.assertTrue(self._channel().live_enabled)
        # ...and the guide is refreshed once the index has found its streams.
        self.assertTrue(self.started[0]["guide"])

    def test_live_tv_off_asks_for_no_guide_refresh(self):
        self._add(live=False)
        self.assertFalse(self._channel().live_enabled)
        self.assertFalse(self.started[0]["guide"])

    def test_a_channel_with_no_uploads_keeps_its_past_streams_instead(self):
        # Decided here so nobody has to know that such channels exist.
        self.info["has_uploads"] = False
        self._add()
        self.assertTrue(self._channel().include_streams)

    def test_a_channel_with_uploads_does_not(self):
        self._add()
        self.assertFalse(self._channel().include_streams)

    def test_old_fields_are_ignored_not_rejected(self):
        # A client built against the previous form still works.
        body = self.youtube.ChannelCreate(url="u", backfill=30, min_duration=60,
                                          include_streams=True)
        self.youtube.add_channel(body, db=self.db)
        ch = self._channel()
        self.assertEqual(ch.min_duration, 0)
        self.assertFalse(ch.include_streams)


class TestKeepNewest(unittest.TestCase):
    """"Keep the newest N" is one number driving two things."""

    def _channel(self, keep):
        class C:
            keep_count = keep
        return C()

    def test_the_listing_reads_a_little_past_n(self):
        # A few of the newest may be private or members-only; reading exactly N
        # would leave the library short.
        from services.youtube.indexer import listing_limit
        self.assertEqual(listing_limit(self._channel(10)), 15)

    def test_the_listing_is_bounded(self):
        from services.youtube.indexer import MAX_KEEP, listing_limit
        self.assertEqual(listing_limit(self._channel(10_000)), MAX_KEEP + 5)

    def test_a_missing_value_still_reads_something(self):
        from services.youtube.indexer import listing_limit
        self.assertGreater(listing_limit(self._channel(None)), 0)

    def test_retention_counts_videos_not_broadcasts(self):
        import tempfile as _tf
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from models.database import YouTubeChannel, YouTubeVideo
        from services.youtube import library, sync
        engine = create_engine(f"sqlite:///{_tf.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        ch = YouTubeChannel(input_url="u", kind="channel", title="C", slug="c",
                            enabled=True, keep_count=2, live_enabled=True, extra_tags=[])
        db.add(ch)
        db.commit()
        db.refresh(ch)
        for i, st in enumerate(("is_live", "is_upcoming", None, None)):
            db.add(YouTubeVideo(channel_fk=ch.id, video_id=str(i) * 11, title=str(i),
                                live_status=st, published_at=datetime(2026, 1, 10 - i)))
        db.commit()
        real = library.remove_video
        library.remove_video = lambda v: 0
        try:
            retired = sync.apply_retention(db, ch)
        finally:
            library.remove_video = real
        # Two uploads within a keep of 2: nothing retired. The two broadcasts
        # are guide entries and must not have counted against the two uploads.
        self.assertEqual(retired, 0)


class TestRetiredSubscriptionTable(unittest.TestCase):
    def test_the_old_table_is_dropped_and_dropping_twice_is_fine(self):
        import sqlite3
        import tempfile as _tf
        import models.database as mdb
        conn = sqlite3.connect(_tf.mkdtemp() + "/t.db")
        cur = conn.cursor()
        cur.execute("CREATE TABLE youtube_row_subscriptions (id INTEGER PRIMARY KEY)")
        conn.commit()
        mdb._drop_retired_tables(cur, conn)
        mdb._drop_retired_tables(cur, conn)
        cur.execute("SELECT name FROM sqlite_master WHERE name='youtube_row_subscriptions'")
        self.assertIsNone(cur.fetchone())


class TestChannelListForLiveTv(unittest.TestCase):
    """The Live TV page lists YouTube channels next to everything else in
    the lineup, so the channel list has to say what the guide knows."""

    def setUp(self):
        import tempfile as _tf
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from models.database import EPGProgram, YouTubeChannel
        from services.youtube import livetv
        engine = create_engine(f"sqlite:///{_tf.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.on = YouTubeChannel(input_url="u", kind="channel", title="On", slug="on",
                                 enabled=True, live_enabled=True, extra_tags=[])
        self.off = YouTubeChannel(input_url="u", kind="channel", title="Off", slug="off",
                                  enabled=True, live_enabled=False, extra_tags=[])
        self.db.add_all([self.on, self.off])
        self.db.commit()
        for i in range(3):
            self.db.add(EPGProgram(channel_id=livetv.epg_channel_id(self.on),
                                   title=f"p{i}", start=datetime(2026, 1, 1, i),
                                   stop=datetime(2026, 1, 1, i + 1)))
        self.db.commit()

    def test_guide_number_and_entry_count_are_reported(self):
        from routers.youtube import list_channels
        from services.youtube import livetv
        rows = {r["title"]: r for r in list_channels(db=self.db)}
        self.assertEqual(rows["On"]["guide_number"], livetv.guide_number(self.on))
        self.assertEqual(rows["On"]["guide_programmes"], 3)
        # Off Live TV: nothing to count, and nothing counted.
        self.assertEqual(rows["Off"]["guide_programmes"], 0)


class TestStreamsTabIsOnlyPeekedAt(unittest.TestCase):
    """A Live TV channel reads its streams tab to find what is on air.

    Live and upcoming broadcasts sit at the top of that tab; below them is the
    channel's whole history of finished streams, each of which costs a
    rate-limited detail fetch just to be recorded as skipped. Reading fifteen
    of those to find two live ones is what made "keep newest 10" report thirty
    entries being indexed — and doubled the first index's running time.
    """

    def _channel(self, **kw):
        class C:
            kind = "channel"; channel_id = "UC" + "x" * 22; handle = None
            playlist_id = None; include_videos = True; include_shorts = False
            keep_count = 10; include_streams = False; live_enabled = True
        c = C()
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def test_uploads_read_past_n_but_streams_only_peeked(self):
        from services.youtube.indexer import LIVE_PEEK, _tab_urls, tab_limit
        ch = self._channel()
        limits = {url.rsplit("/", 1)[-1]: tab_limit(ch, url) for url in _tab_urls(ch)}
        self.assertEqual(limits["videos"], 15)
        self.assertEqual(limits["streams"], LIVE_PEEK)

    def test_a_channel_that_keeps_past_streams_reads_them_fully(self):
        # Its library IS the streams tab, so the peek would starve it.
        from services.youtube.indexer import _tab_urls, tab_limit
        ch = self._channel(include_streams=True)
        limits = {url.rsplit("/", 1)[-1]: tab_limit(ch, url) for url in _tab_urls(ch)}
        self.assertEqual(limits["streams"], 15)


class TestKeepNewestStopsFetching(unittest.TestCase):
    """Once the newest N are in hand, nothing further down is fetched.

    The listing is read a little past N so that private or unavailable
    entries among the newest do not leave the library short. But every entry
    fetched costs a rate-limited detail call, and anything below the N-th kept
    item is older than "the newest N" — it would only be retired by retention.
    Fetching it anyway is what made "keep newest 10" report thirty entries.
    """

    def setUp(self):
        import tempfile as _tf
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from models.database import YouTubeChannel, YouTubeVideo
        from services.youtube import client, indexer
        from services.youtube.errors import VideoUnavailable
        self.YouTubeVideo, self.indexer, self.Unavailable = YouTubeVideo, indexer, VideoUnavailable
        engine = create_engine(f"sqlite:///{_tf.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.channel = YouTubeChannel(
            input_url="u", kind="channel", channel_id="UC" + "k" * 22, title="Ch",
            slug="ch", enabled=True, include_videos=True, include_streams=False,
            include_shorts=False, min_duration=0, keep_count=3, live_enabled=False,
            extra_tags=[])
        self.db.add(self.channel)
        self.db.commit()
        self.db.refresh(self.channel)

        self.tabs = {"videos": [], "streams": []}
        self.details = {}
        self.calls, self.progress = [], []
        self._real = (client.flat_listing, client.video_details, indexer.time.sleep)
        client.flat_listing = lambda url, limit: {
            "entries": [{"id": v, "title": v} for v in self.tabs[url.rsplit("/", 1)[-1]][:limit]]}

        def _details(vid):
            self.calls.append(vid)
            d = self.details.get(vid, {"duration": 600, "availability": "public"})
            if d == "gone":
                raise self.Unavailable(vid)
            return dict(d)
        client.video_details = _details
        indexer.time.sleep = lambda s: None

    def tearDown(self):
        from services.youtube import client
        client.flat_listing, client.video_details, self.indexer.time.sleep = self._real

    def _index(self):
        self.calls.clear()
        return self.indexer.index_channel(self.db, self.channel,
                                          on_progress=lambda k, n: self.progress.append((k, n)))

    def _library(self):
        return sorted(v.video_id for v in self.db.query(self.YouTubeVideo).filter(
            self.YouTubeVideo.removed_at.is_(None)).all())

    def test_fetching_stops_at_the_nth_kept(self):
        self.tabs["videos"] = [c * 11 for c in "abcdef"]
        r = self._index()
        self.assertEqual(self.calls, ["a" * 11, "b" * 11, "c" * 11])
        self.assertEqual(r["beyond"], 3)
        self.assertEqual(self._library(), ["a" * 11, "b" * 11, "c" * 11])

    def test_a_skipped_entry_does_not_count_so_the_margin_still_helps(self):
        self.tabs["videos"] = [c * 11 for c in "abcdef"]
        self.details["b" * 11] = "gone"
        r = self._index()
        self.assertEqual(self.calls, [c * 11 for c in "abcd"])
        self.assertEqual(self._library(), [c * 11 for c in "acd"])
        self.assertEqual(r["beyond"], 2)

    def test_a_new_upload_later_is_still_fetched_when_the_library_is_full(self):
        self.tabs["videos"] = [c * 11 for c in "abcdef"]
        self._index()
        self.tabs["videos"] = ["z" * 11] + [c * 11 for c in "abcdef"]
        r = self._index()
        # Only the newcomer costs a fetch. The three kept ones are known and
        # count towards N as they are passed; d, e, f are beyond it.
        self.assertEqual(self.calls, ["z" * 11])
        self.assertEqual(r["beyond"], 3)
        self.assertIn("z" * 11, self._library())

    def test_the_streams_tab_is_not_subject_to_it(self):
        # Read for what is on air, and nothing on it counts towards N.
        self.channel.live_enabled = True
        self.channel.keep_count = 1
        self.db.commit()
        self.tabs["videos"] = ["a" * 11, "b" * 11]
        self.tabs["streams"] = ["s" * 11, "t" * 11]
        self.details["s" * 11] = {"live_status": "is_live", "availability": "public"}
        self.details["t" * 11] = {"live_status": "was_live", "duration": 3600, "availability": "public"}
        r = self._index()
        self.assertEqual(self.calls, ["a" * 11, "s" * 11, "t" * 11])
        self.assertEqual(r["beyond"], 1)
        live = [v.video_id for v in self.db.query(self.YouTubeVideo).filter(
            self.YouTubeVideo.live_status == "is_live").all()]
        self.assertEqual(live, ["s" * 11])

    def test_progress_is_reported_in_the_users_terms(self):
        self.tabs["videos"] = [c * 11 for c in "abcdef"]
        self._index()
        self.assertEqual(self.progress[-1], (3, 3))
        self.assertTrue(all(k <= n for k, n in self.progress))


class _FakeJellyfin:
    """Stands in for JellyfinService: records calls, answers from a script."""

    def __init__(self, tagged_counts=(), playlist_items=None, playlists=None):
        self.calls = []
        self._tagged = list(tagged_counts)     # successive answers to query_items
        self._playlist_items = playlist_items or {}
        self._playlists = playlists or []

    libraries = [{"Name": "YouTube", "Locations": ["/mnt/media/youtube"], "ItemId": "lib-yt"},
                 {"Name": "Movies", "Locations": ["/mnt/media/movies"], "ItemId": "lib-mov"}]

    def get_libraries(self):
        return list(self.libraries)

    def notify_media_updated(self, paths, update_type="Created"):
        self.calls.append(("notify", tuple(paths)))
        return bool(paths)

    def trigger_library_scan(self, library_id=None):
        self.calls.append(("scan", library_id))

    def query_items(self, include_types, tags=None, **kw):
        n = self._tagged.pop(0) if len(self._tagged) > 1 else (self._tagged[0] if self._tagged else 0)
        self.calls.append(("query", tuple(tags or ()), n))
        return [{"Id": str(i)} for i in range(n)]

    def get_playlist_items(self, playlist_id, limit=50000):
        return [{"Id": str(i)} for i in range(self._playlist_items.get(playlist_id, 0))]

    def get_playlists(self, user_id=None):
        return list(self._playlists)

    def delete_item(self, item_id):
        self.calls.append(("delete", item_id))
        # As Jellyfin does: once deleted, it is no longer listed.
        self._playlists = [p for p in self._playlists if p.get("Id") != item_id]
        return True


class _PublishFixture(unittest.TestCase):
    """A channel with three library videos, one user, and every Jellyfin and
    playlist call replaced by a recorder."""

    def setUp(self):
        import tempfile as _tf
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from models.database import TentacleUser, YouTubeChannel, YouTubeVideo
        import services.jellyfin as jfmod
        import services.smartlists as sm
        from services.youtube import resolver, sync as ysync
        self.sm, self.ysync, self.resolver = sm, ysync, resolver
        engine = create_engine(f"sqlite:///{_tf.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.user = TentacleUser(jellyfin_user_id="u" * 32, display_name="u", is_admin=True)
        self.db.add(self.user)
        self.channel = YouTubeChannel(input_url="u", kind="channel", title="TraderTV Live",
                                      slug="tradertv-live", enabled=True, keep_count=10,
                                      extra_tags=[])
        self.db.add(self.channel)
        self.db.commit()
        self.db.refresh(self.channel)
        for i in range(3):
            self.db.add(YouTubeVideo(channel_fk=self.channel.id, video_id=str(i) * 11,
                                     title=str(i), live_status="not_live",
                                     folder_path=f"/media/youtube/TraderTV Live/v{i}"))
        self.db.add(YouTubeVideo(channel_fk=self.channel.id, video_id="l" * 11,
                                 title="live", live_status="is_live"))
        self.db.commit()
        mdb_set = __import__("models.database", fromlist=["set_setting"]).set_setting
        mdb_set(self.db, "jellyfin_url", "http://jf")
        mdb_set(self.db, "jellyfin_api_key", "k")

        self.log = []
        self.jf = _FakeJellyfin()
        self._saved = {
            "JellyfinService": jfmod.JellyfinService,
            "sync_smartlists": sm.sync_smartlists,
            "refresh_smartlist_playlists": sm.refresh_smartlist_playlists,
            "write_home_config": sm.write_home_config,
            "bump_playlist_version": sm.bump_playlist_version,
            "_notify_jellyfin_plugin": sm._notify_jellyfin_plugin,
            "_get_smartlists_with_playlist_ids": sm._get_smartlists_with_playlist_ids,
            "sleep": ysync.time.sleep,
            "resolve": resolver.resolve,
            "is_cached": resolver.is_cached,
            "refill": ysync.start_background_refill,
        }
        self.resolved = []
        resolver.resolve = lambda vid, height=1080, force=False: self.resolved.append(vid)
        resolver.is_cached = lambda vid: False
        ysync.start_background_refill = lambda: self.log.append(("refill",)) or True
        jfmod.JellyfinService = lambda *a, **k: self.jf
        sm.sync_smartlists = lambda db, user_id=None: self.log.append(("sync", user_id))
        def _refresh(db, user_id=None, only_names=None):
            self.log.append(("refresh", user_id, tuple(only_names or ())))
            # As the real one does: the playlist ends up holding what Jellyfin has.
            self.jf._playlist_items["pl-1"] = self.jf._tagged[0] if self.jf._tagged else 0
            return {}
        sm.refresh_smartlist_playlists = _refresh
        sm.write_home_config = lambda db, user_id=None: self.log.append(("home", user_id))
        sm.bump_playlist_version = lambda: self.log.append(("bump",))
        sm._notify_jellyfin_plugin = lambda db: self.log.append(("notify",))
        sm._get_smartlists_with_playlist_ids = lambda db, user_id=None: [
            {"name": "TraderTV Live", "playlist_id": "pl-1", "is_youtube": True}]
        ysync.time.sleep = lambda s: self.log.append(("sleep", s))

    def tearDown(self):
        import services.jellyfin as jfmod
        s = self._saved
        jfmod.JellyfinService = s["JellyfinService"]
        for k in ("sync_smartlists", "refresh_smartlist_playlists", "write_home_config",
                  "bump_playlist_version", "_notify_jellyfin_plugin",
                  "_get_smartlists_with_playlist_ids"):
            setattr(self.sm, k, s[k])
        self.ysync.time.sleep = s["sleep"]
        self.resolver.resolve, self.resolver.is_cached = s["resolve"], s["is_cached"]
        self.ysync.start_background_refill = s["refill"]


class TestPublishWaitsForJellyfin(_PublishFixture):
    """The playlist is filled only once Jellyfin actually has the videos.

    A fixed 20-second sleep stood between the scan and the fill. Against a
    full scan of a large library that takes minutes, the playlist was filled
    before the videos existed in Jellyfin, came up empty, and stayed empty —
    the library then filled in on its own and the row never appeared.
    """

    def test_jellyfin_is_told_which_folders_appeared_not_asked_to_scan(self):
        # The Radarr way, and why that is fast: the new folders, translated to
        # the path Jellyfin has the same mount under, and no library scan.
        self.jf._tagged = [3]
        self.ysync.publish_to_jellyfin(self.db, [self.channel])
        self.assertEqual(self.jf.calls[0], ("notify", (
            "/mnt/media/youtube/TraderTV Live/v0",
            "/mnt/media/youtube/TraderTV Live/v1",
            "/mnt/media/youtube/TraderTV Live/v2")))
        self.assertNotIn("scan", [c[0] for c in self.jf.calls])

    def test_a_full_scan_is_the_fallback_only_when_the_library_cannot_be_told(self):
        self.jf.libraries = [{"Name": "Videos", "Locations": ["/mnt/media/misc"], "ItemId": "x"}]
        self.jf._tagged = [3]
        self.ysync.publish_to_jellyfin(self.db, [self.channel])
        self.assertIn(("scan", None), self.jf.calls)
        self.assertNotIn("notify", [c[0] for c in self.jf.calls])

    def test_a_short_result_hands_off_instead_of_blocking(self):
        # Like a Radarr add: fill what is there, report "still filling", and
        # let the background loop top the playlist up as the videos land.
        self.jf._tagged = [1]
        self.ysync.SCAN_MAX_WAIT_SECONDS, saved = 0, self.ysync.SCAN_MAX_WAIT_SECONDS
        try:
            result = self.ysync.publish_to_jellyfin(self.db, [self.channel])
        finally:
            self.ysync.SCAN_MAX_WAIT_SECONDS = saved
        self.assertTrue(result["short"])
        self.assertIn(("refill",), self.log)
        self.assertIn("refresh", [e[0] for e in self.log])
        # ...and the one library was given a targeted scan to catch the rest.
        self.assertIn(("scan", "lib-yt"), self.jf.calls)

    def test_a_full_result_reports_done(self):
        self.jf._tagged = [3]
        result = self.ysync.publish_to_jellyfin(self.db, [self.channel])
        self.assertFalse(result["short"])
        self.assertNotIn(("refill",), self.log)

    def test_streams_are_resolved_before_jellyfin_is_told(self):
        # Jellyfin probes every .strm it imports, and each probe reaches the
        # resolver. Warm it first so the probes are instant — and only for the
        # library videos, not the live stream, which is not a .strm.
        self.jf._tagged = [3]
        order = []
        real_notify = self.jf.notify_media_updated
        self.jf.notify_media_updated = lambda paths, update_type="Created": order.append("notify") or real_notify(paths)
        self.resolver.resolve = lambda vid, height=1080, force=False: order.append(("resolve", vid))
        self.ysync.publish_to_jellyfin(self.db, [self.channel])
        resolved = [o[1] for o in order if o != "notify"]
        self.assertEqual(sorted(resolved), ["0" * 11, "1" * 11, "2" * 11])
        self.assertEqual(order[-1], "notify")

    def test_already_cached_streams_are_not_resolved_again(self):
        self.jf._tagged = [3]
        self.resolver.is_cached = lambda vid: True
        self.ysync.publish_to_jellyfin(self.db, [self.channel])
        self.assertEqual(self.resolved, [])

    def test_the_toast_is_told_what_is_being_waited_for(self):
        stages = []
        self.jf._tagged = [0, 3]
        self.ysync.publish_to_jellyfin(self.db, [self.channel], on_stage=stages.append)
        self.assertTrue(any("waiting for Jellyfin to see TraderTV Live (0 of 3)" in st for st in stages), stages)
        self.assertIn("filling the playlists", stages)

    def test_it_waits_until_the_channels_videos_are_there_then_fills(self):
        # Jellyfin reports 0, then 1, then 3 of the 3 library videos (the live
        # stream is not a library item and is not waited for).
        self.jf._tagged = [0, 1, 3]
        self.ysync.publish_to_jellyfin(self.db, [self.channel])
        queries = [c for c in self.jf.calls if c[0] == "query"]
        self.assertEqual([q[2] for q in queries], [0, 1, 3])
        self.assertEqual(queries[0][1], ("yt:tradertv-live",))
        # ...and only then were the playlists created, filled and pushed.
        kinds = [e[0] for e in self.log if e[0] != "sleep"]
        self.assertEqual(kinds, ["sync", "refresh", "home", "bump", "notify"])
        self.assertEqual(next(e for e in self.log if e[0] == "refresh")[2], ("TraderTV Live",))

    def test_it_gives_up_waiting_eventually_but_still_fills(self):
        # A scan that never delivers must not hang the run, nor skip the fill:
        # the hourly check refills later.
        self.jf._tagged = [1]
        self.ysync.SCAN_MAX_WAIT_SECONDS, saved = 0, self.ysync.SCAN_MAX_WAIT_SECONDS
        try:
            self.ysync.publish_to_jellyfin(self.db, [self.channel])
        finally:
            self.ysync.SCAN_MAX_WAIT_SECONDS = saved
        self.assertIn("refresh", [e[0] for e in self.log])


class TestPlaylistsAreRefilledHourly(_PublishFixture):
    """However a playlist fell behind the library, the next run catches it up."""

    def test_a_short_playlist_is_refilled_once_jellyfin_has_more(self):
        self.jf._playlist_items = {"pl-1": 1}      # 1 in the playlist, 3 in the library
        self.jf._tagged = [3]                      # ...and Jellyfin has all 3
        fixed = self.ysync.reconcile_playlists(self.db)
        self.assertEqual(fixed, 1)
        self.assertIn(("refresh", self.user.id, ("TraderTV Live",)), self.log)
        self.assertIn(("notify",), self.log)

    def test_nothing_to_add_yet_means_no_pointless_refresh(self):
        # Jellyfin has no more than the playlist does: the videos have not
        # landed. Refreshing would churn the playlist for nothing.
        self.jf._playlist_items = {"pl-1": 1}
        self.jf._tagged = [1]
        fixed, behind = self.ysync.reconcile_playlists(self.db, report=True)
        self.assertEqual(fixed, 0)
        self.assertTrue(behind)
        self.assertNotIn("refresh", [e[0] for e in self.log])

    def test_a_full_playlist_is_left_alone_and_reported_as_caught_up(self):
        self.jf._playlist_items = {"pl-1": 3}
        self.assertEqual(self.ysync.reconcile_playlists(self.db, report=True), (0, False))
        self.assertNotIn("refresh", [e[0] for e in self.log])


class TestRemovingAChannelRemovesEverything(_PublishFixture):
    """Gone from the page means gone from Jellyfin: row, hero, playlist, guide."""

    def setUp(self):
        super().setUp()
        from models.database import TentacleUser
        self.other = TentacleUser(jellyfin_user_id="o" * 32, display_name="o")
        self.db.add(self.other)
        self.db.commit()
        import models.database as mdb
        import routers.smartlists as rs
        from services.youtube import livetv
        self._more = {"SessionLocal": mdb.SessionLocal, "read": rs._read_home_json,
                      "write": rs._write_home_json, "guide": livetv.refresh_jellyfin_guide}
        mdb.SessionLocal = self.Session
        self.configs = {
            self.user.id: {"hero": {"enabled": True, "playlist_id": "pl-1", "display_name": "TraderTV Live"},
                           "rows": [{"type": "playlist", "playlist_id": "pl-1", "display_name": "TraderTV Live", "order": 1},
                                    {"type": "playlist", "playlist_id": "pl-2", "display_name": "Netflix Movies", "order": 2}]},
            self.other.id: {"hero": {"enabled": False}, "rows": [
                {"type": "playlist", "playlist_id": "pl-9", "display_name": "TraderTV Live", "order": 1}]},
        }
        self.written = {}
        rs._read_home_json = lambda user: dict(self.configs.get(user.id) or {})
        rs._write_home_json = lambda user, cfg: self.written.__setitem__(user.id, cfg)
        livetv.refresh_jellyfin_guide = lambda db: self.log.append(("guide",)) or True
        self.jf._playlists = [{"Id": "pl-1", "Name": "TraderTV Live"}]

    def tearDown(self):
        import models.database as mdb
        import routers.smartlists as rs
        from services.youtube import livetv
        mdb.SessionLocal = self._more["SessionLocal"]
        rs._read_home_json, rs._write_home_json = self._more["read"], self._more["write"]
        livetv.refresh_jellyfin_guide = self._more["guide"]
        super().tearDown()

    def _remove(self, was_live=True):
        from routers.youtube import _cleanup_after_remove
        _cleanup_after_remove("TraderTV Live", was_live)

    def test_the_row_is_removed_from_every_user_not_kept_for_a_grace_period(self):
        self._remove()
        names = lambda uid: [r["display_name"] for r in self.written[uid]["rows"]]
        self.assertEqual(names(self.user.id), ["Netflix Movies"])
        self.assertEqual(names(self.other.id), [])

    def test_a_hero_that_pointed_at_it_is_turned_off(self):
        self._remove()
        self.assertFalse(self.written[self.user.id]["hero"]["enabled"])

    def test_the_recorded_playlist_is_deleted_by_id_never_by_name(self):
        # Orphan cleanup normally deletes it; this covers the case it did not.
        self._remove()
        self.assertIn(("delete", "pl-1"), self.jf.calls)
        self.assertEqual([c for c in self.jf.calls if c[0] == "delete"], [("delete", "pl-1")])

    def test_jellyfin_rescans_and_the_guide_is_refreshed(self):
        self._remove(was_live=True)
        self.assertIn(("scan", "lib-yt"), self.jf.calls)
        self.assertIn(("guide",), self.log)

    def test_no_guide_refresh_for_a_channel_that_was_not_on_live_tv(self):
        self._remove(was_live=False)
        self.assertNotIn(("guide",), self.log)


class TestRemovingAChannelRemovesItsFolder(unittest.TestCase):
    """The channel's own folder goes with it — not just the video folders in it.

    Each video lives in its own folder under <root>/<Channel>/. Removing the
    videos removed those and left an empty channel folder on the drive, which
    then had to be cleaned up by hand.
    """

    def setUp(self):
        import tempfile as _tf
        from pathlib import Path
        self.root = Path(_tf.mkdtemp())

    def test_an_emptied_channel_folder_is_removed(self):
        from pathlib import Path
        folder = self.root / "TraderTV Live"
        (folder / "2026-01-01 A [aaaaaaaaaaa]").mkdir(parents=True)
        (folder / "2026-01-01 A [aaaaaaaaaaa]" / "a.strm").write_text("x")

        class V:
            folder_path = str(folder / "2026-01-01 A [aaaaaaaaaaa]")
        library.remove_video(V())
        self.assertTrue(library.remove_channel_folder("TraderTV Live", root=self.root))
        self.assertFalse(folder.exists())

    def test_a_folder_holding_someone_elses_files_is_left_alone(self):
        # Only what Tentacle made is Tentacle's to delete. A file it did not
        # write means the folder is shared, and it stays, with a log line.
        folder = self.root / "TraderTV Live"
        folder.mkdir(parents=True)
        (folder / "notes.txt").write_text("mine")
        self.assertFalse(library.remove_channel_folder("TraderTV Live", root=self.root))
        self.assertTrue((folder / "notes.txt").exists())

    def test_a_missing_folder_is_fine(self):
        self.assertFalse(library.remove_channel_folder("Never Added", root=self.root))
