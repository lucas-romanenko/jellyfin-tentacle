"""The YouTube source added in 0e1805f must stay out of the VOD removal paths.

YouTube videos live in their own tables (youtube_videos) and media root
(/media/youtube), and appear in Jellyfin as Movie items without a TMDB id. None
of the nightly removal paths may touch them: the provider prune, the VOD sweep
and the orphan download sweep. These pass at 0e1805f (regression cover).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

from models.database import Movie, YouTubeChannel, YouTubeVideo, Setting
import services.jellyfin as jellyfin
from nightly_harness import NightlyHarness


class TestYouTubeIsolation(NightlyHarness):

    def _add_videos(self, n=5):
        yt_root = self.vod.parent / "youtube"
        ch = YouTubeChannel(input_url="https://www.youtube.com/@x", title="Chan", slug="chan")
        self.db.add(ch)
        self.db.commit()
        paths = []
        for i in range(n):
            folder = yt_root / "Chan" / f"2026-09-0{i + 1} Frozen [vid{i:08d}]"
            folder.mkdir(parents=True)
            strm = folder / f"{folder.name}.strm"
            strm.write_text("http://tentacle/api/youtube/v/x/master.m3u8")
            (folder / "movie.nfo").write_text("<movie/>")
            self.db.add(YouTubeVideo(channel_fk=ch.id, video_id=f"vid{i:08d}", title="Frozen",
                                     folder_path=str(folder), strm_path=str(strm)))
            paths.append(strm)
        self.db.commit()
        return paths

    def test_vod_prune_and_sweep_leave_youtube_alone(self):
        """A provider title named like a video is dropped and pruned, and the VOD
        root even loses files; YouTube rows and files are unaffected."""
        paths = self._add_videos()
        self.add_category("1")
        self.catalogue_movies("1", ["Frozen"] + [f"Movie {i}" for i in range(99)])
        self.night()
        self.client.movies["1"] = [(t, s) for t, s in self.client.movies["1"] if t != "Frozen"]
        for _ in range(3):
            self.night()
        self.assertEqual(self.db.query(YouTubeVideo).count(), 5)
        for p in paths:
            self.assertTrue(p.exists(), f"{p.name} removed by a VOD removal path")

    def test_orphan_download_sweep_ignores_youtube_items(self):
        """Jellyfin lists YouTube videos (no Tmdb provider id) next to real movies."""
        self._add_videos(2)
        self.db.add(Setting(key="jellyfin_url", value="http://jf"))
        self.db.add(Setting(key="jellyfin_api_key", value="k"))
        self.db.add(Movie(tmdb_id=77, title="Downloaded", source="radarr"))
        self.db.commit()
        items = {"Movie": [{"ProviderIds": {"Tmdb": "77"}},
                           {"ProviderIds": {}, "Tags": ["youtube"]}],
                 "Series": []}
        saved = jellyfin.JellyfinService._get
        jellyfin.JellyfinService._get = lambda s, path, params=None: {
            "Items": items[params["IncludeItemTypes"]] if int(params["StartIndex"]) == 0 else [],
            "TotalRecordCount": len(items[params["IncludeItemTypes"]])}
        self.addCleanup(setattr, jellyfin.JellyfinService, "_get", saved)
        self.assertEqual(jellyfin.sweep_orphaned_downloads(self.db), 0)
        self.assertEqual(self.db.query(YouTubeVideo).count(), 2)


if __name__ == "__main__":
    unittest.main()
