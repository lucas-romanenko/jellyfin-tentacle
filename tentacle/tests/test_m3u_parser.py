"""Tests for the M3U VOD classification helpers (episode parsing + container).

These back the M3U-provider VOD sync (M3UClient). Run from tentacle/:
    python -m unittest discover -s tests
"""
import unittest

from services.m3u_parser import episode_from_title, container_from_url


class TestEpisodeFromTitle(unittest.TestCase):
    def test_sxxexx_compact(self):
        self.assertEqual(episode_from_title("Breaking Bad S01E05"), ("Breaking Bad", 1, 5))

    def test_sxxexx_spaced(self):
        self.assertEqual(episode_from_title("Breaking Bad S1 E5"), ("Breaking Bad", 1, 5))

    def test_sxxexx_dotted(self):
        self.assertEqual(episode_from_title("The Office S03.E10"), ("The Office", 3, 10))

    def test_nxnn_fallback(self):
        self.assertEqual(episode_from_title("The Wire 1x05"), ("The Wire", 1, 5))

    def test_separators_and_episode_title(self):
        self.assertEqual(episode_from_title("Show Name - S02 E03 - Ep Title"), ("Show Name", 2, 3))

    def test_high_numbers(self):
        self.assertEqual(episode_from_title("Long Runner S12 E240"), ("Long Runner", 12, 240))

    def test_movie_returns_none(self):
        self.assertIsNone(episode_from_title("Inception (2010)"))
        self.assertIsNone(episode_from_title("The Matrix"))


class TestContainerFromUrl(unittest.TestCase):
    def test_extension_extracted(self):
        self.assertEqual(container_from_url("http://s/movie/u/p/123.mkv"), "mkv")
        self.assertEqual(container_from_url("http://s/series/u/p/9.ts"), "ts")

    def test_default_when_no_extension(self):
        self.assertEqual(container_from_url("http://s/movie/u/p/123"), "mp4")

    def test_query_string_ignored(self):
        self.assertEqual(container_from_url("http://s/x.ts?token=abc"), "ts")


if __name__ == "__main__":
    unittest.main()
