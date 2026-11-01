"""Jellyfin Web: a recording still recording plays from its start (TV #239 on the web).

Lucas, 2026-10-30: "im testing 239, dont see the jump to live button. testing on web".
A click on a "Recording now" card asks Watch live / Resume from … / Play from start
(tentacle-recordings.js), and Play from start plays the growing file in Jellyfin's
player: seeks stay inside what's written, a resume waits for the server's remux, and
the playback reports leave the position out (Jellyfin marks an item without a runtime
watched at 0:00 on any position) and stop as failed, with the position saved on its
own. Runs the real functions under node (skipped without node).

Lucas, 2026-10-30 ~17:00 Toronto: "Its like it uses two different players. If i hit play
from start it does show more options then if I hit watch live". Watch live is the same
recording in the same player, at its live end; Jump to live seeks there. And "the
scrubbing back is a little weird though it goes all the back then jumps up then you can
scroll back more again and again": the first playlist holds only what the remux did in
its first second, so the bar stays hidden (Watch live: a spinner) until it has caught up.

Run from the tentacle/ directory:  python -m unittest tests.test_recordings_web_growing
"""
import json
import unittest

from inject_js import HAVE_NODE, INJECT, node

SRC = (INJECT / "tentacle-recordings.js").read_text(encoding="utf-8")


def _function(name):
    start = SRC.index("  function " + name + "(")
    return SRC[start:SRC.index("\n  }\n", start) + 4]


def _line(prefix):
    # one statement, to its ";" at a line's end
    start = SRC.index(prefix)
    return SRC[start:SRC.index(";\n", start) + 2]


FUNCS = "".join(_function(f) for f in ("position", "clock", "recordingChoices", "normId", "growingSeekTarget",
                                       "growingResumeReady", "growingStopTicks", "growingReport",
                                       "growingExpectedMs", "growingCaughtUp"))
PRELUDE = _line("  var EDGE_MS =") + _line("  var GR =")


def run(body):
    return node(PRELUDE + FUNCS + "console.log(JSON.stringify((function () {" + body + "})()));")


