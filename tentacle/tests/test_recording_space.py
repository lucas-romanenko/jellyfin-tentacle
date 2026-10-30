"""A space limit for Live TV recordings (#868).

Rob, 29 Oct 2026: recordings fill the disk with no limit (sports run 2.5-4
GB an hour). Keep them under N GB, oldest first, sizes from Jellyfin's
items, deleted through Jellyfin; never a favourite, one in progress, or what
a rule keeps; skip the whole pass when anything can't be read; each deletion
in the Deletion Log; the total and the limit on the Recordings page.
Lucas, 30 Oct: build it; off until an admin sets a limit.

The fake Jellyfin answers the calls the pass makes and records deletes.

Run from the tentacle/ directory:  python tests/hermetic.py test_recording_space
"""
import unittest
from datetime import datetime, timedelta

from fastapi import HTTPException

from models.database import DeletionLog, RecordingBooking, RecordingRule, RecordingSize, get_setting, set_setting
from services import recording_space as RS
from test_recording_art import LIB, _db

NOW = datetime(2026, 10, 30, 12, 0)
ROOT = "/config/data/livetv/recordings"
GB = 1000 ** 3


def _id(n: int) -> str:
    return f"{n:032x}"


class FakeJellyfin:
    def __init__(self):
        self.items = {}
        self.users = [{"Id": "u1", "Name": "lucas"}, {"Id": "u2", "Name": "ann"}]
        self.fav = {}             # user id -> {item ids}
        self.resume = {}          # user id -> {item ids}
        self.timers = []
        self.sessions = []
        self.deleted = []
        self.fail = set()         # paths that raise
        self.delete_fails = False

    def add(self, n, name, created, gb=3.0, path=None, parent=LIB, folder=False, size=None):
        iid = _id(n)
        self.items[iid] = {
            "Id": iid, "Name": name, "ParentId": parent, "IsFolder": folder,
            "Path": path if path is not None else f"{ROOT}/{name}/{name} {n}.ts",
            "DateCreated": created.strftime("%Y-%m-%dT%H:%M:%S.0000000Z") if created else None,
            "RunTimeTicks": None if folder else 3600 * 10_000_000,
            "MediaSources": [] if folder else [{"Size": int(gb * GB) if size is None else size}]}
        return iid

    def get(self, path, **p):
        if path in self.fail:
            raise RuntimeError(f"{path} answered 500")
        if path == "/System/Configuration/livetv":
            return {"RecordingPath": "", "MediaLocationsCreated": [ROOT]}
        if path == "/Library/VirtualFolders":
            return [{"Name": "Recordings", "ItemId": LIB, "Locations": [ROOT], "LibraryOptions": {}}]
        if path == "/LiveTv/Timers":
            return {"Items": self.timers}
        if path == "/Sessions":
            return self.sessions
        if path == "/Users":
            return self.users
        if path == "/Items":
            if p.get("Ids"):
                return {"Items": [self.items[i] for i in p["Ids"].split(",") if i in self.items]}
            if p.get("userId"):
                if ("/Items", p["userId"]) in self.fail:
                    raise RuntimeError("500")
                marks = (self.fav if p["Filters"] == "IsFavorite" else self.resume).get(p["userId"], set())
                return {"Items": [{"Id": i} for i in marks]}
            folders = p.get("IsFolder") == "true"
            return {"Items": [i for i in self.items.values() if i["IsFolder"] == folders]}
        raise AssertionError(path)

    def delete_item(self, iid):
        if self.delete_fails:
            raise RuntimeError("Jellyfin answered 500")
        self.deleted.append(iid)
        self.items.pop(iid, None)


