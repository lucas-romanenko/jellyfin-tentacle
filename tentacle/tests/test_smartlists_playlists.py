"""Tests for the SmartList playlist diff.

Regression cover for the series playlist that drained itself: Jellyfin expands
a Series into its episodes inside a playlist, so the stored entries are episode
ids while the desired list holds series ids. Diffing the two id spaces directly
made every desired series look new and every stored episode look stale, and the
removal pass deleted the entries the add pass had just created.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

from services.smartlists import _update_episode_playlist


class FakeJellyfin:
    def __init__(self, add_ok=True, remove_ok=True):
        self.added = []
        self.removed = []
        self._add_ok = add_ok
        self._remove_ok = remove_ok

    def add_to_playlist(self, playlist_id, ids):
        self.added.append(list(ids))
        return self._add_ok

    def remove_from_playlist(self, playlist_id, entry_ids):
        self.removed.append(list(entry_ids))
        return self._remove_ok


def episodes_of(series_ids, per_series=3):
    """Playlist entries as Jellyfin stores them for a series playlist."""
    return [
        {"Id": f"{sid}-ep{n}", "Type": "Episode", "SeriesId": sid,
         "PlaylistItemId": f"pi-{sid}-{n}"}
        for sid in series_ids for n in range(per_series)
    ]


def new_stats():
    return {"updated": 0, "processed": 0, "changed": 0, "errors": 0, "item_counts": {}}


class TestEpisodePlaylistDiff(unittest.TestCase):
    def test_in_sync_playlist_is_left_alone(self):
        jf, stats = FakeJellyfin(), new_stats()
        _update_episode_playlist(jf, "P", "TV", ["s1", "s2", "s3"],
                                 episodes_of(["s1", "s2", "s3"]), stats)
        self.assertEqual(jf.added, [])
        self.assertEqual(jf.removed, [])
        self.assertEqual(stats["item_counts"]["TV"], 3)

    def test_one_series_without_indexed_episodes_does_not_drain_the_playlist(self):
        # The reported failure: a single desired series has no episodes in the
        # playlist yet (still scanning). The old code added all three series and
        # removed every episode id it had recorded — including the ones the adds
        # had just recreated.
        jf, stats = FakeJellyfin(), new_stats()
        _update_episode_playlist(jf, "P", "TV", ["s1", "s2", "s3"],
                                 episodes_of(["s1", "s2"]), stats)
        self.assertEqual(jf.added, [["s3"]])
        self.assertEqual(jf.removed, [])

    def test_dropped_series_removes_only_its_own_episodes(self):
        jf, stats = FakeJellyfin(), new_stats()
        _update_episode_playlist(jf, "P", "TV", ["s1", "s3"],
                                 episodes_of(["s1", "s2", "s3"]), stats)
        self.assertEqual(jf.added, [])
        self.assertEqual(jf.removed, [["pi-s2-0", "pi-s2-1", "pi-s2-2"]])

    def test_reordering_rebuilds_in_series_space(self):
        jf, stats = FakeJellyfin(), new_stats()
        current = episodes_of(["s1", "s2"])
        _update_episode_playlist(jf, "P", "TV", ["s2", "s1"], current, stats)
        self.assertEqual(jf.removed, [[e["PlaylistItemId"] for e in current]])
        self.assertEqual(jf.added, [["s2", "s1"]])

    def test_mixed_movie_and_series_playlist(self):
        jf, stats = FakeJellyfin(), new_stats()
        current = episodes_of(["s1"]) + [
            {"Id": "mv1", "Type": "Movie", "PlaylistItemId": "pi-mv1"}]
        _update_episode_playlist(jf, "P", "Mixed", ["s1", "mv1", "mv2"], current, stats)
        self.assertEqual(jf.added, [["mv2"]])
        self.assertEqual(jf.removed, [])

    def test_failed_add_aborts_before_removing_anything(self):
        jf, stats = FakeJellyfin(add_ok=False), new_stats()
        _update_episode_playlist(jf, "P", "TV", ["s1", "s3"], episodes_of(["s1", "s2"]), stats)
        self.assertEqual(jf.removed, [])
        self.assertEqual(stats["errors"], 1)


if __name__ == "__main__":
    unittest.main()


class TestChangeReporting(unittest.TestCase):
    """The 15-minute native refresh notifies clients off `changed`, not `updated`.

    Every visited playlist counts as "updated", including the ones that needed
    no work, so keying the notification off it pushed a plugin + WebSocket
    update every run whether or not anything had happened.
    """

    def test_no_change_does_not_count_as_changed(self):
        jf, stats = FakeJellyfin(), new_stats()
        _update_episode_playlist(jf, "P", "TV", ["s1", "s2"], episodes_of(["s1", "s2"]), stats)
        self.assertEqual(stats.get("changed", 0), 0)
        self.assertEqual(stats["updated"], 1)

    def test_real_mutations_count_as_changed(self):
        jf, stats = FakeJellyfin(), new_stats()
        _update_episode_playlist(jf, "P", "TV", ["s1", "s2"], episodes_of(["s1"]), stats)
        self.assertEqual(stats.get("changed", 0), 1)

    def test_reorder_counts_as_changed(self):
        jf, stats = FakeJellyfin(), new_stats()
        _update_episode_playlist(jf, "P", "TV", ["s2", "s1"], episodes_of(["s1", "s2"]), stats)
        self.assertEqual(stats.get("changed", 0), 1)


class TestRowShape(unittest.TestCase):
    """Rows say how their cards are drawn: poster (2:3) or wide (16:9).

    Some content has no portrait artwork at all — a YouTube thumbnail forced
    into a poster slot is cropped to a strip of its middle — so the shape is per
    row, and YouTube rows start wide.
    """

    def test_a_youtube_playlist_is_recognised_by_its_tag(self):
        # By tag, not by name: the user can rename a playlist.
        from services.smartlists import _get_smartlists_with_playlist_ids
        import services.smartlists as sm

        configs = {
            "TraderTV Live": (None, {
                "UserPlaylists": [{"JellyfinPlaylistId": "yt-pl"}],
                "ExpressionSets": [{"Expressions": [
                    {"MemberName": "Tags", "Operator": "Contains",
                     "TargetValue": "yt:tradertv-live"}]}],
            }),
            "Netflix Movies": (None, {
                "UserPlaylists": [{"JellyfinPlaylistId": "nf-pl"}],
                "ExpressionSets": [{"Expressions": [
                    {"MemberName": "Tags", "Operator": "Contains",
                     "TargetValue": "Netflix Movies"}]}],
            }),
        }
        real = sm._scan_existing
        sm._scan_existing = lambda path: configs
        try:
            out = {r["name"]: r["is_youtube"]
                   for r in _get_smartlists_with_playlist_ids(_FakeDb(), user_id=None)}
        finally:
            sm._scan_existing = real
        self.assertEqual(out, {"TraderTV Live": True, "Netflix Movies": False})

    def test_the_endpoint_refuses_a_shape_it_does_not_know(self):
        from fastapi import HTTPException
        from routers.smartlists import ROW_SHAPES, RowShapeRequest, set_row_shape
        self.assertEqual(ROW_SHAPES, ("poster", "wide"))
        with self.assertRaises(HTTPException) as cm:
            set_row_shape(RowShapeRequest(row_key="playlist:x", shape="square"),
                          db=None, user=None)
        self.assertEqual(cm.exception.status_code, 400)


class _FakeDb:
    """Stands in for a Session — _get_smartlists_with_playlist_ids only reads a setting."""

    def query(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def first(self):
        return None
