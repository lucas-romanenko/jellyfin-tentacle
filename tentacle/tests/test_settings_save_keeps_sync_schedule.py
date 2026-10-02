"""A Settings save keeps a custom sync schedule (#385).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The stored sync_schedule is a 5-field cron; the Settings page shows it as a
daily time (cronToTime reads the first two fields, 03:00 when it can't) and
Save always posted timeToCron(field) = "M H * * *". So any Save turned
"0 */6 * * *" into "0 3 * * *" and "0 4 * * 1-5" into "0 4 * * *", and the
new schedule applied at once. Save now sends sync_schedule only when the time
was changed; the hint names a custom schedule.

Runs the real loadSettings(), saveSettings(), loadScheduleInfo(),
cronToTime() and timeToCron() under node, then hands what the page posted to
the real POST /api/settings handler.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import get_setting, set_setting
from tmp_dirs import temp_dir

APP = (Path(__file__).resolve().parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")


def _fn(name):
    start = APP.index(f"function {name}(")
    if APP[start - 6:start] == "async ":
        start -= 6
    return APP[start:APP.index("\n}\n", start) + 2]


# Page sessions, one per scenario: load Settings with `stored`, do `steps`,
# record every POST body. A step is ["save"], ["time", "HH:MM"],
# ["edit", "recently_added_days", "14"] or ["reload"] (Settings opened again).
SCRIPT = """
const SETTINGS_TEXT_FIELDS = ['recently_added_days'];
const SETTINGS_CHECKBOX_FIELDS = [];
const SETTINGS_PICKER_FIELDS = [];
const state = {};
let els = {};
function fresh() {
  els = {};
  // <input type="time" value="03:00">: its defaultValue is the HTML value.
  els.sync_schedule_time = { value: '03:00', defaultValue: '03:00', dataset: {} };
  els.sync_schedule_hint = { textContent: '' };
  els.recently_added_days = { value: '', dataset: {} };
}
const document = { getElementById: id => els[id] || null };
let stored = {}, rawFails = false, posts = [];
async function api(path, opts) {
  if (opts && opts.method === 'POST') {
    posts.push(opts.body.settings);
    for (const [k, v] of Object.entries(opts.body.settings)) stored[k] = v;
    return {};
  }
  if (path === '/api/settings/raw') { if (rawFails) throw new Error('down'); return Object.assign({}, stored); }
  if (path === '/api/settings/schedule-info') return { cron: stored.sync_schedule || '0 3 * * *', timezone: '' };
  throw new Error('unexpected ' + path);
}
const tick = () => new Promise(r => setTimeout(r, 0));
function toast() {}
function loadServicePickers() {}
function applyMusicVisibility() {}
function loadPathStatus() {}
function loadConnectionStatus() {}
%(fns)s
(async () => {
  const results = [];
  for (const sc of %(scenarios)s) {
    stored = Object.assign({}, sc.stored); rawFails = !!sc.raw_fails; posts = [];
    fresh();
    await loadSettings(); await tick();
    const hints = [els.sync_schedule_hint.textContent];
    for (const step of sc.steps) {
      if (step[0] === 'save') await saveSettings();
      else if (step[0] === 'time') els.sync_schedule_time.value = step[1];
      else if (step[0] === 'edit') els[step[1]].value = step[2];
      else if (step[0] === 'reload') { rawFails = false; fresh(); await loadSettings(); }
      await tick();
      hints.push(els.sync_schedule_hint.textContent);
    }
    results.push({ posts, stored, hints });
  }
  process.stdout.write(JSON.stringify(results));
})();
"""

CUSTOM = ["0 */6 * * *", "0 3,15 * * *", "0 4 * * 1-5", "15 1 * * 0", "*/30 * * * *", "0 2 1 * *"]
DAILY = ["0 3 * * *", "30 2 * * *", "5 23 * * *"]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SettingsSaveKeepsSyncSchedule(unittest.TestCase):
    def pages(self, scenarios):
        fns = "\n".join(_fn(n) for n in ("cronToTime", "timeToCron", "loadScheduleInfo",
                                         "loadSettings", "saveSettings"))
        run = subprocess.run(["node", "-"], input=SCRIPT % {"scenarios": json.dumps(scenarios), "fns": fns},
                             capture_output=True, text=True, timeout=120)
        if run.returncode:
            raise AssertionError(run.stderr)
        return json.loads(run.stdout)

    def page(self, stored, steps, raw_fails=False):
        return self.pages([{"stored": stored, "steps": steps, "raw_fails": raw_fails}])[0]

    def backend_save(self, posted, stored_cron):
        """The real POST /api/settings with what the page posted."""
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        self.addCleanup(db.close)
        set_setting(db, "sync_schedule", stored_cron)
        import main
        from routers import settings as r
        with mock.patch.object(main, "reschedule_main_sync") as resched:
            r.update_settings(r.SettingsUpdate(settings=posted), db=db)
        return get_setting(db, "sync_schedule"), resched.call_args_list

    def test_an_unrelated_save_keeps_every_stored_schedule(self):
        for cron in CUSTOM + DAILY:
            with self.subTest(cron=cron):
                out = self.page({"sync_schedule": cron}, [["edit", "recently_added_days", "14"], ["save"]])
                self.assertEqual(len(out["posts"]), 1)
                self.assertNotIn("sync_schedule", out["posts"][0], "an unchanged time was posted")
                self.assertEqual(out["posts"][0]["recently_added_days"], "14")
                cron_after, resched = self.backend_save(out["posts"][0], cron)
                self.assertEqual(cron_after, cron)
                self.assertEqual(resched, [], "the nightly job was rescheduled")

    def test_a_changed_time_is_stored_as_a_daily_schedule(self):
        for cron in CUSTOM + DAILY:
            with self.subTest(cron=cron):
                out = self.page({"sync_schedule": cron}, [["time", "04:45"], ["save"], ["save"]])
                self.assertEqual(out["posts"][0]["sync_schedule"], "45 4 * * *")
                self.assertNotIn("sync_schedule", out["posts"][1], "second Save, nothing changed")
                cron_after, resched = self.backend_save(out["posts"][0], cron)
                self.assertEqual(cron_after, "45 4 * * *")
                self.assertEqual(len(resched), 1)

    def test_a_time_changed_and_changed_back_keeps_the_schedule(self):
        out = self.page({"sync_schedule": "0 4 * * 1-5"}, [["time", "05:00"], ["time", "04:00"], ["save"]])
        self.assertNotIn("sync_schedule", out["posts"][0])

    def test_settings_that_failed_to_load_do_not_post_the_placeholder_time(self):
        out = self.page({"sync_schedule": "0 */6 * * *"}, [["save"]], raw_fails=True)
        self.assertNotIn("sync_schedule", out["posts"][0])
        out = self.page({"sync_schedule": "0 */6 * * *"}, [["time", "01:10"], ["save"]], raw_fails=True)
        self.assertEqual(out["posts"][0]["sync_schedule"], "10 1 * * *")

    def test_the_hint_names_a_custom_schedule(self):
        out = self.page({"sync_schedule": "0 */6 * * *"}, [["time", "02:00"], ["save"]])
        self.assertIn('Custom schedule "0 */6 * * *"', out["hints"][0])
        self.assertTrue(out["hints"][-1].startswith("Runs every day at this time"), out["hints"][-1])
        for cron in DAILY:
            self.assertTrue(self.page({"sync_schedule": cron}, [])["hints"][0].startswith("Runs every day"))

    def test_property_schedule_changes_only_when_the_time_does(self):
        """1,000 seeds of random sessions (unrelated edits, time changes,
        changes reverted, repeated Saves, reloads). Model: the stored cron
        changes only at a Save whose time differs from the one shown at the
        last load or Save, and then to that time as a daily cron."""
        import random

        def show(c):            # what cronToTime shows for a stored cron
            p = c.split()
            try:
                h, m = int(p[1]), int(p[0])
                if 0 <= h < 24 and 0 <= m < 60:
                    return f"{h:02d}:{m:02d}"
            except ValueError:
                pass
            return "03:00"

        def daily(t):
            h, m = t.split(":")
            return f"{int(m)} {int(h)} * * *"

        scenarios, expected = [], []
        for seed in range(1000):
            rng = random.Random(seed)
            cron = rng.choice(CUSTOM + DAILY)
            steps, expect = [], cron
            shown = field = show(cron)
            for _ in range(rng.randint(1, 8)):
                a = rng.choice(["save", "time", "time", "edit", "reload", "revert"])
                if a == "time":
                    field = f"{rng.randint(0, 23):02d}:{rng.choice([0, 15, 30, 45]):02d}"
                    steps.append(["time", field])
                elif a == "revert":
                    field = shown
                    steps.append(["time", field])
                elif a == "edit":
                    steps.append(["edit", "recently_added_days", str(rng.randint(1, 99))])
                elif a == "reload":
                    steps.append(["reload"])
                    shown = field = show(expect)
                else:
                    steps.append(["save"])
                    if field != shown:
                        expect = daily(field)
                    shown = field
            steps.append(["save"])
            if field != shown:
                expect = daily(field)
            scenarios.append({"stored": {"sync_schedule": cron}, "steps": steps})
            expected.append(expect)
        for seed, (sc, out, expect) in enumerate(zip(scenarios, self.pages(scenarios), expected)):
            self.assertEqual(out["stored"]["sync_schedule"], expect,
                             f"seed {seed}: {sc['stored']['sync_schedule']} {sc['steps']}")

if __name__ == "__main__":
    unittest.main()
