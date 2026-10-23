"""#771 check: a real sync() against the fake Jellyfin, run a second time with
nothing changed (as the hourly check does), still POSTs /Tentacle/Refresh
?scope=livetv -- the push every TV app turns into a forced Home reload."""
from unittest import mock

from services import livetv_tuners as T

import os

import test_livetv_lineup as lt


class Jf(lt.FakeJellyfin):
    def post(self, path, body=None, **p):
        if path == "/Tentacle/Refresh":
            self.calls.append(("POST", path, p))
            return {}
        return super().post(path, body, **p)


class HourlyNoopPush(lt.DbCase):
    def setUp(self):
        super().setUp()
        self.jf = Jf(self.Session, [(lt.ADMIN, "admin"), (lt.ANN, "ann"), (lt.BEN, "ben")])
        for name, value in (("jellyfin_for", lambda db: self.jf),
                            ("refresh_and_wait", lambda db, jf: jf.refresh() or True),
                            ("wait_for_guide", lambda jf: True),
                            ("BACKUP_DIR", os.path.join(self.tmp, "backups")),
                            ("SessionLocal", self.Session)):
            pt = mock.patch.object(T, name, value)
            pt.start()
            self.addCleanup(pt.stop)
        pt = mock.patch("services.jellyfin_guide.refresh_jellyfin_guide", lambda *a, **k: self.jf.refresh())
        pt.start()
        self.addCleanup(pt.stop)

    def pushes(self):
        return [c for c in self.jf.calls if c[1] == "/Tentacle/Refresh"]

    def test_second_run_with_nothing_to_do_pushes_nothing(self):
        T.run_sync("start-up")
        self.assertEqual(1, len(self.pushes()))
        done = T.run_sync("hourly check")
        changed = {k: v for k, v in done.items() if v and k not in ("backup", "guide_added")}
        self.assertEqual({}, changed, "the hourly run did change something")
        self.assertEqual(1, len(self.pushes()), f"no-op hourly run still pushed: {self.pushes()[-1]}")
