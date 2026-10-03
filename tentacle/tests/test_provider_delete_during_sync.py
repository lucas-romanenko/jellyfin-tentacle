"""Deleting a provider while it syncs, or syncing it while it is being deleted.

A sync writes a category's .strm/.nfo files first and commits their rows once,
at the end of the category (services/sync.py). delete_provider removes the
files of the rows it can see, then the rows. While a sync of the provider was
writing a category, the delete could not see that category's new rows: it
answered "deleted", the sync's category commit then failed ("UPDATE on
provider_categories matched 0 rows") and rolled the rows back, and the files
stayed in the VOD folders with no row and no provider. Nothing removed them:
the prune and the VOD sweep work from rows, and a second delete answers 404.
Jellyfin went on listing the deleted provider's titles. The other way round
did the same: a sync started ("Sync now", the nightly) while the delete was
removing files restored or added files whose rows the delete then removed.

Now the two never overlap. The delete is refused (409) while the provider's
sync slot (routers.sync._running_syncs, which "Sync now" and the nightly check
and set) is held, and it holds that slot while it runs.

The sessions are production's: WAL + busy_timeout, autoflush off
(models.database); the sync and the delete each have their own. Only the
provider, TMDB and /media/vod are faked (nightly_harness).

TENTACLE_DELETE_PROVIDER_SEEDS (default 60) and
TENTACLE_DELETE_PROVIDER_FIRST_SEED (default 0) pick the interleaving seeds.
Before the fix 13 of the default 60 (180 of 1,200) left files with no row.
"""
import os
import random
import shutil
import threading
import unittest
from datetime import datetime
from pathlib import Path as _RealPath
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import models.database as mdb  # noqa: E402
from models.database import (  # noqa: E402
    CategorySnapshot, Movie, Provider, ProviderCategory, Series, SyncRun,
)
import routers.providers as providers  # noqa: E402
import routers.sync as rs  # noqa: E402
import services.sync as sync  # noqa: E402
from nightly_harness import FakeClient, FakeTMDB, NightlyHarness  # noqa: E402
from tmp_dirs import temp_dir  # noqa: E402


def _production_sessions(url):
    """A sessionmaker opened the way models.database opens the app's DB."""
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


class _ProdSessions(NightlyHarness):
    def setUp(self):
        super().setUp()
        url = self.db.bind.url
        self.db.close()
        self.db.bind.dispose()
        self.Session = _production_sessions(url)
        self.db = self.Session()             # the sync's session (routers.sync, main.py)
        self.provider = self.db.query(Provider).one()
        self.pid = self.provider.id
        # delete_provider's playlist rebuild talks to Jellyfin: not under test.
        for name in ("sync_smartlists", "refresh_smartlist_playlists", "write_home_config"):
            p = mock.patch(f"services.smartlists.{name}", lambda *a, **k: None)
            p.start()
            self.addCleanup(p.stop)
        for slot in (rs._running_syncs, rs._after_sync):
            slot.pop(self.pid, None)
            self.addCleanup(slot.pop, self.pid, None)

    # -- the two sides -----------------------------------------------------
    def sync_as_the_callers_do(self, **kw):
        """A sync holding the provider's slot, as trigger_sync + its thread
        and the nightly hold it."""
        with rs._sync_lock:
            self.assertNotIn(self.pid, rs._running_syncs)
            rs._running_syncs[self.pid] = True
        try:
            return sync.sync_provider(self.provider, "full", self.db, **kw)
        finally:
            rs._running_syncs.pop(self.pid, None)

    def try_delete(self):
        """Settings -> Providers -> Delete, on the request's own session."""
        s = self.Session()
        try:
            return "deleted", providers.delete_provider(self.pid, db=s)
        except HTTPException as e:
            return e.status_code, e.detail
        finally:
            s.close()

    # -- what is on disk and in the DB -------------------------------------
    def vod_files(self):
        return sorted(str(p.relative_to(self.vod)) for p in self.vod.rglob("*")
                      if p.is_file() and p.suffix in (".strm", ".nfo"))

    def orphans(self):
        """.strm/.nfo files in the VOD folders that no row accounts for."""
        s = self.Session()
        try:
            owned = {p for m in s.query(Movie).all() for p in (m.strm_path, m.nfo_path) if p}
            shows = [_RealPath(r.strm_path) for r in s.query(Series).all() if r.strm_path]
        finally:
            s.close()
        return [f for f in self.vod_files()
                if str(self.vod / f) not in owned
                and not any(sd in (self.vod / f).parents for sd in shows)]

    def rows(self):
        s = self.Session()
        try:
            return {"providers": s.query(Provider).count(), "categories": s.query(ProviderCategory).count(),
                    "snapshots": s.query(CategorySnapshot).count(), "runs": s.query(SyncRun).count(),
                    "movies": s.query(Movie).count(), "series": s.query(Series).count()}
        finally:
            s.close()

    def assert_provider_gone(self):
        self.assertEqual([], self.vod_files())
        self.assertEqual({"providers": 0, "categories": 0, "snapshots": 0, "runs": 0,
                          "movies": 0, "series": 0}, self.rows())
        self.assertNotIn(self.pid, rs._running_syncs, "the delete must give the slot back")