class Base(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        self.addCleanup(self.db.close)
        self.jf = FakeJellyfin()
        RS._last.update(at=None, error=None, deleted=0, seen=None)

    def limit(self, gb=None, days=None):
        RS.set_limits(self.db, gb, days)

    def run_pass(self, now=NOW):
        return RS.run_pass(self.db, self.jf, now=now)

    def days_ago(self, d, hours=0):
        return NOW - timedelta(days=d, hours=hours)

    def log(self):
        return [(e.kind, e.name, e.size_bytes, e.reason, e.detail) for e in self.db.query(DeletionLog).all()]

    def five(self):
        """Five 3 GB recordings, one a day, the oldest five days ago (15 GB)."""
        return [self.jf.add(n, f"Game {n}", self.days_ago(6 - n)) for n in range(1, 6)]


class Off(Base):
    def test_no_limit_deletes_nothing_and_keeps_the_list(self):
        ids = self.five()
        out = self.run_pass()
        self.assertEqual([], self.jf.deleted)
        self.assertEqual((5, 15 * GB, 0), (out["recordings"], out["total_bytes"], out["deleted"]))
        self.assertEqual(sorted(ids), sorted(r.item_id for r in self.db.query(RecordingSize).all()))
        self.assertEqual([], self.log())

    def test_an_upgrade_has_no_limit(self):
        self.assertEqual((None, None), RS.limits(self.db))
        self.assertEqual("", get_setting(self.db, RS.LIMIT_SETTING, "") or "")

    def test_zero_or_junk_is_no_limit(self):
        self.five()
        set_setting(self.db, RS.LIMIT_SETTING, "0")
        set_setting(self.db, RS.DAYS_SETTING, "soon")
        self.run_pass()
        self.assertEqual([], self.jf.deleted)

    def test_clearing_the_limit_stops_deleting(self):
        self.five()
        self.limit(10)
        self.limit(None)
        self.run_pass()
        self.assertEqual([], self.jf.deleted)


class OverTheLimit(Base):
    def test_the_oldest_go_until_under_the_limit(self):
        ids = self.five()
        self.limit(10)                       # 15 GB: two of 3 GB go (9 GB left)
        out = self.run_pass()
        self.assertEqual(ids[:2], self.jf.deleted)
        self.assertEqual((2, 6 * GB, 3, 9 * GB), (out["deleted"], out["freed_bytes"], out["recordings"],
                                                 out["total_bytes"]))
        self.assertEqual(3, self.db.query(RecordingSize).count())

    def test_under_the_limit_nothing_goes(self):
        self.five()
        self.limit(15)
        self.run_pass()
        self.assertEqual([], self.jf.deleted)

    def test_each_deletion_is_in_the_deletion_log(self):
        self.five()
        self.limit(13)
        self.run_pass()
        (kind, name, size, reason, detail), = self.log()
        self.assertEqual(("recordings-limit", "Game 1", 3 * GB, "auto"), (kind, name, size, reason))
        self.assertEqual("Over the recordings limit (13 GB); recorded 2026-10-25 12:00 UTC", detail)

    def test_order_is_by_when_recorded_not_by_name_or_size(self):
        a = self.jf.add(1, "Zed", self.days_ago(1), gb=8)
        b = self.jf.add(2, "Alpha", self.days_ago(3), gb=1)
        self.jf.add(3, "Mid", self.days_ago(2), gb=4)
        self.limit(12)                       # 13 GB: the oldest (1 GB) is enough
        self.run_pass()
        self.assertEqual([b], self.jf.deleted)
        self.assertNotIn(a, self.jf.deleted)

    def test_older_than_days(self):
        ids = self.five()
        self.limit(days=3)                   # 5, 4 days old go; 3 days old stays
        self.run_pass()
        self.assertEqual(ids[:2], self.jf.deleted)
        self.assertTrue(all(d.startswith("Older than 3 days") for *_, d in self.log()))

    def test_days_and_size_together(self):
        ids = self.five()
        self.limit(4, days=4)                # 5 days old by age, then down to 4 GB by size
        self.run_pass()
        self.assertEqual(ids[:4], self.jf.deleted)

    def test_not_a_recording_is_never_counted_or_deleted(self):
        # a library over a parent folder: a film next to the recordings
        film = self.jf.add(9, "A Film", self.days_ago(30), gb=40, path="/config/data/livetv/movies/film.mkv")
        self.five()
        self.limit(100)
        out = self.run_pass()
        self.assertEqual(5, out["recordings"])
        self.assertNotIn(film, self.jf.deleted)

    def test_a_folder_name_that_only_starts_like_the_recordings_folder(self):
        self.jf.add(9, "Other", self.days_ago(30), gb=40, path=ROOT + "-old/x.ts")
        self.five()
        self.limit(1)
        self.run_pass()
        self.assertNotIn(_id(9), self.jf.deleted)


class NeverDeleted(Base):
    def test_a_favourite(self):
        ids = self.five()
        self.jf.fav["u2"] = {ids[0]}
        self.limit(10)
        self.run_pass()
        self.assertEqual(ids[1:3], self.jf.deleted)      # the favourite counts, the next two go
        row = self.db.query(RecordingSize).filter_by(item_id=ids[0]).one()
        self.assertEqual("favourite", row.kept)

    def test_a_favourite_show_keeps_its_recordings(self):
        show = self.jf.add(50, "Game", None, folder=True)
        ids = [self.jf.add(n, f"Game {n}", self.days_ago(6 - n), parent=show) for n in range(1, 4)]
        self.jf.fav["u1"] = {show}
        self.limit(1)
        self.run_pass()
        self.assertEqual([], self.jf.deleted)
        self.assertEqual({"favourite"}, {r.kept for r in self.db.query(RecordingSize).all()})
        self.assertEqual(3, len(ids))

    def test_one_someone_is_part_way_through(self):
        ids = self.five()
        self.jf.resume["u1"] = {ids[0], ids[1]}
        self.limit(10)
        self.run_pass()
        self.assertEqual(ids[2:4], self.jf.deleted)

    def test_one_playing_now(self):
        ids = self.five()
        self.jf.sessions = [{"NowPlayingItem": {"Id": ids[0]}}, {"UserName": "idle"}]
        self.limit(13)
        self.run_pass()
        self.assertEqual([ids[1]], self.jf.deleted)

    def test_one_recording_now(self):
        # Jellyfin's timer is on it, and one began 5 minutes ago
        now_on = self.jf.add(7, "Live: NHL Hockey", NOW - timedelta(hours=1), gb=3)
        fresh = self.jf.add(8, "The National", NOW - timedelta(minutes=5), gb=3)
        self.jf.timers = [{"Status": "InProgress", "Name": "Live: NHL Hockey",
                           "StartDate": (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                           "EndDate": (NOW + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")}]
        self.limit(1)
        self.run_pass()
        self.assertEqual([], self.jf.deleted)
        kept = {r.item_id: r.kept for r in self.db.query(RecordingSize).all()}
        self.assertEqual({now_on: "recording", fresh: "recording"}, kept)

    def test_its_booking_says_recording(self):
        iid = self.jf.add(7, "NHL Hockey", NOW - timedelta(hours=2))
        self.db.add(RecordingBooking(pool_key="p1", start=NOW - timedelta(hours=2), stop=NOW + timedelta(hours=1),
                                     title="NHL Hockey", state="recording", pre_padding_seconds=300))
        self.db.commit()
        self.limit(1)
        self.run_pass()
        self.assertEqual([], self.jf.deleted)
        self.assertEqual("recording", self.db.query(RecordingSize).filter_by(item_id=iid).one().kept)

    def _rule_recordings(self, **keep):
        rule = RecordingRule(name="Maple Leafs", kind="team", league="nhl", team="tor", mode="record", **keep)
        self.db.add(rule)
        self.db.commit()
        ids = []
        for n in range(1, 5):
            start = self.days_ago(5 - n)
            ids.append(self.jf.add(n, "NHL Hockey", start + timedelta(minutes=-5)))
            self.db.add(RecordingBooking(rule_id=rule.id, pool_key="p1", start=start, stop=start + timedelta(hours=3),
                                         title="NHL Hockey", state="ended", pre_padding_seconds=300))
        other = self.jf.add(9, "The National", self.days_ago(9))
        self.db.commit()
        return ids, other

    def test_a_rule_that_keeps_forever(self):
        ids, other = self._rule_recordings(keep_forever=True)
        self.limit(1)
        self.run_pass()
        self.assertEqual([other], self.jf.deleted)
        self.assertEqual({"keep"}, {r.kept for r in self.db.query(RecordingSize).all()})

    def test_a_rule_that_keeps_the_last_two(self):
        ids, other = self._rule_recordings(keep_last=2)
        self.limit(1)
        self.run_pass()
        self.assertEqual([other] + ids[:2], self.jf.deleted)     # its two newest stay

    def test_a_rule_without_keep_is_like_any_recording(self):
        ids, other = self._rule_recordings()
        self.limit(1)
        self.run_pass()
        self.assertEqual(5, len(self.jf.deleted))

    def test_kept_alone_over_the_limit_deletes_the_rest_and_says_so(self):
        ids = self.five()
        self.jf.fav["u1"] = set(ids[2:])
        self.limit(5)
        self.run_pass()
        self.assertEqual(ids[:2], self.jf.deleted)
        self.assertTrue(RS.status(self.db, now=NOW)["kept_over"])


class AnythingUnreadable(Base):
    def setUp(self):
        super().setUp()
        self.ids = self.five()
        self.limit(1)

    def assertSkipped(self, out, words):
        self.assertEqual([], self.jf.deleted)
        self.assertIn(words, out["skipped"])
        self.assertIn("nothing deleted", out["skipped"])
        self.assertEqual([], self.log())

    def test_an_item_without_a_size(self):
        self.jf.add(9, "Mystery", self.days_ago(1), size=0)
        self.assertSkipped(self.run_pass(), "the size of “Mystery”")

    def test_an_item_without_a_date(self):
        self.jf.add(9, "Mystery", None)
        self.assertSkipped(self.run_pass(), "when “Mystery” was recorded")

    def test_an_item_without_a_path(self):
        self.jf.add(9, "Mystery", self.days_ago(1), path="")
        self.assertSkipped(self.run_pass(), "“Mystery” has no path")

    def test_the_items(self):
        self.jf.fail.add("/Library/VirtualFolders")
        self.assertSkipped(self.run_pass(), "Jellyfin's recordings")

    def test_the_timers(self):
        self.jf.fail.add("/LiveTv/Timers")
        self.assertSkipped(self.run_pass(), "Jellyfin's recordings")

    def test_the_sessions(self):
        self.jf.fail.add("/Sessions")
        self.assertSkipped(self.run_pass(), "Jellyfin's recordings")

    def test_the_users(self):
        self.jf.fail.add("/Users")
        self.assertSkipped(self.run_pass(), "favourites")

    def test_one_users_favourites(self):
        self.jf.fail.add(("/Items", "u2"))
        self.assertSkipped(self.run_pass(), "favourites")

    def test_a_skipped_pass_keeps_the_last_list_and_says_why(self):
        self.limit(None)
        self.run_pass()
        self.jf.fail.add("/Sessions")
        self.limit(1)
        self.run_pass()
        st = RS.status(self.db, now=NOW)
        self.assertEqual(5, st["count"])
        self.assertIn("nothing deleted", st["error"])

    def test_no_jellyfin(self):
        out = RS.run_pass(self.db, None, now=NOW)       # no Jellyfin address or key saved
        self.assertEqual("Jellyfin isn't connected", out["skipped"])

    def test_a_failed_delete_ends_the_pass(self):
        self.jf.delete_fails = True
        out = self.run_pass()
        self.assertEqual(0, out["deleted"])
        self.assertEqual([], self.log())
        self.assertIn("didn't delete “Game 1”", RS._last["error"])

    def test_gone_before_its_delete_is_not_logged(self):
        doomed = []
        get = self.jf.get

        def get_and_vanish(path, **p):
            if p.get("Ids") and not doomed:
                doomed.append(p["Ids"])
                self.jf.items.pop(p["Ids"])
            return get(path, **p)
        self.jf.get = get_and_vanish
        self.limit(13)
        self.run_pass()
        # someone deleted it meanwhile: that was enough (12 GB), nothing else goes, nothing logged
        self.assertEqual([self.ids[0]], doomed)
        self.assertEqual([], self.jf.deleted)
        self.assertEqual([], self.log())
        self.assertEqual(4, self.db.query(RecordingSize).count())


class ThePage(Base):
    def test_status_total_and_limit(self):
        self.five()
        self.limit(12)
        self.run_pass()
        st = RS.status(self.db, now=NOW)
        self.assertEqual((12.0, None, True, 4, 12 * GB, []), (st["limit_gb"], st["days"], st["on"], st["count"],
                                                             st["total_bytes"], st["would_go"]))

    def test_preview_is_a_dry_run(self):
        ids = self.five()
        self.run_pass()
        st = RS.status(self.db, now=NOW, limit_gb=7, preview=True)
        self.assertEqual(ids[:3], [w["item_id"] for w in st["would_go"]])
        self.assertEqual(9 * GB, st["would_free_bytes"])
        self.assertFalse(st["on"])                       # nothing saved
        self.assertEqual([], self.jf.deleted)
        self.assertEqual(5, self.db.query(RecordingSize).count())

    def test_the_routes(self):
        from routers import recordings as R
        self.five()
        self.run_pass()

        class Req:
            cookies, headers, query_params = {}, {}, {}
        got = R.recordings_space(Req(), limit_gb=10, days=None, preview=True, db=self.db)
        self.assertEqual(2, len(got["would_go"]))
        self.assertTrue(got["is_admin"])                 # first-run (no users): like require_admin
        with self.assertRaises(HTTPException):
            R.recordings_space(Req(), limit_gb=0.5, days=None, preview=True, db=self.db)
        import services.recording_space as mod
        asked = []
        orig, mod.request_pass = mod.request_pass, lambda delay=5.0: asked.append(delay)
        try:
            from services.recording_rules import book
            n, book.notify = book.notify, lambda: None
            try:
                out = R.recordings_space_set(R.SpaceBody(limit_gb=300, days=None), db=self.db)
                self.assertEqual((300.0, True), (out["limit_gb"], out["on"]))
                self.assertEqual([5.0], asked)
                out = R.recordings_space_set(R.SpaceBody(limit_gb=None, days=None), db=self.db)
                self.assertFalse(out["on"])
                self.assertEqual([5.0], asked)           # off: no pass asked for
            finally:
                book.notify = n
        finally:
            mod.request_pass = orig

    def test_the_deletion_kind_has_a_label(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "static" / "js" / "pages.js").read_text(encoding="utf-8")
        self.assertIn("'recordings-limit':", js)


class RuleKeep(Base):
    def test_a_rule_saves_keep(self):
        from routers import recordings as R
        from services.recording_rules import book
        n, s = book.notify, book.request_scan
        book.notify = book.request_scan = lambda *a, **k: None
        self.addCleanup(setattr, book, "notify", n)
        self.addCleanup(setattr, book, "request_scan", s)

        class Req:
            cookies, headers, query_params = {}, {}, {}
        r = R.create_rule(R.RuleBody(kind="keywords", terms=["Grand Prix"], keep_last=3), Req(), db=self.db)
        self.assertEqual((False, 3), (r["keep_forever"], r["keep_last"]))
        r = R.update_rule(r["id"], R.RulePatch(keep_forever=True), Req(), db=self.db)
        self.assertEqual((True, None), (r["keep_forever"], r["keep_last"]))
        r = R.update_rule(r["id"], R.RulePatch(keep_forever=False, keep_last=0), Req(), db=self.db)
        self.assertEqual((False, None), (r["keep_forever"], r["keep_last"]))


if __name__ == "__main__":
    unittest.main()
