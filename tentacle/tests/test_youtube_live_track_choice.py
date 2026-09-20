"""resolver.pick_tracks (9aa3977) vs. the channel's "max height" setting.

9aa3977 added pick_tracks() because handing ffmpeg the master playlist made it
pick 240p. Selecting explicitly is right, and the audio fix (splitting on
vcodec, because yt-dlp reports an audio-only HLS rendition with acodec=None) is
right. Two selection rules are not:

* When no rendition fits under the cap, `eligible = [...] or videos` falls back
  to every rendition, and the sort is "-height" — so the cap turns into
  "pick the tallest available". A user who set 480p to fit a slow uplink gets
  the 2160p rendition.
* The H.264 preference is a sort key, not a filter. The live endpoint remuxes
  with `-c copy -f mpegts`, and ffmpeg's mpegts muxer cannot carry VP9 or AV1,
  so a non-H.264/HEVC rendition is not a worse candidate — it is not a
  candidate. When it is the only thing on offer the current code selects it and
  ffmpeg exits, which (with the stderr pipe never drained, see
  test_youtube_livetv_tuner) surfaces as an empty stream and no message.

Upstream's own TestTrackSelection never sees either, because its fixture always
contains an avc1 rendition below the cap.

Run from tentacle/:  python -m unittest tests.test_youtube_live_track_choice
"""
import unittest
from unittest import mock

AUDIO = {"format_id": "234", "vcodec": "none", "acodec": None, "tbr": 128,
         "protocol": "m3u8_native", "url": "https://x/a234.m3u8"}


def _v(height, codec="avc1.4D4020"):
    return {"format_id": f"v{height}", "height": height, "vcodec": codec,
            "acodec": "none", "protocol": "m3u8_native",
            "url": f"https://x/v{height}-{codec.split('.')[0]}.m3u8"}


def _pick(formats, max_height):
    from services.youtube import resolver
    with mock.patch.object(resolver.client, "extract",
                           return_value={"formats": formats}):
        return resolver.pick_tracks("v9LArDyyNxw", max_height)


class TestTheHeightCapIsNeverExceeded(unittest.TestCase):

    def test_the_smallest_available_is_used_when_nothing_fits(self):
        # A channel capped at 480p; the stream only offers 720p and up.
        formats = [AUDIO, _v(720), _v(1080), _v(2160)]
        video, _, _ = _pick(formats, 480)
        self.assertEqual(
            "https://x/v720-avc1.m3u8", video,
            "no rendition fits under the cap, so the SMALLEST should be used; "
            "the fallback sorts by -height and takes the tallest instead, which "
            "is the opposite of what a maximum means",
        )

    def test_a_cap_below_every_rendition_does_not_select_2160p(self):
        formats = [AUDIO, _v(1080), _v(2160)]
        video, _, _ = _pick(formats, 360)
        self.assertNotIn("v2160", video,
                         "a 360p cap selected the 2160p rendition")


class TestOnlyCodecsMpegTsCanCarryAreSelected(unittest.TestCase):
    """routers/youtube.py remuxes with `-c copy -f mpegts`.

    ffmpeg's mpegts muxer carries H.264 and HEVC; it has no stream type for VP9
    or AV1. The codec preference is a sort key, so when the ladder holds no
    H.264 rendition a VP9 one is selected and the remux cannot succeed.
    """

    def test_a_vp9_only_ladder_does_not_win_over_a_shorter_h264_one(self):
        formats = [AUDIO, _v(720, "avc1.4D4020"), _v(1080, "vp09.00.40.08"),
                   _v(2160, "av01.0.12M.08")]
        video, _, _ = _pick(formats, 2160)
        self.assertIn("avc1", video,
                      "a codec the mpegts muxer cannot carry was selected")

    def test_a_ladder_with_no_h264_at_all_is_not_offered_to_the_remux(self):
        from services.youtube.errors import YouTubeError
        formats = [AUDIO, _v(1080, "vp09.00.40.08"), _v(2160, "av01.0.12M.08")]
        try:
            video, _, _ = _pick(formats, 1080)
        except YouTubeError:
            return      # refusing outright is a fine answer
        self.fail(
            f"selected {video}: `-c copy -f mpegts` cannot carry this codec, so "
            f"ffmpeg exits and the tuner gets an empty stream with no message"
        )


class TestUpstreamBehaviourStillHolds(unittest.TestCase):
    """The parts 9aa3977 got right — kept so a fix does not undo them."""

    def test_tallest_within_the_cap_still_wins(self):
        formats = [AUDIO, _v(144), _v(720), _v(1080)]
        self.assertEqual("https://x/v720-avc1.m3u8", _pick(formats, 720)[0])
        self.assertEqual("https://x/v1080-avc1.m3u8", _pick(formats, 1080)[0])

    def test_h264_still_preferred_at_the_same_height(self):
        formats = [AUDIO, _v(1080, "vp09.00.40.08"), _v(1080, "avc1.4D402A")]
        self.assertIn("avc1", _pick(formats, 1080)[0])

    def test_audio_is_still_found_despite_acodec_being_none(self):
        formats = [AUDIO, _v(720)]
        self.assertEqual("https://x/a234.m3u8", _pick(formats, 720)[1])

    def test_a_muxed_rendition_needs_no_separate_audio(self):
        muxed = [{"format_id": "18", "height": 360, "vcodec": "avc1",
                  "acodec": "mp4a", "protocol": "m3u8_native",
                  "url": "https://x/muxed.m3u8"}]
        video, audio, _ = _pick(muxed, 1080)
        self.assertEqual("https://x/muxed.m3u8", video)
        self.assertIsNone(audio)


if __name__ == "__main__":
    unittest.main()
