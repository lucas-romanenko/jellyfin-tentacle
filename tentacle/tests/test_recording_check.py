"""After a Live TV recording (#869): its clock and its PremiereDate.

Rob, 29 Oct 2026: a provider re-dial that isn't joined goes out "as it
came", so the new stream's clock lands in the recording: 3 h 45 min (13.7
GB) showed as 21.7 h in Jellyfin, ffprobe start 19351 s, duration 78130 s.
On the maintainer's install a 3 h 39 min Canadiens game showed as 21 h
until it was remuxed by hand. Lucas chose the check after the recording
(not rewriting live timestamps): the plugin copies a recording whose probed
length is far over its wall-clock window with `ffmpeg -fflags
+genpts+discardcorrupt -c copy` and swaps the copy in only when it is
right; and PremiereDate = the programme's real start (Jellyfin leaves it
empty on recordings).

The fake Jellyfin answers the plugin's two routes the way the plugin does.
The plugin's C# has no test host here: its source is read for the rules
that matter (as test_delete_handler_rereads.py does), and the ffmpeg recipe
it runs is run here on a made-up recording with a clock jump (skipped
without ffmpeg).

Run from the tentacle/ directory:  python tests/hermetic.py test_recording_check
"""
import re
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tmp_dirs import temp_dir
from services import recording_check as RC
from test_recording_art import LIB, FakeJellyfin, _db

NOW = datetime(2026, 10, 30, 12, 0)
ID = "a" * 32
PLUGIN = Path("../tentacle-plugin")