class DeleteDuringSync(_ProdSessions):
    def _delete_at(self, attempt):
        before = (self.vod_files(), self.rows())
        attempt["answer"] = self.try_delete()
        attempt["unchanged"] = (self.vod_files(), self.rows()) == before

    def _assert_refused_then_clean(self, attempt, run):
        left = self.orphans()
        self.assertEqual([], left, "a delete during the sync left these files with no row and no "
                                   f"provider; nothing ever removes them: {left} (delete answered {attempt})")
        self.assertEqual(409, attempt["answer"][0], attempt)
        self.assertIn("cancel it (or let it finish)", attempt["answer"][1])
        self.assertTrue(attempt["unchanged"], "a refused delete must change nothing")
        self.assertEqual("completed", run.status, run.error_message)
        # Once the sync is over the delete goes through and removes all it wrote.
        self.assertEqual("deleted", self.try_delete()[0])
        self.assert_provider_gone()

    def test_delete_while_the_sync_writes_a_movie_category(self):
        self.add_category("1")
        self.add_category("2")
        self.catalogue_movies("1", ["Heat", "Ronin", "Collateral"], first_tmdb=1000)
        self.catalogue_movies("2", ["Alien"], first_tmdb=2000)
        attempt = {}

        def progress(phase, category, stats, item_title=None, item_pos=None, item_total=None):
            # The admin presses Delete while the dashboard shows "CAT 1 — 2/3: Ronin"
            if phase == "movies" and category == "CAT 1" and item_pos == 2 and not attempt:
                self._delete_at(attempt)

        run = self.sync_as_the_callers_do(progress_callback=progress)
        self._assert_refused_then_clean(attempt, run)

    def test_delete_while_the_sync_writes_a_series_category(self):
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", ["Lost", "Fringe", "Alias"], first_tmdb=5000)
        attempt = {}
        real_info = self.client.get_series_info

        def series_info(series_id):
            # One provider call per new show: the second show of the category
            if series_id == 5001 and not attempt:
                self._delete_at(attempt)
            return real_info(series_id)

        self.client.get_series_info = series_info
        run = self.sync_as_the_callers_do()
        self._assert_refused_then_clean(attempt, run)

    def test_the_last_sync_still_updating_jellyfin_refuses_the_delete(self):
        self.add_category("1")
        self.catalogue_movies("1", ["Heat"], first_tmdb=1000)
        self.sync_as_the_callers_do()
        rs._running_syncs[self.pid] = True       # a manual sync's Jellyfin pipeline
        rs._after_sync[self.pid] = datetime.utcnow()
        before = (self.vod_files(), self.rows())
        status, detail = self.try_delete()
        self.assertEqual(409, status)
        self.assertIn("still updating Jellyfin", detail)
        self.assertEqual(before, (self.vod_files(), self.rows()))
        rs._running_syncs.pop(self.pid)
        rs._after_sync.pop(self.pid)
        self.assertEqual("deleted", self.try_delete()[0])
        self.assert_provider_gone()

    def test_a_run_left_running_by_a_restart_does_not_block_the_delete(self):
        """No sync thread holds the slot after a restart: the stale row is no sync."""
        self.add_category("1")
        self.catalogue_movies("1", ["Heat"], first_tmdb=1000)
        self.sync_as_the_callers_do()
        self.db.add(SyncRun(provider_id=self.pid, status="running", sync_type="full",
                            started_at=datetime.utcnow()))
        self.db.commit()
        self.assertEqual("deleted", self.try_delete()[0])
        self.assert_provider_gone()


