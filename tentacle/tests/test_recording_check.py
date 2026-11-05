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
import os
import re
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
        self.retime_params = []
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
            self.retime_params.append(params)
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


class TheStart(Base):
    """#900: Jellyfin's DateCreated isn't when a recording began. A refresh as
    it stops resets it to about its last write (on the maintainer's install
    every recording's DateCreated is its file's last write: window 0 s,
    no_window), so the server sends the programme's start along."""

    def test_the_programme_start_goes_to_the_plugin(self):
        from models.database import EPGProgram
        start = NOW - timedelta(hours=3)
        self.db.add(EPGProgram(channel_id="tsn", title="NHL Hockey", start=start, stop=start + timedelta(hours=3)))
        self.db.commit()
        self.jf.add(ID, "Live: NHL Hockey", start - timedelta(minutes=1))
        self.run_pass()
        self.assertEqual([{"start": "2026-10-30T09:00:00Z"}], self.jf.retime_params)

    def test_no_start_known_sends_none(self):
        self.jf.add(ID, "The National", NOW - timedelta(hours=2))
        self.run_pass()
        self.assertEqual([{}], self.jf.retime_params)

    def test_a_no_window_from_before_is_asked_once_more(self):
        from models.database import RecordingCheck
        self.jf.add(ID, "Live: NHL Hockey", NOW - timedelta(hours=5))
        self.db.add(RecordingCheck(item_id=ID, state="no_window", attempts=0, premiere_set=1,
                                   created_at=NOW - timedelta(days=1), updated_at=NOW - timedelta(days=1)))
        self.db.commit()
        self.jf.answers[ID] = (200, {"state": "no_window", "wall": 0.0})
        self.run_pass()
        self.run_pass(now=NOW + timedelta(days=1))
        self.assertEqual([ID], self.jf.retimes)
        self.assertEqual(("no_window", 0.0), (self.row().state, self.row().wall))

    def test_an_ok_far_shorter_than_its_window_is_asked_once_more(self):
        # #924: a plugin from before marked a clock that went back "ok" (333 s in 11610 s)
        from models.database import RecordingCheck
        self.jf.add(ID, "Live: NHL Hockey", NOW - timedelta(hours=5))
        self.db.add(RecordingCheck(item_id=ID, state="ok", duration=332.9, wall=11609.8, attempts=0, premiere_set=1,
                                   created_at=NOW - timedelta(days=1), updated_at=NOW - timedelta(days=1)))
        self.db.commit()
        self.jf.answers[ID] = (200, {"state": "ok", "duration": 332.9, "wall": 11609.8})
        self.run_pass()
        self.run_pass(now=NOW + timedelta(days=1))
        self.assertEqual([ID], self.jf.retimes)
        self.assertEqual("ok", self.row().state)

    def test_a_not_fixed_from_a_one_second_window_is_asked_once_more(self):
        # #931: v1.42's 1 s window (#900) made good recordings not_fixed, never asked again
        from models.database import RecordingCheck
        self.jf.add(ID, "Live: NHL Hockey", NOW - timedelta(hours=5))
        self.db.add(RecordingCheck(item_id=ID, state="not_fixed", duration=13500.0, wall=0.68, after=13500.0,
                                   attempts=0, premiere_set=1,
                                   created_at=NOW - timedelta(days=1), updated_at=NOW - timedelta(days=1)))
        self.db.commit()
        self.jf.answers[ID] = (200, {"state": "not_fixed", "duration": 13500.0, "wall": 0.7, "after": 13500.0})
        self.run_pass()
        self.run_pass(now=NOW + timedelta(days=1))
        self.assertEqual([ID], self.jf.retimes)                       # once, not every pass
        self.assertEqual("not_fixed", self.row().state)

    def test_a_not_fixed_with_a_real_window_is_not_asked_again(self):
        from models.database import RecordingCheck
        self.jf.add(ID, "Live: NHL Hockey", NOW - timedelta(hours=5))
        self.db.add(RecordingCheck(item_id=ID, state="not_fixed", duration=332.9, wall=11609.8, attempts=0,
                                   premiere_set=1, created_at=NOW - timedelta(days=1),
                                   updated_at=NOW - timedelta(days=1)))
        self.db.commit()
        self.run_pass()
        self.assertEqual([], self.jf.retimes)

    def test_an_ok_that_fits_its_window_is_not_asked_again(self):
        from models.database import RecordingCheck
        self.jf.add(ID, "Live: NHL Hockey", NOW - timedelta(hours=5))
        self.db.add(RecordingCheck(item_id=ID, state="ok", duration=13155.6, wall=13300.0, attempts=0, premiere_set=1,
                                   created_at=NOW - timedelta(days=1), updated_at=NOW - timedelta(days=1)))
        self.db.commit()
        self.run_pass()
        self.assertEqual([], self.jf.retimes)


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
        for check in ("TooLong(after.Value, wall)", "TooShort(after.Value, copied)", "copied < size * MinSizeShare", "file.LastWriteTimeUtc != written",
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


HARNESS = r"""
using Jellyfin.Plugin.Tentacle.Services;
var a = args;
DateTime? start = a[4] == "-" ? null : DateTime.Parse(a[4], null, System.Globalization.DateTimeStyles.AdjustToUniversal);
var created = DateTime.Parse(a[3], null, System.Globalization.DateTimeStyles.AdjustToUniversal);
var began = RecordingRetime.Began(a[2], created, start);
var o = await RecordingRetime.Run(a[0], a[1], a[2], began, false, null, CancellationToken.None);
Console.WriteLine($"{began:yyyy-MM-ddTHH:mm:ssZ} {o.State} {o.Wall?.ToString(System.Globalization.CultureInfo.InvariantCulture)}");

namespace Microsoft.Extensions.Logging
{
    public interface ILogger { }

    public static class LoggerExtensions
    {
        public static void LogInformation(this ILogger l, string m, params object?[] a) { }

        public static void LogWarning(this ILogger l, string m, params object?[] a) { }
    }
}
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)


@unittest.skipUnless(shutil.which("dotnet"), "needs dotnet")
class TheWindowOnAFile(unittest.TestCase):
    """#900 (Rob, 1 Nov 2026): a good 2 h 17 min game (8.26 GB) probed 8262 s
    "in a 1 s window": its DateCreated was reset as it stopped, to its last
    write minus 1 s, so the whole file was copied for nothing and Activity
    said "2 h 17 min for 0 min recorded". The plugin's RecordingRetime.cs
    (no Jellyfin types) is compiled and run on a file, with an ffprobe that
    says 8262 s and an ffmpeg that only notes it was run."""

    @classmethod
    def setUpClass(cls):
        d = Path(temp_dir(cls))
        cls.dir = d
        major = subprocess.run(["dotnet", "--version"], capture_output=True, text=True, timeout=60).stdout.split(".")[0]
        (d / "h.csproj").write_text(
            f"<Project Sdk=\"Microsoft.NET.Sdk\"><PropertyGroup><OutputType>Exe</OutputType>"
            f"<TargetFramework>net{major}.0</TargetFramework><Nullable>enable</Nullable>"
            f"<ImplicitUsings>enable</ImplicitUsings></PropertyGroup>"
            f"<ItemGroup><Compile Include=\"{(PLUGIN / 'Services/RecordingRetime.cs').resolve()}\" /></ItemGroup></Project>")
        (d / "Program.cs").write_text(HARNESS)
        (d / "tmp").mkdir()          # dotnet's scratch files go with the rest
        cls.env = {**os.environ, "TMPDIR": str(d / "tmp"), "DOTNET_NOLOGO": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                   "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE": "1"}
        built = subprocess.run(["dotnet", "build", "--disable-build-servers", "-nologo", "-v", "q", "-o",
                                str(d / "out"), str(d / "h.csproj")], capture_output=True, text=True, timeout=600,
                               env=cls.env)
        assert built.returncode == 0, built.stdout[-3000:]
        (d / "ffprobe").write_text(f"#!/bin/sh\ncat '{d / 'probed'}' 2>/dev/null || echo 8262.0\n")
        (d / "ffmpeg").write_text(f"#!/bin/sh\ntouch '{d / 'ffmpeg-ran'}'\nexit 1\n")
        for tool in ("ffprobe", "ffmpeg"):
            (d / tool).chmod(0o755)

    def check(self, name, created, start=None, size=8_262_474_792, probed=None):
        """(state, wall) for a recording `name` last written an hour ago;
        `created` and `start` from its last write; ffprobe says `probed` s (8262 s)."""
        rec = self.dir / name
        if probed is not None:
            (self.dir / "probed").write_text(f"{probed}\n")
            self.addCleanup(lambda: (self.dir / "probed").unlink(missing_ok=True))
        with open(rec, "wb") as f:
            f.truncate(size)         # sparse: no real bytes
        self.addCleanup(rec.unlink)
        self.addCleanup(lambda: (self.dir / "ffmpeg-ran").unlink(missing_ok=True))
        written = _utc_now() - timedelta(hours=1)
        stamp = (written - datetime(1970, 1, 1)).total_seconds()
        os.utime(rec, (stamp, stamp))
        iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else "-"
        out = subprocess.run(["dotnet", str(self.dir / "out" / "h.dll"), str(self.dir / "ffmpeg"),
                              str(self.dir / "ffprobe"), str(rec), iso(created(written)), iso(start and start(written))],
                             capture_output=True, text=True, timeout=120, env={**self.env, "TZ": "America/Toronto"})
        self.assertEqual(0, out.returncode, out.stderr)
        _, state, wall = (out.stdout.split() + [""])[:3]
        return state, float(wall) if wall else None

    def ran_ffmpeg(self) -> bool:
        return (self.dir / "ffmpeg-ran").exists()

    def test_a_reset_date_created_and_no_start_is_no_window_without_a_copy(self):
        state, wall = self.check("Game.ts", lambda w: w - timedelta(seconds=1))
        self.assertEqual(("no_window", 1.0), (state, wall))          # 8.26 GB in 1 s: 66 Gbit/s
        self.assertFalse(self.ran_ffmpeg())

    def test_the_programme_start_from_the_server_gives_the_window(self):
        state, wall = self.check("Game.ts", lambda w: w - timedelta(seconds=1),
                                 start=lambda w: w - timedelta(seconds=8300))
        self.assertEqual(("ok", 8300.0), (state, wall))
        self.assertFalse(self.ran_ffmpeg())

    def test_the_start_in_the_file_name_is_local_time(self):
        # Jellyfin names the file after the programme's start in its own time zone
        local = (_utc_now() - timedelta(hours=1, seconds=8300)).replace(tzinfo=timezone.utc).astimezone(
            ZoneInfo("America/Toronto"))
        state, wall = self.check(f"EuroLeague  Zalgiris - Partizan {local:%Y_%m_%d_%H_%M_%S} - 1.ts",
                                 lambda w: w - timedelta(seconds=1))
        self.assertEqual("ok", state)
        self.assertAlmostEqual(8300, wall, delta=2)
        self.assertFalse(self.ran_ffmpeg())

    def test_the_earliest_start_wins(self):
        state, wall = self.check("Game.ts", lambda w: w - timedelta(seconds=9000),
                                 start=lambda w: w - timedelta(seconds=8300))
        self.assertEqual(("ok", 9000.0), (state, wall))

    def test_a_real_clock_jump_is_still_copied(self):
        state, wall = self.check("Game.ts", lambda w: w - timedelta(seconds=1),
                                 start=lambda w: w - timedelta(seconds=3000))
        self.assertEqual("failed", state)                            # the fake ffmpeg fails, but it ran
        self.assertTrue(self.ran_ffmpeg())

    # #924 (Rob, 5 Nov 2026): the provider's clock stepped back near the end, so the last
    # timestamp lands just after the first: a 4 h NHL game (14.8 GB) probed 8.66 s and played
    # as 0:00. On the maintainer's install a 14.0 GB game probed 333 s in an 11610 s window.
    # Short for its window says nothing (split parts and late starts are too); short for its
    # own bytes does: no recording is sent at over 60 Mbit/s.

    def test_a_clock_that_went_back_is_copied(self):
        # its 165 MB "- 1" part: 15 s probed, about 19 min real (88 Mbit/s)
        state, _ = self.check("Live  NHL Hockey - 1.ts", lambda w: w - timedelta(seconds=1),
                              start=lambda w: w - timedelta(seconds=1140), size=165_000_000, probed=15.0)
        self.assertEqual("failed", state)                            # the fake ffmpeg fails, but it ran
        self.assertTrue(self.ran_ffmpeg())

    def test_a_good_later_part_short_for_its_window_is_left(self):
        # 1473 s at the channel's ~8.2 Mbit/s, its window from the programme's start
        state, wall = self.check("Game - 1.ts", lambda w: w - timedelta(seconds=1),
                                 start=lambda w: w - timedelta(seconds=19559), size=1_510_000_000, probed=1473.0)
        self.assertEqual(("ok", 19559.0), (state, wall))
        self.assertFalse(self.ran_ffmpeg())

    # #931 (Rob, 5 Nov 2026, UTC-5): a recording that didn't stop normally keeps the DateCreated
    # Jellyfin gave it while recording: local time stamped as UTC, west of UTC hours before the
    # real start. The first part of a game cut short (booked with 15 min pre-padding, ran 1473 s)
    # got a 19559 s window, so any clock jump up to 6.9 h passed.

    def _local_stamp(self, real):
        """`real` (UTC) as Jellyfin stamps a recording still recording: its local time, called UTC."""
        return real.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Toronto")).replace(tzinfo=None)

    def test_a_local_date_created_west_of_utc_is_read_as_local(self):
        real = lambda w: w - timedelta(seconds=1500)                  # began 15 min before the programme
        state, wall = self.check("Game.ts", lambda w: self._local_stamp(real(w)),
                                 start=lambda w: real(w) + timedelta(minutes=15), size=1_510_000_000, probed=1473.0)
        self.assertEqual("ok", state)
        self.assertAlmostEqual(1500, wall, delta=2)
        self.assertFalse(self.ran_ffmpeg())

    def test_a_clock_jump_in_a_part_cut_short_is_copied(self):
        real = lambda w: w - timedelta(seconds=1500)
        state, _ = self.check("Game.ts", lambda w: self._local_stamp(real(w)),
                              start=lambda w: real(w) + timedelta(minutes=15), size=1_510_000_000,
                              probed=1473.0 + 2 * 3600)              # a re-dial's clock 2 h away
        self.assertEqual("failed", state)                            # the fake ffmpeg fails, but it ran
        self.assertTrue(self.ran_ffmpeg())

    def test_a_long_pre_padding_in_utc_is_kept(self):
        # a server whose clock is UTC: DateCreated is the real start, 4 h before the programme
        state, wall = self.check("Game.ts", lambda w: w - timedelta(seconds=8300 + 4 * 3600),
                                 start=lambda w: w - timedelta(seconds=8300), probed=8262.0 + 4 * 3600)
        self.assertEqual("ok", state)
        self.assertAlmostEqual(8300 + 4 * 3600, wall, delta=2)

    def test_the_start_in_a_second_continuation_name_is_read(self):
        local = (_utc_now() - timedelta(hours=1, seconds=8300)).replace(tzinfo=timezone.utc).astimezone(
            ZoneInfo("America/Toronto"))
        state, wall = self.check(f"Game {local:%Y_%m_%d_%H_%M_%S} - 1 - 2.ts", lambda w: w - timedelta(seconds=1))
        self.assertEqual("ok", state)
        self.assertAlmostEqual(8300, wall, delta=2)

    def test_an_empty_leftover_is_left(self):
        state, _ = self.check("Game - 2.ts", lambda w: w - timedelta(seconds=1),
                              start=lambda w: w - timedelta(seconds=600), size=20_304, probed=0.02)
        self.assertEqual("ok", state)
        self.assertFalse(self.ran_ffmpeg())


if __name__ == "__main__":
    unittest.main()
