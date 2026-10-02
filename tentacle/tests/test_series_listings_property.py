"""A show's episode files over many nights, from one to three listings of it
(series ids) read in any order, with renumbering, re-listing under new ids,
added and removed episodes and listings that fail to load (#376).

After every night:
  - no episode file is removed or renamed (Jellyfin item ids and users'
    watched state hang on the path);
  - a file that plays an episode listed at its SxxEyy (by any listing) is not
    rewritten, so an unchanged catalogue rewrites nothing, whatever the order;
  - when every listing was read: each file plays an episode listed at its
    number, and the files are the same whatever order the listings came in;
  - when one could not be read: no existing file changes.

Seeds are printed on failure. No network.

Run from tentacle/:  python -m unittest tests.test_series_listings_property
"""
import random
import types
import unittest
from pathlib import Path
from unittest import mock

import services.sync as sync
from models.database import Provider
from tmp_dirs import temp_dir


class Catalogue:
    def __init__(self, rnd):
        self.rnd = rnd
        self.next_id = 1000
        self.listings = {}       # series id -> {(season, ep): [episode ids]}
        for _ in range(rnd.randint(1, 3)):
            self.add_listing()

    def new_id(self):
        self.next_id += 1
        return self.next_id

    def add_listing(self):
        sid = self.rnd.choice([s for s in range(1, 60) if s not in self.listings])
        eps = {}
        for season in (1, 2):
            for n in range(1, 7):
                if self.rnd.random() < 0.7:
                    eps[(season, n)] = [self.new_id()] + ([self.new_id()] if self.rnd.random() < 0.08 else [])
        self.listings[sid] = eps

    def mutate(self):
        r = self.rnd.random()
        sid = self.rnd.choice(list(self.listings))
        eps = self.listings[sid]
        if r < 0.45 or not eps:
            return "unchanged"
        if r < 0.55:   # the provider inserts a missing episode and shifts the season's numbers up
            season = self.rnd.choice([1, 2])
            shifted = {(s, n + 1 if s == season else n): ids for (s, n), ids in eps.items()}
            shifted[(season, 1)] = [self.new_id()]
            self.listings[sid] = shifted
            return "renumber"
        if r < 0.65:   # re-listed under a new id (#263)
            key = self.rnd.choice(list(eps))
            eps[key] = [self.new_id()]
            return "relist"
        if r < 0.72:
            del eps[self.rnd.choice(list(eps))]
            return "remove"
        if r < 0.80:
            eps[(self.rnd.choice([1, 2, 3]), self.rnd.randint(1, 9))] = [self.new_id()]
            return "add"
        if r < 0.88 and len(self.listings) < 3:
            self.add_listing()
            return "new listing"
        if r < 0.93 and len(self.listings) > 1:
            del self.listings[sid]
            return "listing gone"
        if r < 0.96 and len(self.listings) > 1:
            self.listings[sid] = {}
            return "listing emptied"
        return "unchanged"

    def episodes(self, sid):
        out = {}
        for (season, n), ids in sorted(self.listings[sid].items()):
            out.setdefault(str(season), []).extend(
                {"id": i, "episode_num": n, "container_extension": "mkv"} for i in ids)
        return out


def files(show):
    return {f.relative_to(show).as_posix(): f.read_text() for f in sorted(show.rglob("*.strm"))}


def plays(text):
    return int(text.rsplit("/", 1)[1].split(".")[0])


def number(name):
    code = name.rsplit(" S", 1)[1].split(".")[0]
    return int(code[:2]), int(code[3:])


class SeriesListingsProperty(unittest.TestCase):
    NIGHTS = 6

    def setUp(self):
        self.tmp = Path(temp_dir(self))
        self.client = sync.XtreamClient(Provider(id=1, name="P", server_url="http://provider",
                                                 username="u", password="p"))

    def night(self, show, parts):
        record = types.SimpleNamespace(tmdb_id=1, title="Dark", year="2017", overview="", genres=[],
                                       rating=None, status=None, poster_path=None, backdrop_path=None,
                                       tags=[], date_updated=None)
        rewritten = []
        real = sync._write_strm

        def spy(f, url):
            if Path(f).exists():
                rewritten.append(Path(f).name)
            real(f, url)
        with mock.patch.object(sync, "_write_strm", spy):
            sync._write_backfill(self.client, (record, show), parts)
        return rewritten

    def restore(self, show, state):
        for f in show.rglob("*.strm"):
            f.unlink()
        for name, text in state.items():
            (show / name).parent.mkdir(parents=True, exist_ok=True)
            (show / name).write_text(text)

    def one(self, seed):
        rnd = random.Random(seed)
        cat = Catalogue(rnd)
        show, twin = self.tmp / f"{seed}" / "Dark (2017)", self.tmp / f"{seed}-twin" / "Dark (2017)"
        show.mkdir(parents=True)
        twin.mkdir(parents=True)
        what, prev_failed = "first night", True
        for night in range(self.NIGHTS):
            failed = {sid for sid in cat.listings if night and rnd.random() < 0.1}
            parts = [(sid, None if sid in failed else sync._compact_episodes(cat.episodes(sid))) for sid in cat.listings]
            rnd.shuffle(parts)
            before = files(show)
            rewritten = self.night(show, parts)
            after = files(show)
            ctx = f"seed {seed} night {night} ({what}, failed={sorted(failed)}): "
            # no file removed or renamed
            self.assertLessEqual(set(before), set(after), ctx + "a file went away")
            listed = {}
            for sid in cat.listings:
                if sid not in failed:
                    for key, ids in cat.listings[sid].items():
                        listed.setdefault(key, set()).update(ids)
            for name, text in before.items():
                if plays(text) in listed.get(number(name), ()):
                    self.assertEqual(text, after[name], ctx + f"{name} was flipped although its episode is listed")
                if failed:
                    self.assertEqual(text, after[name], ctx + f"{name} changed while a listing was unread")
            if what == "unchanged" and not failed and not prev_failed:
                self.assertEqual([], rewritten, ctx + "an unchanged catalogue rewrote files")
            if not failed:
                for name, text in after.items():
                    if number(name) in listed:
                        self.assertIn(plays(text), listed[number(name)], ctx + f"{name} plays a stale episode")
                have = {number(name) for name in after}
                self.assertEqual(set(), set(listed) - have, ctx + "a listed episode has no file")
                # the same night, read in another order, from the same files
                self.restore(twin, before)
                self.night(twin, list(reversed(parts)))
                self.assertEqual(after, files(twin), ctx + "the result depends on the order listings were read")
            what, prev_failed = cat.mutate(), bool(failed)

    def test_1000_seeds(self):
        for seed in range(1000):
            self.one(seed)


class FetchedOnce(unittest.TestCase):
    """The same series id in two categories is read once a night."""

    def test_same_listing_twice_is_fetched_once(self):
        calls = []

        class Client:
            def get_series_info(self, sid):
                calls.append(sid)
                return {"episodes": {"1": [{"id": 1, "episode_num": 1}]}}
        noted = {}
        target = (types.SimpleNamespace(), Path("/nowhere"))
        with mock.patch.object(sync, "_backfill_target", lambda *a: target):
            for _ in range(2):
                sync._backfill_series_episodes(Client(), {"series_id": 11}, 5, None, None, noted)
        self.assertEqual([11], calls)
        self.assertEqual([11], [sid for sid, _ in noted[5]])


if __name__ == "__main__":
    unittest.main()
