"""A failed deletion-log entry must not undo the work of the action it records (#506 follow-up).

log_deletion() adds the DeletionLog row and commits the WHOLE session; when
that commit fails (database locked past the busy timeout, disk full) it rolls
the whole session back and only logs a warning. Callers that call it before
committing their own changes lose those changes silently, after the
irreversible part (files deleted, Radarr title removed) already happened, and
then carry on as if everything worked:

- Keep Downloaded: the VOD .strm/.nfo are deleted, the duplicate is marked
  "Kept Downloaded", but the row is never converted. It stays provider-owned,
  so the next VOD sync rewrites the .strm (both copies again) and the
  duplicate can't be resolved again (already resolved).
- Keep VOD: Radarr's download is deleted, the duplicate is marked "Kept VOD",
  but the release (#549) is lost: the VOD film still claims the download.
- .strm opt-out with "delete files": the files go, the opt-out doesn't stick,
  and the next sync writes them back. The API answers success.

The fault is injected at the database: the INSERT of the deletion-log row
fails as a locked database or a full disk makes it fail.

Run from tentacle/:  python tests/hermetic.py discover -s tests -p 'test_deletion_log_keeps*.py' -v
"""
import logging
import os
import random
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from models.database import DeletionLog, DownloadRequest, Duplicate, Movie, Provider
from routers import duplicates
from routers import library
from tests.test_duplicate_keep_vod_merged_folder import _FakeArr
from tests.test_duplicate_keeps_user_data import _Base, FakeJellyfin


def setUpModule(): logging.disable(logging.CRITICAL)
def tearDownModule(): logging.disable(logging.NOTSET)


class StatementFails:
    """The next `times` statements starting with `prefix` fail at the
    database, as SQLite answers a locked database or a full disk."""

    def __init__(self, engine, prefix="INSERT INTO deletion_log", times=1, message="database is locked"):
        self.engine, self.prefix, self.left, self.message = engine, prefix.upper(), times, message
        event.listen(engine, "before_cursor_execute", self.hook)

    def hook(self, conn, cursor, statement, params, context, executemany):
        if self.left > 0 and statement.lstrip().upper().startswith(self.prefix):
            self.left -= 1
            raise sqlite3.OperationalError(self.message)

    def remove(self):
        event.remove(self.engine, "before_cursor_execute", self.hook)


class _FakeClient:
    def movie_stream_url(self, stream_id, ext="mp4"):
        return f"http://p/movie/u/p/{stream_id}.{ext}"


class _Common(_Base):
    """A VOD .strm and a download of the same film in separate folders, as
    tests.test_duplicate_keeps_user_data.Film sets them up."""
    def setUp(self):
        super().setUp()
        vod = self.root / "vod" / "movies" / "Film (2001)"
        vod.mkdir(parents=True)
        self.strm = vod / "Film (2001).strm"
        self.strm.write_text("http://p/movie/1.mp4")
        dl = self.root / "movies" / "Film (2001)"
        dl.mkdir(parents=True)
        self.mkv = dl / "Film (2001).mkv"
        self.mkv.write_bytes(b"\0" * 64)
        _FakeArr.titles[101] = {"id": 7, "path": str(dl), "files": [{"id": 70, "path": str(self.mkv)}]}
        self.db.add(Movie(tmdb_id=101, title="Film", year="2001", source="provider_1", provider_id=1,
                          strm_path=str(self.strm)))
        self.dup = Duplicate(tmdb_id=101, media_type="movie", resolution="pending",
                             sources=[{"source": "radarr", "path": str(self.mkv)},
                                      {"source": "provider_1", "path": str(self.strm)}])
        self.db.add(self.dup)
        FakeJellyfin.items = [
            {"Id": "vod", "Type": "Movie", "Path": "/vod-movies/Film (2001)/Film (2001).strm",
             "ProviderIds": {"Tmdb": "101"}},
            {"Id": "dl", "Type": "Movie", "Path": "/data/movies/Film (2001)/Film (2001).mkv",
             "ProviderIds": {"Tmdb": "101"}},
        ]
        self.engine = self.db.get_bind()
        self.db.add(Provider(id=1, name="P", server_url="http://p", username="u", password="p"))
        # Another film in the VOD root, as on any real library (an empty root
        # reads as unmounted storage and the sync then writes nothing).
        other = self.strm.parent.parent / "Other (1999)"
        other.mkdir()
        (other / "Other (1999).strm").write_text("http://p/movie/u/p/9.mp4")
        self.db.commit()

    def fresh(self):
        """What was committed, read by a new session."""
        s = sessionmaker(bind=self.engine)()
        self.addCleanup(s.close)
        return s


