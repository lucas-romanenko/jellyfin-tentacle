"""Provider Migrate, property test: random small libraries and listings, checked
against the invariants the fix keeps.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Each seed builds a world: films of the old provider A (namesakes, missing or
absent .strm files), films B already has, series of A, B's VOD list (films in
categories B syncs or not, under another year, listed twice, category ids as
int or str, on a stream an admin blocked or re-matched), B syncing no movie
category at all, a .strm rewrite that empties the file and then fails, a dry
run. Then Migrate A -> B, and B's next two syncs (its prune).
  M1  Migrate deletes nothing: every row and every file is still there
  M2  a film changes provider only to B, only if B lists it under its title and
      year in a category B syncs, on a stream B's sync uses (not blocked or
      re-matched), only when no other film has that name
  M3  a film that moved and has a .strm plays B; one that stayed is untouched
  M4  every listed, unambiguous film whose rewrite worked does move (liveness)
  M5  series, B's own films and (dry run, refusal) everything else: unchanged;
      Migrate refuses exactly when B syncs no movie category
  M6  B's next two syncs delete none of the films that were A's

TENTACLE_FUZZ_SEEDS (default 200) and TENTACLE_FUZZ_FIRST (default 0) pick the
seeds; the PR was checked with 5,000. Self-contained: no network.
"""
import os
import random
import shutil
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import (BlockedStream, Duplicate, MatchOverride, Movie, Series,
                             Provider, ProviderCategory)
import services.migration as migration
import services.sync as sync
from tmp_dirs import temp_dir

TITLES = ["Heat", "Ronin", "Home", "Brothers", "Crash", "Nomad", "The Guest", "Up"]
YEARS = ["1995", "1998", "2015"]