class FakePlugin(FakeJellyfin):
    """FakeJellyfin plus the plugin's after-recording routes."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.answers = {}         # item id -> (status, body) for Retime
        self.retimes = []
        self.premieres = []
        self.premiere = {}        # item id -> PremiereDate Jellyfin has
        self.old_plugin = False

    def post(self, path, timeout=30, **params):
        if self.old_plugin:
            return 404, {}
        m = re.fullmatch(r"/Tentacle/Recordings/([0-9a-f-]{36})/(Retime|Premiere)", path)
        assert m, path
        iid = m.group(1).replace("-", "")
        if m.group(2) == "Retime":
            self.retimes.append(iid)
            return self.answers.get(iid, (200, {"state": "ok", "duration": 3600.0, "wall": 3700.0}))
        self.premieres.append((iid, params["date"]))
        if iid in self.premiere:
            return 200, {"set": False}
        self.premiere[iid] = params["date"]
        return 200, {"set": True}


class Base(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        self.addCleanup(self.db.close)
        self.jf = FakePlugin()

    def run_pass(self, now=NOW, **kw):
        return RC.run_pass(self.db, self.jf, now=now, **kw)

    def row(self, iid=ID):
        from models.database import RecordingCheck
        return self.db.query(RecordingCheck).filter_by(item_id=iid).one_or_none()

    def activity(self):
        from models.database import ActivityLog
        return [(a.event, a.message) for a in self.db.query(ActivityLog).all()]


class TheClock(Base):
    def test_a_recording_whose_clock_jumped_is_retimed_and_said_in_activity(self):
        self.jf.add(ID, "Live: NHL Hockey", NOW - timedelta(hours=5), minutes=1302)   # 21.7 h probed
        self.jf.answers[ID] = (200, {"state": "retimed", "duration": 78130.0, "wall": 13500.0, "after": 13490.0})
        out = self.run_pass()
        self.assertEqual(1, out["retimed"])
        self.assertEqual([ID], self.jf.retimes)
        row = self.row()
        self.assertEqual(("retimed", 78130.0, 13500.0, 13490.0), (row.state, row.duration, row.wall, row.after))
        (event, msg), = self.activity()
        self.assertEqual("livetv_recording_retimed", event)
        self.assertIn("21 h 42 min for 3 h 45 min recorded", msg)
        self.assertIn("now 3 h 44 min", msg)

    def test_each_recording_is_checked_once(self):
        self.jf.add(ID, "The National", NOW - timedelta(hours=2))
        self.run_pass()
        self.run_pass(now=NOW + timedelta(hours=1))
        self.assertEqual([ID], self.jf.retimes)
        self.assertEqual("ok", self.row().state)
        self.assertEqual([], self.activity())

    def test_the_ids_go_to_the_plugin_with_dashes(self):
        self.jf.add(ID, "The National", NOW - timedelta(hours=2))
        seen = []
        post = self.jf.post
        self.jf.post = lambda path, timeout=30, **p: (seen.append((path, timeout)), post(path, timeout, **p))[1]
        self.run_pass()
        self.assertEqual(("/Tentacle/Recordings/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/Retime", RC.COPY_TIMEOUT),
                         seen[0])

    def test_a_copy_that_did_not_fix_it_is_left_and_said(self):
        self.jf.add(ID, "Live: NHL Hockey", NOW - timedelta(hours=5), minutes=1302)
        self.jf.answers[ID] = (200, {"state": "not_fixed", "duration": 78130.0, "wall": 13500.0, "after": 78100.0})
        self.run_pass()
        self.run_pass(now=NOW + timedelta(days=1))
        self.assertEqual([ID], self.jf.retimes)          # never copied again
        self.assertEqual("not_fixed", self.row().state)
        self.assertEqual(["livetv_recording_not_fixed"], [e for e, _ in self.activity()])

    def test_a_failure_is_tried_again_later_then_left(self):
        self.jf.add(ID, "Live: NHL Hockey", NOW - timedelta(hours=5))
        self.jf.answers[ID] = (200, {"state": "failed", "error": "ffmpeg exited 1: boom"})
        self.run_pass()
        self.assertEqual(("failed", 1, "ffmpeg exited 1: boom"), (self.row().state, self.row().attempts,
                                                                   self.row().detail))
        self.run_pass(now=NOW + timedelta(hours=1))           # not yet
        self.assertEqual(1, len(self.jf.retimes))
        for n in range(1, 6):
            self.run_pass(now=NOW + n * (RC.RETRY_AFTER + timedelta(minutes=1)))
        self.assertEqual(RC.MAX_ATTEMPTS, len(self.jf.retimes))
        self.assertEqual(RC.MAX_ATTEMPTS, self.row().attempts)

    def test_a_dropped_answer_is_a_failure_tried_again(self):
        import requests
        self.jf.add(ID, "Live: NHL Hockey", NOW - timedelta(hours=5))

        def boom(*a, **k):
            raise requests.ConnectionError("refused")
        self.jf.post = boom
        out = self.run_pass()
        self.assertEqual(1, out["failed"])
        self.assertIsNone(self.row())


class NeverWhileRecording(Base):
    def test_not_asked_while_its_timer_records(self):
        self.jf.add(ID, "Live: NHL Hockey", NOW - timedelta(hours=1))
        self.jf.timers = [{"Name": "Live: NHL Hockey", "Status": "InProgress",
                           "StartDate": (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "EndDate": (NOW + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}]
        out = self.run_pass()
        self.assertEqual([], self.jf.retimes)
        self.assertEqual([], self.jf.premieres)
        self.assertEqual(1, out["waiting"])

    def test_not_asked_in_its_first_minutes(self):
        self.jf.add(ID, "The National", NOW - timedelta(minutes=5))
        self.run_pass()
        self.assertEqual([], self.jf.retimes)

    def test_no_copy_while_jellyfin_records_something_else(self):
        self.jf.add(ID, "The National", NOW - timedelta(hours=3))
        self.jf.timers = [{"Name": "Live: NHL Hockey", "Status": "InProgress",
                           "StartDate": (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "EndDate": (NOW + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}]
        self.run_pass()
        self.assertEqual([], self.jf.retimes)
        self.jf.timers = []
        self.run_pass(now=NOW + timedelta(hours=3))
        self.assertEqual([ID], self.jf.retimes)

    def test_the_plugin_saying_it_records_is_asked_again(self):
        self.jf.add(ID, "The National", NOW - timedelta(hours=3))
        self.jf.answers[ID] = (409, {"state": "recording"})
        out = self.run_pass()
        self.assertEqual(1, out["waiting"])
        self.assertIsNone(self.row())
        self.assertEqual([], self.jf.premieres)
        self.jf.answers.pop(ID)
        self.run_pass(now=NOW + timedelta(minutes=15))
        self.assertEqual([ID, ID], self.jf.retimes)
        self.assertEqual("ok", self.row().state)

    def test_a_few_per_pass(self):
        for n in range(RC.CHECKS_PER_PASS + 3):
            self.jf.add(f"{n:032x}", f"Show {n}", NOW - timedelta(hours=3, minutes=n))
        self.run_pass()
        self.assertEqual(RC.CHECKS_PER_PASS, len(self.jf.retimes))
        self.assertEqual(f"{0:032x}", self.jf.retimes[0])   # newest first
        self.run_pass(now=NOW + timedelta(minutes=15))
        self.assertEqual(RC.CHECKS_PER_PASS + 3, len(set(self.jf.retimes)))

    def test_an_older_plugin_stops_the_pass_and_stores_nothing(self):
        self.jf.add(ID, "The National", NOW - timedelta(hours=3))
        self.jf.add("b" * 32, "Jeopardy!", NOW - timedelta(hours=4))
        self.jf.old_plugin = True
        self.run_pass()
        self.assertIsNone(self.row())
        self.jf.old_plugin = False
        self.run_pass(now=NOW + timedelta(minutes=15))
        self.assertEqual("ok", self.row().state)

    def test_a_library_saving_pictures_beside_the_media_is_checked_too(self):
        self.jf.save_locally = True
        self.jf.add(ID, "The National", NOW - timedelta(hours=3))
        self.run_pass()
        self.assertEqual([ID], self.jf.retimes)


class PremiereDate(Base):
    def test_the_guide_programme_start(self):
        from models.database import EPGProgram
        created = NOW - timedelta(hours=4)
        self.db.add(EPGProgram(channel_id="tsn2.ca", title="Live: NHL Hockey", start=created + timedelta(minutes=5),
                               stop=created + timedelta(hours=3, minutes=35)))
        self.db.commit()
        self.jf.add(ID, "Live: NHL Hockey", created)
        out = self.run_pass()
        self.assertEqual([(ID, "2026-10-30T08:05:00Z")], self.jf.premieres)
        self.assertEqual((1, 1), (out["premiere"], self.row().premiere_set))

    def test_when_it_began_without_a_programme(self):
        self.jf.add(ID, "Something", NOW - timedelta(hours=2))
        self.run_pass()
        self.assertEqual([(ID, "2026-10-30T10:00:00Z")], self.jf.premieres)

    def test_one_jellyfin_has_is_kept_and_not_asked_again(self):
        self.jf.add(ID, "Something", NOW - timedelta(hours=2))
        self.jf.premiere[ID] = "2026-10-01T00:00:00Z"
        self.run_pass()
        self.run_pass(now=NOW + timedelta(hours=1))
        self.assertEqual(1, len(self.jf.premieres))
        self.assertEqual("2026-10-01T00:00:00Z", self.jf.premiere[ID])
        self.assertEqual(0, self.row().premiere_set)

    def test_a_retimed_recording_gets_one_too(self):
        self.jf.add(ID, "Live: NHL Hockey", NOW - timedelta(hours=5))
        self.jf.answers[ID] = (200, {"state": "retimed", "duration": 78130.0, "wall": 13500.0, "after": 13490.0})
        self.run_pass()
        self.assertEqual(1, len(self.jf.premieres))

    def test_a_failed_check_waits_for_its_date(self):
        self.jf.add(ID, "Live: NHL Hockey", NOW - timedelta(hours=5))
        self.jf.answers[ID] = (200, {"state": "failed", "error": "x"})
        self.run_pass()
        self.assertEqual([], self.jf.premieres)


# ── The plugin ──────────────────────────────────────────────────────────────

def _src(name: str) -> str:
    return (PLUGIN / name).read_text(encoding="utf-8")


class ThePlugin(unittest.TestCase):
    def test_admin_only_and_never_a_file_jellyfin_records(self):
        src = _src("Api/RecordingRetimeController.cs")
        self.assertIn('[Route("Tentacle/Recordings")]', src)
        self.assertIn('[Authorize(Policy = "RequiresElevation")]', src)
        retime = src[src.index("public async Task<ActionResult> Retime"):src.index("public async Task<ActionResult> Premiere")]
        self.assertLess(retime.index("GetActiveRecordingInfo"), retime.index("RecordingRetime.Run"))
        self.assertIn("GetRecordingFolders()", src)              # only files in the recording folders
        self.assertIn('EndsWith(".ts"', src)

    def test_the_refresh_keeps_metadata_pictures_and_the_date_it_was_made(self):
        src = _src("Api/RecordingRetimeController.cs")
        self.assertIn("ReplaceAllMetadata = false", src)
        self.assertIn("ReplaceAllImages = false", src)
        self.assertIn("fresh.DateCreated = created", src)

    def test_premiere_never_replaces_one_jellyfin_has(self):
        src = _src("Api/RecordingRetimeController.cs")
        premiere = src[src.index("public async Task<ActionResult> Premiere"):]
        self.assertLess(premiere.index("if (item.PremiereDate.HasValue)"), premiere.index("item.PremiereDate = utc"))

    def test_the_copy_replaces_the_recording_only_when_it_is_right(self):
        src = _src("Services/RecordingRetime.cs")
        locked = src[src.index("private static async Task<Outcome> RunLocked"):src.index("public static async Task<double?> Probe")]
        move = locked.index("File.Move(temp, path, overwrite: true)")
        for check in ("TooLong(after.Value, wall)", "copied < size * MinSizeShare", "file.LastWriteTimeUtc != written",
                      "if (dryRun)", "DateTime.UtcNow - written < Settle"):
            self.assertLess(locked.index(check), move, check)
        self.assertIn('"." + Path.GetFileName(path) + ".retime.tmp"', src)   # hidden, same folder: one rename
        self.assertIn("SemaphoreSlim OneAtATime = new(1, 1)", src)

    def test_the_recipe(self):
        src = _src("Services/RecordingRetime.cs")
        self.assertRegex(src, r'"-fflags", "\+genpts\+discardcorrupt", "-i", input,\s*"-map", "0", "-c", "copy"')


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class TheRecipeOnAFile(unittest.TestCase):
    """The plugin's ffmpeg arguments on a 2 x 20 s recording whose second
    half came with a clock 18 h later (as a re-dial not joined leaves it)."""

    def probe(self, path) -> float:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                              "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True,
                             timeout=60)
        return float(out.stdout.strip())

    def part(self, path, offset):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
                        "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000", "-t", "20", "-c:v", "mpeg2video",
                        "-c:a", "mp2", "-f", "mpegts", "-output_ts_offset", str(offset), str(path)],
                       check=True, timeout=120)

    def test_a_clock_jump_is_gone(self):
        d = Path(temp_dir(self))
        self.part(d / "a.ts", 19351)
        self.part(d / "b.ts", 19351 + 18 * 3600)
        rec = d / "recording.ts"
        rec.write_bytes((d / "a.ts").read_bytes() + (d / "b.ts").read_bytes())
        self.assertGreater(self.probe(rec), 18 * 3600)
        args = re.search(r'new\[\]\s*\{\s*("-nostdin".*?)\}', _src("Services/RecordingRetime.cs"), re.S).group(1)
        args = [a for a in re.findall(r'"([^"]*)"|(\binput\b|\boutput\b)', args)]
        argv = [lit or (str(rec) if var == "input" else str(d / "copy.ts")) for lit, var in args]
        subprocess.run(["ffmpeg", *argv], check=True, timeout=120)
        self.assertAlmostEqual(40, self.probe(d / "copy.ts"), delta=2)


if __name__ == "__main__":
    unittest.main()
