"""A playlist's "Request N albums" keeps going when the playlist is removed meanwhile.

request_job requests the ticked albums one at a time and notes each outcome on
the import. A Remove while it ran made the next note fail (StaleDataError: the
row was gone): the job stopped, the rest of the ticked albums were never
requested, and the music module showed the error.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import os
import random
import unittest
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from test_music_core import _Base  # noqa: E402

ALBUMS = [f"0000000{i}-0000-0000-0000-000000000000" for i in range(4)]


class TestRequestsFromAnImport(_Base):
    def make_import(self):
        import models.database as mdb
        imp = mdb.MusicImport(user_id=1, name="Road trip", source="exportify_csv", status="ready", done=0,
                              total=0, tracks=[], outcomes={})
        self.db.add(imp)
        self.db.commit()
        return imp.id

    def run_requests(self, import_id, remove_at=None, refuse=(), refresh_at=None):
        import models.database as mdb
        from services.media_requests import RequestRefused
        from services.music import spotify
        requested, other = [], self.Session()

        def request_album(db, rgid, **kw):
            requested.append((rgid, kw.get("via")))
            if remove_at is not None and len(requested) == remove_at:   # the user presses Remove now
                row = other.get(mdb.MusicImport, import_id)
                if row is not None:
                    other.delete(row)
                    other.commit()
            if refresh_at is not None and len(requested) == refresh_at:   # a Refresh writes the row now
                row = other.get(mdb.MusicImport, import_id)
                if row is not None:
                    row.tracks, row.status, row.total = [{"title": "New song", "result": None}], "resolving", 1
                    other.commit()
            if rgid in refuse:
                raise RequestRefused("Lidarr's metadata server doesn't know this album yet.")
            return {"status": "requested"}
        job_db = self.Session()   # the worker's own session for the job
        try:
            with mock.patch("services.media_requests.request_album", side_effect=request_album):
                spotify.request_job(import_id, ALBUMS, 1)(job_db)
        finally:
            job_db.close()
            other.close()
        return requested

    def row(self, import_id):
        import models.database as mdb
        self.db.expire_all()
        return self.db.get(mdb.MusicImport, import_id)

    def outcomes(self, import_id):
        import models.database as mdb
        self.db.expire_all()
        row = self.db.get(mdb.MusicImport, import_id)
        return None if row is None else row.outcomes

    def test_a_remove_meanwhile_doesnt_stop_the_other_requests(self):
        import_id = self.make_import()
        requested = self.run_requests(import_id, remove_at=1)
        self.assertEqual([r for r, _ in requested], ALBUMS)
        self.assertIsNone(self.outcomes(import_id))

    def test_each_outcome_is_noted_as_before(self):
        import_id = self.make_import()
        requested = self.run_requests(import_id, refuse={ALBUMS[2]})
        self.assertEqual({v for _, v in requested}, {"the Spotify import “Road trip”"})
        outcomes = self.outcomes(import_id)
        self.assertEqual([outcomes[a] for a in ALBUMS[:2] + ALBUMS[3:]], ["requested"] * 3)
        self.assertIn("doesn't know this album", outcomes[ALBUMS[2]])

    def test_a_refresh_meanwhile_keeps_its_songs(self):
        import_id = self.make_import()
        self.run_requests(import_id, refresh_at=2)
        row = self.row(import_id)
        self.assertEqual(row.status, "resolving")
        self.assertEqual([t["title"] for t in row.tracks], ["New song"])
        self.assertEqual(set(row.outcomes), set(ALBUMS))

    def test_a_remove_at_any_point(self):
        seeds = int(os.environ.get("GM_SEEDS", "1000"))
        base = int(os.environ.get("GM_SEED", "20260929"))
        bad = []
        for n in range(seeds):
            rnd = random.Random(base + n)
            import_id = self.make_import()
            remove_at = rnd.choice([None, 1, 2, 3, 4])
            refresh_at = rnd.choice([None, 1, 2, 3, 4])
            refuse = set(rnd.sample(ALBUMS, rnd.randint(0, 2)))
            try:
                requested = self.run_requests(import_id, remove_at=remove_at, refuse=refuse, refresh_at=refresh_at)
            except Exception as e:
                bad.append((base + n, repr(e)))
                continue
            if [r for r, _ in requested] != ALBUMS:
                bad.append((base + n, "not every album requested"))
            if {v for _, v in requested} != {"the Spotify import “Road trip”"}:
                bad.append((base + n, "an album requested without the import's name"))
            outcomes = self.outcomes(import_id)
            if remove_at is None:
                expected = {a: ("requested" if a not in refuse else "refused") for a in ALBUMS}
                got = {a: ("refused" if "doesn't know" in (o or "") else o) for a, o in (outcomes or {}).items()}
                if got != expected:
                    bad.append((base + n, f"outcomes {got}"))
                row = self.row(import_id)
                refreshed = refresh_at is not None and (remove_at is None or refresh_at < remove_at)
                if refreshed and (row.tracks, row.status) != ([{"title": "New song", "result": None}], "resolving"):
                    bad.append((base + n, "the Refresh's songs were overwritten"))
            elif outcomes is not None:
                bad.append((base + n, "a removed import came back"))
        print(f"\n[import-requests] {seeds} seeds from {base}: failures={len(bad)} {bad[:3]}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