class Wiring(unittest.TestCase):
    def test_a_click_on_a_recording_now_asks_first(self):
        self.assertIn("if (recording(item)) recordingMenu(item, { el: a, short: true });", SRC)

    def test_play_from_start_never_names_a_start_of_0(self):
        # hls.js takes a start of 0 for "the live edge": 1 ms
        self.assertIn("START_TICKS = 10000", SRC)
        self.assertIn("pm.play({ ids: [item.Id], startPositionTicks: START_TICKS });", SRC)

    def test_a_recording_that_finished_at_its_end_is_stopped_as_watched(self):
        body = _function("checkFinished")
        self.assertIn("GR.ended = true;", body)
        self.assertIn("Command: 'Stop'", body)

    def test_watch_live_is_the_recording_at_its_live_end(self):
        # one player for all three choices; no channel switch any more
        self.assertIn("Growing.play(item, c.key === 'live' ? LIVE : c.key === 'resume' ? position(item) : 0);", SRC)
        self.assertNotIn("playChannel", SRC)
        self.assertIn("if (GR.live) goLive(v);", _function("catchUp"))

    def test_jump_to_live_seeks_to_the_end_of_the_same_recording(self):
        body = _function("liveButton")
        self.assertIn("if (GR.whole) goLive(v);", body)
        self.assertIn("GR.live = true; waiting(true);", body)
        self.assertIn("growingSeekTarget(Number.MAX_SAFE_INTEGER, writtenMs(v))", _function("goLive"))

    def test_the_bar_stays_hidden_until_it_holds_the_whole_recorded_part(self):
        body = _function("waiting")
        self.assertIn("b.classList.toggle('tr-growing-wait', !!on);", body)
        self.assertIn("v.muted = true", body)
        css = (INJECT / "tentacle-recordings.css").read_text(encoding="utf-8")
        self.assertIn("body.tr-growing-wait .videoOsdBottom .sliderContainer", css)
        self.assertIn("body.tr-growing-golive video.htmlvideoplayer { opacity: 0; }", css)

    def test_the_three_reports_are_wrapped(self):
        self.assertIn("['reportPlaybackStart', 'reportPlaybackProgress', 'reportPlaybackStopped']", SRC)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class Choices(unittest.TestCase):
    def labels(self, user_data):
        return run("return recordingChoices({ UserData: %s }).map(function (c) { return c.label; });" % json.dumps(user_data))

    def test_no_position(self):
        self.assertEqual(["Watch live", "Play from start"], self.labels({"PlaybackPositionTicks": 0}))

    def test_a_saved_position_offers_resume_from_it(self):
        self.assertEqual(["Watch live", "Resume from 10:36", "Play from start"], self.labels({"PlaybackPositionTicks": 636 * 10**7}))
        self.assertEqual("Resume from 1:02:05", self.labels({"PlaybackPositionTicks": 3725 * 10**7})[1])


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class Seeking(unittest.TestCase):
    def test_seeks(self):
        out = run("""return [growingSeekTarget(-5, 600000), growingSeekTarget(30000, 600000), growingSeekTarget(999999, 600000),
          growingSeekTarget(5000, 4000), growingSeekTarget(70000, 0)];""")
        # inside: as asked; past the end: 10 s short of what's written; nothing written yet: as asked
        self.assertEqual([0, 30000, 590000, 0, 70000], out)

    def test_resume_waits_for_the_remux_then_gives_up_waiting(self):
        out = run("return [growingResumeReady(600000, 300000, 1000), growingResumeReady(600000, 610000, 1000), growingResumeReady(600000, 300000, 45000)];")
        self.assertEqual([False, True, True], out)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class CatchUp(unittest.TestCase):
    def test_the_length_a_recording_should_have_by_now(self):
        now = "Date.parse('2026-10-31T19:35:00Z')"
        out = run("""var n = %s; return [growingExpectedMs('2026-10-31T19:00:00.0000000Z', n), growingExpectedMs(null, n),
          growingExpectedMs('2026-10-31T19:40:00Z', n), growingExpectedMs('2026-10-29T19:00:00Z', n)];""" % now)
        self.assertEqual([35 * 60000, 0, 0, 0], out)

    def test_caught_up(self):
        out = run("""return [
          growingCaughtUp(145000, 2100000, -1, -1, 500),
          growingCaughtUp(2096000, 2100000, 1951000, 3000, 3500),
          growingCaughtUp(2096000, 5100000, 1951000, 3000, 3500),
          growingCaughtUp(2099000, 5100000, 3000, 3000, 6500),
          growingCaughtUp(600000, 5100000, 60000, 3000, 45000),
          growingCaughtUp(0, 0, -1, -1, 2000)];""")
        # production, 30 Oct: the first playlist held 2:25 of a 35 min recording, the next all of it;
        # started late (expected too long): growth at real time tells; never longer than 45 s
        self.assertEqual([False, True, False, True, True, False], out)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class Reports(unittest.TestCase):
    def report(self, method, info, gr="{ id: 'abc123', ended: false }"):
        return run("Object.assign(GR, %s); return growingReport(%s, %s);" % (gr, json.dumps(method), json.dumps(info)))

    def test_progress_leaves_the_position_out(self):
        out = self.report("reportPlaybackProgress", {"ItemId": "ABC123", "PositionTicks": 900000000, "IsPaused": False})
        self.assertEqual({"ItemId": "ABC123", "IsPaused": False}, out)

    def test_start_leaves_the_position_out(self):
        self.assertNotIn("PositionTicks", self.report("reportPlaybackStart", {"ItemId": "abc-123", "PositionTicks": 1020455999}))

    def test_stop_is_failed_and_saves_the_position_on_its_own(self):
        out = self.report("reportPlaybackStopped", {"ItemId": "abc123", "PositionTicks": 900000000})
        self.assertEqual({"ItemId": "abc123", "Failed": True, "__save": 900000000}, out)

    def test_under_a_minute_saves_no_position(self):
        self.assertEqual(0, self.report("reportPlaybackStopped", {"ItemId": "abc123", "PositionTicks": 590000000})["__save"])

    def test_a_recording_played_to_its_end_stops_as_any_other(self):
        info = {"ItemId": "abc123", "PositionTicks": 72000000000}
        self.assertEqual(info, self.report("reportPlaybackStopped", info, "{ id: 'abc123', ended: true }"))

    def test_other_items_are_untouched(self):
        info = {"ItemId": "other", "PositionTicks": 900000000}
        self.assertEqual(info, self.report("reportPlaybackProgress", info))
        self.assertEqual(info, self.report("reportPlaybackStopped", info, "{ id: null, ended: false }"))


if __name__ == "__main__":
    unittest.main()
