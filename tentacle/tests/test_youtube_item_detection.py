"""#34: a "youtube" tag must not hide real films.

Jellyfin imports TMDB keywords as tags, and several real films carry a
"youtube" keyword ("Bo Burnham: Inside", "The Deep House", "The Sidemen
Story"). Detecting Tentacle's own YouTube videos by that tag therefore skipped
genuine library content — those films' Tentacle tags froze and they were left
out of native playlists.

The signal is the <uniqueid type="youtube"> that services/youtube/library.py
writes into every video's NFO, which Jellyfin exposes as ProviderIds["youtube"].

Run from tentacle/:  python -m unittest discover -s tests
"""
import unittest

from services.jellyfin import is_youtube_video


class TestIsYouTubeVideo(unittest.TestCase):
    def test_a_tentacle_youtube_video_is_detected(self):
        self.assertTrue(is_youtube_video({
            "Name": "Some upload",
            "Tags": ["youtube", "yt:leafs"],
            "ProviderIds": {"youtube": "dQw4w9WgXcQ"},
        }))

    def test_a_real_film_with_a_youtube_keyword_is_not_a_video(self):
        # The reported false positive.
        self.assertFalse(is_youtube_video({
            "Name": "Bo Burnham: Inside",
            "Tags": ["youtube", "comedy"],
            "ProviderIds": {"Tmdb": "777270", "Imdb": "tt14544192"},
        }))

    def test_an_untagged_film_without_a_tmdb_id_is_not_a_video(self):
        # An unmatched film, or a home video someone tagged "youtube": no TMDB
        # id, so the old "tag and no Tmdb" check called it a YouTube video.
        self.assertFalse(is_youtube_video({
            "Name": "Holiday footage",
            "Tags": ["youtube"],
            "ProviderIds": {},
        }))

    def test_provider_id_case_is_ignored(self):
        self.assertTrue(is_youtube_video({"ProviderIds": {"YouTube": "abc"}}))

    def test_missing_fields_are_safe(self):
        self.assertFalse(is_youtube_video({}))
        self.assertFalse(is_youtube_video({"Tags": None, "ProviderIds": None}))


if __name__ == "__main__":
    unittest.main()