class SyncDuringDelete(_ProdSessions):
    """A sync that starts while the delete is removing files: the delete has
    already listed the rows, so what that sync restores or adds stays behind."""

    def setUp(self):
        super().setUp()
        self.add_category("1")
        self.add_category("2")
        self.catalogue_movies("1", ["Heat", "Ronin"], first_tmdb=1000)
        self.sync_as_the_callers_do()
        self.db.expire_all()
        self.catalogue_movies("2", ["Alien"], first_tmdb=2000)   # new upstream since
        self.started = []

    def _during_the_delete(self, start_a_sync):
        """delete_provider, with `start_a_sync` called once the first title's
        files are gone."""
        real = providers.delete_movie_files
        calls = []

        def delete_movie_files(path):
            n = real(path)
            calls.append(path)
            if len(calls) == 1:
                start_a_sync()
            return n

        with mock.patch.object(providers, "delete_movie_files", delete_movie_files):
            return self.try_delete()

    def test_sync_now_is_refused_while_the_provider_is_deleted(self):
        def background(provider_id, sync_type):
            # What routers.sync._run_sync_background does with the provider's VOD
            s = self.Session()
            try:
                p = s.query(Provider).filter(Provider.id == provider_id).first()
                self.started.append(sync.sync_provider(p, sync_type, s).status)
            finally:
                s.close()
                rs._running_syncs.pop(provider_id, None)
                done.set()

        done = threading.Event()
        answers = []

        def sync_now():
            s = self.Session()
            try:
                answers.append(rs.trigger_sync(rs.SyncRequest(provider_id=self.pid), db=s))
                self.assertTrue(done.wait(30), "the sync thread did not finish")
            except HTTPException as e:
                answers.append((e.status_code, e.detail))
            finally:
                s.close()

        with mock.patch.object(rs, "_run_sync_background", background):
            result = self._during_the_delete(sync_now)
        left = self.orphans()
        self.assertEqual([], left, f"a sync started during the delete left files with no row: {left} "
                                   f"(sync answered {answers}, ran {self.started})")
        self.assertEqual([(400, "This provider is being deleted")], answers)
        self.assertEqual([], self.started)
        self.assertEqual("deleted", result[0])
        self.assert_provider_gone()

    def test_the_nightly_skips_a_provider_being_deleted(self):
        import main
        import services.discovery as discovery
        import services.jellyfin as jellyfin
        import services.radarr as radarr
        import services.smartlists as sl
        import services.sonarr as sonarr
        import services.tagger as tagger
        real_sync = sync.sync_provider

        def recorded_sync(provider, sync_type, db, **kw):
            run = real_sync(provider, sync_type, db, **kw)
            self.started.append(run.status)
            return run

        patches = [
            mock.patch.object(main, "SessionLocal", self.Session),
            mock.patch.object(sync, "sync_provider", recorded_sync),
            mock.patch.object(radarr, "scan_radarr_library", lambda db: {}),
            mock.patch.object(sonarr, "scan_sonarr_library", lambda db: {}),
            mock.patch.object(sl, "migrate_global_smartlists_to_user", lambda db, uid: None),
            mock.patch.object(sl, "cleanup_orphaned_playlists", lambda db, uid: 0),
            mock.patch.object(sl, "_notify_jellyfin_plugin", lambda db: {}),
            mock.patch.object(jellyfin, "run_full_jellyfin_pipeline", lambda db, **kw: {}),
            mock.patch.object(jellyfin, "sweep_orphaned_downloads", lambda db: 0),
            mock.patch.object(tagger, "refresh_recently_added_tags", lambda db: None),
            mock.patch.object(discovery, "discover_new_provider_content",
                              lambda db: {"vod_new": [], "live_new": []}),
            mock.patch("services.provider_activity.live_streams_active", lambda: False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(rs._after_sync.pop, "nightly", None)
        mdb.set_setting(self.db, "data_dir", temp_dir(self))
        with self.assertLogs(level="INFO") as logs:
            result = self._during_the_delete(main.run_scheduled_sync)
        left = self.orphans()
        self.assertEqual([], left, f"the nightly synced the provider during its delete and left files "
                                   f"with no row: {left} (runs {self.started})")
        self.assertEqual([], self.started)
        self.assertTrue(any("Scheduled sync skipping P" in line for line in logs.output), logs.output)
        self.assertFalse([line for line in logs.output if "Scheduled sync failed" in line])
        self.assertEqual("deleted", result[0])
        self.assert_provider_gone()

    def test_a_second_delete_while_the_first_runs_is_refused(self):
        answers = []
        result = self._during_the_delete(lambda: answers.append(self.try_delete()))
        self.assertEqual([(409, "This provider is already being deleted")], answers)
        self.assertEqual("deleted", result[0])
        self.assert_provider_gone()

    def test_a_delete_that_fails_gives_the_slot_back(self):
        s = self.Session()
        s.commit = mock.Mock(side_effect=RuntimeError("database is locked"))
        try:
            with self.assertRaises(RuntimeError):
                providers.delete_provider(self.pid, db=s)
        finally:
            s.close()
        self.assertNotIn(self.pid, rs._running_syncs, "a failed delete kept the provider's sync slot")
        # Nothing was committed: the rows are still there, a sync puts the files
        # back, and the next delete removes everything.
        self.assertEqual(1, self.rows()["providers"])
        self.assertEqual("completed", self.sync_as_the_callers_do().status)
        self.assertEqual("deleted", self.try_delete()[0])
        self.assert_provider_gone()


# ── Interleavings ─────────────────────────────────────────────────────────────

def _seeds():
    first = int(os.environ.get("TENTACLE_DELETE_PROVIDER_FIRST_SEED", "0"))
    n = int(os.environ.get("TENTACLE_DELETE_PROVIDER_SEEDS", "60"))
    return range(first, first + n)


MOVIES = ["Heat", "Ronin", "Collateral", "Alien", "Thief", "Sicario", "Arrival"]
SHOWS = ["Lost", "Fringe", "Alias"]


class DeleteAndSyncInterleavings(_ProdSessions):
    """Random catalogues, a first sync or a re-sync (a lost .strm, a new
    title), and the other side attempted at a random point: a delete at any
    provider call, TMDB lookup, file write or progress step of the sync, or a
    "Sync now" at any step of the delete. After it all, and a final delete:

    I1  a delete attempted during the sync is refused and changes nothing; a
        sync attempted during the delete is refused and starts nothing
    I2  every .strm/.nfo in the VOD folders belongs to a row
    I3  the final delete leaves no file and no row of the provider
    I4  the sync the delete met ends "completed"
    I5  nobody holds the provider's slot at the end
    """

    def _fresh_world(self, rnd):
        self.db.close()
        self.db.bind.dispose()
        if getattr(self, "_world_dir", None):
            shutil.rmtree(self._world_dir, True)
        self._world_dir = tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        engine.dispose()
        self.Session = _production_sessions(f"sqlite:///{tmp}/t.db")
        self.db = self.Session()
        self.vod = vod = _RealPath(tmp) / "vod"
        (vod / "movies").mkdir(parents=True)
        (vod / "shows").mkdir(parents=True)

        def mapped_path(*parts):
            p = _RealPath(*parts)
            s = str(p)
            return _RealPath(str(vod) + s[len("/media/vod"):]) if s.startswith("/media/vod") else p

        sync.Path = mapped_path                      # NightlyHarness.tearDown restores it
        sync.VOD_MOVIES_ROOT = vod / "movies"
        sync.VOD_SERIES_ROOT = vod / "shows"
        self.client = FakeClient()
        FakeTMDB.ids = {}
        FakeTMDB.fail = set()
        self.provider = Provider(name="P", server_url="http://provider", username="u", password="p", active=True)
        self.db.add(self.provider)
        self.db.commit()
        rs._running_syncs.pop(self.pid, None)
        self.pid = self.provider.id

        for c in range(rnd.randint(1, 2)):
            self.add_category(str(c + 1))
            self.client.movies[str(c + 1)] = [(t, 1000 + MOVIES.index(t))
                                              for t in rnd.sample(MOVIES, rnd.randint(1, 3))]
        if rnd.random() < 0.5:
            self.add_category("s1", type_="series")
            self.client.series["s1"] = [(t, 5000 + SHOWS.index(t)) for t in rnd.sample(SHOWS, rnd.randint(1, 2))]
        FakeTMDB.ids = {**{t: 1000 + i for i, t in enumerate(MOVIES)}, **{t: 5000 + i for i, t in enumerate(SHOWS)}}

        if rnd.random() < 0.5:                       # a re-sync of a library already there
            self.assertEqual("completed", self.sync_as_the_callers_do().status)
            self.db.expire_all()
            strms = sorted((self.vod / "movies").rglob("*.strm"))
            if strms and rnd.random() < 0.5:
                rnd.choice(strms).unlink()           # a lost .strm the sync restores
            if rnd.random() < 0.5:                   # a title new upstream since
                cat = rnd.choice(sorted(self.client.movies))
                have = {t for t, _ in self.client.movies[cat]}
                more = [t for t in MOVIES if t not in have]
                if more:
                    t = rnd.choice(more)
                    self.client.movies[cat].append((t, 1000 + MOVIES.index(t)))

    def _events(self, at, fire):
        """Count every step of the sync; call fire() at step `at`."""
        lock = threading.Lock()
        n = [0]

        def step():
            with lock:
                n[0] += 1
                now = n[0] == at
            if now:
                fire()

        def around(fn):
            def wrapped(*a, **k):
                step()
                return fn(*a, **k)
            return wrapped

        c = self.client
        c.get_vod_streams = around(c.get_vod_streams)
        c.get_series_list = around(c.get_series_list)
        c.get_series_info = around(c.get_series_info)
        real_pause = sync._pause_between_categories

        def pause(*a, **k):
            real_pause(*a, **k)
            step()                                   # right after the category-boundary commit
        stack = [mock.patch.object(sync, "_pause_between_categories", pause)]
        for name in ("_write_strm", "write_movie_nfo", "write_series_nfo"):
            stack.append(mock.patch.object(sync, name, around(getattr(sync, name))))
        for name in ("search_movie", "search_series"):
            stack.append(mock.patch.object(FakeTMDB, name, around(getattr(FakeTMDB, name))))
        return stack, (lambda *a, **k: step())

    def _one(self, seed):
        """The invariants seed `seed` breaks, as [(invariant, what happened)]."""
        rnd = random.Random(seed)
        self._fresh_world(rnd)
        mode = "delete during sync" if rnd.random() < 0.65 else "sync during delete"
        at = rnd.randint(1, 40)
        what, bad = [], []

        if mode == "delete during sync":
            def fire():
                before = (self.vod_files(), self.rows())
                answer = self.try_delete()
                what.append(("delete", answer, (self.vod_files(), self.rows()) == before))

            stack, progress = self._events(at, fire)
            for p in stack:
                p.start()
            try:
                run = self.sync_as_the_callers_do(progress_callback=progress)
            finally:
                for p in reversed(stack):
                    p.stop()
            try:
                status = run.status
            except Exception as e:                   # its row went with the provider
                status = type(e).__name__
            for kind, answer, unchanged in what:
                if not (answer[0] == 409 and unchanged):
                    bad.append(("I1", f"a delete at step {at} of the sync answered {answer}"))
            if what and status != "completed":
                bad.append(("I4", f"the sync ended {status}"))
        else:
            real = providers.delete_movie_files
            done = threading.Event()

            def background(provider_id, sync_type):
                s = self.Session()
                try:
                    p = s.query(Provider).filter(Provider.id == provider_id).first()
                    if p is not None:
                        what.append(("synced", sync.sync_provider(p, sync_type, s).status))
                finally:
                    s.close()
                    rs._running_syncs.pop(provider_id, None)
                    done.set()

            def sync_now():
                s = self.Session()
                try:
                    rs.trigger_sync(rs.SyncRequest(provider_id=self.pid), db=s)
                    what.append(("sync started",))
                    if not done.wait(30):
                        bad.append(("I1", "the sync thread did not finish"))
                except HTTPException as e:
                    what.append(("sync refused", e.status_code, e.detail))
                finally:
                    s.close()

            calls = [0]
            # Before the 1st..6th film's files are removed, or (0) once the
            # delete has committed (its playlist rebuild): when "Sync now" comes.
            at = rnd.randint(0, 6)

            def delete_movie_files(path):
                calls[0] += 1
                if calls[0] == at:
                    sync_now()
                return real(path)

            def rebuild(*a, **k):
                if at == 0 and not what:
                    sync_now()

            with mock.patch.object(rs, "_run_sync_background", background), \
                    mock.patch.object(providers, "delete_movie_files", delete_movie_files), \
                    mock.patch("services.smartlists.sync_smartlists", rebuild):
                answer = self.try_delete()
            if answer[0] != "deleted":
                bad.append(("I3", f"the delete answered {answer}"))
            if any(w[0] != "sync refused" or w[1] not in (400, 404) for w in what):
                bad.append(("I1", f"a sync ran during the delete (step {at}): {what}"))

        left = self.orphans()
        if left:
            bad.append(("I2", f"files with no row: {left}"))
        if self.rows()["providers"]:
            answer = self.try_delete()
            if answer[0] != "deleted":
                bad.append(("I3", f"the final delete answered {answer}"))
        files, rows = self.vod_files(), self.rows()
        if files or any(rows.values()):
            bad.append(("I3", f"left {files} {rows}"))
        if self.pid in rs._running_syncs:
            bad.append(("I5", "the slot is still held"))
        return [(i, f"seed {seed} ({mode}): {text}; {what}") for i, text in bad]

    def test_interleavings(self):
        seeds = _seeds()
        broken = {}
        for seed in seeds:
            for invariant, text in self._one(seed):
                broken.setdefault(invariant, []).append(text)
        counts = {i: len({t.split(" ")[1] for t in v}) for i, v in sorted(broken.items())}
        self.assertEqual({}, {i: v[:3] for i, v in broken.items()},
                         f"seeds that broke each invariant, of {len(seeds)}: {counts}")


if __name__ == "__main__":
    unittest.main()