class KeepDownloaded(_Common):
    def test_keep_downloaded_is_not_left_half_done_by_a_failed_audit_entry(self):
        StatementFails(self.engine)
        try:
            duplicates._resolve(self.dup, "keep_radarr", self.db)
        except Exception:
            pass   # failing loudly is fine: the duplicate must then still be pending
        s = self.fresh()
        dup, row = s.query(Duplicate).one(), s.query(Movie).one()
        self.assertFalse(self.strm.exists(), "the VOD copy was deleted either way")
        if dup.resolution == "keep_radarr":
            self.assertEqual(
                ("radarr", None), (row.source, row.strm_path),
                "the duplicate says Kept Downloaded and the .strm is gone, but the row is still "
                f"the provider's ({row.source}, {row.strm_path}): the next sync writes the .strm back")
        else:
            self.assertEqual("pending", dup.resolution)

    def test_what_follows_today(self):
        """Documents the consequence on the pinned sha (passes there): the
        next sync's repair brings the VOD copy back and the duplicate can't
        be resolved again. With the fix the first assertion flips."""
        StatementFails(self.engine)
        duplicates._resolve(self.dup, "keep_radarr", self.db)
        s = self.fresh()
        row = s.query(Movie).one()
        if row.source == "radarr":
            self.skipTest("fixed: the conversion was kept")
        from services.sync import _repair_movie_strm
        provider = s.get(Provider, 1)
        self.assertTrue(_repair_movie_strm(_FakeClient(), {"stream_id": 1, "container_extension": "mp4"},
                                           101, provider, s))
        self.assertTrue(self.strm.exists(), "the deleted VOD copy is back after the next sync")
        with self.assertRaises(HTTPException) as cm:
            duplicates.resolve_duplicate(self.dup.id, duplicates.ResolveRequest(resolution="keep_radarr"), s)
        self.assertEqual(409, cm.exception.status_code, "and it can't be resolved again")


class KeepVod(_Common):
    def setUp(self):
        super().setUp()
        row = self.db.query(Movie).one()
        row.radarr_path = str(self.mkv)
        row.downloaded_at = datetime(2026, 9, 1)
        row.tags = ["Netflix Movies", "Downloaded Movies", "Ann's Downloads"]
        self.db.add(DownloadRequest(tmdb_id=101, media_type="movie", user_id=1))
        self.db.commit()

    def test_keep_vod_release_is_not_lost_by_a_failed_audit_entry(self):
        StatementFails(self.engine)
        try:
            duplicates._resolve(self.dup, "keep_vod", self.db)
        except Exception:
            pass
        s = self.fresh()
        dup, row = s.query(Duplicate).one(), s.query(Movie).one()
        self.assertFalse(self.mkv.exists(), "Radarr deleted the download either way")
        if dup.resolution == "keep_vod":
            self.assertIsNone(row.downloaded_at,
                              "Kept VOD, download deleted, but the VOD film still claims it (#549 again)")
            self.assertNotIn("Downloaded Movies", row.tags or [])
            self.assertEqual(0, s.query(DownloadRequest).count())
        else:
            self.assertEqual("pending", dup.resolution)


class StrmOptOut(_Common):
    def test_opt_out_with_delete_files_is_kept_when_the_audit_entry_fails(self):
        StatementFails(self.engine)
        try:
            out = library.set_strm_managed("movie", 101, library.StrmManagedBody(enabled=False, delete_files=True),
                                           self.db, None)
        except Exception:
            out = None
        s = self.fresh()
        row = s.query(Movie).one()
        self.assertFalse(self.strm.exists())
        if out is not None and out.get("success"):
            self.assertTrue(row.strm_disabled,
                            "answered success and deleted the files, but the opt-out was rolled back: "
                            "the next sync writes the .strm back")


class Property(unittest.TestCase):
    """Random action, random failing statement (the audit INSERT, or the
    caller's own UPDATE), 1-2 failures, over many seeds. Invariant: an action
    that answers normally has committed what it did; one that raises has
    left its duplicate pending (it can be retried)."""
    SEEDS = int(os.environ.get("VOD_SEEDS", "300"))

    def test_invariant_over_seeds(self):
        failures = []
        for seed in range(self.SEEDS):
            rnd = random.Random(seed)
            action = rnd.choice(["keep_radarr", "keep_vod", "optout"])
            prefix = rnd.choice(["INSERT INTO deletion_log", "UPDATE movies", "NONE"])
            times = rnd.choice([1, 2])
            msg = self._one(seed, action, prefix, times)
            if msg:
                failures.append(f"seed={seed} {action} fail={prefix}x{times}: {msg}")
        if failures:
            self.fail(f"{len(failures)} of {self.SEEDS} seeds broke the invariant; first 5:\n"
                      + "\n".join(failures[:5]))

    def _one(self, seed, action, prefix, times):
        case = (KeepVod if action == "keep_vod" else _Common)("setUp")
        case.setUp()
        try:
            if prefix != "NONE":
                StatementFails(case.engine, prefix, times)
            raised = False
            try:
                if action == "optout":
                    out = library.set_strm_managed("movie", 101, library.StrmManagedBody(
                        enabled=False, delete_files=True), case.db, None)
                    raised = not (out or {}).get("success")
                else:
                    duplicates._resolve(case.dup, action, case.db)
            except Exception:
                raised = True
            s = case.fresh()
            row, dup = s.query(Movie).one(), s.query(Duplicate).one()
            if action == "keep_radarr":
                if dup.resolution == "keep_radarr" and row.source != "radarr":
                    return "resolved Kept Downloaded, row not converted"
                if raised and dup.resolution != "pending":
                    return "raised but not pending"
            elif action == "keep_vod":
                if dup.resolution == "keep_vod" and row.downloaded_at is not None:
                    return "resolved Kept VOD, row still claims the download"
                if raised and dup.resolution != "pending":
                    return "raised but not pending"
            else:
                if not raised and not row.strm_disabled:
                    return "answered success, opt-out not stored"
            return None
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