def _seeds():
    first = int(os.environ.get("TENTACLE_FUZZ_FIRST", "0"))
    return range(first, first + int(os.environ.get("TENTACLE_FUZZ_SEEDS", "200")))


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class MigrateProperty(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(engine.dispose)
        self.addCleanup(self.db.close)

    def _world(self, seed):
        rnd = random.Random(seed)
        db = self.db
        for model in (BlockedStream, MatchOverride, Duplicate, Movie, Series, ProviderCategory, Provider):
            db.query(model).delete()
        db.commit()
        root = self.tmp / f"s{seed}"
        a = Provider(name="A", server_url="http://a.example", username="ua", password="pa", active=True)
        b = Provider(name="B", server_url="http://b.example", username="ub", password="pb", active=True)
        db.add_all([a, b])
        db.commit()
        synced = set() if rnd.random() < 0.1 else {c for c in ("20", "21") if rnd.random() < 0.7} or {"20"}
        for cid in ("20", "21", "99"):
            db.add(ProviderCategory(provider_id=b.id, category_id=cid, category_name=cid,
                                    type="movie", whitelisted=cid in synced))
        db.add(ProviderCategory(provider_id=b.id, category_id="30", category_name="30",
                                type="series", whitelisted=True))

        listing, sid = [], 500
        tmdb = iter(range(1000, 2000))

        def strm_for(owner, title, year, url):
            r = rnd.random()
            if r < 0.05:
                return None                      # a row without a .strm path
            folder = root / "movies" / f"{title} ({year}) {next(tmdb)}"
            strm = folder / f"{title} ({year}).strm"
            if r < 0.12:
                return str(strm)                 # its .strm is missing on disk
            folder.mkdir(parents=True)
            strm.write_text(url, encoding="utf-8")
            (folder / f"{title} ({year}).nfo").write_text("<movie/>", encoding="utf-8")
            return str(strm)

        def list_on_b(title, year):
            nonlocal sid
            for _ in range(rnd.choice((1, 1, 1, 2))):    # sometimes listed twice
                sid += 1
                cid = rnd.choice(("20", "21", "99"))
                listing.append({"stream_id": sid, "name": rnd.choice(("", "EN - ")) + f"{title} ({year})",
                                "container_extension": rnd.choice(("mp4", "mkv")),
                                "category_id": int(cid) if rnd.random() < 0.3 else cid})

        for i in range(rnd.randint(0, 8)):
            title, year = rnd.choice(TITLES), rnd.choice(YEARS)
            db.add(Movie(tmdb_id=next(tmdb), title=title, year=year, source=f"provider_{a.id}",
                         provider_id=a.id,
                         strm_path=strm_for(a, title, year, f"http://a.example/movie/ua/pa/{100 + i}.mp4")))
            r = rnd.random()
            if r < 0.6:
                list_on_b(title, year)
            elif r < 0.7:
                list_on_b(title, rnd.choice([y for y in YEARS if y != year]))
        for i in range(rnd.randint(0, 3)):           # films B already has
            title, year = rnd.choice(TITLES), rnd.choice(YEARS)
            db.add(Movie(tmdb_id=next(tmdb), title=title, year=year, source=f"provider_{b.id}",
                         provider_id=b.id,
                         strm_path=strm_for(b, title, year, f"http://b.example/movie/ub/pb/{900 + i}.mp4")))
        for i in range(rnd.randint(0, 3)):           # extra B streams
            list_on_b(rnd.choice(TITLES), rnd.choice(YEARS))
        for i in range(rnd.randint(0, 2)):
            show = root / "shows" / f"Show {i}"
            (show / "Season 01").mkdir(parents=True)
            (show / "Season 01" / "E1.strm").write_text(f"http://a.example/series/ua/pa/{i}.mp4",
                                                        encoding="utf-8")
            db.add(Series(tmdb_id=next(tmdb), title=f"Show {i}", year="2002", source=f"provider_{a.id}",
                          provider_id=a.id, strm_path=str(show)))
        db.commit()
        failing = {m.strm_path for m in db.query(Movie).filter(Movie.provider_id == a.id)
                   if m.strm_path and rnd.random() < 0.1}
        dry_run = rnd.random() < 0.15
        # "Wrong movie" fixes: B's blocked or re-matched streams (B's sync skips
        # them, or files them under another film), and A's, which don't matter.
        tmdb_ids = [t for (t,) in db.query(Movie.tmdb_id).all()] + [7777]
        fixed = set()
        for s in listing:
            r, key = rnd.random(), str(s["stream_id"])
            if r < 0.08:
                db.add(BlockedStream(provider_id=b.id, media_type="movie", stream_key=key))
                fixed.add(key)
            elif r < 0.14:
                db.add(MatchOverride(provider_id=b.id, media_type="movie", stream_key=key,
                                     tmdb_id=rnd.choice(tmdb_ids)))
                fixed.add(key)
            elif r < 0.2:
                model = rnd.choice((BlockedStream, MatchOverride))
                db.add(model(provider_id=a.id, media_type="movie", stream_key=key, tmdb_id=rnd.choice(tmdb_ids)))
        db.commit()
        return a.id, b.id, root, listing, synced, failing, dry_run, fixed

    def _snapshot(self, root):
        rows = {(Model.__name__, r.tmdb_id): (r.provider_id, r.source, r.strm_path)
                for Model in (Movie, Series) for r in self.db.query(Model).all()}
        files = {p: p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file()} if root.exists() else {}
        providers = {p.id: p.active for p in self.db.query(Provider).all()}
        return rows, files, providers

    def _check(self, seed):
        a_id, b_id, root, listing, synced, failing, dry_run, fixed = self._world(seed)
        rows0, files0, prov0 = self._snapshot(root)
        real_write = Path.write_text
        failing_dirs = {str(Path(f).parent) for f in failing}

        def write_text(path, *args, **kwargs):
            if str(Path(path).parent) in failing_dirs:   # disk full: emptied (O_TRUNC), then fails
                with open(path, "w", encoding="utf-8"):
                    pass
                raise OSError("disk full")
            return real_write(path, *args, **kwargs)

        with mock.patch("requests.Session.get", return_value=_Resp(listing)), \
                mock.patch.object(Path, "write_text", write_text), \
                mock.patch.object(migration, "logger"):   # the injected failures are logged
            stats = migration.migrate_provider(a_id, b_id, self.db, dry_run=dry_run)
        self.db.expire_all()
        rows1, files1, prov1 = self._snapshot(root)
        where = f"seed {seed}: {stats}"

        # M1
        self.assertEqual(set(rows0), set(rows1), f"{where}: a row was deleted")
        self.assertEqual(set(files0), set(files1), f"{where}: a file was deleted")

        # Which films B's sync lists, by title key -> stream URLs: in a category
        # it syncs, on a stream no admin blocked or re-matched.
        from services.cleaner import clean_title
        listed = {}
        for s in listing:
            if str(s["category_id"]) in synced and str(s["stream_id"]) not in fixed:
                t, y = clean_title(s["name"])
                if t:
                    listed.setdefault(f"{t.lower()}_{y}", set()).add(
                        f"http://b.example/movie/ub/pb/{s['stream_id']}.{s['container_extension']}")
        a_films = [r for r in self.db.query(Movie).all() if rows0[("Movie", r.tmdb_id)][0] == a_id]
        keys = [f"{r.title.lower()}_{r.year}" for r in a_films]
        names = [f"{r.title.lower()}_{r.year}" for r in self.db.query(Movie).all()]
        refused = "error" in stats
        self.assertEqual(refused, not synced, f"{where}: refused exactly when B syncs no movie category")

        for row, key in zip(a_films, keys):
            before = rows0[("Movie", row.tmdb_id)]
            strm = Path(row.strm_path) if row.strm_path else None
            had_file = strm is not None and strm in files0
            moved = row.provider_id != a_id
            eligible = (not dry_run and not refused and key in listed and names.count(key) == 1
                        and not (had_file and row.strm_path in failing))
            if moved:
                # M2
                self.assertTrue(eligible, f"{where}: tmdb:{row.tmdb_id} {key} moved to "
                                          f"{row.provider_id} though B doesn't sync it or it is ambiguous")
                self.assertEqual((row.provider_id, row.source), (b_id, f"provider_{b_id}"), where)
                # M3
                if had_file:
                    self.assertIn(files1[strm], listed[key], f"{where}: moved, but plays {files1[strm]}")
            else:
                # M4
                self.assertFalse(eligible, f"{where}: tmdb:{row.tmdb_id} {key} is listed but didn't move")
                # M3
                self.assertEqual((row.provider_id, row.source, row.strm_path), before, where)
                if had_file:
                    self.assertEqual(files1[strm], files0[strm], f"{where}: a film that stayed was rewritten")

        # M5
        for key, before in rows0.items():
            if key[0] == "Series" or before[0] == b_id or dry_run or refused:
                self.assertEqual(rows1[key], before, f"{where}: {key} changed")
        changed = {p for p in files1 if files1[p] != files0[p]}
        moved_files = {Path(r.strm_path) for r in a_films if r.provider_id == b_id and r.strm_path}
        self.assertLessEqual(changed, moved_files, f"{where}: rewrote files of rows that didn't move")
        if dry_run or refused:
            self.assertEqual(prov0, prov1, where)
        else:
            self.assertEqual(prov1, {a_id: False, b_id: True}, where)

        # M6: B's next two syncs. They see B's own films, and for each name B
        # lists in a category it syncs, one film (TMDB's answer for that name).
        b = self.db.get(Provider, b_id)
        seen = {t for (kind, t), v in rows0.items() if kind == "Movie" and v[0] == b_id}
        for key in listed:
            named = sorted(r.tmdb_id for r in self.db.query(Movie).all()
                           if f"{r.title.lower()}_{r.year}" == key)
            seen.update(named[:1])
        if seen:   # the sync prunes only after a run that saw something
            for _ in range(2):
                sync._prune_removed_content(self.db, b, "movie", seen)
                self.db.expire_all()
        left = {r.tmdb_id for r in self.db.query(Movie).all()}
        lost = {t for (kind, t), v in rows0.items() if kind == "Movie" and v[0] == a_id} - left
        self.assertFalse(lost, f"{where}: B's prune deleted films that were A's: {sorted(lost)}")

        shutil.rmtree(root, ignore_errors=True)

    def test_migrate_invariants(self):
        for seed in _seeds():
            with self.subTest(seed=seed):
                self._check(seed)


if __name__ == "__main__":
    unittest.main()
